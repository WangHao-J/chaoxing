"""Offline account-only login and responsive cancellation checks."""
import contextlib
import gc
import io
import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from account_store import AlreadyRunning, ProfileLock
from login_window import LoginBackend, LoginWindow, entry
from runtime_safety import AccessRestricted


class LoginBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.profile = SimpleNamespace(directory=self.directory, id="test", name="离线账户")
        self.cancelled = threading.Event()
        self.coordinator = SimpleNamespace(issue=None, state=Mock(), guard_session=Mock())
        self.accounts = []
        def account(username, password):
            obj = SimpleNamespace(username=username, password=password)
            self.accounts.append(obj)
            return obj
        self.client = SimpleNamespace(login=Mock(return_value={"status": True}), get_name=None)
        self.main = SimpleNamespace(Account=Mock(side_effect=account), Chaoxing=Mock(return_value=self.client),
                                    logger=SimpleNamespace(remove=Mock()), main=Mock())
        self.base = SimpleNamespace(requests=SimpleNamespace(Session=Mock()))
        self.prepare = patch("login_window.prepare", return_value=self.main).start()
        self.install = patch("login_window.install_runtime_safety", return_value=self.coordinator).start()
        patch.dict("sys.modules", {"api": SimpleNamespace(base=self.base), "api.base": self.base}).start()
        self.addCleanup(patch.stopall)
        self.backend = LoginBackend(self.directory, self.profile)

    def login(self):
        return self.backend.authenticate("synthetic-user", "synthetic-password", self.cancelled)

    def test_login_is_account_only_and_clears_in_memory_credentials(self):
        result = self.login()
        self.assertTrue(result[0])
        self.main.main.assert_not_called()
        self.client.login.assert_called_once_with(login_with_cookies=False)
        self.assertEqual(self.accounts[0].password, "")
        self.assertEqual(self.accounts[0].username, "")
        self.assertEqual(self.client.get_name(), "")
        self.coordinator.state.assert_called_with("stopped", "登录成功，请在账户首页选择运行方式")
        with ProfileLock(self.directory):
            pass

    def test_existing_account_owner_prevents_authentication(self):
        with ProfileLock(self.directory):
            with self.assertRaises(AlreadyRunning):
                self.login()
        self.prepare.assert_not_called()
        self.client.login.assert_not_called()

    def test_failed_authentication_restores_old_cookie_bytes_without_echoing_provider_message(self):
        cookies = self.directory / "cookies.txt"
        cookies.write_bytes(b"synthetic-old-cookie")
        def fail(**kwargs):
            cookies.write_bytes(b"synthetic-partial-cookie")
            return {"status": False, "msg": "synthetic-password synthetic-user"}
        self.client.login.side_effect = fail
        success, message = self.login()
        self.assertFalse(success)
        self.assertEqual(cookies.read_bytes(), b"synthetic-old-cookie")
        self.assertNotIn("synthetic", message)

    def test_cancellation_after_success_restores_old_session_and_does_not_report_login_success(self):
        cookies = self.directory / "cookies.txt"
        cookies.write_bytes(b"synthetic-old-cookie")
        def finish(**kwargs):
            cookies.write_bytes(b"synthetic-new-cookie")
            self.cancelled.set()
            return {"status": True}
        self.client.login.side_effect = finish
        success, message = self.login()
        self.assertFalse(success)
        self.assertIn("取消", message)
        self.assertEqual(cookies.read_bytes(), b"synthetic-old-cookie")

    def test_failed_new_login_does_not_leave_partial_cookies(self):
        cookies = self.directory / "cookies.txt"
        def finish(**kwargs):
            cookies.write_bytes(b"synthetic-partial-cookie")
            raise RuntimeError("synthetic-secret")
        self.client.login.side_effect = finish
        success, message = self.login()
        self.assertFalse(success)
        self.assertFalse(cookies.exists())
        self.assertNotIn("synthetic-secret", message)

    def test_retry_uses_one_runtime_patch_installation(self):
        self.client.login.return_value = {"status": False}
        self.assertFalse(self.login()[0])
        self.client.login.return_value = {"status": True}
        self.assertTrue(self.login()[0])
        self.prepare.assert_called_once()
        self.install.assert_called_once()

    def test_login_request_has_timeout_and_cancel_check(self):
        session = SimpleNamespace(request=Mock(return_value="offline response"))
        original = session.request
        factory = self.base.requests.Session
        factory.return_value = session
        def perform(**kwargs):
            created = self.base.requests.Session()
            self.assertEqual(created.request("POST", "https://example.invalid/login"), "offline response")
            self.cancelled.set()
            with self.assertRaises(InterruptedError):
                created.request("POST", "https://example.invalid/login")
            return {"status": True}
        self.client.login.side_effect = perform
        self.assertFalse(self.login()[0])
        original.assert_called_once_with("POST", "https://example.invalid/login", timeout=(5, 15))
        self.coordinator.guard_session.assert_called_once_with(session)

    def test_restrictions_return_one_actionable_message_without_retries(self):
        self.client.login.side_effect = AccessRestricted(429)
        success, message = self.login()
        self.assertFalse(success)
        self.assertIn("频繁", message)
        self.assertEqual(self.client.login.call_count, 1)

    def test_invalid_credentials_never_prepare_upstream(self):
        with self.assertRaises(ValueError):
            self.backend.authenticate("", "password", self.cancelled)
        self.prepare.assert_not_called()

    def test_restriction_messages_distinguish_redirect_denial_and_challenge(self):
        cases = ((AccessRestricted(401), "401"),
                 (AccessRestricted(403), "未确认有验证码"),
                 (AccessRestricted(401, reason="login_page"), "没有返回登录结果"),
                 (AccessRestricted(403, reason="challenge"), "遇到验证页面"))
        for error, expected in cases:
            with self.subTest(expected=expected):
                self.client.login.side_effect = error
                success, message = self.login()
                self.assertFalse(success)
                self.assertIn(expected, message)

    def test_partial_runtime_initialization_failure_requires_reopen_without_reinstalling_hooks(self):
        self.install.side_effect = RuntimeError("synthetic partial hook installation")
        for _ in range(2):
            success, message = self.login()
            self.assertFalse(success)
            self.assertIn("关闭后重新打开", message)
        self.prepare.assert_called_once()
        self.install.assert_called_once()
        self.client.login.assert_not_called()

    def test_client_initialization_failure_also_clears_credentials(self):
        self.main.Chaoxing.side_effect = RuntimeError("synthetic failure")
        self.assertFalse(self.login()[0])
        self.assertEqual((self.accounts[0].username, self.accounts[0].password), ("", ""))
        self.client.login.assert_not_called()


class LoginWindowTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = SimpleNamespace(directory=Path(self.temp.name), id="test", name="离线账户")
        self.window = tk.Tk()
        self.window.withdraw()
        self.backend = SimpleNamespace(authenticate=Mock(return_value=(False, "离线登录未成功")))
        self.controller = LoginWindow(self.window, self.temp.name, self.profile,
                                      backend_factory=Mock(return_value=self.backend))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if not self.controller.destroyed:
            self.controller.destroy()
        self.controller = self.window = self.backend = None
        gc.collect()

    def pump(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.window.update()
            time.sleep(.005)
        self.assertTrue(predicate())

    def fill(self):
        self.controller.phone.set("synthetic-user")
        self.controller.password.set("synthetic-password")

    def test_native_password_mask_and_toggle(self):
        self.assertTrue(self.controller.password_entry["show"])
        self.controller.show_password.set(True)
        self.controller.toggle_password()
        self.assertEqual(self.controller.password_entry["show"], "")

    def test_async_authentication_clears_password_entry_and_enables_retry(self):
        main_thread = threading.get_ident()
        identities = []
        def auth(*args):
            identities.append(threading.get_ident())
            return False, "离线失败"
        self.backend.authenticate.side_effect = auth
        self.fill()
        self.controller.login()
        self.assertEqual(self.controller.password.get(), "")
        self.pump(lambda: not self.controller.busy)
        self.assertEqual(self.controller.status.get(), "离线失败")
        self.assertNotEqual(identities, [main_thread])
        self.assertEqual(str(self.controller.login_button["state"]), "normal")

    def test_close_waits_for_active_authentication_before_destroy(self):
        started, release = threading.Event(), threading.Event()
        def auth(username, password, cancelled):
            started.set()
            release.wait(3)
            return False, "已取消"
        self.backend.authenticate.side_effect = auth
        self.fill()
        self.controller.login()
        self.assertTrue(started.wait(1))
        self.controller.close()
        self.assertFalse(self.controller.destroyed)
        self.assertTrue(self.controller.cancelled.is_set())
        self.assertIn("等待", self.controller.status.get())
        release.set()
        self.pump(lambda: self.controller.destroyed)

    def test_success_closes_without_starting_courses(self):
        self.backend.authenticate.return_value = True, "离线登录成功"
        self.fill()
        self.controller.login()
        self.pump(lambda: self.controller.destroyed)
        self.backend.authenticate.assert_called_once()
        self.assertTrue(self.controller.authenticated)

    def test_empty_inputs_never_construct_backend(self):
        self.controller.login()
        self.controller.backend_factory.assert_not_called()
        self.assertFalse(self.controller.busy)

    def test_matching_manager_stop_request_cooperatively_closes(self):
        self.controller.destroy()
        token = "a" * 32
        self.window = tk.Tk()
        self.controller = LoginWindow(self.window, self.temp.name, self.profile,
            backend_factory=Mock(), session_id=token)
        self.window.withdraw()
        other = self.profile.directory / ("study_stop_" + "b" * 32 + ".request")
        other.touch()
        self.controller.stop_request.touch()
        self.controller.last_stop_check = 0
        self.pump(lambda: self.controller.destroyed)
        self.assertFalse(self.controller.stop_request.exists())
        self.assertTrue(other.exists())

    def test_offscreen_check_constructs_no_backend_and_never_uses_network(self):
        output = io.StringIO()
        with patch("login_window.AccountStore") as store, \
             patch("login_window.LoginBackend.authenticate", side_effect=AssertionError("No auth")), \
             patch("requests.sessions.Session.request", side_effect=AssertionError("No network")), \
             contextlib.redirect_stdout(output):
            store.return_value.get.return_value = self.profile
            self.assertEqual(entry(["--root", self.temp.name, "--profile", "test", "--ui-check"]), 0)
        self.assertFalse(json.loads(output.getvalue())["network_called"])


if __name__ == "__main__":
    unittest.main()
