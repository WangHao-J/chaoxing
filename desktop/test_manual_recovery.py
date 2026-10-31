"""Offline tests for human verification and external task completion checks."""
import io
import json
import unittest
import tempfile
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PIL import Image

import test_runtime_safety as fixture
from test_runtime_safety import response, main, Chaoxing
from api import decode
from manual_tasks import ManualTasksRequired, install_manual_tasks, safe_link, COURSE_PORTAL
from manual_verification import ManualVerifier, VerificationUnavailable
from runtime_safety import AccessRestricted, IncompleteRun, make_processor, redact_log
from recovery_dialog import RecoveryDialog


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.request = Mock()
        self.verifier = ManualVerifier(self.session, self.request, "https://mooc1.chaoxing.com/test",
                                      {"User-Agent": "same-agent", "Cookie": "never-copy"})

    def image(self):
        buffer = io.BytesIO()
        Image.new("RGB", (120, 40), "white").save(buffer, "PNG")
        result = response(200, content_type="image/png")
        result._content = buffer.getvalue()
        return result

    def test_fetch_and_human_submit_use_original_session_and_no_redirects(self):
        reply = response(302)
        reply.headers["Location"] = "/mycourse/studentstudy"
        self.request.side_effect = [self.image(), reply]
        self.assertTrue(self.verifier.fetch().startswith(b"\x89PNG"))
        with patch("api.cookies.save_cookies") as save:
            self.assertTrue(self.verifier.submit("aB12"))
        save.assert_called_once_with(self.session)
        self.assertEqual(self.request.call_args.kwargs["params"]["ucode"], "aB12")
        for call in self.request.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["timeout"], 15)
            self.assertTrue(call.kwargs["verify"])
            self.assertNotIn("Cookie", call.kwargs["headers"])
            self.assertEqual(call.kwargs["headers"]["User-Agent"], "same-agent")

    def test_non_image_is_an_unavailable_challenge(self):
        self.request.return_value = response(200, "<html>login</html>")
        with self.assertRaises(VerificationUnavailable):
            self.verifier.fetch()
        self.assertFalse(self.verifier.loaded)

    def test_login_and_untrusted_redirects_are_not_verification_success(self):
        for location in ("https://evil.example/", "/login", "/html/processVerify.ac", ""):
            self.verifier.loaded = True
            self.verifier.submissions = 0
            reply = response(302)
            reply.headers["Location"] = location
            self.request.return_value = reply
            with self.subTest(location=location), patch("api.cookies.save_cookies") as save:
                self.assertFalse(self.verifier.submit("1234"))
                save.assert_not_called()

    def test_no_submission_without_user_code_or_a_loaded_challenge(self):
        with self.assertRaises(VerificationUnavailable):
            self.verifier.submit("1234")
        self.verifier.loaded = True
        with self.assertRaises(VerificationUnavailable):
            self.verifier.submit("")
        self.request.assert_not_called()

    def test_unknown_host_cannot_receive_session_credentials(self):
        with self.assertRaises(VerificationUnavailable):
            ManualVerifier(self.session, self.request, "https://evil.example/")


class ManualTaskTests(unittest.TestCase):
    setUp = fixture.RuntimeSafetyTests.setUp
    tearDown = fixture.RuntimeSafetyTests.tearDown
    task = fixture.RuntimeSafetyTests.task
    session = fixture.RuntimeSafetyTests.session
    processor = fixture.RuntimeSafetyTests.processor
    def card(self, **extra):
        return {"type": "hyperlink", "job": True,
                "property": {"title": "外部视频", "href": "https://b23.tv/example"}, **extra}

    def install(self):
        old = (decode._process_attachment_cards, main.process_job, main.process_chapter)
        def restore():
            decode._process_attachment_cards, main.process_job, main.process_chapter = old
        self.addCleanup(restore)
        install_manual_tasks(main)

    def test_real_decoder_and_chapter_keep_external_job_out_of_empty_page_path(self):
        self.install()
        html = "mArg=" + json.dumps({"attachments": [self.card()], "defaults": {}}, ensure_ascii=False) + ";"
        session = Mock()
        session.get.return_value = response(200, html)
        cx = object.__new__(Chaoxing)
        cx.rate_limiter = Mock()
        cx.study_emptypage = Mock()
        task = self.task()
        with patch("api.base.SessionManager.get_session", return_value=session), \
             self.assertRaises(ManualTasksRequired) as caught:
            main.process_chapter(cx, task.course, task.point, 1)
        self.assertEqual(caught.exception.jobs[0]["name"], "外部视频")
        cx.study_emptypage.assert_not_called()

    def test_completed_and_optional_links_do_not_become_required_tasks(self):
        self.install()
        with patch.object(decode.logger, "warning") as warning:
            jobs = decode._process_attachment_cards([self.card(isPassed=True), self.card(job=False)])
        self.assertEqual(jobs, [])
        warning.assert_not_called()

    def test_unknown_required_card_is_preserved_without_raw_token_dump(self):
        self.install()
        with patch.object(decode.logger, "warning") as warning:
            jobs = decode._process_attachment_cards([self.card(type="future-card", enc="synthetic-secret")])
        self.assertEqual(jobs[0]["type"], "manual")
        self.assertNotIn("synthetic-secret", str(jobs))
        warning.assert_not_called()

    def test_supported_video_parser_is_preserved_alongside_manual_links(self):
        self.install()
        jobs = decode._process_attachment_cards([self.card(), {"type": "video", "job": True,
            "mid": "mid", "jobid": "job", "objectId": "object", "property": {"name": "视频"}}])
        self.assertEqual([job["type"] for job in jobs], ["manual", "video"])
        self.assertEqual(jobs[1]["objectid"], "object")

    def test_disabled_quiz_is_kept_pending_instead_of_upstream_success(self):
        self.install()
        card = {"type": "workid", "job": True, "jobid": "work-test",
                "property": {"title": "词汇填空测验"}}
        cx = SimpleNamespace(rate_limiter=Mock(),
            get_job_list=Mock(return_value=(decode._process_attachment_cards([card]), {})),
            tiku=SimpleNamespace(DISABLE=True, SUBMIT=False), study_work=Mock(return_value=main.StudyResult.SUCCESS))
        task = self.task()
        with self.assertRaises(ManualTasksRequired) as caught:
            main.process_chapter(cx, task.course, task.point, 1)
        cx.study_work.assert_not_called()
        self.assertIn("词汇填空测验", caught.exception.jobs[0]["name"])
        self.assertIn("答题服务未启用", caught.exception.jobs[0]["name"])

    def test_draft_only_quiz_does_not_query_or_submit(self):
        self.install()
        cx = SimpleNamespace(rate_limiter=Mock(), tiku=SimpleNamespace(DISABLE=False, SUBMIT=False),
            get_job_list=Mock(return_value=([{"type": "workid", "name": "测验", "jobid": "work-test"}], {})),
            study_work=Mock())
        task = self.task()
        with self.assertRaises(ManualTasksRequired) as caught:
            main.process_chapter(cx, task.course, task.point, 1)
        cx.study_work.assert_not_called()
        self.assertIn("自动提交未启用", caught.exception.jobs[0]["name"])

    def test_explicitly_enabled_quiz_keeps_existing_handler(self):
        self.install()
        cx = SimpleNamespace(tiku=SimpleNamespace(DISABLE=False, SUBMIT=True),
                             study_work=Mock(return_value=main.StudyResult.SUCCESS))
        job = {"type": "workid", "jobid": "work-test"}
        course = {"title": "课程"}
        self.assertEqual(main.process_job(cx, course, job, {}, 1), main.StudyResult.SUCCESS)
        cx.study_work.assert_called_once_with(course, job, {})

    def test_server_completed_quiz_does_not_become_manual_task(self):
        self.install()
        self.assertEqual(decode._process_attachment_cards([
            {"type": "workid", "job": True, "isPassed": True, "jobid": "work-test"}]), [])

    def test_unfinished_links_cannot_be_cleared_by_clicking_recheck(self):
        self.co.dialog.return_value = "retry"
        task = self.task()
        processor = self.processor([task])
        processor.chaoxing.get_course_point.return_value = {"points": [task.point]}
        with patch.object(main, "process_chapter", side_effect=ManualTasksRequired([{"name": "外部视频"}])) as process:
            from runtime_safety import RunNeedsAttention
            with self.assertRaises(RunNeedsAttention):
                processor.run()
        self.assertEqual(process.call_count, 3)
        self.assertFalse(self.co.completed)
        self.assertEqual(self.co.pending[0]["manual_tasks"], ["外部视频"])

    def test_skip_continues_other_chapters_and_still_prevents_completion(self):
        self.co.dialog.return_value = "skip"
        first, second = self.task(), self.task(1)
        processor = self.processor([first, second])
        with patch.object(main, "process_chapter", side_effect=[
                ManualTasksRequired([{"name": "外部视频"}]), main.ChapterResult.SUCCESS]) as process:
            with self.assertRaises(IncompleteRun):
                processor.run()
        self.assertEqual(process.call_count, 2)
        self.assertEqual(processor.failed_tasks, [first])
        self.assertFalse(self.co.completed)
        self.assertEqual(self.co.pending[0]["manual_tasks"], ["外部视频"])

    def test_manual_recheck_reads_server_chapter_status(self):
        self.co.dialog.return_value = "retry"
        task = self.task()
        processor = self.processor([task])
        processor.chaoxing.get_course_point.return_value = {"points": [{**task.point, "has_finished": True}]}
        with patch.object(main, "process_chapter", side_effect=[
                ManualTasksRequired([{"name": "外部视频"}]), main.ChapterResult.SUCCESS]):
            processor.run()
        processor.chaoxing.get_course_point.assert_called_once_with("course", "class", "cpi")
        self.assertTrue(task.point["has_finished"])
        self.assertTrue(self.co.completed)

    def test_generic_403_offers_no_phantom_captcha(self):
        session, _ = self.session(response(403, "Forbidden"))
        with self.assertRaises(AccessRestricted) as caught:
            session.get("https://mooc1.chaoxing.com/test")
        self.assertIsNone(caught.exception.verifier)

    def test_actual_challenge_offers_same_session_manual_verifier(self):
        session, raw = self.session(response(403, "<img src='/processVerifyPng.ac'>"))
        with self.assertRaises(AccessRestricted) as caught:
            session.get("https://mooc1.chaoxing.com/test")
        verifier = caught.exception.verifier
        self.assertIsNotNone(verifier)
        self.assertIs(verifier.session, session)
        self.assertIs(verifier.request, raw)
        self.assertEqual(raw.call_count, 1)

    def test_raw_token_fields_are_redacted(self):
        record = {"message": "{'enc': 'secret-1', 'jtoken': 'secret-2', 'ucode': '1234'}"}
        redact_log(record)
        for secret in ("secret-1", "secret-2", "1234"):
            self.assertNotIn(secret, record["message"])


class ManualDialogTests(unittest.TestCase):
    def setUp(self):
        self.window = tk.Tk()
        self.window.withdraw()

    def tearDown(self):
        if hasattr(self, "dialog"):
            self.dialog.stop()
        self.window.destroy()

    def test_browser_navigation_uses_official_portal_and_keeps_chapter_names_visible(self):
        self.dialog = RecoveryDialog(self.window, "测试", "手动任务", "章节", 0, True,
            course_title="测试课程", manual_jobs=[{"name": "外部视频", "href": "https://b23.tv/example"}])
        with patch("recovery_dialog.webbrowser.open") as browser:
            self.dialog.course_button.invoke()
        browser.assert_called_once_with("https://i.chaoxing.com/")
        self.assertNotIn("studentstudy", browser.call_args.args[0])
        self.dialog.copy_button.invoke()
        self.assertEqual(self.window.clipboard_get(), "课程：测试课程\n章节：章节")
        self.dialog.skip_button.invoke()
        self.assertEqual(self.dialog.result, "skip")

    def test_old_pending_records_can_open_portal_without_any_signed_url_or_ids(self):
        from account_manager import AccountManager
        with tempfile.TemporaryDirectory(dir=fixture.ROOT / "temp") as directory:
            profile = SimpleNamespace(directory=Path(directory), name="测试", id="test")
            (profile.directory / "pending_tasks.json").write_text(json.dumps({"tasks": [{
                "course_title": "原课程", "chapter_title": "原章节", "manual_tasks": ["外部链接"]}]}), encoding="utf-8")
            store = Mock()
            store.list.return_value = [profile]
            store.get.return_value = profile
            manager = AccountManager(self.window, store)
            try:
                manager.open_pending()
                popup = next(child for child in self.window.winfo_children() if isinstance(child, tk.Toplevel))
                widgets = list(popup.winfo_children())
                buttons = []
                while widgets:
                    widget = widgets.pop()
                    widgets.extend(widget.winfo_children())
                    if widget.winfo_class() == "TButton":
                        buttons.append(widget)
                with patch("account_manager.webbrowser.open") as browser:
                    next(button for button in buttons if button.cget("text") == "打开课程入口").invoke()
                browser.assert_called_once_with("https://i.chaoxing.com/")
                next(button for button in buttons if "复制" in button.cget("text")).invoke()
                self.assertEqual(popup.clipboard_get(), "课程：原课程\n章节：原章节")
                popup.destroy()
            finally:
                self.window.after_cancel(manager.timer)

    def test_bad_external_url_cannot_be_opened(self):
        for url in ("javascript:alert(1)", "file:///C:/test", "https://user:password@example.com"):
            self.assertEqual(safe_link(url), "")

    def test_captcha_dialog_requires_manual_fetch_and_submission(self):
        verifier = Mock()
        self.dialog = RecoveryDialog(self.window, "测试", "图片验证", "章节", 0, True, verifier=verifier)
        verifier.fetch.assert_not_called()
        verifier.submit.assert_not_called()
        self.dialog.submit()
        verifier.submit.assert_not_called()
        self.assertEqual(str(self.dialog.submit_button.cget("state")), "disabled")
        self.dialog.events.put(("submitted", True))
        self.dialog.poll_events()
        self.assertIn("重新检查", self.dialog.feedback.get())
        self.assertEqual(self.dialog.result, "stop")

    def test_filled_answer_is_submitted_only_by_explicit_action(self):
        verifier = Mock()
        self.dialog = RecoveryDialog(self.window, "测试", "图片验证", "章节", 0, True, verifier=verifier)
        self.dialog.answer.set("Ab12")
        verifier.submit.assert_not_called()
        with patch.object(self.dialog, "background", side_effect=lambda operation, kind: operation()):
            self.dialog.submit()
        verifier.submit.assert_called_once_with("Ab12")
        self.assertEqual(self.dialog.answer.get(), "")
        self.assertEqual(self.dialog.result, "stop")

    def test_loading_image_keeps_space_for_bottom_controls(self):
        verifier = Mock()
        self.dialog = RecoveryDialog(self.window, "测试", "图片验证", "章节", 0, True, verifier=verifier)
        self.window.update_idletasks()
        before = self.window.winfo_reqheight()
        buffer = io.BytesIO()
        Image.new("RGB", (300, 120), "white").save(buffer, "PNG")
        self.dialog.events.put(("image", buffer.getvalue()))
        self.dialog.poll_events()
        self.window.update_idletasks()
        self.assertEqual(before, self.window.winfo_reqheight())
        self.assertEqual(str(self.dialog.submit_button.cget("state")), "normal")


if __name__ == "__main__":
    unittest.main(verbosity=2)
