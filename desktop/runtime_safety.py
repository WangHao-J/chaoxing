"""Keep sessions stable and preserve unfinished work when a request is restricted."""
import json
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from manual_tasks import ManualTasksRequired, install_manual_tasks
from manual_verification import ManualVerifier, VerificationUnavailable
from resume_tasks import ResumeStore


class AccessRestricted(Exception):
    def __init__(self, status, retry_after=0, verifier=None, reason=None, resource="平台请求"):
        self.status = status
        self.retry_after = retry_after
        self.verifier = verifier
        self.reason = reason or ("challenge" if verifier else "http")
        self.resource = resource
        messages = {401: "程序登录状态已失效，请停止当前账户并在管理界面重新登录", 403: "平台拒绝访问（403）；未提供验证码，需核对课程权限、登录状态或服务器访问限制",
                    429: "请求受到频率限制，请稍后重新检查"}
        super().__init__("平台要求图片验证，请在本窗口显示并填写验证码" if verifier else
                         messages.get(status, "需要在学习通中人工处理验证"))


class RequestFailed(Exception):
    def __init__(self, message, status=None, retry_after=0):
        self.status, self.retry_after = status, retry_after
        super().__init__(message)


class RunNeedsAttention(Exception):
    pass


class IncompleteRun(Exception):
    pass


def retry_delay(value):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 60.0


def redact_log(record):
    message = record["message"]
    if message.startswith("Traceback") and any(name in message for name in
            ("RunNeedsAttention:", "IncompleteRun:")):
        return False
    message = re.sub(r'https?://[^\s<>\'"()]+',
                     lambda match: "https://" + (urlsplit(match[0]).hostname or "学习通") + "/[已隐藏]",
                     message)
    message = re.sub(r"(?im)(cookie|authorization|password)\s*['\"]?\s*[:=].*$",
                     r"\1: [已隐藏]", message)
    message = re.sub(r"(?i)(['\"]?(?:jtoken|ktoken|mtEnc|enc|dtoken|ucode)['\"]?\s*[:=]\s*)['\"]?[^\s,}\"']+['\"]?",
                     r"\1[已隐藏]", message)
    record["message"] = message
    return True


def configure_logging():
    from api.logger import logger, tqdm_sink
    logger.remove()
    logger.add(tqdm_sink, level="INFO", colorize=True, enqueue=True, filter=redact_log)
    logger.add("chaoxing.log", rotation="10 MB", level="INFO", filter=redact_log)


class SafetyCoordinator:
    def __init__(self, profile, dialog=None):
        self.profile = profile
        self.dialog = dialog
        self.lock = threading.RLock()
        self.issue = None
        self.completed = False
        self.pending = []
        self.skip_all_quizzes = False
        self.auto_quizzes = False

    def write_json(self, name, data):
        with self.lock:
            path = self.profile.directory / name
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)

    def state(self, status, message):
        self.current_status, self.current_message = status, message
        self.write_json("runtime_state.json", {
            "status": status, "message": message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    def remember(self, tasks):
        self.pending = [{"course_id": str(task.course.get("courseId", "")),
                         "class_id": str(task.course.get("clazzId", "")),
                         "cpi": str(task.course.get("cpi", "")),
                         "course_title": str(task.course.get("title", "")),
                         "chapter_id": str(task.point.get("id", "")),
                         "chapter_title": str(task.point.get("title", "")),
                         "manual_tasks": getattr(task, "manual_titles", [])} for task in tasks]
        self.write_json("pending_tasks.json", {"tasks": self.pending})
        ResumeStore(self.profile).remember(self.pending)

    def guard_session(self, session):
        original_request = session.request

        def request(method, url, **kwargs):
            response = original_request(method, url, **kwargs)
            status = response.status_code
            is_html = "html" in response.headers.get("Content-Type", "").lower()
            destination = urlsplit(response.url or str(url))
            # The authentication API can return JSON labelled text/html. Inspect
            # the body before treating its URL as a redirected login page.
            if is_html and destination.hostname in ("passport2.chaoxing.com", "passport2-api.chaoxing.com") \
                    and destination.path.lower().rstrip("/") == "/fanyalogin":
                try:
                    payload = response.json()
                except (ValueError, TypeError):
                    pass
                else:
                    if isinstance(payload, dict) and isinstance(payload.get("status"), bool):
                        is_html = False
            progress_request = "/mooc-ans/multimedia/log/" in urlsplit(str(url)).path
            text = response.text[:32768].lower() if is_html or progress_request else ""
            challenge = any(marker in text for marker in
                ("processverifypng", "processverify.ac", "请输入验证码", "安全验证", "人机验证", "captcha"))
            challenge = challenge or (progress_request and ("验证码" in text or "validate" in text))
            login_page = is_html and destination.hostname in ("passport2.chaoxing.com", "passport2-api.chaoxing.com") \
                and destination.path.lower().startswith(("/login", "/fanyalogin"))
            if status in (401, 403, 429) or challenge or login_page:
                verifier = None
                if challenge and status != 429 and not login_page:
                    try:
                        verifier = ManualVerifier(session, original_request, response.url or str(url), kwargs.get("headers"))
                    except VerificationUnavailable:
                        pass
                issue = AccessRestricted(status if status in (401, 403, 429) else (401 if login_page else 403),
                    retry_delay(response.headers.get("Retry-After", "60")) if status == 429 else 0, verifier,
                    reason="login_page" if login_page else ("challenge" if challenge else "http"))
                path = urlsplit(str(url)).path
                issue.resource = ("章节任务页" if path.endswith("/knowledge/cards") else
                                  "音视频信息" if "/ananas/status/" in path else
                                  "音视频进度接口" if progress_request else
                                  "课程目录" if path.endswith("/mycourse/studentcourse") else "平台请求")
                self.issue = issue
                raise issue
            if status >= 400:
                raise RequestFailed(f"请求未成功（HTTP {status}），章节仍保留为未完成", status,
                                    retry_delay(response.headers.get("Retry-After", "0")))
            return response

        session.request = request

    def recover(self, issue, task, attempts):
        self.state("needs_attention", str(issue))
        if self.dialog is None:
            from recovery_dialog import ask_recovery
            dialog = ask_recovery
        else:
            dialog = self.dialog
        # Only an explicit user click can retry a restricted request.
        return dialog(self.profile.name, str(issue), task.point.get("title", "当前章节"),
                      max(issue.retry_after, min(30, 10 * (attempts + 1))), attempts < 2,
                      verifier=issue.verifier, course_title=getattr(task, "course", {}).get("title", ""))

    def manual_tasks(self, issue, task, attempts):
        jobs = [job for job in issue.jobs if not job.get("skipped_quiz", False) and not job.get("awaiting_confirmation", False) and not (
            (self.skip_all_quizzes or self.auto_quizzes) and job.get("kind") == "workid")]
        if not jobs:
            return "skip"
        self.state("needs_attention", str(issue))
        from recovery_dialog import ask_recovery
        dialog = self.dialog or ask_recovery
        options = {}
        if any(job.get("kind") == "workid" for job in jobs):
            from quiz_dialog import ensure_ai_config
            options["on_quiz"] = lambda parent: ensure_ai_config(parent, self.profile)
        description = "\n".join(job["name"] for job in jobs) if all(job.get("kind") == "workid" for job in jobs) else \
            str(ManualTasksRequired(jobs))
        action = dialog(self.profile.name, description, task.point.get("title", "当前章节"),
                        0 if all(job.get("kind") == "workid" for job in jobs) else 5,
                        attempts < 2, course_title=task.course.get("title", ""), manual_jobs=jobs, **options)
        if action == "skip_all_quizzes" and any(job.get("kind") == "workid" for job in jobs):
            self.skip_all_quizzes = True
            self.auto_quizzes = False
            remaining = [job for job in jobs if job.get("kind") != "workid"]
            if remaining:
                return self.manual_tasks(ManualTasksRequired(remaining), task, attempts)
            return "skip"
        return action


def harden_session_manager(cls, coordinator):
    original_init = cls.__init__
    initialization_lock = threading.RLock()

    def stable_new(target, *args, **kwargs):
        with initialization_lock:
            if target._instance is None:
                target._instance = object.__new__(target)
            return target._instance

    def stable_init(self):
        with initialization_lock:
            if getattr(self, "_local_session_ready", False):
                return
            original_init(self)
            from requests.adapters import HTTPAdapter
            # The scheduler bounds retries, rather than stacking adapter retries.
            self._session.mount("https://", HTTPAdapter(max_retries=0))
            self._session.mount("http://", HTTPAdapter(max_retries=0))
            coordinator.guard_session(self._session)
            self._local_session_ready = True

    cls.__new__ = staticmethod(stable_new)
    cls.__init__ = stable_init


def make_processor(original_class, main, coordinator, estimate_courses=False):
    class ReliableProcessor(original_class):
        def run(self):
            pending = deque(ResumeStore(coordinator.profile).order(self.tasks))
            self.completed_tasks = []
            manual_attempts = {}
            link_attempts = {}
            estimated_courses = set()
            if self.worker_num != 1:
                main.logger.info("当前账户按顺序处理章节；其他账户仍可同时运行。")
            coordinator.state("running", "正在处理章节")
            coordinator.remember(list(pending))
            while pending:
                task = pending.popleft()
                coordinator.remember([task, *pending, *self.failed_tasks])
                coordinator.issue = None
                try:
                    if estimate_courses:
                        from resume_tasks import course_key
                        key = course_key(task.course)
                        if key not in estimated_courses:
                            estimated_courses.add(key)
                            from course_eta import log_course_estimate
                            try:
                                log_course_estimate(self.chaoxing, task.course, [task, *pending], self.speed, main.logger)
                            except AccessRestricted:
                                raise
                            except Exception:
                                main.logger.info("预计剩余时间暂无法读取，继续处理课程；答题、验证用时另计。")
                    if getattr(task, "refresh_point", False):
                        points = self.chaoxing.get_course_point(task.course["courseId"],
                            task.course["clazzId"], task.course["cpi"])["points"]
                        match = next((p for p in points if str(p["id"]) == str(task.point["id"])), None)
                        if match:
                            task.point.update(match)
                            from quiz_assistant import report_quiz_completed
                            report_quiz_completed(coordinator, task.course, task.point)
                        task.refresh_point = False
                    cached_quizzes = getattr(task, "resume_quizzes", None)
                    if cached_quizzes:
                        del task.resume_quizzes
                        task.result = main.process_pending_quizzes(self.chaoxing, task.course, task.point,
                                                                  cached_quizzes, self.speed)
                    else:
                        task.result = main.process_chapter(self.chaoxing, task.course, task.point, self.speed)
                    # Some upstream routines run a child thread and swallow its exception.
                    if coordinator.issue is not None:
                        raise coordinator.issue
                except ManualTasksRequired as issue:
                    task.manual_titles = [job["name"] for job in issue.jobs]
                    coordinator.remember([task, *pending, *self.failed_tasks])
                    main.logger.info("发现 {} 个未完成的手动任务，已保留记录。", len(issue.jobs))
                    count = link_attempts.get(id(task), 0)
                    action = coordinator.manual_tasks(issue, task, count)
                    if action == "quiz" and any(job.get("quiz_context") for job in issue.jobs):
                        task.resume_quizzes = list(issue.jobs)
                        coordinator.state("running", "正在打开当前测验答题页面")
                        pending.appendleft(task)
                        continue
                    if action == "retry" and count < 2:
                        link_attempts[id(task)] = count + 1
                        coordinator.state("running", "正在核对学习通中的任务状态")
                        task.refresh_point = True
                        pending.appendleft(task)
                        continue
                    if action == "skip":
                        task.result = main.ChapterResult.ERROR
                        self.failed_tasks.append(task)
                        coordinator.remember([*pending, *self.failed_tasks])
                        coordinator.state("running", "已保留待处理任务，继续其他章节")
                        continue
                    coordinator.state("needs_attention", "待处理的章节测验或外部任务已保留")
                    raise RunNeedsAttention("已停止。仍有任务尚未完成，请在对应课程章节中处理后再启动。")
                except AccessRestricted as issue:
                    main.logger.warning("当前账户已暂停：{}；当前章节保持未完成。", str(issue))
                    count = manual_attempts.get(id(task), 0)
                    action = coordinator.recover(issue, task, count)
                    if action != "retry" or count >= 2:
                        coordinator.state("needs_attention", "已停止，待处理章节已保留；可处理验证后重新登录")
                        raise RunNeedsAttention("当前账户已停止。待处理章节已保留，请处理验证或重新登录后再启动。")
                    manual_attempts[id(task)] = count + 1
                    coordinator.state("running", "正在重新读取当前章节状态")
                    pending.appendleft(task)
                    continue
                except Exception as error:
                    task.result = main.ChapterResult.ERROR
                    main.logger.warning("章节处理未成功（{}），保留为待处理。", type(error).__name__)

                if task.result == main.ChapterResult.SUCCESS:
                    self.completed_tasks.append(task)
                elif task.result == main.ChapterResult.NOT_OPEN and self.config.get("notopen_action") == "continue":
                    self.failed_tasks.append(task)
                else:
                    task.tries += 1
                    if task.tries >= self.max_tries:
                        self.failed_tasks.append(task)
                    else:
                        pending.append(task)
                        delay = min(30, max(1, self.retry_interval) * 2 ** (task.tries - 1))
                        main.logger.info("章节未完成，将稍后重新检查（{}/{}）。", task.tries, self.max_tries)
                        time.sleep(delay)
                coordinator.remember([*pending, *self.failed_tasks])
            if self.failed_tasks:
                coordinator.state("needs_attention", f"仍有 {len(self.failed_tasks)} 个章节未完成")
                raise IncompleteRun(f"仍有 {len(self.failed_tasks)} 个章节未完成；请查看待处理列表，未发送全部完成通知。")
            coordinator.completed = True
            coordinator.state("completed", "本轮任务处理结束，请在学习通核对进度")

    return ReliableProcessor


def install_runtime_safety(main, profile):
    from api.base import SessionManager
    coordinator = SafetyCoordinator(profile)
    configure_logging()
    harden_session_manager(SessionManager, coordinator)
    install_manual_tasks(main, profile, coordinator)
    main.JobProcessor = make_processor(main.JobProcessor, main, coordinator, estimate_courses=True)
    coordinator.state("running", "正在登录或准备课程")
    return coordinator
