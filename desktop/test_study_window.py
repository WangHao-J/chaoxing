"""Offline UI checks for account-isolated study modes and safe cancellation."""
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

from account_store import AlreadyRunning
from quiz_assistant import Question
from study_window import MODE_TITLES, StudyWindow, entry


COURSES = [
    {"courseId": "same-course", "clazzId": "class-a", "cpi": "p", "title": "同名课程"},
    {"courseId": "same-course", "clazzId": "class-b", "cpi": "p", "title": "同名课程"},
    {"courseId": "other-course", "clazzId": "class-c", "cpi": "p", "title": "另一课程"},
]


class FakeEngine:
    def __init__(self, root, profile, mode, emit, call_ui, cancelled):
        self.root, self.profile, self.mode = root, profile, mode
        self.emit, self.call_ui, self.cancelled = emit, call_ui, cancelled
        self.calls, self.threads = [], []
        self.hold, self.started, self.release = False, threading.Event(), threading.Event()
        self.saved = [("same-course", "class-b")]
        self.reviews = 0
        self.review_results = []
        self.closed = False

    def load_courses(self):
        self.calls.append("load")
        self.threads.append(threading.get_ident())
        self.emit("log", "已读取课程")
        return COURSES

    def saved_selection(self):
        return self.saved

    def run(self, selected, speed=1.0):
        self.calls.append(("run", selected, speed))
        self.threads.append(threading.get_ident())
        self.started.set()
        if self.hold:
            self.release.wait(5)
        for _ in range(self.reviews):
            self.review_results.append(self.call_ui("quiz", "离线账户", "离线课程",
                [Question("q", "离线题目", "shortanswer", "", ["q"])], Mock(), Mock()))
        self.emit("progress", {"done": 1, "total": 2, "title": "所选章节"})
        self.started.clear()
        return "已停止，进度保留。" if self.cancelled.is_set() else "所选课程已处理完毕。"

    def close(self):
        if self.started.is_set():
            raise AssertionError("Cannot release account ownership before run ends")
        self.calls.append("close")
        self.threads.append(threading.get_ident())
        self.closed = True


class StudyWindowTests(unittest.TestCase):
    def setUp(self):
        # Earlier GUI fixtures use update() rather than a running mainloop.
        # Collect their destroyed Tk cycles here, before our worker can trigger GC.
        gc.collect()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.profile = SimpleNamespace(id="test", name="离线账户", directory=Path(self.directory.name))
        self.window = tk.Tk()
        self.window.withdraw()
        self.main_thread = threading.get_ident()
        self.controller, self.engine = None, None
        self.ai = patch("study_window.ensure_ai_config", return_value=True).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        if self.engine is not None:
            self.engine.release.set()
        if self.controller is not None and not self.controller.destroyed:
            self.controller.close()
            self.pump(lambda: self.controller.destroyed)
        elif self.controller is None:
            self.window.destroy()
        # Mock call arguments can retain Tk masters after destroy(). Release
        # them and their callback cycles on the Tk-owning thread, not a worker.
        self.controller = self.engine = self.window = None
        self.ai.reset_mock()
        self.ai = None
        patch.stopall()
        gc.collect()

    def create(self, mode="mixed", factory=None, session_id=None):
        def make_engine(*args):
            self.engine = FakeEngine(*args)
            return self.engine
        self.controller = StudyWindow(self.window, Path(self.directory.name), self.profile, mode,
                                      engine_factory=factory or make_engine, autoload=False,
                                      session_id=session_id)
        return self.controller

    def pump(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            if self.controller is not None and self.controller.destroyed:
                break
            self.window.update()
            time.sleep(.005)
        self.assertTrue(condition(), "UI operation did not complete")

    def load(self, mode="mixed"):
        controller = self.create(mode)
        controller.refresh_courses()
        self.pump(lambda: not controller.busy)
        return controller

    def test_three_modes_have_distinct_titles_and_video_has_no_ai_controls(self):
        for mode, title in MODE_TITLES.items():
            popup = tk.Toplevel(self.window)
            popup.withdraw()
            controller = StudyWindow(popup, self.directory.name, self.profile, mode, autoload=False,
                                     engine_factory=Mock(side_effect=AssertionError("Unexpected engine")))
            self.assertIn(title, popup.title())
            self.assertEqual(controller.settings_button is None, mode == "videos")
            controller.destroy()
        self.ai.assert_not_called()

    def test_load_and_selected_identity_are_exact_and_run_off_the_ui_thread(self):
        controller = self.load()
        self.assertEqual(controller.selected_keys(), [("same-course", "class-b")])
        controller.listing.selection_set("course-0", "course-2")
        controller.start_selected()
        self.pump(lambda: not controller.busy)
        self.assertEqual(self.engine.calls[1], ("run", [("same-course", "class-a"),
                                                       ("other-course", "class-c")], 1.0))
        self.assertTrue(all(ident != self.main_thread for ident in self.engine.threads))
        self.assertIn("处理完毕", controller.status.get())
        self.assertIn("已读取课程", controller.log.get("1.0", "end"))
        self.assertEqual(float(controller.progress["value"]), 1)

    def test_refresh_keeps_explicit_selection_and_does_not_expand_matching_course(self):
        controller = self.load()
        controller.listing.selection_set("course-0")
        controller.refresh_courses()
        self.pump(lambda: not controller.busy)
        self.assertEqual(controller.selected_keys(), [("same-course", "class-a")])
        self.assertEqual(len(controller.listing.get_children()), 3)

    def test_no_selection_does_not_invoke_engine_or_ai_configuration(self):
        controller = self.create()
        controller.start_selected()
        self.assertIsNone(self.engine)
        self.ai.assert_not_called()
        self.assertIn("选择", controller.status.get())

    def test_missing_ai_configuration_prevents_start_but_video_never_reads_ai(self):
        controller = self.load("videos")
        self.ai.return_value = False
        controller.start_selected()
        self.pump(lambda: not controller.busy)
        self.ai.assert_not_called()
        controller.mode = "quizzes"
        before = list(self.engine.calls)
        controller.start_selected()
        self.assertEqual(self.engine.calls, before)
        self.ai.assert_called_once()
        self.assertIn("配置", controller.status.get())

    def test_running_controls_and_methods_cannot_start_another_background_task(self):
        controller = self.load()
        self.engine.hold = True
        controller.start_selected()
        self.pump(self.engine.started.is_set)
        for button in (controller.refresh_button, controller.start_button, controller.settings_button):
            self.assertEqual(str(button["state"]), "disabled")
        self.assertEqual(str(controller.stop_button["state"]), "normal")
        controller.refresh_courses()
        controller.start_selected()
        with patch("study_window.SettingsDialog") as settings:
            controller.open_settings()
        settings.assert_not_called()
        self.assertEqual(self.engine.calls.count("load"), 1)
        self.assertEqual(len([call for call in self.engine.calls if isinstance(call, tuple)]), 1)
        self.engine.release.set()
        self.pump(lambda: not controller.busy)

    def test_stop_keeps_window_and_lock_until_work_returns_and_next_run_can_start(self):
        controller = self.load()
        self.engine.hold = True
        controller.start_selected()
        self.pump(self.engine.started.is_set)
        controller.stop()
        self.assertTrue(controller.cancelled.is_set())
        self.assertFalse(self.engine.closed)
        self.assertEqual(str(controller.start_button["state"]), "disabled")
        self.engine.release.set()
        self.pump(lambda: not controller.busy)
        self.assertFalse(controller.destroyed)
        self.assertIn("进度保留", controller.status.get())
        self.engine.hold = False
        controller.start_selected()
        self.pump(lambda: not controller.busy)
        self.assertFalse(controller.cancelled.is_set())
        self.assertFalse(self.engine.closed)

    def test_close_waits_for_active_operation_then_releases_engine_before_destroying(self):
        controller = self.load()
        self.engine.hold = True
        controller.start_selected()
        self.pump(self.engine.started.is_set)
        controller.close()
        self.assertFalse(controller.destroyed)
        self.assertFalse(self.engine.closed)
        self.assertTrue(controller.cancelled.is_set())
        self.engine.release.set()
        self.pump(lambda: controller.destroyed)
        self.assertTrue(self.engine.closed)
        self.assertEqual(self.engine.calls[-1], "close")
        self.assertNotEqual(self.engine.threads[-1], self.main_thread)

    def test_repeated_quiz_dialogs_return_to_live_parent_without_quitting_root(self):
        controller = self.load("quizzes")
        self.engine.reviews = 2
        with patch.object(self.window, "quit") as quit_root:
            controller.start_selected()
            for index in range(2):
                self.pump(lambda: controller.dialog is not None)
                dialog = controller.dialog
                self.assertIsInstance(dialog.window, tk.Toplevel)
                dialog.result = "submitted"
                dialog.finish()
                self.assertIsNone(controller.dialog)
                self.assertTrue(self.window.winfo_exists())
                if index == 0:
                    self.pump(lambda: len(self.engine.review_results) == 1)
            self.pump(lambda: not controller.busy)
            quit_root.assert_not_called()
        self.assertEqual(self.engine.review_results, ["submitted", "submitted"])
        self.assertIn("处理完毕", controller.status.get())

    def test_close_during_submit_waits_for_result_before_destroying_dialog_or_lock(self):
        controller = self.load("quizzes")
        self.engine.reviews = 1
        controller.start_selected()
        self.pump(lambda: controller.dialog is not None)
        dialog = controller.dialog
        dialog.busy, dialog.operation = True, "submit"
        controller.close()
        self.window.update()
        self.assertIs(controller.dialog, dialog)
        self.assertFalse(controller.destroyed)
        self.assertFalse(self.engine.closed)
        self.assertTrue(dialog.window.winfo_exists())
        dialog.busy, dialog.result = False, "submitted"
        dialog.finish()
        self.pump(lambda: controller.destroyed)
        self.assertEqual(self.engine.review_results, ["submitted"])
        self.assertTrue(self.engine.closed)

    def test_stop_defers_idle_review_and_no_further_review_is_opened(self):
        controller = self.load("quizzes")
        self.engine.reviews = 2
        controller.start_selected()
        self.pump(lambda: controller.dialog is not None)
        controller.stop()
        self.pump(lambda: not controller.busy)
        self.assertIsNone(controller.dialog)
        self.assertEqual(self.engine.review_results, ["defer", "defer"])

    def test_stop_cancels_bulk_generation_before_waiting_for_current_response(self):
        controller = self.load("quizzes")
        self.engine.reviews = 1
        controller.start_selected()
        self.pump(lambda: controller.dialog is not None)
        dialog = controller.dialog
        dialog.busy, dialog.operation = True, "all"
        controller.stop()
        self.assertTrue(dialog.cancel_generation.is_set())
        self.assertIs(controller.dialog, dialog)
        self.assertFalse(self.engine.closed)
        dialog.busy = False
        self.pump(lambda: not controller.busy)
        self.assertEqual(self.engine.review_results, ["defer"])

    def test_recovery_rpc_releases_waiter_and_keeps_root_event_loop(self):
        controller = self.create()
        results = []
        worker = threading.Thread(target=lambda: results.append(controller.call_ui(
            "recovery", "测试账户", "测试原因", "章节", 0, True)))
        worker.start()
        self.pump(lambda: controller.dialog is not None)
        controller.dialog.finish("retry")
        worker.join(2)
        self.assertEqual(results, ["retry"])
        self.assertTrue(self.window.winfo_exists())

    def test_dialog_constructor_failure_releases_waiter(self):
        controller = self.create()
        results = []
        worker = threading.Thread(target=lambda: results.append(controller.call_ui("quiz")))
        with patch("study_window.ReviewDialog", side_effect=RuntimeError("offline")):
            worker.start()
            self.pump(lambda: bool(results))
        worker.join(2)
        self.assertEqual(results, ["defer"])
        self.assertIsNone(controller.dialog)

    def test_load_failure_closes_partial_engine_and_never_leaves_controls_busy(self):
        engine = FakeEngine(None, self.profile, "videos", Mock(), Mock(), threading.Event())
        engine.load_courses = Mock(side_effect=[ValueError("请回管理界面重新登录"), COURSES])
        factory = Mock(return_value=engine)
        controller = self.create(factory=factory)
        controller.refresh_courses()
        self.pump(lambda: not controller.busy)
        self.assertTrue(engine.closed)
        self.assertIs(controller.engine, engine)
        self.assertIn("重新登录", controller.status.get())
        controller.refresh_courses()
        self.pump(lambda: not controller.busy)
        factory.assert_called_once()
        self.assertIs(controller.engine, engine)
        self.assertEqual(len(controller.courses), 3)

    def test_failed_refresh_clears_stale_course_selection_and_cannot_start_old_courses(self):
        controller = self.load()
        self.assertTrue(controller.selected_keys())
        self.engine.load_courses = Mock(side_effect=ValueError("登录状态已失效"))
        controller.refresh_courses()
        self.pump(lambda: not controller.busy)
        self.assertTrue(self.engine.closed)
        self.assertEqual(controller.courses, [])
        self.assertEqual(controller.course_rows, {})
        self.assertEqual(controller.listing.get_children(), ())
        self.assertEqual(controller.selected_keys(), [])
        self.assertEqual(str(controller.start_button["state"]), "disabled")
        before = list(self.engine.calls)
        controller.start_selected()
        self.assertEqual(self.engine.calls, before)
        self.ai.assert_not_called()

    def test_manager_stop_request_only_consumes_matching_session_and_waits_for_work(self):
        session_id = "a" * 32
        controller = self.create(session_id=session_id)
        controller.refresh_courses()
        self.pump(lambda: not controller.busy)
        self.engine.hold = True
        controller.start_selected()
        self.pump(self.engine.started.is_set)
        other_request = self.profile.directory / ("study_stop_" + "b" * 32 + ".request")
        other_request.touch()
        controller.stop_request.touch()
        controller.last_stop_check = 0
        self.pump(controller.cancelled.is_set)
        self.assertFalse(controller.stop_request.exists())
        self.assertTrue(other_request.exists())
        self.assertTrue(controller.busy)
        self.assertFalse(self.engine.closed)
        self.engine.release.set()
        self.pump(lambda: not controller.busy)
        self.assertIn("进度保留", controller.status.get())

    def test_absent_session_does_not_check_for_stop_request_files(self):
        controller = self.create("quizzes")
        self.assertIsNone(controller.stop_request)
        with patch.object(Path, "is_file", side_effect=AssertionError("Unexpected file check")), \
             patch.object(Path, "unlink", side_effect=AssertionError("Unexpected unlink")):
            self.window.update()

    def test_session_identifier_cannot_construct_an_arbitrary_path(self):
        with self.assertRaises(ValueError):
            self.create(session_id="../escape")

    def test_video_speed_uses_profile_default_and_passes_exact_selected_speed(self):
        (self.profile.directory / "config.ini").write_text(
            "[common]\nspeed = 1.5\npassword = synthetic-not-used\n", encoding="utf-8")
        controller = self.load("videos")
        self.assertEqual(controller.speed.get(), "1.5")
        self.assertEqual(str(controller.speed_box["state"]), "readonly")
        controller.speed.set("1.25")
        self.engine.hold = True
        controller.start_selected()
        self.pump(self.engine.started.is_set)
        self.assertEqual(self.engine.calls[-1][2], 1.25)
        self.assertEqual(str(controller.speed_box["state"]), "disabled")
        self.engine.release.set()
        self.pump(lambda: not controller.busy)
        self.assertEqual(str(controller.speed_box["state"]), "readonly")

    def test_quiz_window_never_reads_video_speed_configuration(self):
        with patch("study_window.configured_video_speed", side_effect=AssertionError("No video settings")):
            controller = self.create("quizzes")
        self.assertIsNone(controller.speed)
        self.assertIsNone(controller.speed_box)

    def test_video_progress_uses_media_time_and_chapter_progress_stays_a_count(self):
        controller = self.create("videos")
        controller.emit("progress", {"done": 31, "total": 508, "title": "视频", "unit": "seconds"})
        self.pump(lambda: "00:31" in controller.progress_text.get())
        self.assertEqual(controller.progress_text.get(), "00:31 / 08:28　视频")
        controller.emit("progress", {"done": 2, "total": 4, "title": "章节"})
        self.pump(lambda: controller.progress_text.get().startswith("2/4"))

    def test_close_waits_for_busy_recovery_request_before_releasing_rpc(self):
        controller = self.load("videos")
        self.engine.run = lambda *args: self.engine.call_ui("recovery", "账户", "网络验证", "章节", 0, True)
        controller.start_selected()
        self.pump(lambda: controller.dialog is not None)
        dialog = controller.dialog
        dialog.busy = True
        controller.close()
        self.window.update()
        self.assertIs(controller.dialog, dialog)
        self.assertFalse(self.engine.closed)
        self.assertFalse(controller.destroyed)
        dialog.busy = False
        self.pump(lambda: controller.destroyed)
        self.assertTrue(self.engine.closed)

    def test_second_window_account_lock_failure_is_displayed_and_no_run_starts(self):
        factory = Mock(side_effect=AlreadyRunning("该账户已有一个运行窗口"))
        controller = self.create(factory=factory)
        controller.refresh_courses()
        self.pump(lambda: not controller.busy)
        self.assertIn("已有一个运行窗口", controller.status.get())
        self.assertIsNone(controller.engine)
        self.assertEqual(str(controller.start_button["state"]), "disabled")

    def test_settings_and_run_are_mutually_exclusive_and_restore_after_close(self):
        controller = self.load()
        settings = Mock()
        with patch("study_window.SettingsDialog", return_value=settings):
            controller.open_settings()
        popup = controller.settings_window
        self.assertIsNotNone(popup)
        before = list(self.engine.calls)
        controller.start_selected()
        controller.refresh_courses()
        self.assertEqual(self.engine.calls, before)
        self.assertEqual(str(controller.start_button["state"]), "disabled")
        popup.destroy()
        self.assertIsNone(controller.settings_window)
        self.assertEqual(str(controller.start_button["state"]), "normal")

    def test_offscreen_ui_check_all_modes_never_constructs_engine_or_calls_network(self):
        for mode in MODE_TITLES:
            output = io.StringIO()
            with patch("study_window.AccountStore") as store, \
                 patch("study_window.StudyWindow.launch") as launch, \
                 patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")), \
                 contextlib.redirect_stdout(output):
                store.return_value.get.return_value = self.profile
                self.assertEqual(entry(["--root", self.directory.name, "--profile", "test", "--mode", mode,
                                        "--ui-check"]), 0)
            launch.assert_not_called()
            result = json.loads(output.getvalue())
            self.assertEqual(result["mode"], mode)
            self.assertFalse(result["network_called"])

    def test_logs_are_collapsed_until_requested_and_keep_existing_messages(self):
        controller = self.create()
        controller.append_log("离线运行记录")
        self.assertFalse(controller.logs_visible)
        self.assertEqual(controller.log_frame.winfo_manager(), "")
        controller.toggle_logs()
        self.window.update_idletasks()
        self.assertTrue(controller.logs_visible)
        self.assertEqual(controller.log_frame.winfo_manager(), "grid")
        self.assertIn("离线运行记录", controller.log.get("1.0", "end"))
        controller.toggle_logs()
        self.assertEqual(controller.log_frame.winfo_manager(), "")

    def test_activity_distinguishes_elapsed_operation_from_last_update(self):
        controller = self.load("videos")
        self.engine.hold = True
        controller.start_selected()
        self.pump(self.engine.started.is_set)
        self.pump(lambda: "最近更新" in controller.activity_text.get())
        self.assertIn("已用", controller.activity_text.get())
        self.engine.release.set()
        self.pump(lambda: not controller.busy)
        self.assertEqual(controller.activity_text.get(), "操作已结束")

    def test_rebuild_closes_engine_before_independent_repository_lock_and_requires_reopen(self):
        controller = self.load()
        order = []
        close = self.engine.close
        def close_engine():
            close()
            order.append("engine closed")
        self.engine.close = close_engine
        def rebuild(profile, mode):
            self.assertTrue(self.engine.closed)
            self.assertEqual((profile, mode), (self.profile, "mixed"))
            self.assertNotEqual(threading.get_ident(), self.main_thread)
            order.append("rebuild")
            return self.profile.directory / "backup.json"
        repository = SimpleNamespace(recover_mode=Mock(side_effect=rebuild))
        with patch("study_window.messagebox.askyesno", return_value=True), \
             patch.dict("sys.modules", {"task_repository": repository}):
            controller.recover_mode()
            self.pump(lambda: not controller.busy)
        self.assertEqual(order, ["engine closed", "rebuild"])
        self.assertTrue(controller.recovered)
        self.assertIsNone(controller.engine)
        self.assertEqual(str(controller.start_button["state"]), "disabled")
        self.assertEqual(str(controller.refresh_button["state"]), "disabled")
        self.assertIn("重新打开", controller.status.get())
        controller.start_selected()
        self.assertIsNone(controller.engine)

    def test_rebuild_is_unavailable_during_run_and_cancel_does_not_change_files(self):
        controller = self.load()
        with patch("study_window.messagebox.askyesno", return_value=False) as confirm:
            controller.recover_mode()
        confirm.assert_called_once()
        self.assertFalse(self.engine.closed)
        self.engine.hold = True
        controller.start_selected()
        self.pump(self.engine.started.is_set)
        with patch("study_window.messagebox.askyesno") as confirm:
            controller.recover_mode()
        confirm.assert_not_called()
        self.assertEqual(str(controller.recover_button["state"]), "disabled")
        self.engine.release.set()
        self.pump(lambda: not controller.busy)

    def test_all_main_controls_fit_minimum_window_with_log_expanded(self):
        controller = self.create("mixed")
        self.window.geometry("760x620+10000+10000")
        self.window.deiconify()
        controller.toggle_logs()
        self.window.update()
        bottom = self.window.winfo_rooty() + self.window.winfo_height()
        for widget in (controller.start_button, controller.stop_button, controller.settings_button,
                       controller.refresh_button, controller.listing, controller.log_button,
                       controller.recover_button, controller.log):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom)


if __name__ == "__main__":
    unittest.main()
