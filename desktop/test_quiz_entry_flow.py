"""Offline direct entry tests: resume the live quiz without reloading videos/cards."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_runtime_safety as fixture
from api import decode
from manual_tasks import install_manual_tasks, ManualTasksRequired
from runtime_safety import AccessRestricted, IncompleteRun, RunNeedsAttention


main = fixture.main


class QuizEntryFlowTests(unittest.TestCase):
    setUp = fixture.RuntimeSafetyTests.setUp
    tearDown = fixture.RuntimeSafetyTests.tearDown
    task = fixture.RuntimeSafetyTests.task
    processor = fixture.RuntimeSafetyTests.processor

    def install(self):
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

    def client(self, extra=None):
        self.job = {"type": "workid", "jobid": "work-test", "name": "章节测验", "enc": "synthetic-secret-enc"}
        self.info = {"ktoken": "synthetic-secret-ktoken", "knowledgeid": "0", "cpi": "cpi"}
        jobs = [{"type": "video", "jobid": "video"}, self.job, *(extra or [])]
        return SimpleNamespace(rate_limiter=Mock(), get_job_list=Mock(return_value=(jobs, self.info)),
            study_video=Mock(return_value=main.StudyResult.SUCCESS), study_work=Mock(),
            tiku=SimpleNamespace(DISABLE=True, SUBMIT=False),
            get_course_point=Mock(return_value={"points": [self.task().point]}))

    def test_open_uses_same_quiz_and_session_context_without_reloading_video_or_cards(self):
        self.install()
        cx = self.client()
        processor = self.processor([self.task()])
        processor.chaoxing = cx
        self.co.dialog.return_value = "quiz"
        with patch("quiz_assistant.reviewed_quiz", side_effect=["disabled", "skip"]) as reviewed, \
             self.assertRaises(IncompleteRun):
            processor.run()
        self.assertEqual(reviewed.call_count, 2)
        self.assertIs(reviewed.call_args.args[0], cx)
        self.assertEqual(reviewed.call_args.args[2], self.job)
        self.assertEqual(reviewed.call_args.args[3], self.info)
        cx.get_job_list.assert_called_once()
        cx.study_video.assert_called_once()
        cx.get_course_point.assert_not_called()
        cx.study_work.assert_not_called()
        self.co.dialog.assert_called_once()
        self.assertFalse(self.co.completed)

    def test_authentication_parameters_are_never_written_to_progress_files(self):
        self.install()
        cx = self.client()
        processor = self.processor([self.task()])
        processor.chaoxing = cx
        self.co.dialog.return_value = "quiz"
        with patch("quiz_assistant.reviewed_quiz", side_effect=["disabled", "skip"]), self.assertRaises(IncompleteRun):
            processor.run()
        for name in ("pending_tasks.json", "resume_state.json", "runtime_state.json"):
            content = (self.profile.directory / name).read_text(encoding="utf-8")
            self.assertNotIn("synthetic-secret", content)
            self.assertNotIn("quiz_context", content)
        self.assertTrue(self.co.pending[0]["manual_tasks"])

    def test_mixed_external_tasks_remain_pending_and_are_not_reexecuted(self):
        self.install()
        external = {"type": "manual", "kind": "hyperlink", "name": "外部视频", "href": "https://example.org/"}
        cx = self.client([external])
        processor = self.processor([self.task()])
        processor.chaoxing = cx
        self.co.dialog.side_effect = ["quiz", "skip"]
        with patch("quiz_assistant.reviewed_quiz", side_effect=["disabled", "skip"]), self.assertRaises(IncompleteRun):
            processor.run()
        cx.get_job_list.assert_called_once()
        cx.study_video.assert_called_once()
        self.assertEqual([job["kind"] for job in self.co.dialog.call_args.kwargs["manual_jobs"]], ["hyperlink"])
        self.assertEqual(len(self.co.pending[0]["manual_tasks"]), 2)

    def test_manual_submit_still_needs_platform_confirmation_to_complete(self):
        self.install()
        cx = self.client()
        cx.get_course_point.return_value = {"points": [{**self.task().point, "has_finished": True}]}
        processor = self.processor([self.task()])
        processor.chaoxing = cx
        self.co.dialog.side_effect = ["quiz", "retry"]
        with patch("quiz_assistant.reviewed_quiz", side_effect=["disabled", "submitted"]):
            processor.run()
        self.assertTrue(self.co.completed)
        cx.get_job_list.assert_called_once()
        cx.study_video.assert_called_once()
        cx.get_course_point.assert_called_once()

    def test_access_restriction_on_direct_quiz_entry_still_pauses(self):
        self.install()
        cx = self.client()
        processor = self.processor([self.task()])
        processor.chaoxing = cx
        self.co.dialog.side_effect = ["quiz", "stop"]
        with patch("quiz_assistant.reviewed_quiz", side_effect=["disabled", AccessRestricted(403)]), \
             self.assertRaises(RunNeedsAttention):
            processor.run()
        self.assertFalse(self.co.completed)
        self.assertEqual(len(self.co.pending), 1)
        cx.get_job_list.assert_called_once()
        cx.study_video.assert_called_once()

    def test_quiz_dialog_receives_zero_countdown_and_configuration_callback(self):
        with patch("quiz_dialog.ensure_ai_config", return_value=False) as configure:
            result = self.co.manual_tasks(ManualTasksRequired([{"kind": "workid", "name": "测验"}]), self.task(), 0)
            self.assertEqual(result, "stop")
            self.assertEqual(self.co.dialog.call_args.args[3], 0)
            callback = self.co.dialog.call_args.kwargs["on_quiz"]
            self.assertFalse(callback("synthetic-parent"))
            configure.assert_called_once_with("synthetic-parent", self.profile)

    def test_legacy_pending_notices_do_not_expand_into_unrelated_tasks(self):
        self.co.dialog.return_value = "quiz"
        processor = self.processor([self.task()])
        with patch.object(main, "process_chapter", side_effect=ManualTasksRequired([{"kind": "workid", "name": "旧记录"}])), \
             self.assertRaises(RunNeedsAttention):
            processor.run()
        self.assertFalse(self.co.completed)


if __name__ == "__main__":
    unittest.main()
