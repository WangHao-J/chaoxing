"""Account-isolated GUI runner for quizzes, media, or their combined workflow."""
import configparser
import io
import json
import math
import re
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from account_store import ProfileLock
from card_reader import flag_is_true
from card_tasks import StudyError, StudyCancelled, StudyPaused, check_cancelled, read_chapter_jobs, empty_template, read_chapter_job_states
from task_repository import clear_confirmed
from quiz_assistant import QuizError, reviewed_quiz, report_quiz_completed
from resume_tasks import course_key, task_key


from app_modes import MODE_LABELS


def clean_text(value):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:240]


def row_key(row):
    return tuple(str(row.get(name, "")) for name in ("course_id", "class_id", "chapter_id"))


class ModeStore:
    """Only ID/title checkpoints; shared credentials and the submission ledger stay put."""
    def __init__(self, profile, mode):
        if mode not in MODE_LABELS:
            raise StudyError("未知的运行窗口类型。")
        self.mode = mode
        self.path = Path(profile.directory) / f"study_state_{mode}.json"
        self.unavailable = set()

    def read(self):
        if not self.path.exists():
            return {"version": 1, "mode": self.mode, "courses": [], "pending": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or data.get("mode") != self.mode:
                raise ValueError()
            for name in ("courses", "pending"):
                if not isinstance(data.get(name), list) or any(not isinstance(row, dict) for row in data[name]):
                    raise ValueError()
                for row in data[name]:
                    fields = ("course_id", "class_id", "chapter_id") if name == "pending" else ("course_id", "class_id")
                    if any(not isinstance(row.get(field), str) or not row[field] for field in fields):
                        raise ValueError()
            return data
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            raise StudyError("此窗口的续接记录无法读取，原记录已保留；请查看账户文件。") from None

    def write(self, data):
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def saved_selection(self):
        return [(row["course_id"], row["class_id"]) for row in self.read()["courses"]]

    def save_selection(self, courses):
        data = self.read()
        data["courses"] = [{"course_id": course_key(course)[0], "class_id": course_key(course)[1],
                            "title": clean_text(course.get("title"))} for course in courses]
        self.write(data)

    def plan(self, tasks):
        data = self.read()
        chosen = set(self.saved_selection())
        available = {task_key(task) for task in tasks}
        rank = {row_key(row): index for index, row in enumerate(data["pending"])
                if row_key(row)[:2] in chosen}
        self.unavailable = set(rank) - available
        # Checkpoints remember work, not playback order. Their pending rows may
        # have been rotated after a failure; the fresh directory stays authoritative.
        return [task for task in tasks if not flag_is_true(task.point.get("has_finished"))]

    def update(self, tasks, reasons=None):
        data = self.read()
        chosen = set(self.saved_selection())
        old = {row_key(row): row for row in data["pending"]}
        pending = [row for row in data["pending"] if row_key(row)[:2] not in chosen
                   or row_key(row) in self.unavailable]
        reasons = reasons or {}
        seen = {row_key(row) for row in pending}
        for task in tasks:
            key = task_key(task)
            if key in seen:
                continue
            seen.add(key)
            pending.append({**old.get(key, {}), "course_id": key[0], "class_id": key[1], "chapter_id": key[2],
                            "course_title": clean_text(task.course.get("title")),
                            "chapter_title": clean_text(task.point.get("title")),
                            "reason": clean_text(reasons.get(key, old.get(key, {}).get("reason", "待处理")))})
        data["pending"] = pending
        self.write(data)


class CancellableClock:
    def __init__(self, original, cancelled):
        self.original, self.cancelled = original, cancelled

    def __getattr__(self, name):
        return getattr(self.original, name)

    def sleep(self, duration):
        if self.cancelled.wait(max(0, duration)):
            raise StudyCancelled("已停止，任务记录已保留。")


class StudyEngine:
    def __init__(self, root, profile, mode, emit, call_ui, cancelled):
        if mode not in MODE_LABELS:
            raise StudyError("未知的运行窗口类型。")
        self.root, self.profile, self.mode = Path(root), profile, mode
        self.emit, self.call_ui, self.cancelled = emit, call_ui, cancelled
        self.main = self.cx = self.coordinator = None
        self.setup_failed = False
        self.courses, self.lock = [], None
        self.store = ModeStore(profile, mode)
        self.original_clock = self.original_progress = None
        self.confirmation_delays = (0, 2, 5)
        self.network_retry_delays = (3, 10, 30)
        self.max_restart_rounds = 3
        self.round_history = []
        self._round_number = 0
        self._round_outcome = None
        self.report_observations = []
        self.course_estimates = {}
        self.current_media_key = None

    def _check(self):
        check_cancelled(self.cancelled)

    def _state(self, status, message):
        self.coordinator.state(status, message)

    def _log(self, message):
        self.main.logger.info(clean_text(message))

    def _setup(self):
        if self.setup_failed:
            raise StudyError("账户窗口初始化未完成，请关闭本窗口后重新打开。")
        if self.lock is None:
            self.lock = ProfileLock(self.profile.directory).acquire()
        try:
            self._initialize()
            self._patch_media()
        except Exception:
            self.setup_failed = True
            raise StudyError("账户窗口初始化未完成，请关闭本窗口后重新打开。") from None

    def _initialize(self):
        if self.main is None:
            import sys
            if sys.stderr is None:
                sys.stderr = io.StringIO()
            if sys.stdout is None:
                sys.stdout = io.StringIO()
            from run_account import prepare
            from runtime_safety import install_runtime_safety, redact_log
            self.main = prepare(self.root, self.profile)
            self.coordinator = install_runtime_safety(self.main, self.profile)
            self.coordinator.cancelled = self.cancelled
            self.coordinator.dialog = lambda *args, **kwargs: self.call_ui("recovery", *args, **kwargs)
            original_state = self.coordinator.state

            def state(kind, message):
                original_state(kind, f"{MODE_LABELS[self.mode]}｜{message}")
                self.emit("status", {"state": kind, "message": message})

            self.coordinator.state = state
            self.main.logger.remove()
            self.main.logger.add(lambda message: self.emit("log", str(message).rstrip()),
                                 level="INFO", filter=redact_log, format="{time:HH:mm:ss} | {level} | {message}")
            self.main.logger.add(str(self.profile.directory / "chaoxing.log"), rotation="10 MB", level="INFO", filter=redact_log)
            from api.base import SessionManager
            session = SessionManager.get_session()
            original_request = session.request

            def request(method, url, **kwargs):
                self._check()
                kwargs.setdefault("timeout", (5, 20))
                response = original_request(method, url, **kwargs)
                self._check()
                return response

            session.request = request
            self.cx = self.main.Chaoxing(account=self.main.Account("", ""), tiku=None)
            original_progress_log = self.cx.video_progress_log
            def progress_log(*args, **kwargs):
                passed, state = original_progress_log(*args, **kwargs)
                return flag_is_true(passed), state
            self.cx.video_progress_log = progress_log

    def _patch_media(self):
        from api import base
        if self.original_clock is not None:
            return
        self.original_clock, self.original_progress = base.time, base.tqdm
        base.time = CancellableClock(base.time, self.cancelled)
        engine = self

        class GuiProgress:
            def __init__(self, total, initial=0, desc="", **options):
                self.total, self.n, self.desc, self.leave = total, initial, desc, False
                self.last = 0

            def refresh(self):
                now = time.monotonic()
                if now - self.last >= .5:
                    self.last = now
                    engine.emit("progress", {"done": self.n, "total": self.total, "title": clean_text(self.desc), "unit": "seconds"})
                    engine._update_media_estimate(self.total, self.n)

            def close(self):
                pass

        base.tqdm = GuiProgress

    @staticmethod
    def _transient_network(error):
        from runtime_safety import RequestFailed
        return (isinstance(error, (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError))
                and not isinstance(error, requests.exceptions.SSLError)) or (
                    isinstance(error, RequestFailed) and error.status in (500, 502, 503, 504))

    def _retry_network(self, operation, title):
        """Only callers with read-only or fresh-media replay semantics may use this."""
        for attempt in range(len(self.network_retry_delays) + 1):
            self._check()
            try:
                return operation()
            except Exception as error:
                if not self._transient_network(error):
                    raise
                if attempt == len(self.network_retry_delays):
                    raise StudyError(f"网络恢复未成功（{type(error).__name__}，已重试 {attempt} 次）；"
                                     "当前章节仍未完成，后续章节保留待处理。") from None
                delay = max(self.network_retry_delays[attempt], min(120, max(0, getattr(error, "retry_after", 0))))
                message = (f"网络暂时中断（{type(error).__name__}），{delay:g} 秒后恢复当前任务"
                           f"（{attempt + 1}/{len(self.network_retry_delays)}）：{clean_text(title)}")
                self._state("running", message)
                self._log(message)
                if self.cancelled.wait(delay):
                    self._check()

    def _with_recovery(self, operation, title, course=None, *, optional_estimate=False, read_only=False):
        from runtime_safety import AccessRestricted
        for attempt in range(3):
            self._check()
            self.coordinator.issue = None
            try:
                result = self._retry_network(operation, title) if read_only else operation()
                if self.coordinator.issue is not None:
                    raise self.coordinator.issue
                return result
            except AccessRestricted as error:
                if error.status == 403 and error.reason == "http" and error.verifier is None:
                    # An optional scan can fail on a later chapter. The real
                    # current-chapter request must still pass its own guard.
                    if optional_estimate:
                        self.coordinator.issue = None
                        raise
                    message = (f"当前项已保留待处理：{error.resource}被平台拒绝访问（403），未返回验证码。"
                               f"当前章节：{clean_text(title)}。请先在学习通官方页面核对；"
                               "若官方页面可访问，需核查服务器访问限制。当前任务保持未完成，账号记录已保留。")
                    self._log(message)
                    raise StudyError(message) from None
                task = SimpleNamespace(point={"title": title}, course=course or {})
                if self.coordinator.recover(error, task, attempt) != "retry" or attempt == 2:
                    raise StudyPaused("本轮已暂停：当前账户需要处理验证或重新登录，任务记录已保留。") from None
        raise StudyError("当前任务仍未恢复，请稍后重试。")

    def load_courses(self):
        try:
            self._check()
            self._setup()
            self._state("running", "正在读取账户课程…")
            from api.base import SessionManager
            if not SessionManager.get_session().cookies.get("_uid"):
                raise StudyError("此账户尚未登录。请关闭本窗口，在账户首页点击“登录账户”，然后重新打开。")
            courses = self._with_recovery(self.cx.get_course_list, "读取课程", read_only=True)
            if not isinstance(courses, list) or not courses or any(not isinstance(row, dict) or not all(
                    str(row.get(field, "")) for field in ("courseId", "clazzId", "cpi")) for row in courses):
                raise StudyError("未读取到有效课程。请关闭本窗口，在账户管理页重新登录后再试。")
            seen = set()
            self.courses = []
            for course in courses:
                if course_key(course) not in seen:
                    seen.add(course_key(course))
                    self.courses.append(dict(course))
            self._state("running", "请选择课程，再点击开始。")
            return self.courses
        except Exception:
            self.courses = []
            self.close()
            raise

    def saved_selection(self):
        own = self.store.saved_selection()
        if own:
            return own
        # The original selection can seed a new window, but is never written here.
        from resume_tasks import ResumeStore
        return [(row["course_id"], row["class_id"]) for row in ResumeStore(self.profile).read()["courses"]]

    def _selected(self, keys):
        normalized = [tuple(str(value) for value in key) for key in keys]
        available = {course_key(course): course for course in self.courses}
        if not normalized or any(len(key) != 2 or key not in available for key in normalized):
            raise StudyError("请从当前课程列表中选择课程和班级，没有自动扩大选课范围。")
        return [available[key] for key in dict.fromkeys(normalized)]

    def _estimate_course(self, course, tasks, speed):
        from course_eta import Estimate, log_course_estimate
        from runtime_safety import AccessRestricted
        self._state("running", "正在估算课程剩余音视频用时…")
        try:
            estimate = self._with_recovery(lambda: log_course_estimate(self.cx, course, tasks, speed, self.main.logger),
                                          "估算课程时间", course, optional_estimate=True)
        except AccessRestricted as error:
            if error.status != 403 or error.reason != "http" or error.verifier is not None:
                raise
            chapters = {str(task.point.get("id", "")) for task in tasks
                        if course_key(task.course) == course_key(course) and not flag_is_true(task.point.get("has_finished"))}
            estimate = Estimate(len(chapters), unknown=True,
                                unavailable_reason="估时读取被平台拒绝（403）；将单独核对当前章节权限")
            self._log(estimate.unavailable_reason + "。估时结果不用于判定章节完成。")
        if hasattr(estimate, "jobs") and hasattr(estimate, "message"):
            self.course_estimates[course_key(course)] = (course, estimate, speed)
            self._emit_estimate(course_key(course))

    def _emit_estimate(self, key):
        course, estimate, speed = self.course_estimates[key]
        self.emit("estimate", {"course_title": clean_text(course.get("title")), "message": estimate.message(speed),
            "chapters": estimate.chapters, "read_chapters": estimate.sampled})

    def _update_media_estimate(self, duration, played):
        if self.current_media_key is None:
            return
        key, chapter, job = self.current_media_key
        if key in self.course_estimates:
            _, estimate, _ = self.course_estimates[key]
            identity = chapter + "/" + job
            if identity not in estimate.jobs:
                estimate.unknown = True
                estimate.media_count += 1
            estimate.jobs[identity] = max(0, duration - played)
            estimate.media_seconds = sum(estimate.jobs.values())
            estimate.partial_seconds = 0
            self._emit_estimate(key)

    def run(self, selected_course_keys, speed=None, auto_quizzes=None):
        """One initial pass, then at most three fresh passes over the selection."""
        selected_course_keys = list(selected_course_keys)
        self.round_history = []
        finished = submitted = 0
        try:
            for number in range(1, self.max_restart_rounds + 2):
                self._check()
                self._round_number = number
                self._round_outcome = None
                self._log(f"开始第 {number}/{self.max_restart_rounds + 1} 轮：失败章节保留待处理，继续其他可执行章节。")
                result = self._run_round(selected_course_keys, speed, auto_quizzes if number == 1 else None)
                if self._round_outcome != "finished":
                    return result
                finished += self._round_counts[0]
                submitted += self._round_counts[1]
                chosen = set(self.store.saved_selection())
                pending = [row for row in self.store.read()["pending"] if row_key(row)[:2] in chosen]
                self.round_history.append({"round": number, "remaining": len(pending),
                                           "successful_tasks": self._round_counts[0], "new_submissions": self._round_counts[1]})
                if not pending:
                    break
                if number <= self.max_restart_rounds:
                    self._state("running", f"第 {number} 轮结束，仍有 {len(pending)} 个章节待处理；准备第 {number + 1} 轮重新核对所选课程。")
                    self._log(f"第 {number} 轮剩余 {len(pending)} 个章节；已完成的章节不重复执行。")
            summary = (f"已执行 {self._round_number} 轮（首轮后最多重跑 {self.max_restart_rounds} 轮）："
                       f"任务成功 {finished} 个，测验新提交 {submitted} 份")
            if pending:
                summary += f"，{len(pending)} 个章节保留待处理；已停止自动重跑，请查看运行报告中的课程、章节及原因。"
            else:
                summary += "，本模式队列已处理结束。"
            summary += "课程整体进度以学习通为准。"
            self._state("needs_attention" if pending else "completed", summary)
            return summary
        except StudyCancelled:
            self._state("stopped", "已停止，后续轮次已取消；选课与待处理记录已保留。")
            return "已停止，后续轮次已取消；选课与待处理记录已保留。"
        finally:
            if self.coordinator is not None:
                try:
                    from study_reports import write_report
                    path = write_report(self.root, self.profile, mode=self.mode,
                        state=getattr(self.coordinator, "current_status", "unknown"),
                        observations=self.report_observations,
                        rounds={"current": self._round_number, "restart_limit": self.max_restart_rounds,
                                "history": self.round_history})
                    self.emit("report", {"path": str(path)})
                except (OSError, ValueError, TypeError):
                    self._log("本账号报告暂未保存成功，原任务记录已保留。")

    def _run_round(self, selected_course_keys, speed=None, auto_quizzes=None):
        if self.main is None or self.cx is None or self.coordinator is None:
            raise StudyError("请先读取课程。")
        self._check()
        if self.lock is None:
            raise StudyError("账户窗口尚未取得运行权限，请先刷新课程。")
        chosen = self._selected(selected_course_keys)
        if self.mode == "quizzes":
            speed = 1
        elif speed is None:
            config = configparser.ConfigParser(interpolation=None)
            config.read(self.profile.directory / "config.ini", encoding="utf-8")
            speed = config.get("common", "speed", fallback="1")
        try:
            speed = float(speed)
            if not math.isfinite(speed):
                raise ValueError()
        except (TypeError, ValueError):
            raise StudyError("请填写有效的视频倍速。") from None
        speed = min(2.0, max(1.0, speed))
        if self.mode != "videos":
            from quiz_assistant import read_settings
            from quiz_dialog import ai_configured
            if not ai_configured(read_settings(self.profile)):
                raise StudyError("请先在本窗口的“AI设置”中配置并启用 AI 答题。")
        if auto_quizzes is not None:
            if not isinstance(auto_quizzes, bool):
                raise StudyError("自动答题开关状态无效，请重新选择。")
            self.coordinator.auto_quizzes = auto_quizzes and self.mode != "videos"
        # Automatic answering is a window-lifetime user choice. Starting another
        # selection or finishing one round must not silently revoke it.
        if self._round_number <= 1:
            self.coordinator.skip_all_quizzes = False
        if self.coordinator.auto_quizzes:
            self._log("自动答题保持开启，直到当前账户窗口关闭；个别失败保留待处理并继续。")
        self.store.save_selection(chosen)
        self.report_observations = []
        tasks, deferred, reasons = [], [], {}
        finished = submitted = 0
        active_task = None
        try:
            directory_succeeded = set()
            for course in chosen:
                self._state("running", "正在读取课程章节：" + clean_text(course.get("title")))
                try:
                    result = self._with_recovery(lambda: self.cx.get_course_point(
                        course["courseId"], course["clazzId"], course["cpi"]), "读取课程章节", course, read_only=True)
                    if not isinstance(result, dict) or not isinstance(result.get("points"), list):
                        raise StudyError("课程章节未读取完整，原模式记录已保留。")
                    if any(not isinstance(point, dict) or not str(point.get("id", "")) for point in result["points"]):
                        raise StudyError("课程章节编号缺失，原模式记录已保留。")
                    for point in result["points"]:
                        tasks.append(SimpleNamespace(course=course, point=dict(point), index=len(tasks)))
                    directory_succeeded.add(course_key(course))
                except (StudyCancelled, StudyPaused):
                    raise
                except Exception as error:
                    from runtime_safety import AccessRestricted
                    if isinstance(error, AccessRestricted):
                        raise
                    reason = str(error) if isinstance(error, StudyError) else "课程目录读取失败（" + type(error).__name__ + "）"
                    tasks.append(SimpleNamespace(course=course, point={"id": "__directory__", "title": "课程目录读取失败", "_read_error": reason}, index=len(tasks)))
            saved = self.store.read()
            cleaned = [row for row in saved["pending"] if not (
                row_key(row)[:2] in directory_succeeded and row.get("chapter_id") == "__directory__")]
            if len(cleaned) != len(saved["pending"]):
                saved["pending"] = cleaned
                self.store.write(saved)
            for task in tasks:
                if flag_is_true(task.point.get("has_finished")):
                    clear_confirmed(self.profile, task_key(task))
            tasks = self.store.plan(tasks)
            if self.store.unavailable:
                self._log(f"有 {len(self.store.unavailable)} 个原待处理章节暂未出现在平台目录中，已保留记录；继续其他章节。")
            self.store.update(tasks)
            total = len(tasks)
            estimated_courses = set()
            for index, task in enumerate(tasks):
                self._check()
                active_task = task
                remaining = tasks[index:]
                self.store.update([*remaining, *deferred], reasons)
                title = clean_text(task.point.get("title"))
                self._state("running", "正在读取章节：" + title)
                self.emit("progress", {"done": index, "total": total, "title": title})
                self.coordinator.quiz_chapter_title = title
                problems = []
                selected = []
                scope_confirmed = False
                try:
                    if task.point.get("_read_error"):
                        raise StudyError(task.point["_read_error"])
                    if self.mode != "quizzes" and course_key(task.course) not in estimated_courses:
                        estimated_courses.add(course_key(task.course))
                        try:
                            self._estimate_course(task.course, remaining, speed)
                        except (StudyCancelled, StudyPaused):
                            raise
                        except Exception:
                            self._log("预计用时暂无法读取，继续处理课程。")
                    self._state("running", "正在读取章节：" + title)
                    entries = self._with_recovery(lambda: read_chapter_jobs(
                        self.cx, task.course, task.point, self.cancelled), title, task.course, read_only=True)
                    selected = [(job, info) for job, info in entries if self._accept(job)]
                    for job, info in selected:
                        self._check()
                        name = clean_text(job.get("name") or title)
                        self._state("running", "正在处理：" + name)
                        if job.get("type") == "workid":
                            if self.coordinator.skip_all_quizzes:
                                problems.append("本轮已跳过答题")
                                continue
                            try:
                                outcome = self._with_recovery(lambda: reviewed_quiz(self.cx, task.course, job, info,
                                    self.profile, self.coordinator, reviewer=lambda *args: self.call_ui("quiz", *args)),
                                    title, task.course)
                            except (StudyCancelled, StudyPaused):
                                raise
                            except Exception as error:
                                from runtime_safety import AccessRestricted
                                if isinstance(error, AccessRestricted) or not self.coordinator.auto_quizzes:
                                    raise
                                reason = str(error) if isinstance(error, QuizError) else \
                                    f"测验处理未成功（{type(error).__name__}）"
                                problems.append(name + "：" + reason)
                                self._state("needs_attention", "本测验保留待处理，继续后续自动答题：" + reason)
                                self.main.logger.warning("自动答题未完成：{}｜{}", name, reason)
                                continue
                            if outcome in ("submitted", "auto_submitted", "pending_confirmation"):
                                submitted += outcome != "pending_confirmation"
                                problems.append("测验已提交或结果待确认")
                            else:
                                if outcome == "skip_all_quizzes":
                                    self.coordinator.skip_all_quizzes = True
                                    self.coordinator.auto_quizzes = False
                                problems.append("测验尚未提交")
                        elif job.get("type") in ("video", "document", "read"):
                            self.current_media_key = (course_key(task.course), str(task.point["id"]), str(job.get("jobid") or job.get("id") or ""))
                            result = self._process_media(task, job, info, speed)
                            if result == self.main.StudyResult.SUCCESS:
                                finished += 1
                                self._update_media_estimate(0, 0)
                                self._log("任务处理成功：" + name)
                                if flag_is_true(task.point.get("has_finished")):
                                    break
                            else:
                                problems.append("任务尚未成功：" + name)
                        else:
                            problems.append("需要在网页处理：" + name)
                    # A successful local job does not confirm the whole chapter.
                    if selected or problems:
                        current, scope_confirmed, verification_reason = self._confirm_current(task, selected)
                        if current is not None and flag_is_true(current.get("has_finished")):
                            clear_confirmed(self.profile, task_key(task))
                            report_quiz_completed(self.coordinator, task.course, current)
                            problems.clear()
                            self._observe(task, "confirmed", "平台已确认章节完成")
                        elif scope_confirmed:
                            problems = ["本模式任务已确认完成，章节仍有其他任务需处理"]
                        else:
                            problems.append(verification_reason)
                except StudyPaused as error:
                    reasons[task_key(task)] = str(error)
                    self._observe(task, "pending", str(error))
                    self.store.update([*remaining, *deferred], reasons)
                    raise
                except StudyCancelled:
                    raise
                except Exception as error:
                    from runtime_safety import AccessRestricted
                    if isinstance(error, AccessRestricted):
                        raise
                    problems.append(str(error) if isinstance(error, (StudyError, QuizError)) else
                                    f"读取或处理未成功（{type(error).__name__}）")
                self._check()
                if problems:
                    deferred.append(task)
                    reasons[task_key(task)] = "；".join(problems)
                    self._log("待处理：" + title + "｜" + reasons[task_key(task)])
                    self._observe(task, "pending", reasons[task_key(task)])
                    self._log("当前章节保留待处理，继续检查后续章节。")
                elif not selected:
                    self._log("本窗口没有需要处理的对应任务：" + title)
                self.store.update([*tasks[index + 1:], *deferred], reasons)
                self.emit("progress", {"done": index + 1, "total": total, "title": f"第 {self._round_number} 轮已检查（成功数见最终汇总）"})
            if deferred and self.mode != "quizzes":
                deferred = self._recheck_pending_chapters(deferred, reasons)
                self.store.update(deferred, reasons)
            self._round_counts = (finished, submitted)
            self._round_outcome = "finished"
            summary = f"本轮{MODE_LABELS[self.mode]}处理结束：任务成功 {finished} 个，测验新提交 {submitted} 份"
            if deferred or self.store.unavailable:
                summary += f"，{len(deferred) + len(self.store.unavailable)} 个章节保留待处理。"
            else:
                summary += "。"
            summary += "课程整体进度以学习通为准。"
            self._state("needs_attention" if deferred or self.store.unavailable else "completed", summary)
            return summary
        except StudyCancelled:
            self._round_outcome = "stopped"
            self._state("stopped", "已停止，本窗口的选课与待处理记录已保留。")
            return "已停止，本窗口的选课与待处理记录已保留。"
        except StudyPaused as error:
            self._round_outcome = "paused"
            self._state("needs_attention", str(error))
            return str(error)
        except Exception as error:
            if active_task is not None:
                from runtime_safety import AccessRestricted
                reason = str(error) if isinstance(error, (StudyError, QuizError, AccessRestricted)) else "执行失败（" + type(error).__name__ + "）"
                self._observe(active_task, "pending", reason)
            self._state("needs_attention", "本轮处理已停止，任务记录已保留。")
            raise
        finally:
            self.current_media_key = None
            try:
                from study_reports import write_report
                path = write_report(self.root, self.profile, mode=self.mode,
                    state=getattr(self.coordinator, "current_status", "unknown"), observations=self.report_observations)
                self.emit("report", {"path": str(path)})
            except (OSError, ValueError, TypeError):
                self._log("本账号报告暂未保存成功，原任务记录已保留，可在“运行报告”中重新汇总。")

    def _observe(self, task, state, reason):
        self.report_observations.append({"course_id": course_key(task.course)[0], "class_id": course_key(task.course)[1],
            "chapter_id": str(task.point["id"]), "course_title": clean_text(task.course.get("title")),
            "chapter_title": clean_text(task.point.get("title")), "state": state, "reason": clean_text(reason)})

    def _confirm_current(self, task, selected):
        last_reason = "任务已处理，平台尚未确认本章节完成；已保留当前章节，继续检查其他章节"
        for attempt, delay in enumerate(self.confirmation_delays or (0,)):
            self._check()
            if delay and self.cancelled.wait(delay):
                self._check()
            self._state("running", f"正在确认当前章节完成状态（{attempt + 1}/{len(self.confirmation_delays or (0,))}）：" + clean_text(task.point.get("title")))
            try:
                fresh = self._with_recovery(lambda: self.cx.get_course_point(task.course["courseId"],
                    task.course["clazzId"], task.course["cpi"]), clean_text(task.point.get("title")), task.course, read_only=True)
                points = fresh.get("points") if isinstance(fresh, dict) else None
                if not isinstance(points, list):
                    raise StudyError("平台复查未完整返回章节目录")
                matches = [point for point in points if isinstance(point, dict) and str(point.get("id")) == str(task.point["id"])]
                if len(matches) != 1:
                    raise StudyError("平台复查的章节编号缺失或重复")
                current = matches[0]
                if flag_is_true(current.get("has_finished")):
                    return current, True, ""
                # Single-purpose modes may legitimately leave other task types.
                # Only explicit pass flags for every selected task allow advance.
                if self.mode != "mixed" and selected:
                    states = self._with_recovery(lambda: read_chapter_job_states(self.cx, task.course, current, self.cancelled),
                        clean_text(task.point.get("title")), task.course, read_only=True)
                    keys = [(str(job.get("type", "")), str(job.get("jobid") or job.get("id") or job.get("mid") or "")) for job, _ in selected]
                    if all(key[1] and states.get(key) is True for key in keys):
                        accepted = "video" if self.mode == "videos" else "workid"
                        if not any(kind == accepted and not passed for (kind, _), passed in states.items()):
                            return current, True, ""
            except (StudyCancelled, StudyPaused):
                raise
            except Exception as error:
                from runtime_safety import AccessRestricted
                if isinstance(error, AccessRestricted):
                    raise
                last_reason = "平台复查未成功（" + type(error).__name__ + "），未确认完成；已保留当前章节，继续检查其他章节"
        return None, False, last_reason

    def _recheck_pending_chapters(self, pending, reasons):
        """Confirm late platform updates without replaying a video or submitting a quiz."""
        from runtime_safety import AccessRestricted
        remaining = list(pending)
        courses = {course_key(task.course): task.course for task in pending}
        for key, course in courses.items():
            self._check()
            self._state("running", "正在核对未完成章节：" + clean_text(course.get("title")))
            try:
                fresh = self.cx.get_course_point(course["courseId"], course["clazzId"], course["cpi"])
                self._check()
                if not isinstance(fresh, dict) or not isinstance(fresh.get("points"), list):
                    raise StudyError("章节确认信息未读取完整")
                points = {}
                for point in fresh["points"]:
                    if not isinstance(point, dict) or not str(point.get("id", "")) or str(point["id"]) in points:
                        raise StudyError("章节确认信息存在缺失或重复编号")
                    points[str(point["id"])] = point
            except StudyCancelled:
                raise
            except AccessRestricted:
                self._log("平台限制了最终核对；未确认的章节仍保留待处理。")
                break
            except Exception:
                self._log("最终核对未成功；未确认的章节仍保留待处理。")
                continue
            confirmed = []
            for task in remaining:
                point = points.get(str(task.point["id"])) if course_key(task.course) == key else None
                if point and flag_is_true(point.get("has_finished")):
                    clear_confirmed(self.profile, task_key(task))
                    report_quiz_completed(self.coordinator, course, point)
                    self._observe(task, "confirmed", "平台最终复查已确认章节完成")
                    confirmed.append(task_key(task))
                    reasons.pop(task_key(task), None)
            remaining = [task for task in remaining if task_key(task) not in confirmed]
        return remaining

    def _accept(self, job):
        kind = job.get("type")
        if self.mode == "mixed":
            return True
        return kind == ("workid" if self.mode == "quizzes" else "video") or (
            kind == "manual" and job.get("kind") == ("workid" if self.mode == "quizzes" else "video"))

    def _refresh_media(self, task, original):
        """Re-read server progress/tokens for this exact video; never replay quizzes."""
        title = clean_text(task.point.get("title"))
        fresh = self._with_recovery(lambda: self.cx.get_course_point(task.course["courseId"],
            task.course["clazzId"], task.course["cpi"]), title, task.course, read_only=True)
        points = fresh.get("points") if isinstance(fresh, dict) else None
        if not isinstance(points, list):
            raise StudyError("网络恢复后未能核对当前章节，保留待处理。")
        matches = [p for p in points if isinstance(p, dict) and str(p.get("id")) == str(task.point["id"])]
        if len(matches) != 1:
            raise StudyError("网络恢复后的当前章节编号缺失或重复，保留待处理。")
        point = matches[0]
        if flag_is_true(point.get("has_finished")):
            task.point.update(point)
            return None
        rows = self._with_recovery(lambda: read_chapter_jobs(self.cx, task.course, point, self.cancelled),
                                   title, task.course, read_only=True)
        identity = str(original.get("jobid") or original.get("id") or original.get("mid") or "")
        matching = [(job, info) for job, info in rows if job.get("type") == "video"
                    and str(job.get("jobid") or job.get("id") or job.get("mid") or "") == identity]
        if identity and len(matching) == 1:
            job, info = matching[0]
            if not original.get("objectid") or job.get("objectid") != original["objectid"]:
                raise StudyError("网络恢复后的音视频对象与原任务不一致，未继续播放。")
            self._log("已从平台重新读取当前音视频的进度与参数；继续同一任务。")
            return dict(job), dict(info)
        if not matching and identity:
            states = self._with_recovery(lambda: read_chapter_job_states(self.cx, task.course, point, self.cancelled),
                                         title, task.course, read_only=True)
            if states.get(("video", identity)) is True:
                return None
        raise StudyError("网络恢复后未能唯一确认原音视频任务，仍保留待处理。")

    def _process_media(self, task, job, info, speed):
        if job.get("type") != "video":
            return self._with_recovery(lambda: self.main.process_job(self.cx, task.course, job, info, speed),
                                       clean_text(task.point.get("title")), task.course)
        attempted = False
        def play():
            nonlocal attempted
            current_job, current_info = job, info
            if attempted:
                refreshed = self._refresh_media(task, job)
                if refreshed is None:
                    self._log("平台已确认原音视频任务完成，不重复处理。")
                    return self.main.StudyResult.SUCCESS
                current_job, current_info = refreshed
            attempted = True
            return self.main.process_job(self.cx, task.course, current_job, current_info, speed)
        title = clean_text(task.point.get("title"))
        return self._retry_network(lambda: self._with_recovery(play, title, task.course), title)

    def close(self):
        if self.original_clock is not None:
            from api import base
            base.time, base.tqdm = self.original_clock, self.original_progress
            self.original_clock = self.original_progress = None
        if self.lock is not None:
            try:
                if self.coordinator is not None and getattr(self.coordinator, "current_status", "") not in ("completed", "needs_attention"):
                    self._state("stopped", "窗口已关闭，任务记录已保留。")
            finally:
                self.lock.release()
                self.lock = None
