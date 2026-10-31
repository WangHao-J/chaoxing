"""Account isolation, cross-process locking and course-selection regression tests."""
import configparser
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from account_store import AccountStore, AlreadyRunning, ProfileLock, is_running, selected_courses
from run_account import configure
from password_dialog import PasswordDialog

INSTALL_ROOT = Path(os.environ.get("CHAOXING_INSTALL_ROOT", str(Path(__file__).resolve().parent))).resolve()
MODULE_ROOT = Path(__file__).resolve().parent


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="multi-account-test-", dir=INSTALL_ROOT / "temp")
        self.root = Path(self.temporary.name).resolve()
        assert self.root.is_relative_to(INSTALL_ROOT / "temp")
        self.app = self.root / "app"
        (self.app / "resource").mkdir(parents=True)
        (self.app / "resource" / "font_map_table.json").write_text("{}", encoding="utf-8")
        (self.app / "config.ini").write_text(
            "[common]\nusername=fake-user\npassword=fake-password\ncourse_list=123\n"
            "speed=2\njobs=4\n[tiku]\nprovider=AI\nkey=fake-api-key\n"
            "[notification]\nprovider=Telegram\nurl=fake-notify-token\n", encoding="utf-8"
        )
        self.store = AccountStore(self.root)

    def tearDown(self):
        assert self.root.is_relative_to(INSTALL_ROOT / "temp")
        self.temporary.cleanup()

    def config(self, profile):
        parser = configparser.ConfigParser()
        parser.read(profile.directory / "config.ini", encoding="utf-8")
        return parser

    def test_new_profile_does_not_inherit_credentials_or_external_keys(self):
        profile = self.store.create("新账户")
        parser = self.config(profile)
        self.assertEqual(parser["common"]["username"], "")
        self.assertEqual(parser["common"]["password"], "")
        self.assertEqual(parser["common"]["course_list"], "")
        self.assertEqual(parser["common"]["jobs"], "1")
        self.assertEqual(parser["tiku"]["provider"], "")
        self.assertNotIn("key", parser["tiku"])
        self.assertEqual(parser["notification"]["provider"], "")
        self.assertNotIn("url", parser["notification"])

    def test_legacy_default_preserves_existing_state_once(self):
        (self.app / "cookies.txt").write_text("fake_cookie=legacy", encoding="utf-8")
        original = self.store.ensure_default()
        self.assertEqual(self.config(original)["common"]["username"], "fake-user")
        self.assertEqual((original.directory / "cookies.txt").read_text(), "fake_cookie=legacy")
        (original.directory / "cookies.txt").write_text("fake_cookie=updated", encoding="utf-8")
        again = self.store.ensure_default()
        self.assertEqual(again.id, original.id)
        self.assertEqual((again.directory / "cookies.txt").read_text(), "fake_cookie=updated")

    def test_display_name_is_not_used_as_a_directory_path(self):
        profile = self.store.create("../账户名称")
        self.assertTrue(profile.directory.is_relative_to(self.store.accounts))
        self.assertNotEqual(profile.directory.name, profile.name)

    def test_invalid_profile_ids_are_rejected(self):
        for invalid in ("..", "../default", r"..\default", "C:\\other", "/tmp/other"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.directory(invalid)

    def test_duplicate_names_are_rejected(self):
        self.store.create("Account A")
        with self.assertRaises(ValueError):
            self.store.create(" account a ")

    def test_same_profile_lock_and_release(self):
        profile = self.store.create("账户一")
        first = ProfileLock(profile.directory).acquire()
        try:
            self.assertTrue(is_running(profile))
            with self.assertRaises(AlreadyRunning):
                ProfileLock(profile.directory).acquire()
        finally:
            first.release()
        self.assertFalse(is_running(profile))

    def test_different_profiles_can_run_simultaneously(self):
        a, b = self.store.create("账户一"), self.store.create("账户二")
        with ProfileLock(a.directory), ProfileLock(b.directory):
            self.assertTrue(is_running(a))
            self.assertTrue(is_running(b))

    def test_lock_blocks_a_second_process(self):
        profile = self.store.create("账户一")
        code = (
            "import sys; from account_store import ProfileLock, AlreadyRunning\n"
            "try:\n ProfileLock(sys.argv[1]).acquire()\n"
            "except AlreadyRunning:\n sys.exit(0)\n"
            "else:\n sys.exit(3)\n"
        )
        env = dict(os.environ, PYTHONPATH=str(MODULE_ROOT))
        with ProfileLock(profile.directory):
            result = subprocess.run([sys.executable, "-c", code, str(profile.directory)],
                                    env=env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def fake_main(self, *, use_cookies=False, login=None):
        cx = SimpleNamespace(login=login or Mock(return_value={"status": True}))
        return SimpleNamespace(
            init_config=lambda: ({"username": "", "use_cookies": use_cookies}, {}, {}, "config.ini"),
            init_chaoxing=lambda *args, **kwargs: cx,
            Account=lambda user, password: SimpleNamespace(username=user, password=password),
            InputFormatError=ValueError,
        )

    def test_password_uses_account_specific_dialog(self):
        profile = self.store.create("账户一")
        main = self.fake_main()
        configure(main, profile, False)
        with patch("run_account.ask_password", return_value="synthetic-password") as dialog, patch("builtins.print"):
            self.assertEqual(main.input("请输入你的密码："), "synthetic-password")
            dialog.assert_called_once_with(profile.name)

    def test_cookie_reuse_is_specific_to_selected_profile(self):
        a, b = self.store.create("账户一"), self.store.create("账户二")
        (a.directory / "cookies.txt").write_text("fake_cookie=one", encoding="utf-8")
        first, second = self.fake_main(), self.fake_main()
        configure(first, a, False)
        configure(second, b, False)
        self.assertTrue(first.init_config()[0]["use_cookies"])
        self.assertFalse(second.init_config()[0]["use_cookies"])

    def test_fresh_login_bypasses_saved_cookie(self):
        profile = self.store.create("账户一")
        (profile.directory / "cookies.txt").write_text("fake_cookie=one", encoding="utf-8")
        main = self.fake_main(use_cookies=True)
        configure(main, profile, True)
        self.assertFalse(main.init_config()[0]["use_cookies"])

    def test_expired_cookie_prompts_for_login_in_same_process(self):
        profile = self.store.create("账户一")
        login = Mock(side_effect=[{"status": False}, {"status": True}])
        main = self.fake_main(login=login)
        configure(main, profile, False)
        cx = main.init_chaoxing()
        with patch("builtins.input", return_value="fake-phone"), \
             patch("run_account.ask_password", return_value="fake-password"), patch("builtins.print"):
            self.assertTrue(cx.login(login_with_cookies=True)["status"])
        self.assertEqual(login.call_args_list[0].kwargs, {"login_with_cookies": True})
        self.assertEqual(login.call_args_list[1].kwargs, {"login_with_cookies": False})
        self.assertEqual(cx.account.username, "fake-phone")

    def test_course_filter_adapter_rejects_unknown_id(self):
        profile = self.store.create("账户一")
        main = self.fake_main()
        configure(main, profile, False)
        with self.assertRaises(ValueError):
            main.filter_courses([{"courseId": "100", "clazzId": "1"}], ["999"])

    def test_real_cookie_cache_and_log_are_isolated_in_two_processes(self):
        shutil.copy2(INSTALL_ROOT / "app" / "main.py", self.app / "main.py")
        shutil.copytree(INSTALL_ROOT / "app" / "api", self.app / "api",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"))
        a, b = self.store.create("账户一"), self.store.create("账户二")
        code = (
            "import sys,json; from pathlib import Path; import requests\n"
            "from account_store import AccountStore,ProfileLock\n"
            "from run_account import prepare\n"
            "root=Path(sys.argv[1]); profile=AccountStore(root).get(sys.argv[2])\n"
            "with ProfileLock(profile.directory):\n"
            " main=prepare(root,profile)\n"
            " from api.cookies import save_cookies,use_cookies\n"
            " from api.answer import CacheDAO\n"
            " session=requests.Session(); session.cookies.set('fake_cookie',profile.id)\n"
            " save_cookies(session); CacheDAO().add_cache('fake-question',profile.id)\n"
            " main.logger.info('Synthetic log '+profile.id); main.logger.complete(); main.logger.remove()\n"
            " print(json.dumps({'cookie':use_cookies()['fake_cookie'],'cache':CacheDAO().get_cache('fake-question')}))\n"
        )
        env = dict(os.environ, PYTHONPATH=str(MODULE_ROOT), PYTHONUTF8="1")
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.root), profile.id],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                     for profile in (a, b)]
        try:
            for profile, process in zip((a, b), processes):
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr.decode("utf-8", errors="replace"))
                actual = json.loads(stdout.decode("utf-8"))
                self.assertEqual(actual, {"cookie": profile.id, "cache": profile.id})
                log = (profile.directory / "chaoxing.log").read_text(encoding="utf-8")
                self.assertIn(profile.id, log)
                self.assertNotIn(b.id if profile.id == a.id else a.id, log)
                checked = subprocess.run(
                    [sys.executable, str(MODULE_ROOT / "run_account.py"), "--root", str(self.root),
                     "--profile", profile.id, "--check"], capture_output=True, env=env, timeout=30
                )
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
                check_data = json.loads(checked.stdout.decode("utf-8"))
                self.assertTrue(check_data["isolated"])
                self.assertEqual(check_data["profile"], profile.id)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


class CourseSelectionTests(unittest.TestCase):
    def setUp(self):
        self.courses = [
            {"courseId": "100", "clazzId": "1"},
            {"courseId": "100", "clazzId": "2"},
            {"courseId": "200", "clazzId": "3"},
        ]

    def test_unknown_id_does_not_select_all_courses(self):
        with self.assertRaises(ValueError):
            selected_courses(self.courses, ["does-not-exist"])

    def test_partial_invalid_selection_is_rejected(self):
        with self.assertRaises(ValueError):
            selected_courses(self.courses, ["100", "999"])

    def test_empty_selection_is_rejected(self):
        with self.assertRaises(ValueError):
            selected_courses(self.courses, ["", " "])

    def test_exact_course_preserves_all_its_classes(self):
        self.assertEqual(selected_courses(self.courses, [" 100 "]), self.courses[:2])

    def test_duplicate_classes_are_not_run_twice(self):
        courses = self.courses + self.courses[:1]
        self.assertEqual(selected_courses(courses, ["100", "100"]), self.courses[:2])

    def test_explicit_all_courses(self):
        self.assertEqual(selected_courses(self.courses, ["*"]), self.courses)


class PasswordDialogTests(unittest.TestCase):
    def setUp(self):
        self.window = tk.Tk()
        self.window.withdraw()
        self.dialog = PasswordDialog(self.window, "测试账户")

    def tearDown(self):
        self.window.destroy()

    def test_masked_typing_and_reveal_switch(self):
        self.dialog.entry.insert(0, "synthetic-password")
        self.assertEqual(self.dialog.entry.cget("show"), "●")
        self.dialog.checkbox.invoke()
        self.assertEqual(self.dialog.entry.cget("show"), "")
        self.assertEqual(self.dialog.entry.get(), "synthetic-password")
        self.dialog.checkbox.invoke()
        self.assertEqual(self.dialog.entry.cget("show"), "●")

    def test_empty_password_does_not_submit_and_confirm_preserves_characters(self):
        self.dialog.confirm_button.invoke()
        self.assertIsNone(self.dialog.result)
        self.assertTrue(self.dialog.message.get())
        self.dialog.entry.insert(0, " synthetic-中文-password ")
        self.dialog.confirm_button.invoke()
        self.assertEqual(self.dialog.result, " synthetic-中文-password ")
        self.assertEqual(self.dialog.password.get(), "")

    def test_cancel_discards_entered_password(self):
        self.dialog.entry.insert(0, "synthetic-password")
        self.dialog.cancel()
        self.assertIsNone(self.dialog.result)
        self.assertEqual(self.dialog.password.get(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
