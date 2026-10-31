"""Bounded, read-only estimates of remaining media time before each course starts."""
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import uuid
from urllib.parse import quote

import requests

from card_reader import flag_is_true, parse_course_card, has_card_data_assignment
from resume_tasks import course_key
from card_tasks import StudyCancelled


def seconds(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def format_time(value):
    minutes = max(1, math.ceil(value / 60))
    hours, minutes = divmod(minutes, 60)
    return (f"{hours}小时" if hours else "") + (f"{minutes}分钟" if minutes else "")


@dataclass
class Estimate:
    chapters: int
    sampled: int = 0
    media_seconds: float = 0
    partial_seconds: float = 0
    media_count: int = 0
    measured_count: int = 0
    other_count: int = 0
    unknown: bool = False
    jobs: dict = field(default_factory=dict)
    unavailable_reason: str = ""

    def message(self, speed):
        if self.unavailable_reason:
            return "预计剩余时间暂不可用：" + self.unavailable_reason
        if self.sampled == self.chapters and not self.unknown and self.media_seconds > 0:
            duration = (self.media_seconds + self.partial_seconds) / speed
            basis = "已读取全部待处理章节"
            return f"预计剩余音视频学习时间：约{format_time(duration)}（{speed:g}倍速；{basis}）"
        known = (self.media_seconds + self.partial_seconds) / speed
        if known > 0:
            return f"预计剩余时间暂无法完整估算；已读取的音视频至少还需{format_time(known)}（{speed:g}倍速；完整读取 {self.sampled}/{self.chapters} 章，其余待估算）"
        if self.media_count and self.media_count == self.measured_count and not self.unknown and self.sampled == self.chapters:
            return "已读取的音视频播放进度已到结尾；仍需平台确认，课程完成时间暂无法估算"
        return "预计剩余时间暂无法估算：未读取到可计时的音视频，答题或其他任务仍需处理"


class DurationCache:
    """Cache only public media length, never account progress or signed tokens."""
    def __init__(self, root):
        self.directory = Path(root) / "cache" / "media_durations"

    def path(self, object_id):
        return self.directory / (hashlib.sha256(object_id.encode("utf-8")).hexdigest() + ".json")

    def get(self, object_id):
        try:
            data = json.loads(self.path(object_id).read_text(encoding="utf-8"))
            age = time.time() - float(data["saved_at"])
            return seconds(data["duration"]) if 0 <= age <= 7 * 86400 else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, object_id, duration):
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.path(object_id)
            temporary = target.with_name(".duration-" + uuid.uuid4().hex + ".tmp")
            temporary.write_text(json.dumps({"duration": duration, "saved_at": time.time()}), encoding="utf-8")
            temporary.replace(target)
        except OSError:
            pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class CourseTimeEstimator:
    def __init__(self, session, fid, headers=None, budget=16.0, samples=64, clock=time.monotonic, duration_cache=None, before_request=None):
        self.session, self.fid = session, fid
        self.headers = headers or {}
        self.budget, self.samples, self.clock = budget, max(1, samples), clock
        self.duration_cache = duration_cache
        self.before_request = before_request

    def estimate(self, course, tasks):
        # Deduplicate chapter IDs, and never use a local success as platform completion.
        points = {str(task.point.get("id", "")): task.point for task in tasks
                  if course_key(task.course) == course_key(course) and not flag_is_true(task.point.get("has_finished"))}
        points = list(points.values())
        result = Estimate(len(points))
        count = min(self.samples, len(points))
        indices = [round(i * (len(points) - 1) / (count - 1)) for i in range(count)] if count > 1 else [0] if count else []
        deadline = self.clock() + self.budget

        def get(url, **kwargs):
            if self.before_request is not None:
                self.before_request()
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError()
            response = self.session.get(url, timeout=min(2.0, remaining), **kwargs)
            response.raise_for_status()
            return response

        for index in indices:
            point = points[index]
            if flag_is_true(point.get("need_unlock")) or not point.get("id"):
                result.unknown = True
                continue
            seen = set()
            chapter_seconds = 0
            readable, unknown_media = False, False
            expected = seconds(point.get("jobCount"))
            complete = False
            try:
                for page in range(7):
                    response = get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params={
                        "clazzid": course["clazzId"], "courseid": course["courseId"],
                        "knowledgeid": point["id"], "cpi": course["cpi"], "ut": "s",
                        "v": "2025-0424-1038-3", "mooc2": 1, "num": str(page)})
                    cards, info, recognized = parse_course_card(response.text, lambda data: data.get("defaults", {}), lambda cards: cards)
                    if info.get("notOpen"):
                        break
                    if not recognized:
                        # Empty extra card slots need not contain mArg, but malformed JSON is uncertain.
                        if has_card_data_assignment(response.text):
                            unknown_media = True
                        continue
                    readable = True
                    for card in cards:
                        kind = str(card.get("type", "")).lower()
                        if not flag_is_true(card.get("job")) and not (kind == "workid" and card.get("jobid")):
                            continue
                        identity = str(card.get("jobid") or card.get("mid") or "")
                        if not identity:
                            unknown_media = True
                            continue
                        if identity in seen:
                            continue
                        seen.add(identity)
                        if flag_is_true(card.get("isPassed")):
                            continue
                        if kind != "video":
                            result.other_count += 1
                            continue
                        result.media_count += 1
                        object_id = card.get("objectId")
                        played = seconds(card.get("playTime", 0))
                        if not isinstance(object_id, str) or not object_id or played is None:
                            unknown_media = True
                            continue
                        duration = self.duration_cache.get(object_id) if self.duration_cache is not None else None
                        if duration is None:
                            metadata = get("https://mooc1.chaoxing.com/ananas/status/" + quote(object_id, safe=""),
                                params={"k": self.fid, "flag": "normal"}, headers=self.headers).json()
                            duration = seconds(metadata.get("duration")) if isinstance(metadata, dict) and metadata.get("status") == "success" else None
                            if duration is not None and self.duration_cache is not None:
                                self.duration_cache.put(object_id, duration)
                        if duration is None:
                            unknown_media = True
                            continue
                        chapter_seconds += max(0, duration - math.floor(played / 1000))
                        result.jobs[str(point["id"]) + "/" + identity] = max(0, duration - math.floor(played / 1000))
                        result.measured_count += 1
                    if expected is not None and expected > 0 and len(seen) >= expected:
                        complete = True
                        break
                else:
                    complete = readable and (expected == 0 or expected is not None and len(seen) >= expected)
                if complete and readable and not unknown_media:
                    result.sampled += 1
                    result.media_seconds += chapter_seconds
                else:
                    result.partial_seconds += chapter_seconds
                    result.unknown = True
            except StudyCancelled:
                raise
            except (requests.RequestException, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
                result.partial_seconds += chapter_seconds
                result.unknown = True
                break
        return result


def log_course_estimate(cx, course, tasks, speed, logger):
    from api.base import SessionManager
    from api.config import GlobalConst as gc

    speed = min(2.0, max(1.0, seconds(speed) or 1.0))
    title = re.sub(r"[\x00-\x1f\x7f]", " ", str(course.get("title") or "当前课程"))[:200]
    logger.info("课程即将开始：{}；正在估算剩余时间…", title)
    estimate = CourseTimeEstimator(SessionManager.get_session(), cx.get_fid(), gc.VIDEO_HEADERS,
        duration_cache=DurationCache(Path(__file__).resolve().parent),
        before_request=lambda: cx.rate_limiter.limit_rate()).estimate(course, tasks)
    logger.info("课程「{}」{}。答题、外链、验证和网络等待用时另计；多课程交替执行时不含其他课程用时。",
                title, estimate.message(speed))
    return estimate
