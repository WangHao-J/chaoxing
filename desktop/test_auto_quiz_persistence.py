"""Keep one explicit run-wide choice through pending and duplicate quiz cards."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_quiz_assistant as base
import test_runtime_safety as fixture
from api import decode
from manual_tasks import install_manual_tasks
from quiz_assistant import reviewed_quiz, SubmissionLedger, QuizError
from runtime_safety import SafetyCoordinator, make_processor, IncompleteRun


class AutoPolicyTests(unittest.TestCase):
    setUp = base.QuizTests.setUp

    def review(self, job, reviewer):
        return reviewed_quiz(None, base.COURSE, job, base.INFO, self.profile, self.co, reviewer)

    def test_one_click_applies_to_new_quizzes_after_repeated_completed_job_reads(self):
        reviewer = Mock(return_value="auto_all_quizzes")
        jobs = [base.JOB, base.JOB, base.JOB, {**base.JOB, "jobid": "work-second"},
                {**base.JOB, "jobid": "work-third"}]
        with patch("quiz_assistant.read_settings", return_value=base.SETTINGS), \
             patch("api.base.SessionManager.get_session", return_value=self.session), \
             patch("quiz_assistant.generate_suggestion", return_value=(["B"], "解释")) as ai:
            outcomes = [self.review(job, reviewer) for job in jobs]
        self.assertEqual(outcomes, ["auto_submitted", "pending_confirmation", "pending_confirmation",
                                    "auto_submitted", "auto_submitted"])
        reviewer.assert_called_once()
        self.assertEqual(ai.call_count, 3)
        self.assertEqual(self.session.post.call_count, 3)
        self.assertEqual(self.session.get.call_count, 3)
        self.assertTrue(self.co.auto_quizzes)

    def test_preexisting_unknown_or_acknowledged_result_does_not_turn_off_this_run_choice(self):
        for state in ("uncertain", "acknowledged"):
            with self.subTest(state=state):
                SubmissionLedger(self.profile).record("course:class:work-123", state)
                self.co.auto_quizzes = True
                reviewer = Mock()
                self.session.reset_mock()
                with patch("quiz_assistant.read_settings", return_value=base.SETTINGS), \
                     patch("api.base.SessionManager.get_session", return_value=self.session), \
                     patch("quiz_assistant.generate_suggestion") as ai:
                    self.assertEqual(self.review(base.JOB, reviewer), "pending_confirmation")
                self.assertTrue(self.co.auto_quizzes)
                self.session.get.assert_not_called()
                self.session.post.assert_not_called()
                ai.assert_not_called()
                reviewer.assert_not_called()

    def test_pending_submission_does_not_enable_auto_in_default_review_mode(self):
        self.co.auto_quizzes = False
        SubmissionLedger(self.profile).record("course:class:work-123", "acknowledged")
        with patch("quiz_assistant.read_settings", return_value=base.SETTINGS):
            self.assertEqual(self.review(base.JOB, Mock()), "pending_confirmation")
        self.assertFalse(self.co.auto_quizzes)

    def test_explicit_auto_choice_continues_across_courses_in_the_same_run(self):
        self.co.auto_quizzes = True
        reviewer = Mock()
        with patch("quiz_assistant.read_settings", return_value=base.SETTINGS), \
             patch("api.base.SessionManager.get_session", return_value=self.session), \
             patch("quiz_assistant.generate_suggestion", return_value=(["B"], "解释")):
            for course in (base.COURSE, {**base.COURSE, "courseId": "another-course", "clazzId": "another-class"}):
                self.assertEqual(reviewed_quiz(None, course, base.JOB, base.INFO,
                                              self.profile, self.co, reviewer), "auto_submitted")
        reviewer.assert_not_called()
        self.assertEqual(self.session.post.call_count, 2)
        self.assertTrue(self.co.auto_quizzes)

    def test_incompatible_quiz_does_not_reopen_review_for_next_quiz(self):
        self.co.auto_quizzes = True
        reviewer = Mock()
        invalid = fixture.response(200, base.page(base.card("1", "2",
            '<textarea name="unconfirmed"></textarea>', options=False)))
        self.session.get.side_effect = [invalid, fixture.response(200, base.page(base.card()))]
        with patch("quiz_assistant.read_settings", return_value=base.SETTINGS), \
             patch("api.base.SessionManager.get_session", return_value=self.session), \
             patch("quiz_assistant.generate_suggestion", return_value=(["B"], "解释")):
            with self.assertRaisesRegex(QuizError, "不兼容"):
                self.review(base.JOB, reviewer)
            self.assertEqual(self.review({**base.JOB, "jobid": "work-after-error"}, reviewer), "auto_submitted")
        reviewer.assert_not_called()
        self.assertTrue(self.co.auto_quizzes)
        self.session.post.assert_called_once()

    def test_auto_failure_remains_pending_without_manual_recovery_dialog(self):
        co = SafetyCoordinator(self.profile, dialog=Mock())
        co.auto_quizzes = True
        task = SimpleNamespace(point={"title": "合成章节"}, course=base.COURSE)
        from manual_tasks import ManualTasksRequired
        issue = ManualTasksRequired([{"kind": "workid", "name": "合成失败测验", "unread": True}])
        self.assertEqual(co.manual_tasks(issue, task, 0), "skip")
        co.dialog.assert_not_called()
        self.assertTrue(co.auto_quizzes)


class AutoSchedulerTests(unittest.TestCase):
    setUp = fixture.RuntimeSafetyTests.setUp
    tearDown = fixture.RuntimeSafetyTests.tearDown

    def test_ai_error_keeps_legacy_scheduler_automatic_for_next_chapter(self):
        main = fixture.main
        previous = (decode._process_attachment_cards, main.process_job, main.process_chapter,
                    getattr(main, "process_pending_quizzes", None))
        def restore():
            decode._process_attachment_cards, main.process_job, main.process_chapter = previous[:3]
            if previous[3] is None:
                del main.process_pending_quizzes
            else:
                main.process_pending_quizzes = previous[3]
        self.addCleanup(restore)
        self.co.auto_quizzes = True
        install_manual_tasks(main, self.profile, self.co)
        course = {**base.COURSE, "cpi": "cpi"}
        points = [{"id": "first", "title": "第一章", "has_finished": False},
                  {"id": "second", "title": "第二章", "has_finished": False}]
        tasks = [main.ChapterTask(index=i, course=course, point=point) for i, point in enumerate(points)]
        cx = SimpleNamespace(rate_limiter=Mock(), get_job_list=Mock(side_effect=lambda c, p:
            ([{**base.JOB, "jobid": "work-" + p["id"]}], {**base.INFO, "knowledgeid": p["id"]})),
            get_course_point=Mock(return_value={"points": [points[0], {**points[1], "has_finished": True}]}))
        processor = make_processor(main.JobProcessor, main, self.co)(cx, tasks, {"jobs": 1, "speed": 1.0})
        session = Mock()
        session.get.return_value = fixture.response(200, base.page(base.card()))
        session.post.return_value = fixture.response(200, '{"status":true}', "application/json")
        with patch("quiz_assistant.read_settings", return_value=base.SETTINGS), \
             patch("api.base.SessionManager.get_session", return_value=session), \
             patch("quiz_assistant.generate_suggestion", side_effect=[QuizError("合成 AI 失败"), (["B"], "解释")]), \
             patch("quiz_dialog.ask_quiz") as reviewer, self.assertRaises(IncompleteRun):
            processor.run()
        reviewer.assert_not_called()
        self.co.dialog.assert_not_called()
        self.assertTrue(self.co.auto_quizzes)
        session.post.assert_called_once()
        self.assertEqual(processor.failed_tasks, [tasks[0]])
        self.assertEqual(processor.completed_tasks, [tasks[1]])
        self.assertIn("合成 AI 失败", self.co.pending[0]["manual_tasks"][0])

    def test_duplicate_cards_and_unconfirmed_chapter_do_not_prompt_for_next_chapter(self):
        main = fixture.main
        previous = (decode._process_attachment_cards, main.process_job, main.process_chapter,
                    getattr(main, "process_pending_quizzes", None))

        def restore():
            decode._process_attachment_cards, main.process_job, main.process_chapter = previous[:3]
            if previous[3] is None:
                del main.process_pending_quizzes
            else:
                main.process_pending_quizzes = previous[3]

        self.addCleanup(restore)
        co = SafetyCoordinator(self.profile, dialog=Mock(return_value="stop"))
        install_manual_tasks(main, self.profile, co)
        course = {**base.COURSE, "cpi": "cpi"}
        points = [{"id": "first", "title": "第一章", "has_finished": False},
                  {"id": "second", "title": "第二章", "has_finished": False}]
        tasks = [main.ChapterTask(index=i, course=course, point=point) for i, point in enumerate(points)]
        first, second = dict(base.JOB), {**base.JOB, "jobid": "work-second"}

        def jobs(_course, point):
            return ([first, first] if point["id"] == "first" else [second]), {
                **base.INFO, "knowledgeid": point["id"]}

        cx = SimpleNamespace(rate_limiter=Mock(), get_job_list=Mock(side_effect=jobs),
            get_course_point=Mock(side_effect=[{"points": points},
                {"points": [points[0], {**points[1], "has_finished": True}]}]))
        processor = make_processor(main.JobProcessor, main, co)(cx, tasks, {"jobs": 1, "speed": 1.0})
        session = Mock()
        session.get.return_value = fixture.response(200, base.page(base.card()))
        session.post.return_value = fixture.response(200, '{"status":true}', "application/json")
        reviewer = Mock(return_value="auto_all_quizzes")
        with patch("quiz_assistant.read_settings", return_value=base.SETTINGS), \
             patch("api.base.SessionManager.get_session", return_value=session), \
             patch("quiz_assistant.generate_suggestion", return_value=(["B"], "解释")), \
             patch("quiz_dialog.ask_quiz", reviewer), \
             patch("runtime_safety.time.sleep") as sleep, self.assertRaises(IncompleteRun):
            processor.run()
        reviewer.assert_called_once()
        co.dialog.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(session.post.call_count, 2)
        self.assertTrue(co.auto_quizzes)
        self.assertEqual(processor.failed_tasks, [tasks[0]])
        self.assertEqual(processor.completed_tasks, [tasks[1]])
        self.assertEqual(len(co.pending), 1)
        self.assertEqual(co.pending[0]["chapter_id"], "first")
        self.assertFalse(points[0]["has_finished"])


if __name__ == "__main__":
    unittest.main()
