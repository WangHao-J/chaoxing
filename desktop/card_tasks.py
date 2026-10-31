"""Read-only card discovery shared by all quiz and media entry points."""
import re

from card_reader import flag_is_true, parse_course_card, has_card_data_assignment


class StudyError(ValueError):
    pass


class StudyCancelled(StudyError):
    pass


class StudyPaused(StudyError):
    pass


def check_cancelled(cancelled):
    if cancelled.is_set():
        raise StudyCancelled("已停止，任务记录已保留。")


def empty_template(html):
    if not isinstance(html, str):
        return False
    if re.search(r"\bmArg\s*=\s*(?:[\"'][\"']|null)", html) and not has_card_data_assignment(html):
        return not any(value in html for value in ("enc校验失败", "请登录", "验证码"))
    return not re.search(r"<(?:script|iframe|input|form|a)\b", html, re.I) and not re.sub(
        r"<!--.*?-->|<[^>]*>", "", html, flags=re.S).strip()


def read_chapter_jobs(cx, course, point, cancelled, session=None):
    """Read cards only; never use the legacy reader's empty-page completion side effect."""
    from api import decode
    from api.base import SessionManager
    if flag_is_true(point.get("need_unlock")):
        raise StudyError("章节尚未开放，已保留待处理。")
    if flag_is_true(point.get("has_finished")):
        return []
    session = session if session is not None else SessionManager.get_session()
    params = {"clazzid": course["clazzId"], "courseid": course["courseId"], "knowledgeid": point["id"],
              "cpi": course["cpi"], "ut": "s", "v": "2025-0424-1038-3", "mooc2": 1}
    result, seen, recognized_any = [], {}, False
    for page in range(7):
        check_cancelled(cancelled)
        cx.rate_limiter.limit_rate()
        check_cancelled(cancelled)
        response = session.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards",
                               params={**params, "num": str(page)}, timeout=(5, 15))
        check_cancelled(cancelled)
        response.raise_for_status()
        jobs, info, recognized = parse_course_card(response.text, decode._extract_job_info, decode._process_attachment_cards)
        if info.get("notOpen"):
            raise StudyError("章节尚未开放，已保留待处理。")
        if not recognized:
            if has_card_data_assignment(response.text) or not empty_template(response.text):
                raise StudyError("章节任务信息未读取完整，已保留待处理。")
            continue
        recognized_any = True
        if jobs and any(not str(info.get(name, "")) or str(info[name]) != str(expected) for name, expected in
                        (("knowledgeid", point["id"]), ("cpi", course["cpi"]))):
            raise StudyError("返回的任务与所选章节不一致，没有执行任务。")
        for index, job in enumerate(jobs):
            identifier = str(job.get("jobid") or job.get("id") or job.get("mid") or "")
            key = (job.get("type"), identifier) if identifier else ("unread", page, index)
            if key in seen:
                previous = seen[key]
                if job.get("objectid") and previous.get("objectid") != job["objectid"]:
                    raise StudyError("平台返回了不一致的视频对象，没有执行任务。")
                if job.get("type") == "workid" and (job.get("enc") != previous.get("enc") or
                        info.get("ktoken") != previous.get("_card_ktoken")):
                    raise StudyError("平台返回了不一致的测验参数，没有执行答题。")
                continue
            seen[key] = {**job, "_card_ktoken": info.get("ktoken")}
            result.append((dict(job), dict(info)))
    try:
        outstanding = int(point.get("jobCount", -1))
    except (ValueError, TypeError):
        outstanding = -1
    if not recognized_any or not result and outstanding != 0:
        raise StudyError("未读取到可处理的任务信息，仍保留为待处理。")
    return result


def read_chapter_job_states(cx, course, point, cancelled):
    """Require explicit card completion flags; missing tasks never prove success."""
    from api import decode
    from api.base import SessionManager
    session = SessionManager.get_session()
    states = {}
    for page in range(7):
        check_cancelled(cancelled)
        cx.rate_limiter.limit_rate()
        response = session.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params={
            "clazzid": course["clazzId"], "courseid": course["courseId"], "knowledgeid": point["id"],
            "cpi": course["cpi"], "ut": "s", "v": "2025-0424-1038-3", "mooc2": 1, "num": str(page)}, timeout=(5, 15))
        check_cancelled(cancelled)
        response.raise_for_status()
        cards, info, recognized = parse_course_card(response.text, decode._extract_job_info, lambda cards: cards)
        if not recognized:
            if not empty_template(response.text):
                raise StudyError("复查时未完整读取任务状态，不能确认完成。")
            continue
        if info.get("notOpen"):
            raise StudyError("章节尚未开放，不能确认完成。")
        if cards and any(str(info.get(name, "")) != str(value) for name, value in
                         (("knowledgeid", point["id"]), ("cpi", course["cpi"]))):
            raise StudyError("复查返回的章节或班级不匹配。")
        for card in cards:
            key = (str(card.get("type", "")).lower(), str(card.get("jobid") or card.get("id") or card.get("mid") or ""))
            if key[1]:
                passed = flag_is_true(card.get("isPassed"))
                if key in states and states[key] != passed:
                    raise StudyError("平台返回了冲突的任务完成状态。")
                states[key] = passed
    return states


