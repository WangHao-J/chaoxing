"""Offline AI reuse, bounded failures, safe drafts and cancellation regressions."""
import json
import threading
import tkinter as tk
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import requests
import test_quiz_assistant as base
import test_runtime_safety as fixture
from quiz_assistant import (AIServiceError, AnswerDrafts, Question, QuizError,
                            generate_all, generate_suggestion, SubmissionLedger)
from quiz_dialog import ReviewDialog


def questions():
    return [Question("1", "第一题", "single", "A. 第一\nB. 第二", ["answer1"]),
            Question("2", "第二题", "shortanswer", "", ["answer2"]),
            Question("3", "第三题", "shortanswer", "", ["answer3"])]


class EfficiencyTests(unittest.TestCase):
    def test_bulk_only_requests_missing_or_invalid_answers_and_preserves_edits(self):
        generate = Mock(return_value=(["补齐"], "说明"))
        result = generate_all(questions(), generate, initial_answers={"1": ["B"], "2": ["人工修改"], "3": [""]})
        self.assertEqual(result["answers"], {"1": ["B"], "2": ["人工修改"], "3": ["补齐"]})
        self.assertEqual(generate.call_args.args[0].id, "3")
        generate.assert_called_once()

    def test_global_service_error_stops_remaining_questions_and_keeps_later_seeds(self):
        generate = Mock(side_effect=AIServiceError("密钥已过期"))
        result = generate_all(questions(), generate, initial_answers={"3": ["已填写"]})
        generate.assert_called_once()
        self.assertEqual(result["answers"], {"3": ["已填写"]})
        self.assertIn("密钥", result["stopped_error"])

    def test_single_parse_failure_continues_with_other_questions(self):
        generate = Mock(side_effect=[QuizError("本题格式不正确"), (["第二题答案"], ""), (["第三题答案"], "")])
        result = generate_all(questions(), generate)
        self.assertEqual(set(result["answers"]), {"2", "3"})
        self.assertEqual(generate.call_count, 3)

    def test_auth_balance_rate_limit_server_failures_are_actionable_and_sanitized(self):
        for code, word in ((401, "Key"), (402, "余额"), (403, "权限"), (429, "额度"), (503, "暂时")):
            with self.subTest(code=code):
                post = Mock(return_value=fixture.response(code, "raw-private-service-body"))
                with self.assertRaises(AIServiceError) as caught:
                    generate_suggestion(base.SETTINGS, questions()[0], post)
                self.assertIn(word, str(caught.exception))
                self.assertNotIn("raw-private", str(caught.exception))
                self.assertEqual(post.call_args.kwargs["timeout"], (8, 60))

    def test_connection_failure_is_global_not_one_request_per_question(self):
        post = Mock(side_effect=requests.exceptions.Timeout("private-token"))
        result = generate_all(questions(), lambda question: generate_suggestion(base.SETTINGS, question, post))
        post.assert_called_once()
        self.assertNotIn("private-token", str(result))
        self.assertTrue(result["stopped_error"])

    def test_completed_answer_checkpoint_is_written_before_next_request(self):
        checkpoint = Mock()
        def generate(question):
            if question.id == "2":
                self.assertEqual(checkpoint.call_args.args[0], {"1": ["A"]})
                raise AIServiceError("已限流")
            return ["A"], "解释"
        result = generate_all(questions(), generate, on_result=checkpoint)
        self.assertEqual(result["answers"], {"1": ["A"]})

    def test_cancel_between_requests_retains_completed_answers_without_further_requests(self):
        cancelled = threading.Event()
        def generate(question):
            cancelled.set()
            return ["A"], "解释"
        generate = Mock(side_effect=generate)
        result = generate_all(questions(), generate, cancelled=cancelled.is_set)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["answers"], {"1": ["A"]})
        generate.assert_called_once()

    def test_batch_deadline_preserves_answers_without_starting_another_request(self):
        generate = Mock()
        result = generate_all(questions(), generate, initial_answers={"1": ["B"]}, deadline_seconds=0)
        self.assertEqual(result["answers"], {"1": ["B"]})
        self.assertTrue(result["stopped_error"])
        generate.assert_not_called()

    def test_cancellation_during_final_request_is_still_reported_as_cancelled(self):
        cancelled = threading.Event()
        def generate(question):
            cancelled.set()
            return ["A"], "解释"
        result = generate_all(questions()[:1], generate, cancelled=cancelled.is_set)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["answers"], {"1": ["A"]})


class DraftTests(unittest.TestCase):
    setUp = base.QuizTests.setUp
    run_quiz = base.QuizTests.run_quiz

    def test_draft_roundtrip_keeps_only_valid_answers_and_question_fingerprint(self):
        draft = AnswerDrafts(self.profile, "course:class:work", questions())
        draft.save({"1": ["b"], "2": ["人工答案"], "3": [""], "token": ["private-token"]}, {"2": "说明"})
        loaded = AnswerDrafts(self.profile, "course:class:work", questions())
        self.assertEqual(loaded.read()[0], {"1": ["B"], "2": ["人工答案"]})
        raw = draft.path.read_text(encoding="utf-8")
        self.assertNotIn("private-token", raw)
        self.assertNotIn("answer1", raw)
        self.assertNotIn("第一题", raw)

    def test_changed_question_content_discards_old_draft_but_token_changes_do_not(self):
        draft = AnswerDrafts(self.profile, "course:class:work", questions())
        draft.save({"1": ["A"]})
        modified_fields = [replace(question, fields=["fresh-form-field"]) for question in questions()]
        self.assertEqual(AnswerDrafts(self.profile, "course:class:work", modified_fields).read()[0], {"1": ["A"]})
        changed = [replace(questions()[0], title="新的题目"), *questions()[1:]]
        self.assertEqual(AnswerDrafts(self.profile, "course:class:work", changed).read(), ({}, {}))

    def test_automatic_choice_reuses_current_manual_answer_without_an_ai_request(self):
        with patch("quiz_assistant.generate_suggestion") as ai:
            result = self.run_quiz(Mock(return_value={"action": "auto_all_quizzes", "answers": {"1": ["B"]}}))
        self.assertEqual(result, "auto_submitted")
        ai.assert_not_called()
        self.assertEqual(self.session.post.call_args.kwargs["data"]["answer1"], "B")
        self.assertTrue(self.co.auto_quizzes)

    def test_restart_reuses_draft_and_acknowledged_submission_clears_it(self):
        def first_review(account, course, question_list, generate, submit):
            generate.save_draft({"1": ["B"]}, {})
            return "defer"
        self.run_quiz(first_review)
        with patch("quiz_assistant.generate_suggestion") as ai:
            self.assertEqual(self.run_quiz(Mock(return_value="auto_all_quizzes")), "auto_submitted")
        ai.assert_not_called()
        self.assertFalse(json.loads((self.profile.directory / "quiz_drafts.json").read_text(encoding="utf-8")))

    def test_uncertain_submission_retains_draft_and_never_reposts(self):
        self.session.post.side_effect = requests.exceptions.Timeout()
        def reviewer(account, course, question_list, generate, submit):
            with self.assertRaises(QuizError):
                submit({"1": ["A"]})
            return "defer"
        self.run_quiz(reviewer)
        self.assertEqual(json.loads((self.profile.directory / "quiz_drafts.json").read_text(encoding="utf-8"))
            ["course:class:work-123"]["answers"], {"1": ["A"]})
        self.assertEqual(self.run_quiz(Mock()), "pending_confirmation")
        self.session.post.assert_called_once()

    def test_failed_automatic_batch_resumes_only_missing_question_after_restart(self):
        self.co.auto_quizzes = True
        self.session.get.return_value = fixture.response(200, base.page(base.card("1"), base.card("2")))
        with patch("quiz_assistant.generate_suggestion", side_effect=[(["A"], "解释"), AIServiceError("请求限流")]), \
             self.assertRaises(QuizError):
            self.run_quiz(Mock())
        self.session.post.assert_not_called()
        self.assertTrue(self.co.auto_quizzes)
        with patch("quiz_assistant.generate_suggestion", return_value=(["B"], "解释")) as ai:
            self.assertEqual(self.run_quiz(Mock(return_value="auto_all_quizzes")), "auto_submitted")
        ai.assert_called_once()
        self.assertEqual(ai.call_args.args[1].id, "2")
        self.assertEqual(self.session.post.call_args.kwargs["data"]["answer1"], "A")
        self.assertEqual(self.session.post.call_args.kwargs["data"]["answer2"], "B")

    def test_corrupt_optional_draft_does_not_modify_submission_ledger(self):
        (self.profile.directory / "quiz_drafts.json").write_text("broken", encoding="utf-8")
        self.assertEqual(AnswerDrafts(self.profile, "x", questions()).read(), ({}, {}))
        self.assertFalse(SubmissionLedger(self.profile).data)


class ReviewEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.window = tk.Tk()
        self.window.withdraw()
        self.generate = Mock(return_value=(["新答案"], "解释"))
        self.generate.initial_answers = {"1": ["B"]}
        self.generate.initial_explanations = {}
        self.generate.save_draft = Mock()
        self.dialog = ReviewDialog(self.window, "模拟账户", "模拟课程", questions(), self.generate, Mock())

    def tearDown(self):
        if self.dialog.timer:
            self.window.after_cancel(self.dialog.timer)
        self.window.destroy()

    def test_open_restores_draft_without_ai_and_auto_returns_manual_edits(self):
        self.assertEqual(self.dialog.variables[0].get(), "B")
        self.dialog.variables[0].set("A")
        with patch.object(self.dialog, "finish"):
            self.dialog.automatic()
        self.assertEqual(self.dialog.result["answers"]["1"], ["A"])
        self.generate.assert_not_called()

    def test_bulk_retry_only_fills_missing_and_keeps_current_edited_answer(self):
        self.dialog.variables[0].set("A")
        with patch("quiz_dialog.start_job") as start:
            self.dialog.suggest_all()
        result = start.call_args.args[2]()
        self.assertEqual(result["answers"]["1"], ["A"])
        self.assertEqual([call.args[0].id for call in self.generate.call_args_list], ["2", "3"])
        self.assertEqual(str(self.dialog.copy_button.cget("state")), "disabled")
        self.assertEqual(str(self.dialog.review_checkbox.cget("state")), "disabled")

    def test_closing_during_generation_requests_cancel_and_waits_for_current_request(self):
        self.dialog.busy, self.dialog.operation = True, "all"
        with patch.object(self.dialog, "finish") as finish:
            self.dialog.defer()
        self.assertTrue(self.dialog.cancel_generation.is_set())
        self.assertTrue(self.dialog.pending_close)
        finish.assert_not_called()
        self.dialog.events.put(("all", {"answers": {"2": ["保留"]}, "explanations": {}, "errors": {}, "cancelled": True}, None))
        with patch.object(self.dialog, "finish") as finish:
            self.dialog.poll()
        self.assertEqual(self.dialog.answers["2"], ["保留"])
        finish.assert_called_once()


if __name__ == "__main__":
    unittest.main()

