"""Offline checks of account selection and independent-window launch/stop."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from account_manager import AccountManager


class StudyManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = SimpleNamespace(id="test-id", name="合成账户", directory=self.root / "accounts/test-id")
        self.profile.directory.mkdir(parents=True)
        (self.root / "python").mkdir()
        (self.root / "python/python.exe").touch()
        (self.root / "python/pythonw.exe").touch()
        self.manager = AccountManager.__new__(AccountManager)
        self.manager.window = Mock()
        self.manager.store = SimpleNamespace(root=self.root)
        self.manager.selected = Mock(return_value=self.profile)
        self.manager.processes = {}
        self.manager.quiz_processes = {}
        self.manager.study_sessions = {}
        self.manager.quiz_sessions = {}
        self.manager.login_processes = {}
        self.manager.note = Mock()
        self.manager.refresh = Mock()

    def test_each_primary_action_launches_exact_gui_mode_without_credentials(self):
        for mode in ("quizzes", "videos", "mixed"):
            with self.subTest(mode=mode):
                self.manager.processes.clear()
                with patch("account_manager.is_running", return_value=False), \
                     patch("account_manager.subprocess.Popen") as spawn:
                    self.manager.open_study(mode)
                argv = spawn.call_args.args[0]
                self.assertEqual(Path(argv[0]).name, "pythonw.exe")
                self.assertEqual(Path(argv[1]).name, "study_window.py")
                self.assertEqual(argv[argv.index("--mode") + 1], mode)
                self.assertEqual(argv[argv.index("--profile") + 1], self.profile.id)
                token = argv[argv.index("--session-id") + 1]
                self.assertRegex(token, r"^[a-f0-9]{32}$")
                self.assertEqual(self.manager.study_sessions[self.profile.id], token)
                self.assertEqual(len(argv), 10)
                self.assertEqual(spawn.call_args.kwargs["cwd"], self.profile.directory)
                self.assertEqual(spawn.call_args.kwargs["env"]["TEMP"], str(self.profile.directory / "temp"))

    def test_missing_account_or_invalid_mode_never_launches(self):
        self.manager.selected.return_value = None
        with patch("account_manager.subprocess.Popen") as spawn:
            self.manager.open_study("videos")
            self.manager.open_study("unknown")
        spawn.assert_not_called()

    def test_login_launches_gui_only_and_never_the_legacy_course_runner(self):
        with patch("account_manager.is_running", return_value=False), \
             patch("account_manager.subprocess.Popen") as spawn:
            self.manager.open_login()
        argv = spawn.call_args.args[0]
        self.assertEqual(Path(argv[0]).name, "pythonw.exe")
        self.assertEqual(Path(argv[1]).name, "login_window.py")
        self.assertNotIn("run_account.py", " ".join(argv))
        self.assertEqual(argv[argv.index("--profile") + 1], self.profile.id)
        self.assertRegex(argv[-1], r"^[a-f0-9]{32}$")
        self.assertIn(self.profile.id, self.manager.login_processes)

    def test_reselect_courses_uses_current_mixed_gui(self):
        self.manager.open_study = Mock()
        self.manager.start_selected(choose_courses=True)
        self.manager.open_study.assert_called_once_with("mixed")

    def test_any_existing_account_process_or_lock_blocks_second_window(self):
        for kind in ("profile_lock", "process", "helper"):
            with self.subTest(kind=kind):
                self.manager.processes.clear()
                self.manager.quiz_processes.clear()
                active = Mock()
                active.poll.return_value = None
                if kind == "process":
                    self.manager.processes[self.profile.id] = active
                if kind == "helper":
                    self.manager.quiz_processes[self.profile.id] = active
                with patch("account_manager.is_running", return_value=kind == "profile_lock"), \
                     patch("account_manager.subprocess.Popen") as spawn, \
                     patch("account_manager.messagebox.showinfo") as info:
                    self.manager.open_study("mixed")
                spawn.assert_not_called()
                info.assert_called_once()

    def test_other_account_can_open_in_parallel(self):
        active = Mock()
        active.poll.return_value = None
        self.manager.processes["another-profile"] = active
        with patch("account_manager.is_running", return_value=False), \
             patch("account_manager.subprocess.Popen") as spawn:
            self.manager.open_study("quizzes")
        spawn.assert_called_once()

    def test_launch_failure_does_not_claim_session_or_expose_raw_exception(self):
        with patch("account_manager.is_running", return_value=False), \
             patch("account_manager.subprocess.Popen", side_effect=OSError("synthetic-private-value")), \
             patch("account_manager.messagebox.showerror") as error:
            self.manager.open_study("quizzes")
        self.assertEqual(self.manager.processes, {})
        self.assertEqual(self.manager.study_sessions, {})
        self.assertNotIn("synthetic-private-value", str(error.call_args))

    def test_stop_is_cooperative_and_scoped_to_current_window_token(self):
        process = Mock()
        process.poll.return_value = None
        self.manager.processes[self.profile.id] = process
        self.manager.study_sessions[self.profile.id] = "a" * 32
        self.manager.stop()
        self.assertTrue((self.profile.directory / ("study_stop_" + "a" * 32 + ".request")).is_file())
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertEqual(len(list(self.profile.directory.glob("*.request"))), 1)


if __name__ == "__main__":
    unittest.main()
