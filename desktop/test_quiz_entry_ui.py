"""Offline regressions for compact quiz recovery and local AI configuration."""
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from quiz_assistant import QuizError
from quiz_dialog import SettingsDialog, ensure_ai_config
from recovery_dialog import RecoveryDialog


READY = {"enabled": True, "endpoint": "https://ai.example/v1", "model": "test-model", "key": "test-key"}
EMPTY = {"enabled": False, "endpoint": "", "model": "", "key": ""}


class QuizEntryTests(unittest.TestCase):
    def setUp(self):
        self.window = tk.Tk()
        self.window.withdraw()
        self.dialog = None

    def tearDown(self):
        if self.dialog and self.dialog.timer is not None:
            self.window.after_cancel(self.dialog.timer)
        self.window.destroy()

    def recovery(self, jobs=None, **options):
        self.dialog = RecoveryDialog(self.window, "测试账户", "答题服务未启用，需要配置", "1.1 测试章节", 0, True,
            course_title="测试课程", manual_jobs=jobs if jobs is not None else [
                {"name": "章节测验", "kind": "workid"}], **options)
        return self.dialog

    def test_quiz_only_page_has_no_browser_navigation_or_duplicate_task_list(self):
        dialog = self.recovery()
        self.assertTrue(dialog.only_quizzes)
        self.assertFalse(hasattr(dialog, "course_button"))
        self.assertFalse(hasattr(dialog, "copy_button"))
        self.assertFalse(hasattr(dialog, "job_list"))
        self.assertFalse(hasattr(dialog, "retry_button"))
        self.assertEqual(dialog.quiz_button.cget("text"), "打开答题页面")
        self.assertFalse(dialog.details.winfo_manager())

    def test_all_primary_controls_fit_at_560_by_360(self):
        dialog = self.recovery()
        self.window.geometry("560x360+10000+10000")
        self.window.deiconify()
        self.window.update()
        for button in (dialog.quiz_button, dialog.skip_button, dialog.skip_all_button,
                       dialog.stop_button, dialog.details_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                 self.window.winfo_rootx() + self.window.winfo_width())
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                 self.window.winfo_rooty() + self.window.winfo_height())

    def test_missing_callback_preserves_page_and_explains_configuration(self):
        dialog = self.recovery()
        with patch.object(dialog, "finish") as finish:
            dialog.quiz_button.invoke()
        finish.assert_not_called()
        self.assertIn("配置", dialog.feedback.get())

    def test_configured_quiz_button_returns_quiz_action(self):
        on_quiz = Mock(return_value=True)
        dialog = self.recovery(on_quiz=on_quiz)
        dialog.quiz_button.invoke()
        on_quiz.assert_called_once_with(self.window)
        self.assertEqual(dialog.result, "quiz")

    def test_cancelled_configuration_keeps_pause_page_and_buttons_available(self):
        on_quiz = Mock(return_value=False)
        dialog = self.recovery(on_quiz=on_quiz)
        with patch.object(dialog, "finish") as finish:
            dialog.quiz_button.invoke()
        finish.assert_not_called()
        self.assertEqual(str(dialog.quiz_button.cget("state")), "normal")
        self.assertFalse(dialog.busy)

    def test_confirmation_records_keep_platform_recheck(self):
        dialog = self.recovery(jobs=[{"name": "已提交测验", "kind": "workid", "needs_confirmation": True}])
        self.assertTrue(hasattr(dialog, "retry_button"))
        dialog.retry_button.invoke()
        self.assertEqual(dialog.result, "retry")

    def test_mixed_external_and_quiz_tasks_keep_existing_browser_controls(self):
        dialog = self.recovery(jobs=[{"name": "测验", "kind": "workid"},
            {"name": "外部视频", "kind": "hyperlink", "href": "https://b23.tv/test"}])
        self.assertFalse(dialog.only_quizzes)
        self.assertTrue(hasattr(dialog, "course_button"))
        self.assertTrue(hasattr(dialog, "copy_button"))
        self.assertTrue(hasattr(dialog, "quiz_button"))
        self.assertTrue(hasattr(dialog, "job_list"))

    def test_captcha_retains_human_verification_ui(self):
        verifier = Mock()
        dialog = self.recovery(verifier=verifier)
        self.assertFalse(dialog.only_quizzes)
        self.assertTrue(hasattr(dialog, "fetch_button"))
        self.assertTrue(hasattr(dialog, "submit_button"))
        verifier.fetch.assert_not_called()
        verifier.submit.assert_not_called()

    def test_reason_details_start_collapsed_and_toggle_without_network(self):
        dialog = self.recovery()
        dialog.details_button.invoke()
        self.assertEqual(dialog.details.winfo_manager(), "pack")
        dialog.details_button.invoke()
        self.assertFalse(dialog.details.winfo_manager())


class AIConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.window = tk.Tk()
        self.window.withdraw()
        self.profile = SimpleNamespace(name="离线测试账户")
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            if dialog.window.winfo_exists():
                dialog.close()
        self.window.destroy()

    def test_valid_existing_configuration_requires_no_popup_or_ai_request(self):
        with patch("quiz_dialog.read_settings", return_value=READY), \
             patch("quiz_dialog.SettingsDialog") as settings, \
             patch("quiz_dialog.messagebox.showinfo") as notice, \
             patch("quiz_dialog.generate_suggestion") as generate:
            self.assertTrue(ensure_ai_config(self.window, self.profile))
        settings.assert_not_called()
        notice.assert_not_called()
        generate.assert_not_called()

    def modal(self, initially, *, saved=False, after=None):
        constructor = Mock(return_value=SimpleNamespace(saved=saved))
        def wait_for_close(popup):
            try:
                self.assertEqual(str(popup.transient()), str(self.window))
                self.assertIs(popup.grab_current(), popup)
            finally:
                popup.destroy()
        settings = [initially] + ([after] if after is not None else [])
        with patch("quiz_dialog.read_settings", side_effect=settings), \
             patch("quiz_dialog.SettingsDialog", constructor), \
             patch("quiz_dialog.messagebox.showinfo") as notice, \
             patch.object(self.window, "wait_window", side_effect=wait_for_close), \
             patch("quiz_dialog.generate_suggestion") as generate:
            result = ensure_ai_config(self.window, self.profile)
        self.assertEqual(constructor.call_args.args[1], self.profile)
        self.assertTrue(constructor.call_args.kwargs["close_after_save"])
        notice.assert_called_once()
        generate.assert_not_called()
        return result

    def test_missing_ai_prompts_and_cancel_keeps_quiz_closed(self):
        self.assertFalse(self.modal(EMPTY))

    def test_saved_and_enabled_configuration_can_proceed_immediately(self):
        self.assertTrue(self.modal(EMPTY, saved=True, after=READY))

    def test_disabled_partial_or_invalid_endpoint_requires_configuration(self):
        for settings in ({**READY, "enabled": False}, {**READY, "key": ""},
                         {**READY, "endpoint": "https://user:password@ai.example"}):
            with self.subTest(settings=settings["endpoint"]):
                self.assertFalse(self.modal(settings))

    def test_corrupt_settings_open_local_configuration(self):
        self.assertFalse(self.modal(QuizError("设置无法读取")))

    def dialog(self, *, modal=False):
        popup = tk.Toplevel(self.window)
        with patch("quiz_dialog.read_settings", return_value=READY):
            dialog = SettingsDialog(popup, self.profile, close_after_save=modal)
        self.dialogs.append(dialog)
        return dialog

    def test_modal_save_must_explicitly_enable_ai(self):
        dialog = self.dialog(modal=True)
        dialog.enabled.set(False)
        with patch("quiz_dialog.save_settings") as save:
            dialog.save()
        save.assert_not_called()
        self.assertIn("勾选", dialog.feedback.get())
        self.assertFalse(dialog.saved)

    def test_modal_save_closes_only_after_success(self):
        dialog = self.dialog(modal=True)
        with patch("quiz_dialog.save_settings") as save:
            dialog.save()
        save.assert_called_once_with(self.profile, READY)
        self.assertTrue(dialog.saved)
        self.assertFalse(dialog.window.winfo_exists())

    def test_regular_settings_save_keeps_manager_dialog_open(self):
        dialog = self.dialog()
        with patch("quiz_dialog.save_settings"):
            dialog.save()
        self.assertTrue(dialog.window.winfo_exists())

    def test_failed_settings_save_keeps_modal_open(self):
        dialog = self.dialog(modal=True)
        with patch("quiz_dialog.save_settings", side_effect=QuizError("请填写模型名称")):
            dialog.save()
        self.assertFalse(dialog.saved)
        self.assertTrue(dialog.window.winfo_exists())
        self.assertIn("模型", dialog.feedback.get())


if __name__ == "__main__":
    unittest.main()
