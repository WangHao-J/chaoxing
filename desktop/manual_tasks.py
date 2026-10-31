"""Recognize external/unsupported cards without pretending they were completed."""
import re
from urllib.parse import urlsplit


COURSE_PORTAL = "https://i.chaoxing.com/"


class ManualTasksRequired(Exception):
    def __init__(self, jobs):
        self.jobs = jobs
        super().__init__(f"有 {len(jobs)} 个任务需要在课程章节中手动处理")


def safe_link(value):
    value = str(value or "").strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username \
                and not parsed.password and not any(ord(ch) < 32 for ch in value):
            return value
    except ValueError:
        pass
    return ""


def course_location(course_title, chapter_title):
    return f"课程：{course_title or '请在课程列表选择原课程'}\n章节：{chapter_title or '请查看待处理记录'}"


def install_manual_tasks(main, profile=None, coordinator=None):
    from api import decode, base
    from card_reader import flag_is_true, parse_course_card, has_card_data_assignment
    original_cards = decode._process_attachment_cards
    original_job = main.process_job
    original_chapter = main.process_chapter
    manual = []

    def cards(items):
        result = []
        for card in items:
            if flag_is_true(card.get("isPassed", False)):
                continue
            card = dict(card, isPassed=False)
            kind = str(card.get("type", "")).lower()
            prop = card.get("property", {}) or {}
            if not isinstance(prop, dict):
                prop = {}
            card["property"] = prop
            live = any("live" in str(value).lower() for value in
                       (kind, prop.get("type", ""), prop.get("resourceType", ""))) \
                or any(prop.get(key) is not None for key in ("liveId", "streamName", "vdoid"))
            if kind in ("video", "document", "workid", "read") or live:
                if kind == "workid" and card.get("job") is None and card.get("jobid"):
                    card["otherInfo"] = str(card.get("otherInfo", "")).split("&")[0]
                    parsed = [decode._process_work_task(card)]
                else:
                    parsed = original_cards([card])
                if kind == "workid":
                    for job in parsed:
                        job["name"] = str(prop.get("title") or prop.get("name") or "章节测验")[:200]
                if not parsed and (flag_is_true(card.get("job")) or kind == "workid"):
                    parsed = [{"type": "manual", "kind": kind, "unread": True,
                               "needs_confirmation": True, "href": "",
                               "name": str(prop.get("title") or prop.get("name") or "任务")[:200]
                                       + "（任务信息未读取完整，请重新检查）"}]
                result.extend(parsed)
                continue
            required = flag_is_true(card.get("job"))
            if required:
                result.append({"type": "manual", "kind": kind,
                               "name": str(prop.get("title") or prop.get("textValue") or "未命名任务")[:200],
                               "href": safe_link(prop.get("href"))})
            elif kind == "hyperlink":
                main.logger.info("外部参考链接：{}（未标记为任务点）", str(prop.get("title") or "未命名链接")[:200])
            elif card.get("job") is not None:
                main.logger.info("略过未标记为任务点的内容（类型：{}）", kind[:60])
        return result

    def process_job(cx, course, job, info, speed):
        if job.get("type") == "workid":
            if profile is not None and coordinator is not None:
                from quiz_assistant import reviewed_quiz, QuizError, QuizParametersUnavailable
                skipped = False
                unread = False
                outcome = ""
                try:
                    outcome = "skip_all_quizzes" if getattr(coordinator, "skip_all_quizzes", False) else \
                        reviewed_quiz(cx, course, job, info, profile, coordinator)
                    skipped = outcome in ("skip", "skip_all_quizzes")
                    if outcome == "skip_all_quizzes":
                        coordinator.skip_all_quizzes = True
                        coordinator.auto_quizzes = False
                    reason = {"submitted": "已提交，等待平台任务状态确认",
                              "auto_submitted": "AI已提交，等待平台任务状态确认",
                              "pending_confirmation": "已有提交记录，请在网页核对结果",
                              "skip": "已跳过本次答题，仍未完成",
                              "skip_all_quizzes": "本轮已跳过答题，仍未完成",
                              "defer": "尚未提交，请重新检查或在网页完成"}.get(outcome)
                except QuizError as error:
                    unread = isinstance(error, QuizParametersUnavailable)
                    reason = str(error)
                    if getattr(coordinator, "auto_quizzes", False):
                        coordinator.state("needs_attention", "自动答题未完成，保留待处理并继续：" + reason)
                        main.logger.warning("自动答题未完成，保留待处理并继续：{}", reason)
                if reason:
                    notice = {"type": "manual", "kind": "workid", "href": "",
                              "name": f"{job.get('name', '章节测验')}（{reason}）"}
                    if unread:
                        notice["unread"] = True
                        notice["needs_confirmation"] = True
                    else:
                        notice["quiz_context"] = {"job": dict(job), "info": dict(info)}
                    if outcome in ("submitted", "pending_confirmation"):
                        notice["needs_confirmation"] = True
                    if skipped:
                        notice["skipped_quiz"] = True
                    if outcome == "auto_submitted" or (outcome == "pending_confirmation" and
                                                       getattr(coordinator, "auto_quizzes", False)):
                        notice["awaiting_confirmation"] = True
                    if notice not in manual:
                        manual.append(notice)
                    return main.StudyResult.ERROR
            tiku = getattr(cx, "tiku", None)
            disabled = tiku is None or getattr(tiku, "DISABLE", False)
            no_submit = not getattr(tiku, "SUBMIT", False)
            if disabled or no_submit:
                reason = "答题服务未启用" if disabled else "自动提交未启用"
                notice = {"type": "manual", "kind": "workid", "href": "",
                          "name": f"{job.get('name', '章节测验')}（{reason}，请在网页完成并提交）"}
                if profile is not None and coordinator is not None:
                    notice["quiz_context"] = {"job": dict(job), "info": dict(info)}
                if notice not in manual:
                    manual.append(notice)
                return main.StudyResult.ERROR
        if job.get("type") == "manual":
            if job not in manual:
                manual.append(job)
            return main.StudyResult.ERROR
        return original_job(cx, course, job, info, speed)

    def finish_chapter(cx, course, point, result):
        if manual and all(job.get("awaiting_confirmation", False) for job in manual) and callable(
                getattr(cx, "get_course_point", None)):
            points = cx.get_course_point(course["courseId"], course["clazzId"], course["cpi"])["points"]
            current = next((item for item in points if str(item.get("id")) == str(point.get("id"))), None)
            if current and flag_is_true(current.get("has_finished", False)):
                point.update(current)
                from quiz_assistant import report_quiz_completed
                if coordinator is not None:
                    report_quiz_completed(coordinator, course, point)
                manual.clear()
                return main.ChapterResult.SUCCESS
        if manual:
            raise ManualTasksRequired(list(manual))
        return result

    def process_chapter(cx, course, point, speed):
        manual.clear()
        if coordinator is not None:
            coordinator.quiz_chapter_title = point.get("title", "章节测验")
        # Card reads may legitimately be empty, but a failed decoder must not mark
        # a chapter with outstanding tasks complete through the empty-page API.
        readings = []
        original_decode = decode.decode_course_card
        original_base_decode = base.decode_course_card
        missing = object()
        previous_empty = vars(cx).get("study_emptypage", missing)
        original_empty = getattr(cx, "study_emptypage", None)
        previous_jobs = vars(cx).get("get_job_list", missing)
        original_jobs = cx.get_job_list
        empty_result = False

        def read_card(html):
            jobs, info, recognized = parse_course_card(html, decode._extract_job_info, cards)
            readings.append(recognized)
            if not recognized and has_card_data_assignment(html):
                retain_unread(point)
            return jobs, info

        def empty_is_unconfirmed(empty_point):
            try:
                zero_tasks = int(empty_point.get("jobCount", -1)) == 0
            except (TypeError, ValueError):
                zero_tasks = False
            return not zero_tasks or not any(readings)

        def retain_unread(empty_point):
            title = str(empty_point.get("title") or "当前章节")
            quiz = any(word in title for word in ("测试", "测验", "练习", "答题", "考试"))
            notice = {"type": "manual", "kind": "workid" if quiz else "unread_tasks",
                      "unread": True, "needs_confirmation": True, "href": "",
                      "name": title[:200] + "（未读取到任务信息，请重新检查；仍未完成）"}
            if notice not in manual:
                manual.append(notice)
                main.logger.warning("章节任务未读取完整，已保留待处理：{}", title[:200])

        def guarded_empty(empty_course, empty_point):
            if empty_is_unconfirmed(empty_point):
                retain_unread(empty_point)
                return main.StudyResult.ERROR
            return original_empty(empty_course, empty_point)

        def read_jobs(job_course, job_point):
            nonlocal empty_result
            jobs, info = original_jobs(job_course, job_point)
            empty_result = not jobs and not info.get("notOpen", False)
            return jobs, info

        decode.decode_course_card = base.decode_course_card = read_card
        if callable(original_empty):
            cx.study_emptypage = guarded_empty
        cx.get_job_list = read_jobs
        try:
            result = original_chapter(cx, course, point, speed)
            # The upstream reader can return early without reaching study_emptypage.
            if empty_result and result == main.ChapterResult.SUCCESS and empty_is_unconfirmed(point) and not manual:
                retain_unread(point)
        finally:
            decode.decode_course_card = original_decode
            base.decode_course_card = original_base_decode
            if callable(original_empty):
                if previous_empty is missing:
                    del cx.study_emptypage
                else:
                    cx.study_emptypage = previous_empty
            if previous_jobs is missing:
                del cx.get_job_list
            else:
                cx.get_job_list = previous_jobs
        return finish_chapter(cx, course, point, result)

    def process_pending_quizzes(cx, course, point, notices, speed):
        # Reuse live task parameters only; never serialize authentication tokens into checkpoints.
        manual.clear()
        if coordinator is not None:
            coordinator.quiz_chapter_title = point.get("title", "章节测验")
        for notice in notices:
            context = notice.get("quiz_context")
            if notice.get("kind") == "workid" and context and not notice.get("skipped_quiz") \
                    and not notice.get("awaiting_confirmation"):
                process_job(cx, course, context["job"], context["info"], speed)
            else:
                manual.append(notice)
        return finish_chapter(cx, course, point, main.ChapterResult.ERROR)

    decode._process_attachment_cards = cards
    main.process_job = process_job
    main.process_chapter = process_chapter
    main.process_pending_quizzes = process_pending_quizzes
