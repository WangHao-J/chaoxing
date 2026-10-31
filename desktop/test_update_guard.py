"""Installer checks fail closed and pass the path as data rather than shell code."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from update_guard import QUERY, check


class UpdateGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "synthetic installation"
        (self.root / "app").mkdir(parents=True)
        (self.root / "app/main.py").touch()
        (self.root / "account_store.py").touch()

    def test_installation_path_is_not_interpolated_into_shell(self):
        run = Mock(return_value=SimpleNamespace(returncode=0))
        self.assertEqual(check(self.root, run), 0)
        self.assertEqual(run.call_args.args[0][-1], QUERY)
        self.assertEqual(run.call_args.kwargs["env"]["CHAOXING_UPDATE_ROOT"], str(self.root.resolve()))
        self.assertNotIn(str(self.root), QUERY)

    def test_busy_and_failed_check_never_report_ready(self):
        for result, expected in ((2, 2), (1, 3), (3, 3)):
            with self.subTest(result=result):
                self.assertEqual(check(self.root, Mock(return_value=SimpleNamespace(returncode=result))), expected)
        self.assertEqual(check(self.root, Mock(side_effect=OSError())), 3)
        self.assertEqual(check(self.root, Mock(side_effect=subprocess.TimeoutExpired("synthetic", 25))), 3)

    def test_missing_original_install_does_not_run_a_process(self):
        run = Mock()
        self.assertEqual(check(self.root / "missing", run), 3)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
