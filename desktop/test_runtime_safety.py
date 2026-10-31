"""Offline regressions for restriction handling, session reuse and honest results."""
import io
import json
import os
import sys
import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
(ROOT / "temp").mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT / "app"))
_previous_directory = Path.cwd()
try:
    os.chdir(ROOT / "app")
    import main
finally:
    os.chdir(_previous_directory)
import requests
from api.base import Chaoxing, SessionManager
from runtime_safety import (AccessRestricted, IncompleteRun, RunNeedsAttention, SafetyCoordinator,
                            harden_session_manager, make_processor, redact_log)
from recovery_dialog import RecoveryDialog
from account_manager import profile_status


def response(status, text="", content_type="text/html", retry_after=None):
    result = requests.Response()
    result.status_code = status
    result._content = text.encode("utf-8")
    result.encoding = "utf-8"
    result.headers["Content-Type"] = content_type
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    result.url = "https://mooc1.chaoxing.com/private-token?enc=synthetic-secret"
    return result


class RuntimeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="safety-test-", dir=ROOT / "temp")
        self.profile = SimpleNamespace(directory=Path(self.temporary.name), name="回归测试账户")
        self.co = SafetyCoordinator(self.profile, dialog=Mock(return_value="stop"))
        main.logger.remove()

    def tearDown(self):
        self.temporary.cleanup()

    def session(self, result):
        session = requests.Session()
        raw = Mock(return_value=result)
        session.request = raw
        self.co.guard_session(session)
        self.addCleanup(session.close)
        return session, raw

    def task(self, index=0):
        return main.ChapterTask(index=index,
            course={"courseId": "course", "clazzId": "class", "cpi": "cpi", "title": "课程"},
            point={"id": str(index), "title": f"章节{index}", "has_finished": False})

    def processor(self, tasks):
        klass = make_processor(main.JobProcessor, main, self.co)
        result = klass(Mock(), tasks, {"jobs": 3, "speed": 1.0,
                       "retry_interval": 0.01, "notopen_action": "retry"})
        result.max_tries = 2
        return result

    def test_403_unwinds_real_video_request_without_calling_ocr(self):
        session, raw = self.session(response(403, "<html>请输入验证码</html>"))
        cx = object.__new__(Chaoxing)
        cx.video_log_limiter = Mock()
        cx.get_uid = Mock(return_value="synthetic-id")
        cx.get_enc = Mock(return_value="synthetic-enc")
        job = {"jobid": "job", "objectid": "object", "otherinfo": "", "rt": "1",
               "videoFaceCaptureEnc": "", "attDuration": "", "attDurationEnc": ""}
        captcha = SimpleNamespace(CxCaptcha=Mock(), ocr_init=Mock())
        with patch.dict(sys.modules, {"api.captcha": captcha}), self.assertRaises(AccessRestricted):
            cx.video_progress_log(session, {"clazzId": "class", "courseId": "course", "cpi": "cpi"},
                                  job, {}, "synthetic-token", 60, 30, headers={})
        self.assertEqual(raw.call_count, 1)
        captcha.CxCaptcha.assert_not_called()
        captcha.ocr_init.assert_not_called()

    def test_verification_response_is_not_a_success_even_with_http_200(self):
        for result in (response(200, "<html>processVerifyPng.ac</html>"),
                       response(200, '{"validate": true}', "application/json")):
            with self.subTest(content_type=result.headers["Content-Type"]):
                session, _ = self.session(result)
                with self.assertRaises(AccessRestricted):
                    session.get("https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/test")

    def test_429_honors_retry_after(self):
        session, raw = self.session(response(429, retry_after="120"))
        with self.assertRaises(AccessRestricted) as error:
            session.get("https://mooc1.chaoxing.com/test")
        self.assertEqual(error.exception.retry_after, 120)
        self.assertEqual(raw.call_count, 1)

    def test_redirect_to_login_is_not_a_success_even_with_http_200(self):
        result = response(200, "<html>login</html>")
        result.url = "https://passport2.chaoxing.com/login?refer=course"
        session, _ = self.session(result)
        with self.assertRaises(AccessRestricted) as error:
            session.get("https://mooc1.chaoxing.com/test")
        self.assertEqual(error.exception.status, 401)

    def test_stable_session_preserves_cookies_between_requests(self):
        class IsolatedManager(SessionManager):
            _instance = None
        with patch("api.base.use_cookies", return_value={}):
            harden_session_manager(IsolatedManager, self.co)
            first = IsolatedManager.get_session()
            first.cookies.set("synthetic_session", "test-value")
            second = IsolatedManager.get_session()
        self.assertIs(first, second)
        self.assertEqual(second.cookies.get("synthetic_session"), "test-value")
        self.assertEqual(first.adapters["https://"].max_retries.total, 0)
        first.close()

    def test_login_json_mislabeled_as_html_reaches_authentication_parser(self):
        for status in (True, False):
            result = response(200, json.dumps({"status": status, "msg2": "captcha optional"}))
            result.url = "https://passport2.chaoxing.com/fanyalogin"
            session, raw = self.session(result)
            self.assertIs(session.post(result.url), result)
            self.assertIsNone(self.co.issue)
            self.assertEqual(raw.call_count, 1)

    def test_html_login_and_real_http_denial_still_stop(self):
        for code, body in ((200, '<html><form>login</form></html>'),
                           (403, '{"status": true}'), (429, '{"status": true}')):
            result = response(code, body)
            result.url = "https://passport2.chaoxing.com/fanyalogin"
            session, _ = self.session(result)
            with self.assertRaises(AccessRestricted):
                session.post(result.url)

    def test_successful_login_json_saves_session_after_guard(self):
        result = response(200, '{"status": true}')
        result.url = "https://passport2.chaoxing.com/fanyalogin"
        session, _ = self.session(result)
        client = object.__new__(Chaoxing)
        client.account = SimpleNamespace(username="synthetic", password="synthetic")
        client.cipher = Mock()
        client.get_name = Mock(return_value="")
        with patch("api.base.requests.Session", return_value=session), \
             patch("api.base.save_cookies") as save, \
             patch.object(SessionManager, "update_cookies"):
            self.assertTrue(client.login()["status"])
        save.assert_called_once_with(session)

    def test_concurrent_session_initialization_happens_once(self):
        class IsolatedManager(SessionManager):
            _instance = None
        with patch("api.base.use_cookies", return_value={}) as cookies:
            harden_session_manager(IsolatedManager, self.co)
            sessions = []
            workers = [threading.Thread(target=lambda: sessions.append(IsolatedManager.get_session())) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(3)
            self.assertEqual(cookies.call_count, 1)
        self.assertEqual(len(sessions), 8)
        self.assertEqual(len({id(session) for session in sessions}), 1)
        sessions[0].close()

    def test_restricted_task_is_retained_and_stop_sends_no_more_requests(self):
        task = self.task()
        processor = self.processor([task])
        with patch.object(main, "process_chapter", side_effect=AccessRestricted(403)) as process:
            with self.assertRaises(RunNeedsAttention):
                processor.run()
        self.assertEqual(process.call_count, 1)
        self.assertFalse(self.co.completed)
        self.assertEqual(self.co.pending[0]["chapter_id"], "0")
        self.assertEqual(profile_status(self.profile, True), "等待人工处理")

    def test_only_manual_retry_restarts_the_same_chapter(self):
        self.co.dialog.return_value = "retry"
        task = self.task()
        processor = self.processor([task])
        with patch.object(main, "process_chapter", side_effect=[AccessRestricted(403), main.ChapterResult.SUCCESS]) as process:
            processor.run()
        self.assertEqual(process.call_count, 2)
        self.co.dialog.assert_called_once()
        self.assertTrue(self.co.completed)
        self.assertEqual(self.co.pending, [])
        self.assertEqual(task.tries, 0)

    def test_manual_rechecks_are_bounded_even_if_dialog_returns_retry(self):
        self.co.dialog.return_value = "retry"
        processor = self.processor([self.task()])
        with patch.object(main, "process_chapter", side_effect=AccessRestricted(403)) as process:
            with self.assertRaises(RunNeedsAttention):
                processor.run()
        self.assertEqual(process.call_count, 3)
        self.assertFalse(self.co.dialog.call_args.args[-1])

    def test_unexpected_worker_exception_finishes_without_queue_deadlock(self):
        processor = self.processor([self.task()])
        errors = []
        def run():
            try:
                processor.run()
            except Exception as error:
                errors.append(error)
        with patch.object(main, "process_chapter", side_effect=ValueError("synthetic")), patch("runtime_safety.time.sleep"):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], IncompleteRun)
        self.assertEqual(len(processor.failed_tasks), 1)

    def test_failed_tasks_cannot_trigger_all_courses_complete_notification(self):
        cx = Mock()
        cx.login.return_value = {"status": True}
        course = self.task().course
        cx.get_course_list.return_value = [course]
        cx.get_course_point.return_value = {"points": [self.task().point]}
        notification = Mock()
        notification.get_notification_from_config.return_value = notification
        klass = make_processor(main.JobProcessor, main, self.co)
        config = {"jobs": 1, "speed": 1.0, "retry_interval": 0.01, "notopen_action": "retry"}
        with patch.object(main, "init_config", return_value=(config, {}, {}, "unused")), \
             patch.object(main, "init_chaoxing", return_value=cx), \
             patch.object(main, "filter_courses", return_value=[course]), \
             patch.object(main, "Notification", return_value=notification), \
             patch.object(main, "JobProcessor", klass), \
             patch.object(main, "process_chapter", return_value=main.ChapterResult.ERROR), \
             patch.object(main, "logger") as logs, patch("runtime_safety.time.sleep"), \
             patch.object(main.tqdm, "format_sizeof", main.tqdm.format_sizeof):
            with self.assertRaises(IncompleteRun):
                main.main()
        self.assertFalse(any("所有课程学习任务已完成" in str(call) for call in notification.send.call_args_list))
        self.assertFalse(any("所有课程学习任务已完成" in str(call) for call in logs.info.call_args_list))

    def test_new_log_messages_hide_tokens_urls_and_cookie_headers(self):
        record = {"message": "请求url: https://mooc1.chaoxing.com/private-token?enc=secret\nCookie: fake-secret"}
        self.assertTrue(redact_log(record))
        self.assertNotIn("private-token", record["message"])
        self.assertNotIn("fake-secret", record["message"])
        self.assertNotIn("enc=", record["message"])


class RecoveryDialogTests(unittest.TestCase):
    def test_retry_button_waits_for_countdown_and_requires_a_click(self):
        window = tk.Tk()
        window.withdraw()
        clock = [0.0]
        dialog = RecoveryDialog(window, "测试", "访问受限", "章节", 10, True, clock=lambda: clock[0])
        try:
            self.assertEqual(str(dialog.retry_button.cget("state")), "disabled")
            clock[0] = 11
            dialog.tick()
            self.assertEqual(str(dialog.retry_button.cget("state")), "normal")
            self.assertEqual(dialog.result, "stop")
            dialog.retry_button.invoke()
            self.assertEqual(dialog.result, "retry")
        finally:
            dialog.stop()
            window.destroy()

    def test_exhausted_rechecks_cannot_resume(self):
        window = tk.Tk()
        window.withdraw()
        dialog = RecoveryDialog(window, "测试", "访问受限", "章节", 0, False)
        try:
            self.assertEqual(str(dialog.retry_button.cget("state")), "disabled")
            dialog.retry()
            self.assertEqual(dialog.result, "stop")
        finally:
            dialog.stop()
            window.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
