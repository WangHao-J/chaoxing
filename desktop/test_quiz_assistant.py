"""Offline reviewed-answer tests: synthetic questions, sessions and API keys only."""
import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_runtime_safety as fixture
from api import decode
from quiz_assistant import (Question, QuizError, api_url, read_settings, save_settings,
    parse_work, submission_form, generate_suggestion, reviewed_quiz, SubmissionLedger)
from quiz_dialog import ReviewDialog, SettingsDialog
from manual_tasks import install_manual_tasks, ManualTasksRequired
from account_manager import account_python


SETTINGS = {"enabled": True, "endpoint": "https://ai.example/v1", "model": "test-model", "key": "synthetic-api-key"}
COURSE = {"courseId": "course", "clazzId": "class", "title": "测试课程"}
JOB = {"type": "workid", "jobid": "work-123", "enc": "synthetic-enc", "name": "测试测验"}
INFO = {"knowledgeid": "chapter", "ktoken": "synthetic-token", "cpi": "cpi"}


def card(qid="1", kind="0", fields=None, title="测试题目", options=True):
    if fields is None:
        fields = f'<input type="hidden" name="answer{qid}" value="">'
    choices = '<ul><li aria-label="A. 选项甲"></li><li aria-label="B. 选项乙"></li></ul>' if options else ''
    return f'<div class="singleQuesId" data="{qid}"><div class="TiMu" data="{kind}"><div class="Zy_TItle">{title}</div>{choices}{fields}</div></div>'


def page(*cards):
    return '<form><input name="csrf" value="synthetic-csrf"><input name="pyFlag" value="1">' + ''.join(cards) + '</form>'


def ai_reply(values, explanation="测试解释"):
    return fixture.response(200, json.dumps({"choices": [{"message": {"content": json.dumps(
        {"values": values, "explanation": explanation}, ensure_ascii=False)}}]}), "application/json")


class QuizTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="quiz-test-", dir=fixture.ROOT / "temp")
        self.profile = SimpleNamespace(directory=Path(self.temporary.name), name="测试账户")
        self.co = SimpleNamespace(state=Mock(), issue=None)
        self.session = Mock()
        self.session.get.return_value = fixture.response(200, page(card()))
        self.session.post.return_value = fixture.response(200, '{"status":true}', "application/json")
        self.addCleanup(self.temporary.cleanup)

    def run_quiz(self, reviewer, settings=SETTINGS):
        with patch("quiz_assistant.read_settings", return_value=settings), \
             patch("api.base.SessionManager.get_session", return_value=self.session):
            return reviewed_quiz(None, COURSE, JOB, INFO, self.profile, self.co, reviewer)

    def test_windows_encrypted_key_roundtrip_and_account_isolation(self):
        save_settings(self.profile, SETTINGS)
        self.assertEqual(read_settings(self.profile), SETTINGS)
        raw = (self.profile.directory / "ai_quiz.json").read_text(encoding="utf-8")
        self.assertNotIn(SETTINGS["key"], raw)
        other = self.profile.directory / "other"
        other.mkdir()
        self.assertFalse(read_settings(SimpleNamespace(directory=other))["enabled"])

    def test_settings_disabled_by_default_and_missing_fields_refused(self):
        self.assertFalse(read_settings(self.profile)["enabled"])
        with self.assertRaises(QuizError):
            save_settings(self.profile, {"enabled": True})
        self.assertFalse((self.profile.directory / "ai_quiz.json").exists())

    def test_invalid_saved_settings_are_sanitized(self):
        (self.profile.directory / "ai_quiz.json").write_text('[]', encoding="utf-8")
        with self.assertRaises(QuizError):
            read_settings(self.profile)

    def test_endpoint_requires_tls_except_local_services(self):
        self.assertEqual(api_url("https://ai.example/v1/"), "https://ai.example/v1/chat/completions")
        self.assertEqual(api_url("http://localhost:8080/v1"), "http://localhost:8080/v1/chat/completions")
        for address in ("http://ai.example/v1", "https://key@ai.example/v1", "https://ai.example/?key=secret", "ftp://localhost"):
            with self.subTest(address=address), self.assertRaises(QuizError):
                api_url(address)

    def test_single_multiple_judgement_shortanswer_actual_fields(self):
        questions, form = parse_work(page(card("1", "0"), card("2", "1"), card("3", "3"),
            card("4", "4", '<textarea name="answer4"></textarea>', options=False)))
        submitted = submission_form(questions, form, {"1": ["b"], "2": ["B", "A"], "3": ["对"], "4": ["我的答案"]})
        self.assertEqual([submitted["answer" + str(i)] for i in range(1, 5)], ["B", "AB", "true", "我的答案"])
        self.assertEqual(submitted["csrf"], "synthetic-csrf")
        self.assertEqual(submitted["answerwqbid"], "1,2,3,4,")
        self.assertEqual(submitted["pyFlag"], "")

    def test_fifteen_blanks_preserve_actual_field_names_and_order(self):
        fields = ''.join(f'<textarea name="answer9_{i}"></textarea>' for i in range(15))
        questions, form = parse_work(page(card("9", "2", fields, options=False)))
        self.assertEqual(questions[0].blank_count, 15)
        values = [f"word{i}" for i in range(15)]
        result = submission_form(questions, form, {"9": values})
        self.assertEqual([result[f"answer9_{i}"] for i in range(15)], values)
        self.assertNotIn("answer9", result)

    def test_hidden_aggregate_and_unknown_fields_disable_program_submission(self):
        for fields in ('<input type="hidden" name="answer1"><textarea name="answer1_0"></textarea>',
                       '<textarea name="unconfirmed0"></textarea><textarea name="unconfirmed1"></textarea>',
                       '<textarea name="answer1_0"></textarea><input name="tiankongsize1" value="2">'):
            with self.subTest(fields=fields):
                questions, form = parse_work(page(card("1", "2", fields, options=False)))
                self.assertTrue(questions[0].unsupported)
                with self.assertRaises(QuizError):
                    submission_form(questions, form, {"1": ["a"] * questions[0].blank_count})

    def test_empty_duplicate_and_unknown_question_pages_fail_closed(self):
        for html in ("<html>登录</html>", page(), page(card(), card())):
            with self.subTest(html=html), self.assertRaises(QuizError):
                parse_work(html)
        questions, _ = parse_work(page(card(kind="99")))
        self.assertTrue(questions[0].unsupported)

    def test_ai_receives_only_question_data_and_never_cookies(self):
        question = Question("1", "题目", "completion", "", ["answer1_0", "answer1_1"], 2)
        post = Mock(return_value=ai_reply(["one", "two"]))
        self.assertEqual(generate_suggestion(SETTINGS, question, post)[0], ["one", "two"])
        kwargs = post.call_args.kwargs
        self.assertEqual(json.loads(kwargs["json"]["messages"][1]["content"]),
            {"question": "题目", "type": "completion", "options": "", "blank_count": 2})
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer " + SETTINGS["key"]})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["verify"])

    def test_ai_wrong_blank_count_bad_choices_and_errors_are_rejected(self):
        question = Question("1", "题目", "completion", "", ["a", "b"], 2)
        with self.assertRaises(QuizError):
            generate_suggestion(SETTINGS, question, Mock(return_value=ai_reply(["one"])))
        question = Question("1", "题目", "single", "A. 第一\nB. 第二", ["answer1"])
        with self.assertRaises(QuizError):
            generate_suggestion(SETTINGS, question, Mock(return_value=ai_reply(["C"])))
        with self.assertRaises(QuizError) as caught:
            generate_suggestion(SETTINGS, question, Mock(side_effect=RuntimeError(SETTINGS["key"])))
        self.assertNotIn(SETTINGS["key"], str(caught.exception))

    def test_disabled_and_deferred_quizzes_never_post(self):
        self.assertEqual(self.run_quiz(Mock(), {"enabled": False}), "disabled")
        self.session.get.assert_not_called()
        self.assertEqual(self.run_quiz(Mock(return_value="defer")), "defer")
        self.session.post.assert_not_called()

    def test_explicit_review_submits_once_and_persists_before_post(self):
        def post(*args, **kwargs):
            self.assertEqual(SubmissionLedger(self.profile).data["course:class:work-123"]["state"], "uncertain")
            self.assertEqual(kwargs["data"]["answer1"], "B")
            self.assertFalse(kwargs["allow_redirects"])
            return fixture.response(200, '{"status":true}', "application/json")
        self.session.post.side_effect = post
        def reviewer(account, course, questions, generate, submit):
            self.session.post.assert_not_called()
            return submit({"1": ["B"]})
        self.assertEqual(self.run_quiz(reviewer), "submitted")
        self.assertEqual(self.run_quiz(Mock()), "pending_confirmation")
        self.assertEqual(self.session.post.call_count, 1)

    def test_timeout_blocks_second_post_in_same_window_and_after_restart(self):
        self.session.post.side_effect = TimeoutError("synthetic")
        def reviewer(account, course, questions, generate, submit):
            for _ in range(2):
                with self.assertRaises(QuizError):
                    submit({"1": ["A"]})
            return "defer"
        self.run_quiz(reviewer)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.run_quiz(Mock()), "pending_confirmation")

    def test_invalid_answers_never_post_or_lock_submission(self):
        def reviewer(account, course, questions, generate, submit):
            with self.assertRaises(QuizError):
                submit({"1": ["Z"]})
            self.assertFalse(SubmissionLedger(self.profile).data)
            return "defer"
        self.run_quiz(reviewer)
        self.session.post.assert_not_called()

    def test_explicit_rejection_requires_a_new_review_to_try_again(self):
        self.session.post.return_value = fixture.response(200, '{"status":false}', "application/json")
        def reviewer(account, course, questions, generate, submit):
            with self.assertRaises(QuizError):
                submit({"1": ["A"]})
            return "defer"
        self.run_quiz(reviewer)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(SubmissionLedger(self.profile).data["course:class:work-123"]["state"], "rejected")

    def test_corrupt_ledger_does_not_risk_resubmission(self):
        (self.profile.directory / "quiz_submissions.json").write_text('broken', encoding="utf-8")
        with self.assertRaises(QuizError):
            self.run_quiz(Mock())
        self.session.post.assert_not_called()

    def test_approved_submission_stays_pending_until_platform_recheck(self):
        main = fixture.main
        old = decode._process_attachment_cards, main.process_job, main.process_chapter
        def restore():
            decode._process_attachment_cards, main.process_job, main.process_chapter = old
        self.addCleanup(restore)
        install_manual_tasks(main, self.profile, self.co)
        cx = SimpleNamespace(rate_limiter=Mock(), get_job_list=Mock(return_value=([JOB], INFO)),
            tiku=SimpleNamespace(DISABLE=True, SUBMIT=False), study_work=Mock())
        with patch("quiz_assistant.reviewed_quiz", return_value="submitted"), self.assertRaises(ManualTasksRequired) as caught:
            main.process_chapter(cx, COURSE, {"id": "chapter", "title": "章节", "has_finished": False}, 1)
        self.assertIn("等待平台", caught.exception.jobs[0]["name"])
        cx.study_work.assert_not_called()
        self.assertFalse(cx.tiku.SUBMIT)

    def test_manager_selects_available_d_drive_python(self):
        root = self.profile.directory
        self.assertEqual(account_python(root), root / "python/python.exe")
        virtual = root / ".venv/Scripts/python.exe"
        virtual.parent.mkdir(parents=True)
        virtual.touch()
        self.assertEqual(account_python(root), virtual)


class QuizWindowTests(unittest.TestCase):
    def setUp(self):
        self.window = tk.Tk()
        self.window.withdraw()
        self.generate, self.submit = Mock(), Mock(return_value="submitted")

    def tearDown(self):
        self.window.destroy()

    def dialog(self, question=None):
        question = question or Question("1", "测试题目" * 200, "completion", "", [f"answer1_{i}" for i in range(15)], 15)
        dialog = ReviewDialog(self.window, "测试账户", "测试课程", [question], self.generate, self.submit)
        self.window.geometry("740x540+10000+10000")
        self.window.deiconify()
        self.window.update()
        self.addCleanup(lambda: self.window.after_cancel(dialog.timer) if dialog.timer else None)
        return dialog

    def test_initial_window_never_generates_or_submits(self):
        dialog = self.dialog()
        self.generate.assert_not_called()
        self.submit.assert_not_called()
        self.assertEqual(len(dialog.variables), 15)
        self.assertEqual(str(dialog.submit_button.cget("state")), "disabled")
        self.assertGreater(dialog.canvas.bbox("all")[3], dialog.canvas.winfo_height())
        for button in (dialog.generate_button, dialog.copy_button, dialog.defer_button, dialog.submit_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), self.window.winfo_rootx() + self.window.winfo_width())
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), self.window.winfo_rooty() + self.window.winfo_height())

    def test_complete_answers_still_require_confirmation_and_edits_reset_it(self):
        dialog = self.dialog()
        for index, variable in enumerate(dialog.variables):
            variable.set(str(index))
        dialog.confirm_submit()
        self.submit.assert_not_called()
        dialog.reviewed.set(True)
        dialog.update_submit()
        self.assertEqual(str(dialog.submit_button.cget("state")), "normal")
        dialog.variables[0].set("修改")
        self.assertFalse(dialog.reviewed.get())
        self.assertEqual(str(dialog.submit_button.cget("state")), "disabled")

    def test_unsupported_format_allows_copy_but_never_submit(self):
        question = Question("1", "题目", "completion", "", [], 2, "请复制到网页完成")
        dialog = self.dialog(question)
        for variable, value in zip(dialog.variables, ["first", "second"]):
            variable.set(value)
        dialog.reviewed.set(True)
        dialog.confirm_submit()
        self.submit.assert_not_called()
        dialog.copy_answers()
        self.assertIn("1. first\n2. second", self.window.clipboard_get())

    def test_submit_uses_reviewed_snapshot_only_after_button_click(self):
        dialog = self.dialog(Question("1", "题目", "single", "A. 第一\nB. 第二", ["answer1"]))
        dialog.variables[0].set("B")
        dialog.reviewed.set(True)
        with patch("quiz_dialog.start_job") as start:
            dialog.confirm_submit()
        self.submit.assert_not_called()
        self.assertEqual(start.call_args.args[1], "submit")
        start.call_args.args[2]()
        self.submit.assert_called_once_with({"1": ["B"]})

    def test_native_settings_masks_key_and_does_not_connect_on_open(self):
        profile = SimpleNamespace(name="测试账户")
        with patch("quiz_dialog.read_settings", return_value=SETTINGS), patch("quiz_dialog.generate_suggestion") as generate:
            dialog = SettingsDialog(self.window, profile)
            self.window.geometry("600x480+10000+10000")
            self.window.deiconify()
            self.window.update()
            self.addCleanup(lambda: self.window.after_cancel(dialog.timer))
            generate.assert_not_called()
            entries = []
            def visit(widget):
                for child in widget.winfo_children():
                    if child.winfo_class() == "TEntry":
                        entries.append(child)
                    visit(child)
            visit(self.window)
            self.assertEqual(entries[2].cget("show"), "●")
            for button in (dialog.test_button, dialog.save_button):
                self.assertTrue(button.winfo_ismapped())
                self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), self.window.winfo_rooty() + self.window.winfo_height())


if __name__ == "__main__":
    unittest.main()
