"""AI quiz assistance with explicit review or explicitly enabled run-local automation."""
import base64
import ctypes
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit


class QuizError(Exception):
    pass


class QuizParametersUnavailable(QuizError):
    pass


class AIServiceError(QuizError):
    """An account/service failure for which requesting more questions cannot help."""
    stop_batch = True


class AnswerDrafts:
    """Only question fingerprints and validated text answers; never request forms or tokens."""
    def __init__(self, profile, quiz_key, questions):
        self.path = profile.directory / "quiz_drafts.json"
        self.quiz_key, self.questions = quiz_key, questions
        public = [{name: getattr(question, name) for name in
                   ("id", "title", "kind", "options", "blank_count")} for question in questions]
        self.fingerprint = hashlib.sha256(json.dumps(public, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.data = {}
        try:
            if self.path.exists() and self.path.stat().st_size <= 4 * 1024 * 1024:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data = loaded
        except (OSError, ValueError):
            # A draft is optional. The submission ledger remains independently fail-closed.
            pass

    def read(self):
        entry = self.data.get(self.quiz_key, {})
        if not isinstance(entry, dict) or entry.get("fingerprint") != self.fingerprint:
            return {}, {}
        answers, explanations = {}, {}
        source, notes = entry.get("answers", {}), entry.get("explanations", {})
        if not isinstance(source, dict):
            return {}, {}
        for question in self.questions:
            try:
                answers[question.id] = validate_values(question, source.get(question.id))
                if isinstance(notes, dict):
                    explanations[question.id] = str(notes.get(question.id, ""))[:4000]
            except QuizError:
                pass
        return answers, explanations

    def save(self, answers, explanations=None):
        valid = {}
        for question in self.questions:
            try:
                values = validate_values(question, answers.get(question.id))
                if sum(len(value) for value in values) <= 64000:
                    valid[question.id] = values
            except QuizError:
                pass
        notes = explanations if isinstance(explanations, dict) else {}
        self.data[self.quiz_key] = {"fingerprint": self.fingerprint, "answers": valid,
            "explanations": {qid: str(notes.get(qid, ""))[:4000] for qid in valid}}
        # Bound optional history while leaving the safety ledger untouched.
        while len(self.data) > 100:
            del self.data[next(iter(self.data))]
        self._write()

    def clear(self):
        if self.quiz_key in self.data:
            del self.data[self.quiz_key]
            self._write()

    def _write(self):
        try:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            raise QuizError("答案草稿无法保存，请检查账户目录；已有提交记录未改变。") from None


def _log_title(value, fallback):
    return re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", " ", str(value or fallback))[:200]


def _log_account(coordinator):
    return _log_title(getattr(getattr(coordinator, "profile", None), "name", ""), "当前账户")


def remember_pending_quiz(coordinator, course, job, info):
    key = tuple(str(value) for value in (course.get("courseId", ""), course.get("clazzId", ""),
                                         info.get("knowledgeid", "")))
    pending = getattr(coordinator, "_pending_quiz_notifications", None)
    if not isinstance(pending, dict):
        pending = coordinator._pending_quiz_notifications = {}
    pending.setdefault(key, _log_title(getattr(coordinator, "quiz_chapter_title", ""),
                                       job.get("name") or "章节测验"))


def report_quiz_submitted(coordinator, course, job, info, count):
    from api.logger import logger
    remember_pending_quiz(coordinator, course, job, info)
    logger.info("答题已提交，等待平台确认完成｜账户：{}｜课程：{}｜章节：{}｜测验：{}（共 {} 题）",
                _log_account(coordinator), _log_title(course.get("title"), "当前课程"),
                _log_title(getattr(coordinator, "quiz_chapter_title", ""), job.get("name") or "章节测验"),
                _log_title(job.get("name"), "章节测验"), count)


def report_quiz_completed(coordinator, course, point):
    from api.logger import logger
    from card_reader import flag_is_true
    if not flag_is_true(point.get("has_finished", False)):
        return False
    key = tuple(str(value) for value in (course.get("courseId", ""), course.get("clazzId", ""),
                                         point.get("id", "")))
    pending = getattr(coordinator, "_pending_quiz_notifications", {})
    if not isinstance(pending, dict) or key not in pending:
        return False
    title = pending.pop(key)
    logger.success("答题完成（平台已确认）｜账户：{}｜课程：{}｜章节：{}",
                   _log_account(coordinator), _log_title(course.get("title"), "当前课程"),
                   _log_title(point.get("title"), title))
    return True


def crypt_key(value, decrypt=False):
    if os.name != "nt":
        raise QuizError("密钥保存需要 Windows 用户加密服务。")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value) if decrypt else value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    dll = ctypes.windll.crypt32
    function = dll.CryptUnprotectData if decrypt else dll.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise QuizError("密钥无法解密，请在当前 Windows 用户下重新填写。")
    try:
        result = ctypes.string_at(target.data, target.length)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(target.data)
    return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")


def api_url(base):
    base = base.strip().rstrip("/")
    try:
        parsed = urlsplit(base)
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment \
                or any(char.isspace() for char in base) or not (
                    parsed.scheme == "https" or (parsed.scheme == "http" and local)):
            raise ValueError()
    except ValueError:
        raise QuizError("请填写 HTTPS 服务地址；本机服务可使用 http://localhost。") from None
    return base if parsed.path.endswith("/chat/completions") else base + "/chat/completions"


def settings_path(profile):
    """Real account profiles share one encrypted configuration at install root."""
    from pathlib import Path
    directory = Path(profile.directory)
    return (directory.parent.parent if directory.parent.name == "accounts" else directory) / "ai_quiz.json"


def _read_settings_file(path):
    if not path.exists():
        return {"enabled": False, "endpoint": "", "model": "", "key": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"enabled": data.get("enabled") is True, "endpoint": str(data.get("endpoint", "")),
                "model": str(data.get("model", "")),
                "key": crypt_key(data["protected_key"], True) if data.get("protected_key") else ""}
    except Exception:
        raise QuizError("AI 设置无法读取，请打开“AI答题设置”重新保存。") from None


def read_settings(profile):
    return _read_settings_file(settings_path(profile))


def read_legacy_settings(profile):
    return _read_settings_file(profile.directory / "ai_quiz.json")


def save_settings(profile, settings):
    endpoint, model, key = (str(settings.get(name, "")).strip() for name in ("endpoint", "model", "key"))
    enabled = settings.get("enabled") is True
    if endpoint:
        api_url(endpoint)
    if enabled and not all((endpoint, model, key)):
        raise QuizError("启用 AI 答题时，请填写服务地址、模型名称和 API Key。")
    data = {"enabled": enabled, "endpoint": endpoint, "model": model,
            "protected_key": crypt_key(key) if key else ""}
    import uuid
    path = settings_path(profile)
    temporary = path.with_name(".ai_quiz-" + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@dataclass
class Question:
    id: str
    title: str
    kind: str
    options: str
    fields: list
    blank_count: int = 1
    unsupported: str = ""
    type_code: str = ""


def parse_work(html):
    from bs4 import BeautifulSoup
    from api.decode import decode_questions_info
    try:
        decoded = decode_questions_info(html)
        soup = BeautifulSoup(html, "lxml")
        questions = []
        for item in decoded["questions"]:
            qid = str(item["id"])
            element = soup.find("div", class_="singleQuesId", attrs={"data": qid})
            if element is None or not qid or not re.fullmatch(r"[A-Za-z0-9_-]+", qid):
                raise QuizError("题目结构不完整，请在网页完成本测验。")
            prefix = "answer" + qid
            controls = element.find_all(("input", "textarea", "select"))
            matching = [tag for tag in controls if re.fullmatch(
                re.escape(prefix) + r"(?:_?\d+|\[\d+\])?", str(tag.get("name", ""))) and not tag.has_attr("disabled")]
            visible = [tag for tag in matching if str(tag.get("type", "")).lower() != "hidden"]
            names = list(dict.fromkeys(str(tag["name"]) for tag in visible))
            kind = item["type"]
            unsupported = ""
            count = 1
            if kind == "completion":
                visible_inputs = [tag for tag in controls if tag.name == "textarea" or (
                    tag.name == "input" and str(tag.get("type", "text")).lower() in ("text", "search"))]
                count = max(1, len(names), len(visible_inputs))
                hint = element.find("input", attrs={"name": re.compile(r"(?:tiankongsize|completionNum)", re.I)})
                if hint and str(hint.get("value", "")).isdigit():
                    count = max(count, int(hint["value"]))
                if not names or len(names) != count:
                    unsupported = "页面未提供每个空独立的答案字段，可生成建议并复制到网页填写。"
                if any(tag.get("name") == prefix and tag not in visible for tag in matching) and names != [prefix]:
                    unsupported = "页面包含额外的填空合并字段，提交格式尚未确认；请复制建议到网页填写。"
            else:
                names = [prefix] if any(tag.get("name") == prefix for tag in matching) else names
                if len(names) != 1 or kind not in ("single", "multiple", "judgement", "shortanswer"):
                    unsupported = "此题型或答案字段暂不支持程序提交，可复制建议到网页填写。"
            if count > 100:
                raise QuizError("空位过多，请在网页完成本测验。")
            questions.append(Question(qid, item["title"], kind, item["options"], names, count,
                unsupported, str(item["answerField"]["answertype" + qid])))
        if not questions or len(questions) > 100:
            raise QuizError("没有读取到可处理的题目，请在网页检查测验状态。")
        if len({question.id for question in questions}) != len(questions):
            raise QuizError("题目编号重复，请在网页完成本测验。")
        form = {key: str(value) for key, value in decoded.items() if key != "questions"}
        return questions, form
    except QuizError:
        raise
    except Exception:
        raise QuizError("当前测验页面结构不兼容，请在网页完成；没有提交任何答案。") from None


def validate_values(question, values):
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise QuizError("答案格式不正确。")
    values = [value.strip() for value in values]
    if not values or any(not value for value in values):
        raise QuizError("请填写所有答案和空位。")
    if question.kind == "completion":
        if len(values) != question.blank_count:
            raise QuizError(f"这道题需要 {question.blank_count} 个答案，请按空位顺序填写。")
    elif question.kind in ("single", "multiple"):
        values = [value.upper() for value in values]
        options = set(re.findall(r"(?m)^\s*([A-Z])(?:[.、．:：\s])", question.options))
        if not options or any(value not in options for value in values) or (
                question.kind == "single" and len(values) != 1):
            raise QuizError("请填写题目中存在的选项字母。")
        values = sorted(set(values))
    elif question.kind == "judgement":
        mapping = {"true": "true", "false": "false", "正确": "true", "错误": "false", "对": "true", "错": "false"}
        if len(values) != 1 or values[0].lower() not in mapping:
            raise QuizError("判断题请填写 true / false 或 对 / 错。")
        values = [mapping[values[0].lower()]]
    elif len(values) != 1:
        raise QuizError("此题需要一个答案。")
    return values


def submission_form(questions, form, answers):
    result = dict(form)
    for question in questions:
        if question.unsupported:
            raise QuizError(question.unsupported)
        values = validate_values(question, answers.get(question.id))
        if question.kind == "completion":
            if len(question.fields) != len(values):
                raise QuizError("填空字段与答案数量不一致。")
            result.update(zip(question.fields, values))
        else:
            result[question.fields[0]] = "".join(values) if question.kind == "multiple" else values[0]
        result["answertype" + question.id] = question.type_code
    result["answerwqbid"] = ",".join(question.id for question in questions) + ","
    result["pyFlag"] = ""
    return result


def generate_suggestion(settings, question, post=None):
    import requests
    if not all(settings.get(name) for name in ("endpoint", "model", "key")):
        raise AIServiceError("请先在管理界面填写 AI 服务地址、模型名称和 API Key，本批已暂停。")
    try:
        endpoint = api_url(settings["endpoint"])
    except QuizError as error:
        raise AIServiceError(str(error)) from None
    payload = {"model": settings["model"], "messages": [
        {"role": "system", "content": "你是学习辅助助手。给出答案建议和简短解释，供用户审核。只输出JSON："
         '{"values":["答案"],"explanation":"解释"}。单选返回一个选项字母，多选每个字母独立一项，'
         "判断返回true或false，填空按blank_count顺序逐空返回，简答返回一段文本。不能确定时说明，不编造空位。"},
        {"role": "user", "content": json.dumps({"question": question.title, "type": question.kind,
            "options": question.options, "blank_count": question.blank_count}, ensure_ascii=False)}]}
    try:
        response = (post or requests.post)(endpoint, json=payload,
            headers={"Authorization": "Bearer " + settings["key"]}, timeout=(8, 60),
            allow_redirects=False, verify=True)
        if response.status_code != 200:
            code = response.status_code
            reason = {401: "API Key 无效或已过期，请检查 AI 设置。",
                      403: "AI 服务拒绝访问，请检查密钥权限和模型权限。",
                      402: "AI 服务余额不足，请检查服务账户。",
                      429: "AI 服务请求过于频繁或额度不足，请稍后重试。"}.get(code,
                "AI 服务暂时不可用，请稍后重试。" if code >= 500 else
                "请检查服务地址和模型名称。")
            raise AIServiceError(f"AI 请求未成功（HTTP {code}）：{reason} 本批已暂停。")
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
        result = json.loads(content)
        return validate_values(question, result["values"]), str(result.get("explanation", ""))[:4000]
    except QuizError:
        raise
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        raise AIServiceError("AI 服务连接超时或不可达，本批已暂停；请检查网络和服务地址。") from None
    except Exception:
        raise QuizError("AI 响应无法解析或连接失败。你可以重试或自行填写；没有提交答案。") from None


def generate_all(questions, generate, progress=None, cancelled=None, *, initial_answers=None,
                 initial_explanations=None, on_result=None, deadline_seconds=600):
    answers, explanations, errors = {}, {}, {}
    initial_answers = initial_answers if isinstance(initial_answers, dict) else {}
    initial_explanations = initial_explanations if isinstance(initial_explanations, dict) else {}
    for question in questions:
        try:
            answers[question.id] = validate_values(question, initial_answers.get(question.id))
            explanations[question.id] = str(initial_explanations.get(question.id, ""))
        except QuizError:
            pass
    started, stopped_error = time.monotonic(), ""
    for index, question in enumerate(questions):
        if cancelled and cancelled():
            return {"answers": answers, "explanations": explanations, "errors": errors, "cancelled": True}
        if question.id in answers:
            continue
        if time.monotonic() - started >= deadline_seconds:
            stopped_error = "本批 AI 生成已到等待上限，已保留完成的答案，请稍后继续。"
            errors[question.id] = stopped_error
            break
        if progress:
            progress(index + 1, len(questions))
        try:
            values, explanation = generate(question)
            answers[question.id] = validate_values(question, values)
            explanations[question.id] = str(explanation)
            if on_result:
                on_result(dict(answers), dict(explanations))
        except QuizError as error:
            errors[question.id] = str(error)
            if getattr(error, "stop_batch", False):
                stopped_error = str(error)
                break
        except Exception:
            errors[question.id] = "本题 AI 请求未成功，请重试或自行填写。"
    return {"answers": answers, "explanations": explanations, "errors": errors,
            "cancelled": bool(cancelled and cancelled()),
            "stopped_error": stopped_error}


class SubmissionLedger:
    def __init__(self, profile):
        self.path = profile.directory / "quiz_submissions.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            if not isinstance(self.data, dict):
                raise ValueError()
        except Exception:
            raise QuizError("提交记录无法读取，请在网页核对测验状态，避免重复提交。") from None

    def record(self, key, state):
        self.data[key] = {"state": state, "time": datetime.now(timezone.utc).isoformat()}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)


def reviewed_quiz(cx, course, job, info, profile, coordinator, reviewer=None):
    from api.base import SessionManager
    settings = read_settings(profile)
    if not settings["enabled"]:
        return "disabled"
    if any(value is None or not str(value).strip() for value in (
            course.get("courseId"), course.get("clazzId"), job.get("jobid"), job.get("enc"),
            info.get("knowledgeid"), info.get("ktoken"), info.get("cpi"))):
        raise QuizParametersUnavailable("测验参数未读取完整，请重新检查当前章节；尚未提交答案。")
    ledger = SubmissionLedger(profile)
    key = ":".join(str(value) for value in (course["courseId"], course["clazzId"], job["jobid"]))
    if ledger.data.get(key, {}).get("state") in ("uncertain", "acknowledged"):
        remember_pending_quiz(coordinator, course, job, info)
        return "pending_confirmation"
    if getattr(coordinator, "auto_quizzes", False) and ledger.data.get(key, {}).get("state") == "rejected":
        raise QuizError("这份测验先前被平台拒绝提交，已保留待处理；后续测验继续自动答题。")
    session = SessionManager.get_session()
    response = session.get("https://mooc1.chaoxing.com/mooc-ans/api/work", params={
        "api": "1", "workId": job["jobid"].replace("work-", ""), "jobid": job["jobid"],
        "originJobId": job["jobid"], "needRedirect": "true", "skipHeader": "true",
        "knowledgeid": str(info["knowledgeid"]), "ktoken": info["ktoken"], "cpi": info["cpi"],
        "ut": "s", "clazzId": course["clazzId"], "type": "", "enc": job["enc"],
        "mooc2": "1", "courseid": course["courseId"]}, timeout=(10, 30))
    if response.status_code != 200:
        raise QuizError("测验读取失败，请稍后重新检查。")
    questions, form = parse_work(response.text)
    drafts = AnswerDrafts(profile, key, questions)
    draft_answers, draft_explanations = drafts.read()

    def generate(question):
        return generate_suggestion(settings, question)

    generate.initial_answers = draft_answers
    generate.initial_explanations = draft_explanations
    generate.save_draft = drafts.save

    def cancellation_requested():
        cancel = getattr(coordinator, "cancelled", None)
        return cancel is not None and cancel.is_set()

    def submit(answers):
        if cancellation_requested():
            raise QuizError("已取消本次答题，没有提交。")
        if ledger.data.get(key, {}).get("state") in ("uncertain", "acknowledged"):
            raise QuizError("本测验已有一次提交记录，请先在网页核对结果，不能重复交卷。")
        payload = submission_form(questions, form, answers)
        drafts.save(answers, draft_explanations)
        # Persist before POST. Unknown results must never trigger an automatic second POST.
        ledger.record(key, "uncertain")
        remember_pending_quiz(coordinator, course, job, info)
        try:
            reply = session.post("https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew",
                data=payload, headers={"Referer": response.url, "X-Requested-With": "XMLHttpRequest"},
                timeout=(10, 30), allow_redirects=False)
            result = reply.json()
        except Exception:
            raise QuizError("提交结果尚未确认，请在网页核对。本测验不会被自动重复提交。") from None
        if reply.status_code == 200 and result.get("status") is True:
            ledger.record(key, "acknowledged")
            try:
                drafts.clear()
            except QuizError:
                # Confirmed success must not be hidden by an optional draft-cleanup failure.
                pass
            report_quiz_submitted(coordinator, course, job, info, len(questions))
            return "submitted"
        if reply.status_code == 200 and result.get("status") is False:
            ledger.record(key, "rejected")
            raise QuizError("平台未接受提交，请检查答案或到网页处理。")
        raise QuizError("平台未确认提交结果，请在网页核对，本测验不会自动重复提交。")

    def automatic(current_answers=None):
        try:
            if any(question.unsupported for question in questions):
                raise QuizError("当前测验提交格式不兼容，已保留待处理；后续测验继续自动答题。")
            coordinator.state("running", "AI 正在补齐当前测验答案（保留已填写内容）")
            seeds = {**draft_answers, **(current_answers if isinstance(current_answers, dict) else {})}
            generated = generate_all(questions, generate,
                progress=lambda current, total: coordinator.state("running", f"AI 正在填写测验（{current}/{total}题）"),
                cancelled=cancellation_requested, initial_answers=seeds,
                initial_explanations=draft_explanations, on_result=drafts.save)
            if generated["cancelled"] or cancellation_requested():
                raise QuizError("已取消本次答题，没有提交。")
            if generated["errors"] or generated["cancelled"]:
                first_error = next(iter(generated["errors"].values()), "")
                raise QuizError(f"本测验自动答题未完成，没有提交。{first_error} 已保留有效答案，后续测验继续自动答题。")
            submit(generated["answers"])
            return "auto_submitted"
        except Exception:
            if coordinator.issue is not None:
                raise coordinator.issue
            raise

    if getattr(coordinator, "auto_quizzes", False):
        return automatic()
    if reviewer is None:
        from quiz_dialog import ask_quiz
        reviewer = ask_quiz
    coordinator.state("needs_attention", "等待你审核章节测验答案")
    result = reviewer(profile.name, course.get("title", ""), questions,
                      generate, submit)
    if coordinator.issue is not None:
        raise coordinator.issue
    action = result.get("action") if isinstance(result, dict) else result
    if action == "auto_all_quizzes":
        coordinator.auto_quizzes = True
        coordinator.skip_all_quizzes = False
        from api.logger import logger
        logger.info("已启用本账户自动答题至窗口关闭：后续测验自动填写并提交，个别失败保留待处理；重新开始任务仍然生效。")
        return automatic(result.get("answers") if isinstance(result, dict) else None)
    return action
