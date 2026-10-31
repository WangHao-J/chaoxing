"""Offline regression checks: account routing, private workers and shared AI."""
import gc
import json
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
from pathlib import Path
from unittest.mock import patch

from account_store import AccountStore
from quiz_assistant import Question, QuizError, read_settings, save_settings
from shared_ai_settings import migrate_shared_settings, shared_profile
from workspace import Workspace
from workspace_transport import WorkerClient


def make_store(directory):
    root = Path(directory)
    (root / "app" / "resource").mkdir(parents=True)
    (root / "app" / "config.ini").write_text("[common]\nspeed=1\n", encoding="utf-8")
    return AccountStore(root)


class FakeEngine:
    def __init__(self, root, profile, mode, emit, call_ui, cancelled):
        self.profile, self.emit, self.call_ui, self.cancelled = profile, emit, call_ui, cancelled
        self.started, self.submit_started, self.submit_release = threading.Event(), threading.Event(), threading.Event()
        self.closed, self.review = False, False
        self.answers = None
        self.load_count = 0

    def load_courses(self):
        self.load_count += 1
        return [{"courseId": "same", "clazzId": "class-a", "title": "同名课程"},
                {"courseId": "same", "clazzId": "class-b", "title": "同名课程"}]

    def saved_selection(self):
        return [("same", "class-b")]

    def run(self, selected, speed=1, auto_quizzes=None):
        self.auto_quizzes = auto_quizzes
        self.started.set()
        if self.review:
            def generate(question):
                return ["答案"], "说明"
            def submit(answers):
                self.submit_started.set()
                self.submit_release.wait(5)
                self.answers = answers
                return "submitted"
            self.call_ui("quiz", self.profile.name, "离线测验",
                         [Question("q1", "测试题", "shortanswer", "", ["q1"])], generate, submit)
        else:
            self.cancelled.wait(5)
        return "已停止，记录已保留。"

    def close(self):
        self.closed = True


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temp = tempfile.TemporaryDirectory()
        self.store = make_store(self.temp.name)
        self.a, self.b = self.store.create("账号 A"), self.store.create("账号 B")
        self.window = tk.Tk()
        self.window.withdraw()
        self.window.report_callback_exception = self.callback_error
        self.errors, self.engines = [], {}
        def factory(*args):
            engine = FakeEngine(*args)
            self.engines[args[1].id] = engine
            return engine
        self.app = Workspace(self.window, self.store, fixture=True, engine_factory=factory)
        self.window.geometry("1280x820+10000+10000")
        self.window.deiconify()
        self.window.update()

    def callback_error(self, *args):
        self.errors.append(str(args[1]))

    def pump(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.window.update()
            time.sleep(.008)
        self.assertTrue(predicate(), "UI operation timed out")

    def tearDown(self):
        for engine in self.engines.values():
            engine.submit_release.set()
        self.app.close(confirm=False)
        self.pump(lambda: self.app.destroyed)
        self.assertEqual(self.errors, [])
        self.app = self.window = None
        self.engines.clear()
        gc.collect()
        self.temp.cleanup()

    def load(self, profile):
        self.app.select(profile.id)
        panel = self.app.panels[profile.id]
        panel.study.refresh_courses()
        self.pump(lambda: not panel.study.busy)
        return panel

    def test_many_accounts_have_one_window_and_switch_keeps_selection(self):
        a = self.load(self.a)
        a.study.listing.selection_set("course-0")
        self.load(self.b)
        self.app.select(self.a.id)
        self.assertEqual(a.study.selected_keys(), [("same", "class-a")])
        self.assertEqual(self.window.grab_current(), None)
        self.assertFalse(any(isinstance(widget, tk.Toplevel) for widget in self.window.winfo_children()))
        self.assertEqual(len(self.app.panels), 2)

    def test_switching_filtering_and_settings_do_not_stop_other_accounts(self):
        a = self.load(self.a)
        a.study.start_selected()
        self.pump(lambda: self.engines[self.a.id].started.is_set())
        b = self.load(self.b)
        b.study.start_selected()
        self.pump(lambda: self.engines[self.b.id].started.is_set())
        self.app.search.set("账号 B")
        self.app.show_settings()
        self.window.update()
        self.assertFalse(a.study.cancelled.is_set())
        self.assertFalse(b.study.cancelled.is_set())
        self.app.select(self.b.id)
        b.stop()
        self.pump(lambda: not b.study.busy)
        self.assertFalse(a.study.cancelled.is_set())
        self.assertTrue(a.study.busy)

    def test_simultaneous_reviews_never_grab_focus_or_switch_accounts(self):
        a, b = self.load(self.a), self.load(self.b)
        self.engines[self.a.id].review = self.engines[self.b.id].review = True
        a.study.start_selected()
        b.study.start_selected()
        self.app.select(self.b.id)
        self.pump(lambda: a.study.dialog is not None and b.study.dialog is not None)
        self.assertEqual(self.app.current_id, self.b.id)
        self.assertEqual(self.window.grab_current(), None)
        self.assertEqual(a.page, "courses")
        self.assertEqual(b.page, "courses")
        a.show("task")
        a.study.dialog.variables[0].set("A 的答案")
        self.app.select(self.a.id)
        self.app.select(self.b.id)
        b.show("task")
        b.study.dialog.variables[0].set("B 的答案")
        self.assertEqual(a.study.dialog.current_values(), ["A 的答案"])
        self.assertEqual(b.study.dialog.current_values(), ["B 的答案"])
        self.assertFalse(any(isinstance(widget, tk.Toplevel) for widget in self.window.winfo_children()))

    def test_exit_waits_for_inflight_submission_before_releasing_account(self):
        a = self.load(self.a)
        engine = self.engines[self.a.id]
        engine.review = True
        a.study.start_selected()
        self.pump(lambda: a.study.dialog is not None)
        dialog = a.study.dialog
        dialog.variables[0].set("答案")
        dialog.store_current()
        dialog.reviewed.set(True)
        dialog.confirm_submit()
        self.pump(engine.submit_started.is_set)
        controller = a.study
        self.app.close(confirm=False)
        for _ in range(10):
            self.window.update()
            time.sleep(.01)
        self.assertFalse(engine.closed)
        self.assertFalse(controller.destroyed)
        engine.submit_release.set()
        self.pump(lambda: controller.destroyed)
        self.assertTrue(engine.closed)
        self.assertEqual(engine.answers, {"q1": ["答案"]})

    def test_mode_changes_release_old_backend_without_opening_windows(self):
        a = self.load(self.a)
        old = self.engines[self.a.id]
        a.mode_choice.set(MODE_VIDEOS)
        a.change_mode()
        self.pump(lambda: a.study is not None and a.study.mode == "videos")
        self.assertTrue(old.closed)
        self.assertEqual(a.page, "courses")
        self.assertFalse(any(isinstance(widget, tk.Toplevel) for widget in self.window.winfo_children()))

    def test_global_settings_keep_edits_while_switching_accounts(self):
        self.app.show_settings()
        self.app.settings.fields["model"].set("draft-model")
        self.app.select(self.b.id)
        self.app.show_settings()
        self.assertEqual(self.app.settings.fields["model"].get(), "draft-model")
        self.assertEqual(self.app.settings.profile.name, "所有账号共用")

    def test_moving_running_account_into_category_preserves_backend_and_selection(self):
        panel = self.load(self.a)
        panel.study.start_selected()
        self.pump(lambda: self.engines[self.a.id].started.is_set())
        key = self.app.groups.create("同学")
        self.app.move_account(self.a.id, key)
        self.window.update()
        self.assertEqual(self.app.tree.parent(self.a.id), "group/" + key)
        self.assertFalse(panel.study.cancelled.is_set())
        self.assertTrue(panel.study.busy)
        self.assertEqual(self.app.current_id, self.a.id)
        self.app.tree.selection_set("group/" + key)
        self.app.selected()
        self.assertEqual(self.app.current_id, self.a.id)
        self.assertEqual(self.app.selected_group, key)

    def test_successful_login_automatically_loads_courses_once_without_switching_accounts(self):
        a = self.app.panels[self.app.current_id]
        self.app.select(self.b.id)
        a.login_closed(None, True)
        self.pump(lambda: a.study is not None and bool(a.study.courses))
        self.assertEqual(self.engines[a.profile.id].load_count, 1)
        self.assertEqual(self.app.current_id, self.b.id)
        self.assertIsNone(a.study.refresh_button)
        self.assertIsNone(a.study.settings_button)

    def test_saved_login_loads_when_account_is_opened_and_repeated_switches_keep_courses(self):
        (self.b.directory / "cookies.txt").write_text("offline-test-cookie", encoding="utf-8")
        self.app.auto_load_courses = True
        self.app.select(self.b.id)
        b = self.app.panels[self.b.id]
        self.pump(lambda: bool(b.study.courses))
        self.app.select(self.a.id)
        self.app.select(self.b.id)
        self.window.update()
        self.assertEqual(self.engines[self.b.id].load_count, 1)

    def test_cancelled_login_does_not_load_and_api_has_one_visible_entry(self):
        panel = self.app.panels[self.app.current_id]
        panel.login_closed(None, False)
        self.window.update()
        self.assertEqual(self.engines, {})
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        entries = [widget for widget in descendants(self.window) if isinstance(widget, ttk.Button)
                   and widget.cget("text") == "统一 API 设置"]
        self.assertEqual(len(entries), 1)

    def test_review_submission_controls_remain_visible_at_minimum_size(self):
        a = self.load(self.a)
        self.engines[self.a.id].review = True
        a.study.start_selected()
        self.pump(lambda: a.study.dialog is not None)
        a.show("task")
        self.window.geometry("1120x740+10000+10000")
        self.window.update()
        for widget in (a.study.dialog.submit_button, a.study.dialog.review_checkbox,
                       a.study.dialog.skip_button, a.study.dialog.skip_all_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.window.winfo_rooty() + self.window.winfo_height())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), self.window.winfo_rootx() + self.window.winfo_width())

    def test_many_profiles_are_lazy_and_search_does_not_change_current_account(self):
        for number in range(24):
            self.store.create("批量账号 " + str(number))
        self.app.refresh()
        self.assertEqual(len(self.app.account_items()), 26)
        previous = self.app.current_id
        self.app.search.set("批量账号 23")
        self.window.update()
        self.assertEqual(len(self.app.account_items()), 1)
        self.assertEqual(self.app.current_id, previous)
        self.assertEqual(len(self.app.panels), 1)

    def test_real_private_workers_roundtrip_two_embedded_quizzes(self):
        self.app.engine_factory = None
        for panel in self.app.panels.values():
            panel.study.engine_factory = lambda *args: WorkerClient(*args, fixture=True)
        for profile in (self.a, self.b):
            panel = self.load(profile)
            panel.study.speed.set("2")
            panel.study.start_selected()
        a, b = self.app.panels[self.a.id], self.app.panels[self.b.id]
        self.pump(lambda: a.study.dialog is not None and b.study.dialog is not None)
        self.app.select(self.a.id)
        a.show("task")
        dialog = a.study.dialog
        dialog.suggest_all()
        self.pump(lambda: not dialog.busy)
        self.assertEqual(dialog.answers["q1"], ["离线答案"])
        dialog.reviewed.set(True)
        dialog.confirm_submit()
        self.pump(lambda: not a.study.busy)
        self.assertIsNotNone(b.study.dialog)
        self.assertEqual(self.window.grab_current(), None)
        self.assertFalse(any(isinstance(widget, tk.Toplevel) for widget in self.window.winfo_children()))


MODE_VIDEOS = "独立看视频"


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = make_store(self.temp.name)
        self.a = self.store.create("离线 A")
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            if not client.closed:
                client.close()
        self.temp.cleanup()

    def client(self, profile=None, reviewer=None):
        client = WorkerClient(self.store.root, profile or self.a, "mixed", lambda *args: None,
                              reviewer or (lambda *args, **kwargs: "defer"), threading.Event(), fixture=True)
        self.clients.append(client)
        return client

    def test_worker_loads_exact_selection_without_ui_or_network(self):
        client = self.client()
        self.assertEqual(client.load_courses()[0]["clazzId"], "class-1")
        self.assertEqual(client.saved_selection(), [["demo", "class-1"]])

    def test_quiz_callbacks_roundtrip_question_draft_generation_and_submit(self):
        seen = []
        def reviewer(kind, account, course, questions, generate, submit):
            self.assertEqual(kind, "quiz")
            self.assertIsInstance(questions[0], Question)
            self.assertEqual(generate.initial_answers, {})
            self.assertEqual(generate(questions[0]), [["离线答案"], "离线说明"])
            generate.save_draft({"q1": ["答案"]}, {})
            self.assertEqual(submit({"q1": ["答案"]}), "submitted")
            seen.append(account)
            return "submitted"
        client = self.client(reviewer=reviewer)
        self.assertEqual(client.run([["demo", "class-1"]], 2), "submitted")
        self.assertEqual(seen, [self.a.name])

    def test_cancel_is_per_account_and_preserves_second_worker(self):
        b = self.store.create("离线 B")
        a, bclient = self.client(), self.client(b)
        results = {}
        ta = threading.Thread(target=lambda: results.update(a=a.run([])))
        tb = threading.Thread(target=lambda: results.update(b=bclient.run([])))
        ta.start(); tb.start()
        time.sleep(.25)
        a.cancelled.set()
        ta.join(3)
        self.assertFalse(ta.is_alive())
        self.assertIn("已停止", results["a"])
        self.assertFalse(bclient.cancelled.is_set())
        tb.join(4)
        self.assertEqual(results["b"], "离线任务已结束。")

    def test_login_uses_private_pipe_and_worker_has_no_visible_window(self):
        client = self.client()
        self.assertEqual(client.authenticate("not-real-user", "test-only-password", threading.Event())[0], True)
        self.assertNotIn("test-only-password", " ".join(client.process.args))


class SharedSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = make_store(self.temp.name)
        self.a, self.b = self.store.create("A"), self.store.create("B")
        self.settings = {"enabled": True, "endpoint": "https://example.com/v1", "model": "test-model", "key": "test-key"}

    def tearDown(self):
        self.temp.cleanup()

    def legacy(self, profile, settings):
        from types import SimpleNamespace
        # Use a standalone directory profile to prepare a pre-upgrade file.
        with patch("quiz_assistant.settings_path", return_value=profile.directory / "ai_quiz.json"):
            save_settings(profile, settings)

    def test_saving_once_applies_to_existing_and_new_accounts(self):
        save_settings(self.a, self.settings)
        c = self.store.create("new")
        self.assertEqual(read_settings(self.a), read_settings(self.b))
        self.assertEqual(read_settings(c), self.settings)
        self.assertFalse((c.directory / "ai_quiz.json").exists())
        raw = (self.store.root / "ai_quiz.json").read_text(encoding="utf-8")
        self.assertNotIn("test-key", raw)

    def test_single_legacy_configuration_migrates_and_keeps_original(self):
        self.legacy(self.a, self.settings)
        before = (self.a.directory / "ai_quiz.json").read_bytes()
        self.assertEqual(migrate_shared_settings(self.store), "migrated")
        self.assertEqual(read_settings(self.b), self.settings)
        self.assertEqual((self.a.directory / "ai_quiz.json").read_bytes(), before)

    def test_conflicting_providers_wait_for_user_choice(self):
        self.legacy(self.a, self.settings)
        self.legacy(self.b, {**self.settings, "key": "other-key"})
        self.assertEqual(migrate_shared_settings(self.store), "choose")
        self.assertFalse(read_settings(self.a)["enabled"])
        self.assertEqual(read_settings(self.a), read_settings(self.b))
        save_settings(shared_profile(self.store.root), self.settings)
        self.assertEqual(read_settings(self.b), self.settings)

    def test_existing_shared_configuration_is_never_overwritten(self):
        save_settings(self.a, self.settings)
        before = (self.store.root / "ai_quiz.json").read_bytes()
        self.legacy(self.b, {**self.settings, "key": "other-key"})
        self.assertEqual(migrate_shared_settings(self.store), "existing")
        self.assertEqual((self.store.root / "ai_quiz.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
