"""Offline one-click fill and explicitly enabled automatic-quiz tests."""
import json
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_quiz_assistant as base
import test_runtime_safety as fixture
from api import decode
from quiz_assistant import Question, QuizError, generate_all, reviewed_quiz, SubmissionLedger
from quiz_dialog import ReviewDialog
from runtime_safety import SafetyCoordinator, make_processor, IncompleteRun
from manual_tasks import install_manual_tasks, ManualTasksRequired


def questions():
    return [Question("1", "第一题", "single", "A. 第一\nB. 第二", ["answer1"]),
            Question("2", "第二题", "completion", "", ["answer2_0", "answer2_1"], 2)]


class BulkTests(unittest.TestCase):
    def test_one_click_fills_every_question_in_order(self):
        generate = Mock(side_effect=[(["A"], "解释1"), (["one", "two"], "解释2")])
        progress = Mock()
        result = generate_all(questions(), generate, progress)
        self.assertEqual(result["answers"], {"1": ["A"], "2": ["one", "two"]})
        self.assertEqual(result["errors"], {})
        self.assertEqual([call.args[0].id for call in generate.call_args_list], ["1", "2"])
        self.assertEqual([call.args for call in progress.call_args_list], [(1, 2), (2, 2)])

    def test_failure_retains_other_answers_and_never_invents_missing_blanks(self):
        generate = Mock(side_effect=[(["A"], "解释"), (["only-one"], "解释")])
        result = generate_all(questions(), generate)
        self.assertEqual(result["answers"], {"1": ["A"]})
        self.assertIn("2", result["errors"])

    def test_cancel_stops_before_any_further_ai_request(self):
        generate = Mock(return_value=(["A"], "解释"))
        result = generate_all(questions(), generate, cancelled=Mock(side_effect=[False, True]))
        self.assertTrue(result["cancelled"])
        generate.assert_called_once()

    def test_transport_errors_are_sanitized_and_do_not_retry(self):
        generate = Mock(side_effect=RuntimeError("synthetic-secret"))
        result = generate_all(questions(), generate)
        self.assertEqual(generate.call_count, 2)
        self.assertNotIn("synthetic-secret", str(result))
        self.assertFalse(result["answers"])


class AutoTests(unittest.TestCase):
    setUp = base.QuizTests.setUp
    run_quiz = base.QuizTests.run_quiz

    def test_auto_requires_explicit_dialog_action_then_submits_once(self):
        reviewer = Mock(return_value="auto_all_quizzes")
        with patch("quiz_assistant.generate_suggestion", return_value=(["B"], "解释")) as generate:
            self.assertEqual(self.run_quiz(reviewer), "auto_submitted")
        reviewer.assert_called_once()
        generate.assert_called_once()
        self.session.post.assert_called_once()
        self.assertTrue(self.co.auto_quizzes)
        self.assertFalse(self.co.skip_all_quizzes)
        self.assertEqual(self.session.post.call_args.kwargs["data"]["answer1"], "B")

    def test_enabled_auto_fills_all_without_opening_review_window(self):
        self.co.auto_quizzes = True
        self.session.get.return_value = fixture.response(200, base.page(base.card("1"), base.card("2")))
        reviewer = Mock()
        with patch("quiz_assistant.generate_suggestion", return_value=(["A"], "解释")) as generate:
            self.assertEqual(self.run_quiz(reviewer), "auto_submitted")
        self.assertEqual(generate.call_count, 2)
        reviewer.assert_not_called()
        self.session.post.assert_called_once()
        self.assertEqual(self.session.post.call_args.kwargs["data"]["answerwqbid"], "1,2,")

    def test_default_mode_remains_reviewed_and_does_not_generate_on_open(self):
        reviewer = Mock(return_value="defer")
        with patch("quiz_assistant.generate_suggestion") as generate:
            self.assertEqual(self.run_quiz(reviewer), "defer")
        generate.assert_not_called()
        self.session.post.assert_not_called()

    def test_ai_failure_preserves_auto_and_never_posts_partial_answers(self):
        self.co.auto_quizzes = True
        self.session.get.return_value = fixture.response(200, base.page(base.card("1"), base.card("2")))
        with patch("quiz_assistant.generate_suggestion", side_effect=[(["A"], "解释"), QuizError("连接未成功")]), \
             self.assertRaises(QuizError):
            self.run_quiz(Mock())
        self.assertTrue(self.co.auto_quizzes)
        self.session.post.assert_not_called()

    def test_unknown_form_preserves_auto_without_calling_ai_or_posting(self):
        self.co.auto_quizzes = True
        self.session.get.return_value = fixture.response(200, base.page(base.card("1", "2",
            '<textarea name="unconfirmed"></textarea>', options=False)))
        with patch("quiz_assistant.generate_suggestion") as generate, self.assertRaises(QuizError):
            self.run_quiz(Mock())
        self.assertTrue(self.co.auto_quizzes)
        generate.assert_not_called()
        self.session.post.assert_not_called()

    def test_timeout_persists_unknown_result_and_does_not_resubmit(self):
        self.co.auto_quizzes = True
        self.session.post.side_effect = TimeoutError()
        with patch("quiz_assistant.generate_suggestion", return_value=(["A"], "解释")), self.assertRaises(QuizError):
            self.run_quiz(Mock())
        self.assertTrue(self.co.auto_quizzes)
        self.co.auto_quizzes = True
        self.assertEqual(self.run_quiz(Mock()), "pending_confirmation")
        self.assertTrue(self.co.auto_quizzes)
        self.assertEqual(self.session.post.call_count, 1)

    def test_server_rejection_is_not_automatically_retried(self):
        SubmissionLedger(self.profile).record("course:class:work-123", "rejected")
        self.co.auto_quizzes = True
        with self.assertRaises(QuizError):
            self.run_quiz(Mock())
        self.assertTrue(self.co.auto_quizzes)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_auto_policy_is_account_local_and_not_persisted(self):
        first = SafetyCoordinator(self.profile)
        first.auto_quizzes = True
        second = SafetyCoordinator(self.profile)
        self.assertFalse(second.auto_quizzes)
        self.assertFalse(second.skip_all_quizzes)

    def test_auto_submission_only_reports_complete_after_fresh_server_confirmation(self):
        main = fixture.main
        original = decode._process_attachment_cards, main.process_job, main.process_chapter
        def restore():
            decode._process_attachment_cards, main.process_job, main.process_chapter = original
        self.addCleanup(restore)
        co = SafetyCoordinator(self.profile, dialog=Mock())
        install_manual_tasks(main, self.profile, co)
        point = {"id": "chapter", "title": "章节", "has_finished": False}
        cx = SimpleNamespace(rate_limiter=Mock(), get_job_list=Mock(return_value=([base.JOB], base.INFO)),
            get_course_point=Mock(return_value={"points": [{**point, "has_finished": True}]}))
        with patch("quiz_assistant.reviewed_quiz", return_value="auto_submitted"):
            result = main.process_chapter(cx, {**base.COURSE, "cpi": "cpi"}, point, 1)
        self.assertEqual(result, main.ChapterResult.SUCCESS)
        self.assertTrue(point["has_finished"])
        cx.get_course_point.assert_called_once()

    def test_auto_accepted_submission_keeps_unconfirmed_tasks_without_recovery_popups(self):
        main = fixture.main
        original = decode._process_attachment_cards, main.process_job, main.process_chapter
        def restore():
            decode._process_attachment_cards, main.process_job, main.process_chapter = original
        self.addCleanup(restore)
        co = SafetyCoordinator(self.profile, dialog=Mock(return_value="stop"))
        install_manual_tasks(main, self.profile, co)
        cx = SimpleNamespace(rate_limiter=Mock(), get_job_list=Mock(return_value=([base.JOB], base.INFO)))
        task = main.ChapterTask(index=0, course=base.COURSE,
            point={"id": "chapter", "title": "章节", "has_finished": False})
        processor = make_processor(main.JobProcessor, main, co)(cx, [task], {"jobs": 1, "speed": 1.0})
        with patch("quiz_assistant.reviewed_quiz", return_value="auto_submitted"), self.assertRaises(IncompleteRun):
            processor.run()
        co.dialog.assert_not_called()
        self.assertFalse(co.completed)
        self.assertEqual(len(co.pending), 1)
        self.assertIn("AI已提交", co.pending[0]["manual_tasks"][0])


class BulkWindowTests(unittest.TestCase):
    def setUp(self):
        self.window = tk.Tk()
        self.window.withdraw()
        self.generate, self.submit = Mock(), Mock()
        self.dialog = ReviewDialog(self.window, "模拟账户", "模拟课程", questions(), self.generate, self.submit)
        self.window.geometry("740x580+10000+10000")
        self.window.deiconify()
        self.window.update()

    def tearDown(self):
        if self.dialog.timer:
            self.window.after_cancel(self.dialog.timer)
        self.window.destroy()

    def test_bulk_and_auto_buttons_are_visible_and_no_service_called_on_open(self):
        for button in (self.dialog.generate_all_button, self.dialog.auto_button, self.dialog.skip_all_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), self.window.winfo_rootx() + self.window.winfo_width())
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), self.window.winfo_rooty() + self.window.winfo_height())
        self.assertGreater(self.dialog.canvas.winfo_height(), 0)
        self.generate.assert_not_called()
        self.submit.assert_not_called()

    def test_bulk_result_populates_all_questions_without_submitting_or_auto_approval(self):
        self.dialog.events.put(("all", {"answers": {"1": ["B"], "2": ["one", "two"]},
            "explanations": {}, "errors": {}, "cancelled": False}, None))
        self.dialog.poll()
        self.assertEqual(self.dialog.answers, {"1": ["B"], "2": ["one", "two"]})
        self.assertEqual(self.dialog.variables[0].get(), "B")
        self.assertFalse(self.dialog.reviewed.get())
        self.assertEqual(str(self.dialog.submit_button.cget("state")), "disabled")
        self.submit.assert_not_called()

    def test_bulk_progress_does_not_finish_the_operation(self):
        self.dialog.busy = True
        self.dialog.events.put(("progress", (1, 2), None))
        self.dialog.poll()
        self.assertTrue(self.dialog.busy)
        self.assertIn("1/2", self.dialog.feedback.get())

    def test_bulk_button_runs_generator_for_entire_quiz(self):
        self.generate.side_effect = [(["A"], "解释"), (["one", "two"], "解释")]
        with patch("quiz_dialog.start_job") as start:
            self.dialog.generate_all_button.invoke()
        result = start.call_args.args[2]()
        self.assertEqual(set(result["answers"]), {"1", "2"})
        self.assertEqual(self.generate.call_count, 2)
        self.submit.assert_not_called()

    def test_auto_button_is_explicit_and_cannot_interrupt_an_inflight_submission(self):
        self.dialog.busy, self.dialog.operation = True, "submit"
        with patch.object(self.dialog, "finish") as finish:
            self.dialog.automatic()
        finish.assert_not_called()
        self.dialog.busy = False
        self.dialog.auto_button.invoke()
        self.assertEqual(self.dialog.result["action"], "auto_all_quizzes")
        self.submit.assert_not_called()

    def test_unsupported_question_blocks_auto_but_allows_bulk_assistance(self):
        self.dialog.questions[0].unsupported = "请到网页完成"
        with patch.object(self.dialog, "finish") as finish:
            self.dialog.auto_button.invoke()
        finish.assert_not_called()
        self.assertEqual(self.dialog.result, "defer")


if __name__ == "__main__":
    unittest.main()

