"""Offline checks for opening a pending chapter in the program's quiz UI."""
import json
import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from account_manager import AccountManager


class PendingListTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.profile = SimpleNamespace(directory=Path(self.directory.name), id="test", name="离线测试账户")
        self.tasks = [self.task("chapter-a"), self.task("chapter-b")]
        self.save(self.tasks)
        self.window = tk.Tk()
        self.window.withdraw()
        self.window.geometry("900x560+10000+10000")
        self.addCleanup(self.window.destroy)
        real_toplevel = tk.Toplevel

        def hidden_toplevel(parent):
            popup = real_toplevel(parent)
            popup.withdraw()
            popup.geometry("760x420+10000+10000")
            return popup

        self.toplevel = self.enter_patch(patch("account_manager.tk.Toplevel", side_effect=hidden_toplevel))
        self.notice = self.enter_patch(patch("account_manager.messagebox.showinfo"))
        self.browser = self.enter_patch(patch("account_manager.webbrowser.open"))
        self.process = self.enter_patch(patch("account_manager.subprocess.Popen"))
        self.enter_patch(patch("account_manager.is_running", return_value=False))
        store = Mock()
        store.list.return_value = [self.profile]
        store.get.return_value = self.profile
        self.manager = AccountManager(self.window, store)
        self.manager.open_pending_quiz = Mock()
        self.addCleanup(lambda: self.window.after_cancel(self.manager.timer))

    def enter_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    @staticmethod
    def task(chapter, **extra):
        return {"course_id": "course-a", "class_id": "class-a", "chapter_id": chapter,
                "course_title": "测试课程", "chapter_title": chapter,
                "manual_tasks": ["测试测验"], **extra}

    def save(self, tasks):
        (self.profile.directory / "pending_tasks.json").write_text(
            json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8")

    def open(self):
        self.manager.open_pending()
        popup = self.toplevel.call_args_list and self.window.winfo_children()[-1]
        self.assertTrue(hasattr(popup, "pending_listing"))
        return popup

    def show_offscreen(self, popup):
        popup.geometry("760x420+10000+10000")
        popup.deiconify()
        self.window.update()

    def click(self, popup, index, *, event_time=1000):
        self.show_offscreen(popup)
        bounds = popup.pending_listing.bbox(index)
        self.assertIsNotNone(bounds)
        return popup.pending_click(SimpleNamespace(x=5, y=bounds[1] + bounds[3] // 2, time=event_time))

    def assert_no_external_actions(self):
        self.browser.assert_not_called()
        self.process.assert_not_called()
        self.notice.assert_not_called()

    def test_initial_selection_and_location_do_not_open_a_quiz(self):
        popup = self.open()
        self.assertEqual(popup.pending_listing.curselection(), (0,))
        self.manager.open_pending_quiz.assert_not_called()
        self.assertEqual(str(popup.state()), "withdrawn")
        self.assert_no_external_actions()

    def test_clicking_a_real_row_opens_the_selected_chapter_in_the_program(self):
        popup = self.open()
        self.assertEqual(self.click(popup, 1), "break")
        self.manager.open_pending_quiz.assert_called_once_with(self.profile, {**self.tasks[1], "sources": ["legacy"]}, parent=popup)
        self.assertEqual(popup.pending_listing.curselection(), (1,))
        self.assert_no_external_actions()

    def test_clicking_blank_list_area_does_not_open_the_nearest_row(self):
        popup = self.open()
        self.show_offscreen(popup)
        listing = popup.pending_listing
        last = listing.bbox(1)
        self.assertGreater(listing.winfo_height(), last[1] + last[3] + 10)
        popup.pending_click(SimpleNamespace(x=5, y=last[1] + last[3] + 10, time=1000))
        self.manager.open_pending_quiz.assert_not_called()

    def test_clicks_outside_the_widget_width_do_not_open_a_task(self):
        popup = self.open()
        self.show_offscreen(popup)
        y = popup.pending_listing.bbox(0)[1] + 2
        for x in (-1, popup.pending_listing.winfo_width()):
            popup.pending_click(SimpleNamespace(x=x, y=y, time=1000))
        self.manager.open_pending_quiz.assert_not_called()

    def test_double_click_is_suppressed_even_when_first_dialog_delays_the_callback(self):
        popup = self.open()
        with patch("account_manager.time.monotonic", side_effect=[20.0, 35.0]):
            self.click(popup, 0, event_time=1000)
            self.click(popup, 0, event_time=1100)
        self.manager.open_pending_quiz.assert_called_once()

    def test_clicking_same_task_later_can_open_it_again(self):
        popup = self.open()
        with patch("account_manager.time.monotonic", side_effect=[20.0, 21.0]):
            self.click(popup, 0, event_time=1000)
            self.click(popup, 0, event_time=2000)
        self.assertEqual(self.manager.open_pending_quiz.call_count, 2)

    def test_clicking_another_task_immediately_opens_that_task(self):
        popup = self.open()
        with patch("account_manager.time.monotonic", side_effect=[20.0, 20.1]):
            self.click(popup, 0, event_time=1000)
            self.click(popup, 1, event_time=1100)
        self.assertEqual(self.manager.open_pending_quiz.call_count, 2)
        self.assertEqual(self.manager.open_pending_quiz.call_args.args[1]["chapter_id"], "chapter-b")

    def test_enter_opens_the_selected_task_and_zero_timestamps_do_not_block_later_opening(self):
        popup = self.open()
        popup.pending_listing.selection_clear(0, "end")
        popup.pending_listing.selection_set(1)
        with patch("account_manager.time.monotonic", side_effect=[20.0, 21.0]):
            popup.pending_open_selected(SimpleNamespace(time=0))
            popup.pending_open_selected(SimpleNamespace(time=0))
        self.assertEqual(self.manager.open_pending_quiz.call_count, 2)
        self.assertTrue(popup.pending_listing.bind("<Return>"))

    def test_button_opens_the_selected_quiz_and_click_bindings_are_installed(self):
        popup = self.open()
        popup.pending_open_button.invoke()
        self.manager.open_pending_quiz.assert_called_once_with(self.profile, {**self.tasks[0], "sources": ["legacy"]}, parent=popup)
        self.assertTrue(popup.pending_listing.bind("<Button-1>"))
        self.assertTrue(popup.pending_listing.bind("<Double-Button-1>"))

    def test_programmatic_selection_event_changes_location_without_opening_a_quiz(self):
        popup = self.open()
        popup.pending_listing.selection_clear(0, "end")
        popup.pending_listing.selection_set(1)
        popup.pending_listing.event_generate("<<ListboxSelect>>")
        self.window.update()
        self.manager.open_pending_quiz.assert_not_called()

    def test_reloading_keeps_the_exact_course_class_and_chapter_selection(self):
        self.tasks = [self.task("same", class_id="class-a"), self.task("same", class_id="class-b")]
        self.save(self.tasks)
        popup = self.open()
        popup.pending_listing.selection_clear(0, "end")
        popup.pending_listing.selection_set(1)
        self.save(list(reversed(self.tasks)))
        popup.pending_reload()
        self.assertEqual(popup.pending_listing.curselection(), (0,))
        self.assertEqual(popup.pending_tasks[0]["class_id"], "class-b")
        self.manager.open_pending_quiz.assert_not_called()

    def test_reloading_after_a_task_is_removed_selects_remaining_task_without_opening_it(self):
        popup = self.open()
        popup.pending_listing.selection_clear(0, "end")
        popup.pending_listing.selection_set(1)
        self.save(self.tasks[:1])
        popup.pending_reload()
        self.assertEqual(popup.pending_listing.curselection(), (0,))
        self.assertEqual(popup.pending_listing.size(), 1)
        self.manager.open_pending_quiz.assert_not_called()

    def test_empty_reloaded_list_has_no_selection_or_automatic_action(self):
        popup = self.open()
        self.save([])
        popup.pending_reload()
        popup.pending_open_button.invoke()
        self.assertFalse(popup.pending_listing.curselection())
        self.assertIn("没有", popup.pending_feedback.get())
        self.manager.open_pending_quiz.assert_not_called()

    def test_legacy_missing_identifiers_show_inline_refresh_instructions(self):
        for missing in ({"chapter_id": ""}, {"class_id": None}, {"course_id": "   "}):
            with self.subTest(missing=missing):
                self.save([self.task("chapter-a", **missing)])
                popup = self.open()
                popup.pending_open_button.invoke()
                self.assertIn("先启动该账户刷新任务", popup.pending_feedback.get())
                self.manager.open_pending_quiz.assert_not_called()
                popup.destroy()
        self.assert_no_external_actions()

    def test_legacy_course_portal_and_copy_controls_remain_available(self):
        self.save([{"course_title": "原课程", "chapter_title": "原章节", "manual_tasks": ["外部链接"]}])
        popup = self.open()
        widgets = list(popup.winfo_children())
        buttons = []
        while widgets:
            widget = widgets.pop()
            widgets.extend(widget.winfo_children())
            if widget.winfo_class() == "TButton":
                buttons.append(widget)
        next(button for button in buttons if button.cget("text") == "打开课程入口").invoke()
        self.browser.assert_called_once_with("https://i.chaoxing.com/")
        next(button for button in buttons if "复制" in button.cget("text")).invoke()
        self.assertEqual(popup.clipboard_get(), "课程：原课程\n章节：原章节")
        self.manager.open_pending_quiz.assert_not_called()

    def test_invalid_task_container_is_visible_as_diagnostic_and_cannot_be_opened(self):
        for tasks in ({"a": 1}, "not-a-list", 42, ["not-a-row"], [{}, None]):
            with self.subTest(tasks=tasks):
                self.save(tasks)
                before = (self.profile.directory / "pending_tasks.json").read_bytes()
                popup = self.open()
                self.assertTrue(popup.pending_tasks[0]["record_error"])
                popup.pending_open_button.invoke()
                self.assertEqual((self.profile.directory / "pending_tasks.json").read_bytes(), before)
                popup.destroy()
        self.manager.open_pending_quiz.assert_not_called()

    def test_invalid_json_does_not_hide_the_diagnostic_or_overwrite_records(self):
        for raw in ("[]", "null", "false", "{bad json"):
            with self.subTest(raw=raw):
                (self.profile.directory / "pending_tasks.json").write_text(raw, encoding="utf-8")
                popup = self.open()
                self.assertTrue(popup.pending_tasks[0]["record_error"])
                self.assertEqual((self.profile.directory / "pending_tasks.json").read_text(encoding="utf-8"), raw)
                popup.destroy()

    def test_new_mode_tasks_are_merged_and_open_with_exact_chapter_scope(self):
        rows = [self.task("chapter-a", reason="等待确认"), self.task("chapter-new", reason="用户跳过")]
        path = self.profile.directory / "study_state_quizzes.json"
        path.write_text(json.dumps({"version": 1, "mode": "quizzes", "courses": [], "pending": rows}), encoding="utf-8")
        popup = self.open()
        self.assertEqual(popup.pending_listing.size(), 3)
        self.assertEqual(popup.pending_tasks[0]["sources"], ["legacy", "quizzes"])
        self.click(popup, 2)
        opened = self.manager.open_pending_quiz.call_args.args[1]
        self.assertEqual(opened["chapter_id"], "chapter-new")
        self.assertEqual(opened["sources"], ["quizzes"])
        self.assertEqual(opened["reason"], "用户跳过")

    def test_invalid_manual_title_field_does_not_crash_list_rendering(self):
        self.save([self.task("chapter-a", manual_tasks=None)])
        popup = self.open()
        self.assertEqual(popup.pending_listing.size(), 1)
        self.assertIn("chapter-a", popup.pending_listing.get(0))


class PendingQuizLauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.profile = SimpleNamespace(directory=self.root / "accounts" / "offline-test",
                                       id="offline-test", name="离线测试账户")
        self.profile.directory.mkdir(parents=True)
        self.pending_path = self.profile.directory / "pending_tasks.json"
        self.pending_path.write_text('{"tasks": []}', encoding="utf-8")
        self.python = self.root / "python" / "python.exe"
        self.python.parent.mkdir()
        self.python.touch()
        self.pythonw = self.python.with_name("pythonw.exe")
        self.pythonw.touch()
        self.task = {"course_id": "course-a", "class_id": "class-b", "chapter_id": "chapter-c",
                     "api_key": "synthetic-private-key", "password": "synthetic-private-password"}
        self.manager = AccountManager.__new__(AccountManager)
        self.manager.store = SimpleNamespace(root=self.root)
        self.manager.note = Mock()
        self.manager.processes = {}
        self.manager.quiz_processes = {}
        self.parent = SimpleNamespace(pending_feedback=Mock(), pending_reload=Mock(),
                                       winfo_exists=Mock(return_value=True), after=Mock())
        self.active = self.enter_patch(patch("account_manager.is_running", return_value=False))
        self.ensure = self.enter_patch(patch("quiz_dialog.ensure_ai_config", return_value=True))
        self.account_python = self.enter_patch(patch("account_manager.account_python", return_value=self.python))
        self.launcher = self.enter_patch(patch("account_manager.subprocess.Popen"))
        self.process = self.launcher.return_value
        self.process.poll.return_value = None

    def enter_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def launch(self):
        return self.manager.open_pending_quiz(self.profile, self.task, parent=self.parent)

    def watch(self):
        self.parent.after.call_args.args[1]()

    def assert_not_launched(self):
        self.ensure.assert_not_called()
        self.launcher.assert_not_called()
        self.parent.after.assert_not_called()

    def test_running_account_lock_blocks_configuration_and_new_worker(self):
        self.active.return_value = True
        self.assertIsNone(self.launch())
        self.assert_not_launched()
        self.assertIn("先停止", self.parent.pending_feedback.set.call_args.args[0])

    def test_running_backend_owned_by_manager_blocks_configuration_and_new_worker(self):
        backend = Mock()
        backend.poll.return_value = None
        self.manager.processes[self.profile.id] = backend
        self.assertIsNone(self.launch())
        self.assert_not_launched()
        backend.terminate.assert_not_called()

    def test_an_open_quiz_worker_blocks_duplicate_launches_before_configuration(self):
        previous = Mock()
        previous.poll.return_value = None
        self.manager.quiz_processes[self.profile.id] = previous
        self.assertIsNone(self.launch())
        self.assert_not_launched()
        self.active.assert_not_called()
        self.assertIn("原窗口", self.parent.pending_feedback.set.call_args.args[0])

    def test_cancelled_ai_configuration_does_not_launch_worker(self):
        self.ensure.return_value = False
        self.assertIsNone(self.launch())
        self.ensure.assert_called_once_with(self.parent, self.profile)
        self.launcher.assert_not_called()
        self.parent.after.assert_not_called()
        self.assertIn("配置", self.parent.pending_feedback.set.call_args.args[0])

    def test_launch_arguments_contain_only_the_exact_account_and_chapter_identifiers(self):
        self.assertIs(self.launch(), self.process)
        self.ensure.assert_called_once_with(self.parent, self.profile)
        argv = self.launcher.call_args.args[0]
        self.assertEqual(argv[:-2], [str(self.pythonw), str(self.root / "pending_quiz_worker.py"),
                               "--root", str(self.root), "--profile", self.profile.id,
                               "--course", "course-a", "--class-id", "class-b", "--chapter", "chapter-c"])
        self.assertEqual(argv[-2], "--session-id")
        self.assertRegex(argv[-1], r"^[a-f0-9]{32}$")
        for secret in (self.task["api_key"], self.task["password"]):
            self.assertNotIn(secret, " ".join(argv))
        self.assertIs(self.manager.quiz_processes[self.profile.id], self.process)

    def test_pythonw_and_no_console_flags_keep_quiz_worker_out_of_the_terminal(self):
        self.launch()
        self.account_python.assert_called_once_with(self.root)
        self.assertEqual(self.launcher.call_args.args[0][0], str(self.pythonw))
        options = self.launcher.call_args.kwargs
        import subprocess
        self.assertEqual(options["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(options["cwd"], self.profile.directory)
        self.assertEqual(options["env"]["PYTHONUTF8"], "1")
        for key in ("TEMP", "TMP"):
            self.assertEqual(options["env"][key], str(self.profile.directory / "temp"))

    def test_missing_pythonw_uses_the_same_account_python_with_no_console_flags(self):
        self.pythonw.unlink()
        self.launch()
        self.assertEqual(self.launcher.call_args.args[0][0], str(self.python))

    def test_launch_failure_is_reported_inline_without_a_watcher_or_process_record(self):
        self.launcher.side_effect = OSError("synthetic-private-key")
        self.assertIsNone(self.launch())
        self.assertNotIn(self.profile.id, self.manager.quiz_processes)
        self.parent.after.assert_not_called()
        text = self.parent.pending_feedback.set.call_args.args[0]
        self.assertIn("未能启动", text)
        self.assertNotIn("synthetic-private-key", text)

    def test_running_worker_with_unchanged_pending_file_only_schedules_the_next_poll(self):
        self.launch()
        self.watch()
        self.parent.pending_reload.assert_not_called()
        self.assertEqual(self.parent.after.call_count, 2)
        self.assertIs(self.manager.quiz_processes[self.profile.id], self.process)

    def test_worker_exit_reloads_the_pending_list_and_clears_its_process_record(self):
        self.launch()
        self.process.poll.return_value = 0
        self.watch()
        self.parent.pending_reload.assert_called_once_with()
        self.assertNotIn(self.profile.id, self.manager.quiz_processes)
        self.assertEqual(self.parent.after.call_count, 1)

    def test_updated_pending_file_reloads_the_list_while_worker_remains_open(self):
        self.launch()
        stamp = self.pending_path.stat().st_mtime_ns
        os.utime(self.pending_path, ns=(stamp + 2000000000, stamp + 2000000000))
        self.watch()
        self.parent.pending_reload.assert_called_once_with()
        self.assertEqual(self.parent.after.call_count, 2)
        self.assertIs(self.manager.quiz_processes[self.profile.id], self.process)

    def test_closed_pending_window_stops_polling_and_does_not_touch_destroyed_widgets(self):
        self.launch()
        self.parent.winfo_exists.return_value = False
        self.watch()
        self.parent.pending_reload.assert_not_called()
        self.assertEqual(self.parent.after.call_count, 1)

    def test_finished_previous_quiz_worker_is_replaced_by_a_new_launch(self):
        previous = Mock()
        previous.poll.return_value = 0
        self.manager.quiz_processes[self.profile.id] = previous
        self.assertIs(self.launch(), self.process)
        self.assertIs(self.manager.quiz_processes[self.profile.id], self.process)
        previous.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
