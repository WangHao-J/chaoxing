"""Offline regressions for unread cards: retain tasks instead of false completion."""
import json
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_runtime_safety as fixture
from api import base, decode
from manual_tasks import ManualTasksRequired, install_manual_tasks
from quiz_assistant import QuizError, reviewed_quiz
from recovery_dialog import RecoveryDialog


main = fixture.main
Chaoxing = fixture.Chaoxing


def card_html(cards=None, *, multiline=False):
    data = {"attachments": cards or [], "defaults": {
        "knowledgeid": "synthetic-chapter", "ktoken": "synthetic-ktoken", "cpi": "synthetic-cpi"}}
    if multiline:
        return "<script>\nvar mArg =\n" + json.dumps(data, ensure_ascii=False, indent=2) + ";\n</script>"
    return "mArg=" + json.dumps(data, ensure_ascii=False) + ";"


class UnreadTaskTests(unittest.TestCase):
    setUp = fixture.RuntimeSafetyTests.setUp
    tearDown = fixture.RuntimeSafetyTests.tearDown
    task = fixture.RuntimeSafetyTests.task
    processor = fixture.RuntimeSafetyTests.processor

    def install(self):
        previous = decode._process_attachment_cards, main.process_job, main.process_chapter
        missing = object()
        previous_pending = getattr(main, "process_pending_quizzes", missing)

        def restore():
            decode._process_attachment_cards, main.process_job, main.process_chapter = previous
            if previous_pending is missing:
                del main.process_pending_quizzes
            else:
                main.process_pending_quizzes = previous_pending

        self.addCleanup(restore)
        install_manual_tasks(main)

    def client(self, html=None):
        cx = object.__new__(Chaoxing)
        cx.rate_limiter = Mock()
        cx.tiku = SimpleNamespace(DISABLE=True, SUBMIT=False)
        cx.study_emptypage = Mock(return_value=main.StudyResult.SUCCESS)
        cx.study_work = Mock(return_value=main.StudyResult.SUCCESS)
        session = Mock()
        if isinstance(html, list):
            session.get.side_effect = [fixture.response(200, page) for page in html]
        else:
            session.get.return_value = fixture.response(200, html if html is not None else card_html())
        return cx, session

    def point(self, count, title="5.4 章节测试"):
        task = self.task()
        task.point.update(jobCount=count, title=title)
        return task

    def process(self, cx, session, task):
        with patch("api.base.SessionManager.get_session", return_value=session):
            return main.process_chapter(cx, task.course, task.point, 1)

    def test_unread_quiz_with_remaining_task_cannot_enter_empty_page_completion(self):
        self.install()
        cx, session = self.client()
        task = self.point(1)
        with self.assertRaises(ManualTasksRequired) as caught:
            self.process(cx, session, task)
        notice = caught.exception.jobs[0]
        self.assertEqual(notice["kind"], "workid")
        self.assertTrue(notice["unread"])
        self.assertTrue(notice["needs_confirmation"])
        self.assertNotIn("quiz_context", notice)
        self.assertIn("仍未完成", notice["name"])
        cx.study_emptypage.assert_not_called()
        cx.study_work.assert_not_called()
        self.assertFalse(task.point["has_finished"])
        self.assertEqual(session.get.call_count, 7)

    def test_positive_count_ordinary_chapter_is_retained_without_guessing_quiz(self):
        self.install()
        cx, session = self.client()
        task = self.point(1, "设计发展历程")
        with self.assertRaises(ManualTasksRequired) as caught:
            self.process(cx, session, task)
        self.assertEqual(caught.exception.jobs[0]["kind"], "unread_tasks")
        self.assertTrue(caught.exception.jobs[0]["unread"])
        cx.study_emptypage.assert_not_called()

    def test_missing_invalid_or_positive_string_counts_never_complete_an_empty_read(self):
        self.install()
        for count in (None, "bad", "1", -1):
            with self.subTest(count=count):
                cx, session = self.client()
                task = self.point(count)
                with self.assertRaises(ManualTasksRequired):
                    self.process(cx, session, task)
                cx.study_emptypage.assert_not_called()

    def test_zero_tasks_and_valid_card_json_preserve_original_empty_page_handler(self):
        self.install()
        for count in (0, "0"):
            with self.subTest(count=count):
                cx, session = self.client()
                task = self.point(count, "课程介绍")
                original_empty = cx.study_emptypage
                self.assertEqual(self.process(cx, session, task), main.ChapterResult.SUCCESS)
                original_empty.assert_called_once_with(task.course, task.point)
                self.assertIs(cx.study_emptypage, original_empty)
                cx.study_work.assert_not_called()

    def test_login_html_cannot_be_counted_as_a_completed_empty_chapter_even_with_zero_count(self):
        self.install()
        cx, session = self.client("<html><form action='/login'>请登录后查看课程</form></html>")
        with self.assertRaises(ManualTasksRequired) as caught:
            self.process(cx, session, self.point(0))
        self.assertTrue(caught.exception.jobs[0]["unread"])
        cx.study_emptypage.assert_not_called()

    def test_malformed_json_cannot_complete_an_empty_chapter_even_with_zero_count(self):
        self.install()
        for html in ("mArg={broken};", "mArg=[];", "mArg={\"attachments\":{}};",
                     "mArg={\"attachments\":[null]};", "mArg={\"attachments\":[],\"defaults\":[]};"):
            with self.subTest(html=html):
                cx, session = self.client(html)
                with self.assertRaises(ManualTasksRequired):
                    self.process(cx, session, self.point(0))
                cx.study_emptypage.assert_not_called()

    def test_non_200_early_return_without_empty_handler_remains_unfinished(self):
        self.install()
        cx, session = self.client()
        session.get.return_value = fixture.response(302, "<html>请重新登录</html>")
        task = self.point(1)
        with self.assertRaises(ManualTasksRequired) as caught:
            self.process(cx, session, task)
        self.assertTrue(caught.exception.jobs[0]["unread"])
        self.assertIn("仍未完成", caught.exception.jobs[0]["name"])
        self.assertFalse(task.point["has_finished"])
        session.get.assert_called_once()
        cx.study_emptypage.assert_not_called()
        cx.study_work.assert_not_called()

    def test_real_reader_in_processor_opens_recovery_once_and_preserves_unread_task(self):
        self.install()
        cx, session = self.client()
        task = self.point(1)
        processor = self.processor([task])
        processor.chaoxing = cx
        self.co.dialog.return_value = "stop"
        with patch("api.base.SessionManager.get_session", return_value=session), \
             patch("runtime_safety.time.sleep") as sleep, \
             self.assertRaises(fixture.RunNeedsAttention):
            processor.run()
        self.co.dialog.assert_called_once()
        notice = self.co.dialog.call_args.kwargs["manual_jobs"][0]
        self.assertTrue(notice["unread"])
        self.assertNotIn("quiz_context", notice)
        self.assertEqual(self.co.dialog.call_args.args[3], 0)
        self.assertFalse(self.co.completed)
        self.assertFalse(task.point["has_finished"])
        self.assertEqual(processor.completed_tasks, [])
        self.assertEqual(len(self.co.pending), 1)
        self.assertEqual(self.co.pending[0]["chapter_id"], task.point["id"])
        self.assertIn("仍未完成", self.co.pending[0]["manual_tasks"][0])
        stored = json.loads((self.profile.directory / "pending_tasks.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["tasks"], self.co.pending)
        self.assertEqual(self.co.current_status, "needs_attention")
        sleep.assert_not_called()
        cx.study_emptypage.assert_not_called()

    def test_recheck_refreshes_server_completed_status_without_reopening_unread_quiz(self):
        self.install()
        cx, session = self.client()
        task = self.point(1)
        cx.get_course_point = Mock(return_value={"points": [{**task.point, "has_finished": True}]})
        processor = self.processor([task])
        processor.chaoxing = cx
        self.co.dialog.return_value = "retry"
        with patch("api.base.SessionManager.get_session", return_value=session), \
             patch("runtime_safety.time.sleep") as sleep:
            processor.run()
        self.co.dialog.assert_called_once()
        cx.get_course_point.assert_called_once_with("course", "class", "cpi")
        self.assertTrue(task.point["has_finished"])
        self.assertTrue(self.co.completed)
        self.assertEqual(processor.completed_tasks, [task])
        self.assertEqual(self.co.pending, [])
        stored = json.loads((self.profile.directory / "pending_tasks.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["tasks"], [])
        self.assertEqual(session.get.call_count, 7)
        cx.study_emptypage.assert_not_called()
        cx.study_work.assert_not_called()
        sleep.assert_not_called()

    def test_not_open_chapter_keeps_original_status_and_never_completes_empty_page(self):
        self.install()
        cx, session = self.client("<html>章节未开放</html>")
        self.assertEqual(self.process(cx, session, self.point(1)), main.ChapterResult.NOT_OPEN)
        cx.study_emptypage.assert_not_called()
        cx.study_work.assert_not_called()

    def test_multiline_json_and_missing_job_marker_preserve_real_unfinished_quiz(self):
        self.install()
        card = {"type": "workid", "jobid": "work-synthetic", "isPassed": "false",
                "enc": "synthetic-enc", "otherInfo": "known&ignored=secret",
                "property": {"title": "Words about college"}}
        cx, session = self.client([card_html([card], multiline=True), *[card_html()] * 6])
        with self.assertRaises(ManualTasksRequired) as caught:
            self.process(cx, session, self.point(1))
        self.assertEqual(caught.exception.jobs[0]["kind"], "workid")
        self.assertIn("Words about college", caught.exception.jobs[0]["name"])
        self.assertIn("答题服务未启用", caught.exception.jobs[0]["name"])
        self.assertFalse(caught.exception.jobs[0].get("unread", False))
        cx.study_emptypage.assert_not_called()
        cx.study_work.assert_not_called()

    def test_false_pass_flags_are_not_truthy_completed_task_markers(self):
        self.install()
        for passed in (False, "false", "0", 0):
            with self.subTest(passed=passed):
                jobs = decode._process_attachment_cards([{"type": "workid", "jobid": "work-synthetic",
                    "isPassed": passed, "property": {"title": "测验"}}])
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]["type"], "workid")

    def test_true_pass_flags_ignore_completed_quizzes(self):
        self.install()
        for passed in (True, "true", "1", 1):
            with self.subTest(passed=passed):
                self.assertEqual(decode._process_attachment_cards([{"type": "workid",
                    "jobid": "work-synthetic", "isPassed": passed, "job": True}]), [])

    def test_completed_chapter_does_not_fetch_or_reopen_any_quiz(self):
        self.install()
        cx, session = self.client()
        task = self.point(1)
        task.point["has_finished"] = True
        self.assertEqual(self.process(cx, session, task), main.ChapterResult.SUCCESS)
        session.get.assert_not_called()
        cx.study_emptypage.assert_not_called()

    def test_required_video_missing_mid_becomes_unread_manual_task(self):
        self.install()
        card = {"type": "video", "job": True, "jobid": "video-synthetic",
                "property": {"name": "未读完整的视频"}}
        cx, session = self.client([card_html([card]), *[card_html()] * 6])
        with self.assertRaises(ManualTasksRequired) as caught:
            self.process(cx, session, self.point(1, "视频章节"))
        notice = caught.exception.jobs[0]
        self.assertEqual(notice["kind"], "video")
        self.assertTrue(notice["unread"])
        cx.study_emptypage.assert_not_called()

    def test_missing_quiz_identifier_becomes_unread_manual_task_without_guessing(self):
        self.install()
        jobs = decode._process_attachment_cards([{"type": "workid", "property": {"title": "无参数测验"}}])
        self.assertEqual(jobs[0]["type"], "manual")
        self.assertTrue(jobs[0]["unread"])
        self.assertTrue(jobs[0]["needs_confirmation"])
        self.assertNotIn("jobid", jobs[0])

    def test_valid_video_is_processed_but_malformed_additional_card_keeps_chapter_pending(self):
        self.install()
        card = {"type": "video", "job": True, "jobid": "video-synthetic", "mid": "synthetic-mid",
                "property": {"name": "正常视频"}}
        cx, session = self.client([card_html([card]), "<script>mArg = {broken;</script>",
                                   *[card_html()] * 5])
        cx.study_video = Mock(return_value=main.StudyResult.SUCCESS)
        task = self.point(2, "视频及章节测验")
        with self.assertRaises(ManualTasksRequired) as caught:
            self.process(cx, session, task)
        cx.study_video.assert_called_once()
        self.assertEqual(cx.study_video.call_args.args[1]["jobid"], "video-synthetic")
        self.assertTrue(caught.exception.jobs[0]["unread"])
        self.assertIn("仍未完成", caught.exception.jobs[0]["name"])
        self.assertFalse(task.point["has_finished"])
        cx.study_emptypage.assert_not_called()
        self.assertEqual(session.get.call_count, 7)

    def test_valid_video_with_unused_blank_card_pages_does_not_create_phantom_pending_task(self):
        self.install()
        card = {"type": "video", "job": True, "jobid": "video-synthetic", "mid": "synthetic-mid",
                "property": {"name": "正常视频"}}
        cx, session = self.client([card_html([card]), *["<html></html>"] * 6])
        cx.study_video = Mock(return_value=main.StudyResult.SUCCESS)
        task = self.point(1, "视频章节")
        self.assertEqual(self.process(cx, session, task), main.ChapterResult.SUCCESS)
        cx.study_video.assert_called_once()
        cx.study_emptypage.assert_not_called()
        cx.study_work.assert_not_called()
        self.assertEqual(session.get.call_count, 7)

    def test_official_template_initializer_and_unused_card_slots_do_not_pause_video(self):
        self.install()
        card = {"type": "video", "job": True, "jobid": "video-synthetic", "mid": "synthetic-mid",
                "property": {"name": "正常视频"}}
        unused = '<script>var mArg=""; mArg = $.parseJSON(mArg);</script>'
        html = '<script>var mArg="";</script>' + card_html([card]) + '<script>mArg = $.parseJSON(mArg);</script>'
        cx, session = self.client([html, *[unused] * 6])
        cx.study_video = Mock(return_value=main.StudyResult.SUCCESS)
        task = self.point(1, "视频章节")
        self.assertEqual(self.process(cx, session, task), main.ChapterResult.SUCCESS)
        cx.study_video.assert_called_once()
        cx.study_emptypage.assert_not_called()
        self.assertFalse(task.point["has_finished"])

    def test_all_empty_template_slots_keep_real_outstanding_task_pending(self):
        self.install()
        html = '<script>var mArg=""; mArg = $.parseJSON(mArg);</script>'
        cx, session = self.client(html)
        task = self.point(1, "未读取到的视频")
        with self.assertRaises(ManualTasksRequired) as caught:
            self.process(cx, session, task)
        self.assertTrue(caught.exception.jobs[0]["unread"])
        cx.study_emptypage.assert_not_called()

    def test_initializer_and_truly_malformed_additional_card_still_remain_pending(self):
        self.install()
        card = {"type": "video", "job": True, "jobid": "video-synthetic", "mid": "synthetic-mid",
                "property": {"name": "正常视频"}}
        cx, session = self.client(['var mArg="";' + card_html([card]), 'var mArg="";mArg={broken};',
                                   *['var mArg="";mArg = $.parseJSON(mArg);'] * 5])
        cx.study_video = Mock(return_value=main.StudyResult.SUCCESS)
        with self.assertRaises(ManualTasksRequired):
            self.process(cx, session, self.point(2, "视频和另一个任务"))
        cx.study_video.assert_called_once()
        cx.study_emptypage.assert_not_called()

    def test_decoder_aliases_and_instance_empty_handler_are_restored_after_unread_chapter(self):
        self.install()
        original_decode, original_base_decode = decode.decode_course_card, base.decode_course_card
        cx, session = self.client()
        original_empty = cx.study_emptypage
        with self.assertRaises(ManualTasksRequired):
            self.process(cx, session, self.point(1))
        self.assertIs(decode.decode_course_card, original_decode)
        self.assertIs(base.decode_course_card, original_base_decode)
        self.assertIs(cx.study_emptypage, original_empty)

    def test_decoder_aliases_and_instance_handler_are_restored_on_network_exception(self):
        self.install()
        original_decode, original_base_decode = decode.decode_course_card, base.decode_course_card
        cx, session = self.client()
        session.get.side_effect = RuntimeError("synthetic request failure")
        original_empty = cx.study_emptypage
        with self.assertRaisesRegex(RuntimeError, "synthetic request failure"):
            self.process(cx, session, self.point(1))
        self.assertIs(decode.decode_course_card, original_decode)
        self.assertIs(base.decode_course_card, original_base_decode)
        self.assertIs(cx.study_emptypage, original_empty)

    def test_temporary_empty_handler_does_not_shadow_the_class_method_after_exception(self):
        self.install()
        cx, session = self.client()
        del cx.study_emptypage
        session.get.side_effect = RuntimeError("synthetic request failure")
        original_decode, original_base_decode = decode.decode_course_card, base.decode_course_card
        with self.assertRaises(RuntimeError):
            self.process(cx, session, self.point(1))
        self.assertNotIn("study_emptypage", vars(cx))
        self.assertIs(decode.decode_course_card, original_decode)
        self.assertIs(base.decode_course_card, original_base_decode)


class UnreadQuizDialogTests(unittest.TestCase):
    def setUp(self):
        self.window = tk.Tk()
        self.window.withdraw()
        self.dialog = None

    def tearDown(self):
        if self.dialog and self.dialog.timer is not None:
            self.window.after_cancel(self.dialog.timer)
        self.window.destroy()

    def recovery(self, allow_retry=True):
        on_quiz = Mock(return_value=True)
        self.dialog = RecoveryDialog(self.window, "测试账户", "未读到测验", "5.4 章节测试", 0,
            allow_retry, course_title="测试课程", on_quiz=on_quiz,
            manual_jobs=[{"kind": "workid", "unread": True, "needs_confirmation": True,
                          "name": "章节测试（尚未读取）"}])
        return self.dialog, on_quiz

    def test_unread_quiz_offers_reread_instead_of_a_nonfunctional_quiz_button(self):
        dialog, on_quiz = self.recovery()
        self.assertEqual(dialog.retry_button.cget("text"), "重新读取题目")
        self.assertFalse(hasattr(dialog, "quiz_button"))
        self.assertIn("仍未完成", dialog.feedback.get())
        dialog.retry_button.invoke()
        self.assertEqual(dialog.result, "retry")
        on_quiz.assert_not_called()

    def test_exhausted_reread_button_is_disabled_and_does_not_open_quiz(self):
        dialog, on_quiz = self.recovery(allow_retry=False)
        self.assertEqual(str(dialog.retry_button.cget("state")), "disabled")
        dialog.retry_button.invoke()
        self.assertEqual(dialog.result, "stop")
        on_quiz.assert_not_called()

    def test_mixed_unread_and_readable_quizzes_offer_recheck_for_missing_information(self):
        on_quiz = Mock(return_value=True)
        self.dialog = RecoveryDialog(self.window, "测试账户", "部分测验未读取完整", "章节测验", 0,
            True, course_title="测试课程", on_quiz=on_quiz, manual_jobs=[
                {"kind": "workid", "unread": True, "name": "未读到参数的测验"},
                {"kind": "workid", "name": "已读取的测验"}])
        self.assertTrue(self.dialog.only_quizzes)
        self.assertTrue(hasattr(self.dialog, "retry_button"))
        self.assertEqual(str(self.dialog.retry_button.cget("state")), "normal")
        self.dialog.retry_button.invoke()
        self.assertEqual(self.dialog.result, "retry")
        on_quiz.assert_not_called()


class IncompleteQuizParametersTests(unittest.TestCase):
    setUp = fixture.RuntimeSafetyTests.setUp
    tearDown = fixture.RuntimeSafetyTests.tearDown

    def test_missing_critical_fields_raise_before_any_session_request_or_submission(self):
        course = {"courseId": "synthetic-course", "clazzId": "synthetic-class"}
        job = {"jobid": "work-synthetic", "enc": "synthetic-enc"}
        info = {"knowledgeid": "synthetic-chapter", "ktoken": "synthetic-ktoken", "cpi": "synthetic-cpi"}
        for group, key in (("course", "courseId"), ("course", "clazzId"), ("job", "jobid"),
                           ("job", "enc"), ("info", "knowledgeid"), ("info", "ktoken"), ("info", "cpi")):
            for invalid in (None, "", "   "):
                with self.subTest(group=group, key=key, invalid=invalid):
                    data = {"course": dict(course), "job": dict(job), "info": dict(info)}
                    data[group][key] = invalid
                    self.co.auto_quizzes = True
                    session, reviewer = Mock(), Mock()
                    with patch("quiz_assistant.read_settings", return_value={"enabled": True}), \
                         patch("api.base.SessionManager.get_session", return_value=session) as get_session, \
                         self.assertRaisesRegex(QuizError, "参数未读取完整"):
                        reviewed_quiz(Mock(), data["course"], data["job"], data["info"],
                                      self.profile, self.co, reviewer=reviewer)
                    get_session.assert_not_called()
                    session.get.assert_not_called()
                    session.post.assert_not_called()
                    reviewer.assert_not_called()
                    self.assertTrue(self.co.auto_quizzes)
                    self.assertFalse((self.profile.directory / "quiz_submissions.json").exists())


if __name__ == "__main__":
    unittest.main()

