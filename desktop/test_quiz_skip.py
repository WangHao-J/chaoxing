"""Offline coverage for explicit quiz skips, honest pending state and account scope."""
import json
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_runtime_safety as fixture
from api import decode
from manual_tasks import ManualTasksRequired, install_manual_tasks
from quiz_assistant import Question
from quiz_dialog import ReviewDialog
from recovery_dialog import RecoveryDialog
from runtime_safety import AccessRestricted, IncompleteRun, SafetyCoordinator


main = fixture.main


def quiz(number=1):
    return {"type": "workid", "jobid": f"work-{number}", "name": f"测验{number}"}


def manual_quiz(skipped=False):
    return {"type": "manual", "kind": "workid", "name": "章节测验", "href": "",
            **({"skipped_quiz": True} if skipped else {})}


def external():
    return {"type": "manual", "kind": "hyperlink", "name": "外部视频", "href": "https://example.org/video"}


class QuizSkipFlowTests(unittest.TestCase):
    setUp = fixture.RuntimeSafetyTests.setUp
    tearDown = fixture.RuntimeSafetyTests.tearDown
    task = fixture.RuntimeSafetyTests.task
    processor = fixture.RuntimeSafetyTests.processor

    def install(self):
        previous = decode._process_attachment_cards, main.process_job, main.process_chapter

        def restore():
            decode._process_attachment_cards, main.process_job, main.process_chapter = previous

        self.addCleanup(restore)
        install_manual_tasks(main, self.profile, self.co)
        return restore

    def client(self, jobs):
        return SimpleNamespace(rate_limiter=Mock(), get_job_list=Mock(return_value=(jobs, {})),
            tiku=SimpleNamespace(DISABLE=True, SUBMIT=False), study_work=Mock(),
            study_document=Mock(return_value=main.StudyResult.SUCCESS),
            study_video=Mock(return_value=main.StudyResult.SUCCESS),
            study_read=Mock(return_value=main.StudyResult.SUCCESS))

    def test_skip_one_retains_current_quiz_and_asks_for_next_in_same_chapter(self):
        self.install()
        client, task = self.client([quiz(1), quiz(2)]), self.task()
        with patch("quiz_assistant.reviewed_quiz", side_effect=["skip", "defer"]) as reviewer, \
                self.assertRaises(ManualTasksRequired) as caught:
            main.process_chapter(client, task.course, task.point, 1)
        self.assertEqual(reviewer.call_count, 2)
        self.assertFalse(self.co.skip_all_quizzes)
        self.assertTrue(caught.exception.jobs[0]["skipped_quiz"])
        self.assertFalse(caught.exception.jobs[1].get("skipped_quiz", False))
        client.study_work.assert_not_called()

    def test_skip_all_suppresses_later_quizzes_but_keeps_document_video_and_reading(self):
        self.install()
        jobs = [quiz(1), quiz(2), {"type": "document", "jobid": "doc"},
                {"type": "video", "jobid": "video"}, {"type": "read", "jobid": "read"}]
        client, task = self.client(jobs), self.task()
        with patch("quiz_assistant.reviewed_quiz", return_value="skip_all_quizzes") as reviewer, \
                self.assertRaises(ManualTasksRequired) as caught:
            main.process_chapter(client, task.course, task.point, 1)
        reviewer.assert_called_once()
        self.assertTrue(self.co.skip_all_quizzes)
        self.assertEqual(len(caught.exception.jobs), 2)
        self.assertTrue(all(job["skipped_quiz"] for job in caught.exception.jobs))
        client.study_work.assert_not_called()
        client.study_document.assert_called_once_with(task.course, jobs[2])
        client.study_video.assert_called_once_with(task.course, jobs[3], {}, _speed=1, _type="Video")
        client.study_read.assert_called_once_with(task.course, jobs[4], {})

    def test_already_enabled_skip_all_does_not_read_settings_or_contact_quiz_services(self):
        self.install()
        self.co.skip_all_quizzes = True
        client, task = self.client([quiz()]), self.task()
        with patch("quiz_assistant.reviewed_quiz") as reviewer, \
             patch("quiz_assistant.read_settings") as settings, \
             patch("api.base.SessionManager.get_session") as session, \
             self.assertRaises(ManualTasksRequired):
            main.process_chapter(client, task.course, task.point, 1)
        reviewer.assert_not_called()
        settings.assert_not_called()
        session.assert_not_called()
        client.study_work.assert_not_called()

    def test_real_scheduler_skips_quiz_dialogs_without_retries_and_retains_all_pending_chapters(self):
        self.install()
        tasks = [self.task(0), self.task(1)]
        client = self.client([])
        client.get_job_list.side_effect = [([quiz(1)], {}), ([quiz(2)], {})]
        processor = self.processor(tasks)
        processor.chaoxing = client
        with patch("quiz_assistant.reviewed_quiz", return_value="skip_all_quizzes") as reviewer, \
             patch("runtime_safety.time.sleep") as sleep, self.assertRaises(IncompleteRun):
            processor.run()
        reviewer.assert_called_once()
        self.co.dialog.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(processor.failed_tasks, tasks)
        self.assertEqual([task.tries for task in tasks], [0, 0])
        self.assertFalse(self.co.completed)
        self.assertEqual(len(self.co.pending), 2)
        self.assertTrue(all(row["manual_tasks"] for row in self.co.pending))
        saved = json.loads((self.profile.directory / "resume_state.json").read_text(encoding="utf-8"))
        self.assertEqual({row["chapter_id"] for row in saved["pending"]}, {"0", "1"})
        self.assertFalse(any(row["has_finished"] for row in (task.point for task in tasks)))

    def test_skip_one_continues_to_next_chapter_and_still_requests_review(self):
        self.install()
        tasks = [self.task(0), self.task(1)]
        client = self.client([])
        client.get_job_list.side_effect = [([quiz(1)], {}), ([quiz(2)], {})]
        processor = self.processor(tasks)
        processor.chaoxing = client
        with patch("quiz_assistant.reviewed_quiz", return_value="skip") as reviewer, \
             patch("runtime_safety.time.sleep") as sleep, self.assertRaises(IncompleteRun):
            processor.run()
        self.assertEqual(reviewer.call_count, 2)
        self.assertFalse(self.co.skip_all_quizzes)
        self.co.dialog.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(processor.failed_tasks, tasks)

    def test_mixed_skipped_quiz_and_external_task_still_opens_external_recovery(self):
        self.install()
        task = self.task()
        client = self.client([quiz(), external()])
        processor = self.processor([task])
        processor.chaoxing = client
        self.co.dialog.return_value = "skip"
        with patch("quiz_assistant.reviewed_quiz", return_value="skip_all_quizzes"), \
             self.assertRaises(IncompleteRun):
            processor.run()
        self.co.dialog.assert_called_once()
        shown = self.co.dialog.call_args.kwargs["manual_jobs"]
        self.assertEqual([job["kind"] for job in shown], ["hyperlink"])
        self.assertEqual(len(self.co.pending[0]["manual_tasks"]), 2)
        self.assertFalse(self.co.completed)

    def test_recovery_skip_all_mixed_tasks_reopens_only_non_quiz_tasks(self):
        self.co.dialog.side_effect = ["skip_all_quizzes", "skip"]
        jobs = [manual_quiz(), external()]
        self.assertEqual(self.co.manual_tasks(ManualTasksRequired(jobs), self.task(), 0), "skip")
        self.assertTrue(self.co.skip_all_quizzes)
        self.assertEqual(self.co.dialog.call_count, 2)
        self.assertEqual(self.co.dialog.call_args_list[0].kwargs["manual_jobs"], jobs)
        self.assertEqual(self.co.dialog.call_args_list[1].kwargs["manual_jobs"], [jobs[1]])

    def test_recovery_skip_all_quiz_only_does_not_open_a_second_dialog(self):
        self.co.dialog.return_value = "skip_all_quizzes"
        self.assertEqual(self.co.manual_tasks(ManualTasksRequired([manual_quiz()]), self.task(), 0), "skip")
        self.assertTrue(self.co.skip_all_quizzes)
        self.co.dialog.assert_called_once()

    def test_skip_filter_does_not_hide_external_tasks_or_access_restrictions(self):
        self.co.skip_all_quizzes = True
        self.co.dialog.return_value = "stop"
        self.assertEqual(self.co.manual_tasks(ManualTasksRequired([manual_quiz(), external()]), self.task(), 0), "stop")
        self.assertEqual([job["kind"] for job in self.co.dialog.call_args.kwargs["manual_jobs"]], ["hyperlink"])
        self.co.dialog.reset_mock()
        self.assertEqual(self.co.recover(AccessRestricted(403), self.task(), 0), "stop")
        self.co.dialog.assert_called_once()
        self.assertIsNone(self.co.dialog.call_args.kwargs.get("manual_jobs"))

    def test_skip_flag_is_account_local_and_survives_new_processor_only_within_this_run(self):
        self.co.skip_all_quizzes = True
        other_directory = self.profile.directory / "other"
        other_directory.mkdir()
        other = SafetyCoordinator(SimpleNamespace(name="另一个账户", directory=other_directory))
        restarted = SafetyCoordinator(self.profile)
        self.assertFalse(other.skip_all_quizzes)
        self.assertFalse(restarted.skip_all_quizzes)
        self.processor([self.task()])
        self.assertTrue(self.co.skip_all_quizzes)

    def test_restart_asks_again_for_quiz_saved_as_pending(self):
        restore = self.install()
        self.co.skip_all_quizzes = True
        task = self.task()
        processor = self.processor([task])
        processor.chaoxing = self.client([quiz()])
        with patch("quiz_assistant.reviewed_quiz") as first_review, self.assertRaises(IncompleteRun):
            processor.run()
        first_review.assert_not_called()
        restore()
        self.co = SafetyCoordinator(self.profile, dialog=Mock(return_value="stop"))
        self.install()
        processor = self.processor([self.task()])
        processor.chaoxing = self.client([quiz()])
        with patch("quiz_assistant.reviewed_quiz", return_value="skip") as next_review, \
             self.assertRaises(IncompleteRun):
            processor.run()
        next_review.assert_called_once()
        self.assertFalse(self.co.skip_all_quizzes)
        self.assertFalse(self.co.completed)


class QuizSkipWindowTests(unittest.TestCase):
    def setUp(self):
        self.window = tk.Tk()
        self.window.withdraw()
        self.generate, self.submit = Mock(), Mock()

    def tearDown(self):
        self.window.destroy()

    def review(self):
        question = Question("1", "按顺序填写15个空", "completion", "", [f"answer1_{i}" for i in range(15)], 15)
        dialog = ReviewDialog(self.window, "离线测试账户", "测试课程", [question], self.generate, self.submit)
        self.window.geometry("740x540+10000+10000")
        self.window.deiconify()
        self.window.update()
        self.addCleanup(lambda: self.window.after_cancel(dialog.timer) if dialog.timer else None)
        return dialog

    def test_both_skip_buttons_are_visible_at_minimum_review_size(self):
        dialog = self.review()
        self.assertEqual(dialog.skip_button.cget("text"), "跳过本次答题")
        self.assertEqual(dialog.skip_all_button.cget("text"), "跳过后续所有答题")
        for button in (dialog.skip_button, dialog.skip_all_button, dialog.submit_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), self.window.winfo_rootx() + self.window.winfo_width())
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), self.window.winfo_rooty() + self.window.winfo_height())
        self.assertGreater(dialog.canvas.winfo_height(), 0)
        self.generate.assert_not_called()
        self.submit.assert_not_called()

    def test_single_skip_returns_explicit_action_without_generating_or_submitting(self):
        dialog = self.review()
        dialog.skip_button.invoke()
        self.assertEqual(dialog.result, "skip")
        self.generate.assert_not_called()
        self.submit.assert_not_called()

    def test_skip_all_returns_explicit_action_without_generating_or_submitting(self):
        dialog = self.review()
        dialog.skip_all_button.invoke()
        self.assertEqual(dialog.result, "skip_all_quizzes")
        self.generate.assert_not_called()
        self.submit.assert_not_called()

    def test_submit_in_flight_cannot_be_skipped_or_closed(self):
        dialog = self.review()
        dialog.busy, dialog.operation = True, "submit"
        with patch.object(dialog, "finish") as finish:
            dialog.skip_button.invoke()
            dialog.skip_all_button.invoke()
            dialog.defer()
        finish.assert_not_called()
        self.assertEqual(dialog.result, "defer")
        self.assertIn("提交", dialog.feedback.get())
        self.submit.assert_not_called()

    def test_window_close_preserves_defer_instead_of_skip_all(self):
        dialog = self.review()
        dialog.defer()
        self.assertEqual(dialog.result, "defer")
        self.submit.assert_not_called()

    def test_quiz_recovery_offers_skip_all_and_returns_its_action(self):
        dialog = RecoveryDialog(self.window, "测试账户", "测验未完成", "章节", 0, True,
                                manual_jobs=[manual_quiz()])
        try:
            self.assertEqual(dialog.skip_button.cget("text"), "跳过本次答题")
            self.assertEqual(dialog.skip_all_button.cget("text"), "跳过后续所有答题")
            self.window.update_idletasks()
            self.window.geometry(f"{max(570, self.window.winfo_reqwidth())}x{max(360, self.window.winfo_reqheight())}+10000+10000")
            self.window.deiconify()
            self.window.update()
            self.assertTrue(dialog.skip_all_button.winfo_ismapped())
            self.assertLessEqual(dialog.skip_all_button.winfo_rooty() + dialog.skip_all_button.winfo_height(),
                                 self.window.winfo_rooty() + self.window.winfo_height())
            dialog.skip_all_button.invoke()
            self.assertEqual(dialog.result, "skip_all_quizzes")
        finally:
            if dialog.timer is not None:
                self.window.after_cancel(dialog.timer)

    def test_external_task_recovery_does_not_offer_skip_all_quizzes(self):
        dialog = RecoveryDialog(self.window, "测试账户", "外部链接未完成", "章节", 0, True,
                                manual_jobs=[external()])
        try:
            self.assertFalse(hasattr(dialog, "skip_all_button"))
        finally:
            if dialog.timer is not None:
                self.window.after_cancel(dialog.timer)


if __name__ == "__main__":
    unittest.main()
