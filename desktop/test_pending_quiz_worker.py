"""Offline checks for direct quiz entry, cancellation and account ownership."""
import json
import threading
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import test_runtime_safety as fixture
from account_store import AlreadyRunning, ProfileLock
from pending_quiz import PendingQuizError
from pending_quiz_worker import PendingQuizWindow, run_selected_quizzes
from quiz_assistant import Question, QuizError, reviewed_quiz
from test_quiz_assistant import SETTINGS, COURSE, JOB, INFO, page, card


ROW = {"course_id": "course", "class_id": "class", "chapter_id": "chapter",
       "course_title": "课程", "chapter_title": "所选章节"}


class PendingWorkerTests(unittest.TestCase):
    setUp = fixture.RuntimeSafetyTests.setUp
    tearDown = fixture.RuntimeSafetyTests.tearDown

    def pending(self):
        (self.profile.directory / "pending_tasks.json").write_text(
            json.dumps({"tasks": [ROW]}), encoding="utf-8")

    def test_running_account_prevents_reader_or_ai_from_starting(self):
        self.pending()
        with ProfileLock(self.profile.directory), patch("run_account.prepare") as prepare:
            with self.assertRaises(AlreadyRunning):
                run_selected_quizzes(fixture.ROOT, self.profile, ROW, Mock(), Mock(), threading.Event())
        prepare.assert_not_called()

    def test_cancelled_open_never_prepares_a_session_or_calls_ai(self):
        self.pending()
        cancel = threading.Event()
        cancel.set()
        with patch("pending_quiz_worker.read_settings", return_value=SETTINGS), \
             patch("run_account.prepare") as prepare:
            result = run_selected_quizzes(fixture.ROOT, self.profile, ROW, Mock(), Mock(), cancel)
        self.assertIn("已取消", result)
        prepare.assert_not_called()

    def test_selected_entry_reuses_review_and_never_starts_scheduler_or_plays_video(self):
        self.pending()
        fake_main, cx, session = Mock(), Mock(), Mock()
        fake_main.Chaoxing.return_value = cx
        session.cookies.get.return_value = "synthetic-uid"
        co = self.co
        state = co.state = Mock()
        reviewer = Mock(return_value="submitted")
        point = {"id": "chapter", "title": "所选章节", "has_finished": False}

        def review(_cx, course, job, info, profile, coordinator, reviewer):
            self.assertIs(_cx, cx)
            self.assertEqual(course, COURSE)
            self.assertEqual(job, JOB)
            return reviewer(profile.name, course["title"], [], Mock(), Mock())

        with patch("pending_quiz_worker.read_settings", return_value=SETTINGS), \
             patch("run_account.prepare", return_value=fake_main), \
             patch("runtime_safety.install_runtime_safety", return_value=co), \
             patch("api.base.SessionManager.get_session", return_value=session), \
             patch("pending_quiz_worker.resolve_selected_quizzes", return_value=(COURSE, point, [(JOB, INFO)])) as resolve, \
             patch("pending_quiz_worker.reviewed_quiz", side_effect=review), \
             patch("pending_quiz_worker.confirm_selected_task", return_value=True) as confirm:
            result = run_selected_quizzes(fixture.ROOT, self.profile, ROW, reviewer, Mock(), threading.Event())
        self.assertIn("已确认", result)
        self.assertEqual(resolve.call_args.args, (cx, ROW))
        self.assertFalse(resolve.call_args.kwargs["cancelled"]())
        confirm.assert_called_once_with(cx, self.profile, COURSE, point)
        reviewer.assert_called_once()
        self.assertIn("所选章节", reviewer.call_args.args[1])
        fake_main.main.assert_not_called()
        fake_main.JobProcessor.assert_not_called()
        fake_main.filter_courses.assert_not_called()
        cx.study_video.assert_not_called()
        self.assertEqual(state.call_args.args[0], "stopped")

    def test_deferred_review_does_not_remove_record_or_enter_other_quizzes(self):
        self.pending()
        fake_main, session = Mock(), Mock()
        session.cookies.get.return_value = "synthetic-uid"
        co = self.co
        co.state = Mock()
        point = {"id": "chapter", "title": "所选章节", "has_finished": False}
        before = (self.profile.directory / "pending_tasks.json").read_bytes()
        with patch("pending_quiz_worker.read_settings", return_value=SETTINGS), \
             patch("run_account.prepare", return_value=fake_main), \
             patch("runtime_safety.install_runtime_safety", return_value=co), \
             patch("api.base.SessionManager.get_session", return_value=session), \
             patch("pending_quiz_worker.resolve_selected_quizzes", return_value=(COURSE, point, [(JOB, INFO), (JOB, INFO)])), \
             patch("pending_quiz_worker.reviewed_quiz", return_value="defer") as review, \
             patch("pending_quiz_worker.confirm_selected_task") as confirm:
            run_selected_quizzes(fixture.ROOT, self.profile, ROW, Mock(), Mock(), threading.Event())
        self.assertEqual(review.call_count, 1)
        confirm.assert_not_called()
        self.assertEqual((self.profile.directory / "pending_tasks.json").read_bytes(), before)

    def test_cancel_automatic_generation_prevents_post_and_ledger_write(self):
        session = Mock()
        session.get.return_value = fixture.response(200, page(card("1"), card("2")))
        self.co.auto_quizzes = True
        self.co.cancelled = threading.Event()

        def generate(*args):
            self.co.cancelled.set()
            return ["A"], "synthetic explanation"

        with patch("quiz_assistant.read_settings", return_value=SETTINGS), \
             patch("api.base.SessionManager.get_session", return_value=session), \
             patch("quiz_assistant.generate_suggestion", side_effect=generate) as ai, \
             self.assertRaisesRegex(QuizError, "已取消"):
            reviewed_quiz(Mock(), COURSE, JOB, INFO, self.profile, self.co)
        self.assertEqual(ai.call_count, 1)
        session.post.assert_not_called()
        self.assertFalse((self.profile.directory / "quiz_submissions.json").exists())

    def test_auto_error_continues_later_quizzes_and_reports_pending_without_review(self):
        self.pending()
        fake_main, session = Mock(), Mock()
        session.cookies.get.return_value = "synthetic-uid"
        self.co.auto_quizzes = True
        self.co.state = Mock()
        point = {"id": "chapter", "title": "所选章节", "has_finished": False}
        reviewer = Mock()
        with patch("pending_quiz_worker.read_settings", return_value=SETTINGS), \
             patch("run_account.prepare", return_value=fake_main), \
             patch("runtime_safety.install_runtime_safety", return_value=self.co), \
             patch("api.base.SessionManager.get_session", return_value=session), \
             patch("pending_quiz_worker.resolve_selected_quizzes", return_value=(COURSE, point,
                [(JOB, INFO), ({**JOB, "jobid": "work-next"}, INFO)])), \
             patch("pending_quiz_worker.reviewed_quiz", side_effect=[QuizError("合成 AI 失败"), "auto_submitted"]) as review, \
             patch("pending_quiz_worker.confirm_selected_task", return_value=False), \
             self.assertRaisesRegex(PendingQuizError, "合成 AI 失败"):
            run_selected_quizzes(fixture.ROOT, self.profile, ROW, reviewer, Mock(), threading.Event())
        self.assertEqual(review.call_count, 2)
        reviewer.assert_not_called()
        self.assertTrue(self.co.auto_quizzes)


class PendingWorkerWindowTests(unittest.TestCase):
    def setUp(self):
        self.window = tk.Tk()
        self.window.withdraw()
        self.profile = Mock(name="profile")
        self.profile.name = "测试账户"
        self.dialog = PendingQuizWindow(self.window, fixture.ROOT, self.profile, ROW, autostart=False)

    def tearDown(self):
        if self.dialog.review_dialog and self.dialog.review_dialog.timer:
            self.window.after_cancel(self.dialog.review_dialog.timer)
        if self.dialog.timer:
            self.window.after_cancel(self.dialog.timer)
        self.window.destroy()

    def test_progress_window_does_not_generate_or_submit_on_construction(self):
        self.assertFalse(self.dialog.busy)
        self.assertIn("打开", self.dialog.feedback.get())
        self.assertIsNone(self.dialog.review_dialog)

    def test_review_remains_in_ui_thread_and_defer_releases_waiting_reader(self):
        generate, submit = Mock(), Mock()
        packet = {"args": (self.profile.name, "所选章节", [Question("1", "题目", "single", "A.甲\nB.乙", ["answer1"])],
                            generate, submit), "finished": threading.Event(), "result": "defer"}
        self.dialog.events.put(("review", packet))
        self.dialog.poll()
        review = self.dialog.review_dialog
        self.assertIsNotNone(review)
        self.assertFalse(packet["finished"].is_set())
        generate.assert_not_called()
        submit.assert_not_called()
        review.defer()
        self.assertTrue(packet["finished"].is_set())
        self.assertTrue(review.cancel_generation.is_set())
        self.assertEqual(packet["result"], "defer")
        self.assertIsNone(self.dialog.review_dialog)

    def test_failed_review_window_releases_waiting_reader_and_can_close(self):
        packet = {"args": (), "finished": threading.Event(), "result": "defer"}
        with patch("pending_quiz_worker.ReviewDialog", side_effect=RuntimeError("synthetic-ui-failure")):
            self.dialog.events.put(("review", packet))
            self.dialog.poll()
        self.assertTrue(packet["finished"].is_set())
        self.assertIsNone(self.dialog.review_dialog)
        self.assertIn("未能打开", self.dialog.feedback.get())
        self.assertTrue(self.dialog.close_button.winfo_exists())

    def test_cancel_does_not_release_reader_while_ui_submission_is_still_active(self):
        result = []
        worker = threading.Thread(target=lambda: result.append(self.dialog.request_review(
            "合成账户", "合成课程", [], Mock(), Mock())), daemon=True)
        worker.start()
        kind, packet = self.dialog.events.get(timeout=2)
        self.assertEqual(kind, "review")
        try:
            self.dialog.cancelled.set()
            worker.join(.35)
            self.assertTrue(worker.is_alive(), "Active submit must retain account ownership until UI finishes")
            self.assertEqual(result, [])
        finally:
            packet["result"] = "submitted"
            packet["finished"].set()
            worker.join(2)
        self.assertEqual(result, ["submitted"])


if __name__ == "__main__":
    unittest.main()
