"""Offline quiz notices: accepted submission and fresh platform confirmation differ."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_quiz_assistant as base
import test_runtime_safety as fixture
from api import decode
from manual_tasks import install_manual_tasks
from quiz_assistant import (QuizError, SubmissionLedger, remember_pending_quiz,
                            report_quiz_submitted, report_quiz_completed)
from runtime_safety import SafetyCoordinator, make_processor


COURSE = {**base.COURSE, "cpi": "cpi"}
POINT = {"id": "chapter", "title": "当前章节", "has_finished": False}


def rendered(calls):
    """Capture the final log text rather than judging only format strings."""
    result = []
    for call in calls:
        text, *args = call.args
        result.append(str(text).format(*args, **call.kwargs))
    return result


class SubmissionNotificationTests(unittest.TestCase):
    setUp = base.QuizTests.setUp
    run_quiz = base.QuizTests.run_quiz

    def prepare_logging(self):
        self.co.profile = self.profile
        self.co.quiz_chapter_title = POINT["title"]
        info = patch("api.logger.logger.info").start()
        success = patch("api.logger.logger.success").start()
        self.addCleanup(patch.stopall)
        return info, success

    @staticmethod
    def reviewer(account, course, questions, generate, submit):
        return submit({"1": ["B"]})

    def test_explicit_accepted_submission_reports_waiting_and_not_completion(self):
        info, success = self.prepare_logging()
        self.assertEqual(self.run_quiz(self.reviewer), "submitted")
        text = "\n".join(rendered(info.call_args_list))
        self.assertIn("答题已提交", text)
        self.assertIn("等待平台确认", text)
        self.assertIn(POINT["title"], text)
        self.assertIn(base.JOB["name"], text)
        self.assertIn("1", text)
        success.assert_not_called()
        self.session.post.assert_called_once()
        self.assertTrue(report_quiz_completed(self.co, COURSE, {**POINT, "has_finished": True}))
        self.assertEqual(success.call_count, 1)

    def test_automatic_accepted_submission_reports_the_same_status(self):
        info, success = self.prepare_logging()
        self.co.auto_quizzes = True
        with patch("quiz_assistant.generate_suggestion", return_value=(["B"], "解释")):
            self.assertEqual(self.run_quiz(Mock()), "auto_submitted")
        self.assertEqual(sum("答题已提交" in text for text in rendered(info.call_args_list)), 1)
        success.assert_not_called()
        self.session.post.assert_called_once()

    def test_rejected_submission_has_no_success_or_submitted_notice(self):
        info, success = self.prepare_logging()
        self.session.post.return_value = fixture.response(200, '{"status":false}', "application/json")
        with self.assertRaises(QuizError):
            self.run_quiz(self.reviewer)
        self.assertFalse(any("答题已提交" in text for text in rendered(info.call_args_list)))
        success.assert_not_called()

    def test_timeout_has_no_submitted_notice_and_does_not_submit_twice(self):
        info, success = self.prepare_logging()
        self.session.post.side_effect = TimeoutError("synthetic-timeout")
        with self.assertRaises(QuizError):
            self.run_quiz(self.reviewer)
        self.assertEqual(self.run_quiz(Mock()), "pending_confirmation")
        self.session.post.assert_called_once()
        self.assertFalse(any("答题已提交" in text for text in rendered(info.call_args_list)))
        success.assert_not_called()

    def test_invalid_answers_do_not_post_or_announce_a_submission(self):
        info, success = self.prepare_logging()

        def invalid(account, course, questions, generate, submit):
            return submit({"1": ["Z"]})

        with self.assertRaises(QuizError):
            self.run_quiz(invalid)
        self.session.post.assert_not_called()
        self.assertFalse(any("答题已提交" in text for text in rendered(info.call_args_list)))
        success.assert_not_called()

    def test_timeout_followed_by_platform_confirmation_can_report_completion_without_resubmitting(self):
        info, success = self.prepare_logging()
        self.session.post.side_effect = TimeoutError("synthetic-timeout")
        with self.assertRaises(QuizError):
            self.run_quiz(self.reviewer)
        self.assertFalse(any("答题已提交" in text for text in rendered(info.call_args_list)))
        success.assert_not_called()
        self.assertTrue(report_quiz_completed(self.co, COURSE, {**POINT, "has_finished": True}))
        self.assertEqual(success.call_count, 1)
        self.session.post.assert_called_once()

    def test_skipping_or_deferring_never_announces_a_submission(self):
        info, success = self.prepare_logging()
        for outcome in ("skip", "skip_all_quizzes", "defer"):
            with self.subTest(outcome=outcome):
                self.assertEqual(self.run_quiz(Mock(return_value=outcome)), outcome)
        self.session.post.assert_not_called()
        self.assertFalse(any("答题已提交" in text for text in rendered(info.call_args_list)))
        success.assert_not_called()

    def test_existing_acknowledgement_is_not_announced_as_a_new_submission(self):
        info, success = self.prepare_logging()
        SubmissionLedger(self.profile).record("course:class:work-123", "acknowledged")
        self.assertEqual(self.run_quiz(Mock()), "pending_confirmation")
        self.session.post.assert_not_called()
        self.assertFalse(any("答题已提交" in text for text in rendered(info.call_args_list)))
        success.assert_not_called()

    def test_notifications_never_log_answers_tokens_form_or_ai_key(self):
        info, success = self.prepare_logging()
        answer = "synthetic-private-answer-429017"
        self.session.get.return_value = fixture.response(200, base.page(base.card(
            "1", "4", '<textarea name="answer1"></textarea>', options=False)))

        def answer_review(account, course, questions, generate, submit):
            return submit({"1": [answer]})

        self.assertEqual(self.run_quiz(answer_review), "submitted")
        self.assertTrue(report_quiz_completed(self.co, COURSE, {**POINT, "has_finished": True}))
        text = "\n".join(rendered(info.call_args_list) + rendered(success.call_args_list))
        for private in (answer, base.JOB["enc"], base.INFO["ktoken"], base.SETTINGS["key"],
                        "synthetic-csrf", "answer1", "pyFlag"):
            with self.subTest(private=private):
                self.assertNotIn(private, text)


class CompletionNotificationTests(unittest.TestCase):
    def setUp(self):
        self.co = SimpleNamespace(profile=SimpleNamespace(name="测试账户"), quiz_chapter_title="当前章节")
        self.info = patch("api.logger.logger.info").start()
        self.success = patch("api.logger.logger.success").start()
        self.addCleanup(patch.stopall)

    def remember(self, course=None, chapter="chapter", co=None):
        remember_pending_quiz(co or self.co, course or COURSE, base.JOB,
                              {**base.INFO, "knowledgeid": chapter})

    def test_unfinished_flags_do_not_consume_the_pending_confirmation(self):
        self.remember()
        for flag in (False, "false", 0, "0", None, "", "no"):
            with self.subTest(flag=flag):
                self.assertFalse(report_quiz_completed(self.co, COURSE, {**POINT, "has_finished": flag}))
        self.success.assert_not_called()
        self.assertTrue(report_quiz_completed(self.co, COURSE, {**POINT, "has_finished": True}))
        self.success.assert_called_once()

    def test_fresh_completion_is_reported_once_and_uses_success_level(self):
        self.remember()
        self.assertTrue(report_quiz_completed(self.co, COURSE, {**POINT, "has_finished": "true"}))
        self.assertFalse(report_quiz_completed(self.co, COURSE, {**POINT, "has_finished": True}))
        self.success.assert_called_once()
        text = rendered(self.success.call_args_list)[0]
        self.assertIn("答题完成", text)
        self.assertIn("平台已确认", text)

    def test_untracked_chapter_does_not_get_a_quiz_completion_notice(self):
        self.assertFalse(report_quiz_completed(self.co, COURSE, {**POINT, "has_finished": True}))
        self.success.assert_not_called()

    def test_course_class_and_chapter_keys_are_independent(self):
        second_class = {**COURSE, "clazzId": "another-class"}
        second_course = {**COURSE, "courseId": "another-course"}
        self.remember()
        self.remember(second_class)
        self.remember(second_course)
        self.remember(chapter="another-chapter")
        for course, chapter in ((COURSE, "unknown"), ({**COURSE, "clazzId": "unknown"}, "chapter"),
                                ({**COURSE, "courseId": "unknown"}, "chapter")):
            self.assertFalse(report_quiz_completed(self.co, course,
                {**POINT, "id": chapter, "has_finished": True}))
        for course, chapter in ((COURSE, "chapter"), (second_class, "chapter"),
                                (second_course, "chapter"), (COURSE, "another-chapter")):
            self.assertTrue(report_quiz_completed(self.co, course,
                {**POINT, "id": chapter, "has_finished": True}))
        self.assertEqual(self.success.call_count, 4)

    def test_notifications_are_isolated_between_accounts(self):
        self.remember()
        other = SimpleNamespace(profile=SimpleNamespace(name="其他账户"))
        self.assertFalse(report_quiz_completed(other, COURSE, {**POINT, "has_finished": True}))
        self.assertTrue(report_quiz_completed(self.co, COURSE, {**POINT, "has_finished": True}))
        self.success.assert_called_once()

    def test_titles_and_account_names_cannot_inject_multiline_messages(self):
        self.co.profile.name = "账户\r\nFAKE SUCCESS\x1b[31m"
        self.co.quiz_chapter_title = "章节\u2028Fake\u2029事件\x00"
        course = {**COURSE, "title": "课程\n假的完成记录"}
        job = {**base.JOB, "name": "测验\t\r\n假的提交"}
        report_quiz_submitted(self.co, course, job, base.INFO, 2)
        self.assertTrue(report_quiz_completed(self.co, course,
            {**POINT, "title": "目录\r\n伪造行", "has_finished": True}))
        for text in rendered(self.info.call_args_list) + rendered(self.success.call_args_list):
            self.assertFalse(any(character in text for character in "\r\n\t\x00\x1b\u2028\u2029"))


class RuntimeCompletionNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="quiz-notice-", dir=fixture.ROOT / "temp")
        self.profile = SimpleNamespace(directory=Path(self.temporary.name), name="日志测试账户")
        self.addCleanup(self.temporary.cleanup)
        self.co = SafetyCoordinator(self.profile, dialog=Mock(return_value="retry"))
        self.main = fixture.main
        self.info = patch("api.logger.logger.info").start()
        self.success = patch("api.logger.logger.success").start()
        self.addCleanup(patch.stopall)

    def install(self):
        main = self.main
        previous = decode._process_attachment_cards, main.process_job, main.process_chapter
        previous_pending = getattr(main, "process_pending_quizzes", None)

        def restore():
            decode._process_attachment_cards, main.process_job, main.process_chapter = previous
            if previous_pending is None:
                del main.process_pending_quizzes
            else:
                main.process_pending_quizzes = previous_pending

        self.addCleanup(restore)
        install_manual_tasks(main, self.profile, self.co)

    def test_manual_submission_announces_completion_only_after_fresh_retry(self):
        self.install()
        session = Mock()
        session.get.return_value = fixture.response(200, base.page(base.card()))
        session.post.return_value = fixture.response(200, '{"status":true}', "application/json")

        def fresh(*args):
            self.assertTrue(any("答题已提交" in text for text in rendered(self.info.call_args_list)))
            self.success.assert_not_called()
            return {"points": [{**POINT, "has_finished": True}]}

        cx = SimpleNamespace(rate_limiter=Mock(), get_job_list=Mock(return_value=([base.JOB], base.INFO)),
                             get_course_point=Mock(side_effect=fresh))
        task = self.main.ChapterTask(index=0, course=COURSE, point=dict(POINT))
        processor = make_processor(self.main.JobProcessor, self.main, self.co)(cx, [task],
            {"jobs": 1, "speed": 1.0, "retry_interval": 0.01})
        with patch("quiz_assistant.read_settings", return_value=base.SETTINGS), \
             patch("api.base.SessionManager.get_session", return_value=session), \
             patch("quiz_dialog.ask_quiz", side_effect=SubmissionNotificationTests.reviewer):
            processor.run()
        session.post.assert_called_once()
        cx.get_course_point.assert_called_once()
        self.success.assert_called_once()
        self.assertIn("平台已确认", rendered(self.success.call_args_list)[0])
        self.assertTrue(self.co.completed)

    def test_scheduler_success_alone_cannot_announce_quiz_completed(self):
        remember_pending_quiz(self.co, COURSE, base.JOB, base.INFO)
        task = self.main.ChapterTask(index=0, course=COURSE, point=dict(POINT))
        processor = make_processor(self.main.JobProcessor, self.main, self.co)(Mock(), [task],
            {"jobs": 1, "speed": 1.0})
        with patch.object(self.main, "process_chapter", return_value=self.main.ChapterResult.SUCCESS):
            processor.run()
        self.success.assert_not_called()
        self.assertTrue(report_quiz_completed(self.co, COURSE, {**POINT, "has_finished": True}))


if __name__ == "__main__":
    unittest.main()
