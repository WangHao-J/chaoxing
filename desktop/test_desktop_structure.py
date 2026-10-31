"""Offline regression checks for filtering, keyboard editing, contrast and launch scope."""
import gc
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from tkinter import ttk

from account_manager import AccountManager
from app_modes import MODE_IDS, MODE_LABELS
from quiz_assistant import Question
from quiz_dialog import ReviewDialog
from ui_theme import PALETTE, apply_theme
from window_launch import gui_launch


class LaunchTests(unittest.TestCase):
    def test_unicode_spaces_and_exact_task_ids_remain_separate_arguments(self):
        with tempfile.TemporaryDirectory(prefix="窗口 测试 ") as folder:
            root = Path(folder)
            profile = SimpleNamespace(id="profile-id", directory=root / "accounts/profile-id")
            launch = gui_launch(root, profile, "pending_quiz_worker.py", "a" * 32,
                                arguments=("--course", "course a", "--class-id", "class b", "--chapter", "chapter c"))
            self.assertEqual(launch.argv[launch.argv.index("--chapter") + 1], "chapter c")
            self.assertEqual(launch.argv[1], str(root / "pending_quiz_worker.py"))
            self.assertEqual(launch.env["TEMP"], str(profile.directory / "temp"))
            self.assertEqual(launch.cwd, profile.directory)
            self.assertEqual(launch.argv[-2:], ["--session-id", "a" * 32])

    def test_rejects_unknown_script_or_session_before_launch(self):
        profile = SimpleNamespace(id="x", directory=Path("accounts/x"))
        for script, token in (("../other.py", "a" * 32), ("study_window.py", "wrong")):
            with self.subTest(script=script, token=token), self.assertRaises(ValueError):
                gui_launch(Path("root"), profile, script, token)

    def test_mode_registry_keeps_all_checkpoint_ids_stable(self):
        self.assertEqual(MODE_IDS, ("quizzes", "videos", "mixed"))
        self.assertEqual(tuple(MODE_LABELS), MODE_IDS)


class DesktopTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.window = tk.Tk()
        self.window.withdraw()
        self.manager = self.review = None

    def tearDown(self):
        if self.manager:
            self.window.after_cancel(self.manager.timer)
        if self.review and self.review.timer:
            self.window.after_cancel(self.review.timer)
        self.window.destroy()
        self.manager = self.review = self.window = None
        gc.collect()

    def home(self):
        names = ("Alpha账户", "Beta账户", "一个完整显示且可以复制的非常长的合成账户名称" * 3)
        profiles = [SimpleNamespace(id=f"id-{i}", name=name, directory=self.root / f"p{i}") for i, name in enumerate(names)]
        store = SimpleNamespace(root=self.root, list=lambda: profiles, get=lambda key: profiles[int(key[-1])])
        with patch("account_manager.is_running", return_value=False):
            self.manager = AccountManager(self.window, store)
        return self.manager

    def test_filter_never_reads_files_and_cannot_leave_a_hidden_account_actionable(self):
        home = self.home()
        with patch.object(home.store, "list", side_effect=AssertionError("search must use cached data")):
            home.search_text.set(" beta ")
            self.assertEqual(home.tree.get_children(), ("id-1",))
            self.assertEqual(home.selected().id, "id-1")
            home.search_text.set("not-found")
            self.assertEqual(home.tree.selection(), ())
            self.assertTrue(all(str(button.cget("state")) == "disabled" for button in home.account_actions))
            self.assertEqual(home.empty_label.winfo_manager(), "place")
            home.search_text.set("")
            self.assertEqual(len(home.tree.get_children()), 3)
            self.assertTrue(all(str(button.cget("state")) == "normal" for button in home.account_actions))

    def test_status_filter_and_long_account_value(self):
        home = self.home()
        home.account_rows[1] = ("id-1", "Beta账户", "等待人工处理", True)
        home.status_filter.set("待处理")
        self.assertEqual(home.tree.get_children(), ("id-1",))
        home.status_filter.set("运行中")
        self.assertEqual(home.tree.get_children(), ("id-1",))
        home.status_filter.set("全部账户")
        home.tree.selection_set("id-2")
        home.update_selected_account()
        self.assertEqual(home.selected_account.get(), home.store.get("id-2").name)
        self.assertEqual(str(home.selected_value.cget("state")), "readonly")

    def test_small_home_has_visible_actions_and_focus_style(self):
        home = self.home()
        self.window.geometry("800x600+10000+10000")
        self.window.deiconify()
        self.window.update()
        for widget in [*home.action_buttons, home.search_entry, home.filter_box]:
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.window.winfo_rooty() + self.window.winfo_height())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), self.window.winfo_rootx() + self.window.winfo_width())
        style = ttk.Style(self.window)
        self.assertEqual(style.lookup("TButton", "focuscolor"), PALETTE["focus"])
        self.assertEqual(style.lookup("TEntry", "bordercolor", ("focus",)), PALETTE["focus"])

    def quiz(self):
        questions = [Question("first", "first", "completion", "", [f"a{i}" for i in range(15)], 15),
                     Question("second", "second", "shortanswer", "", ["b"])]
        generate, submit = Mock(), Mock()
        self.review = ReviewDialog(self.window, "合成账户", "合成课程", questions, generate, submit)
        self.window.geometry("740x580+10000+10000")
        self.window.deiconify()
        self.window.update()
        return self.review, generate, submit

    def test_keyboard_question_navigation_preserves_edits_and_never_submits(self):
        review, generate, submit = self.quiz()
        review.variables[0].set("retained")
        review.navigate(1)
        self.assertEqual(review.index, 1)
        review.navigate(-1)
        self.assertEqual(review.variables[0].get(), "retained")
        review.busy = True
        review.navigate(1)
        self.assertEqual(review.index, 0)
        generate.assert_not_called()
        submit.assert_not_called()

    def test_keyboard_focus_reveals_last_blank_and_submit_shortcut_keeps_review_gate(self):
        review, generate, submit = self.quiz()
        entries = [child for child in review.editor.winfo_children() if child.winfo_class() == "TEntry"]
        review.reveal_answer(entries[-1])
        self.window.update_idletasks()
        self.assertGreater(review.canvas.yview()[0], 0)
        self.assertLessEqual(entries[-1].winfo_rooty() + entries[-1].winfo_height(),
                             review.canvas.winfo_rooty() + review.canvas.winfo_height())
        review.confirm_submit()
        submit.assert_not_called()
        self.assertTrue(self.window.bind("<Control-Return>"))
        self.assertTrue(self.window.bind("<Alt-Right>"))


class ContrastTests(unittest.TestCase):
    def test_enabled_text_pairs_have_aa_contrast(self):
        def luminance(color):
            rgb = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
            values = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in rgb]
            return sum(a * b for a, b in zip(values, (.2126, .7152, .0722)))
        pairs = [(foreground, background) for foreground in ("text", "muted", "danger", "success")
                 for background in ("bg", "surface", "accent_surface")]
        pairs += [("on_selection", "selection"), ("danger", "error_surface"),
                  ("sidebar_text", "sidebar"), ("sidebar_muted", "sidebar")]
        for foreground, background in pairs:
            light, dark = sorted((luminance(PALETTE[foreground]), luminance(PALETTE[background])), reverse=True)
            self.assertGreaterEqual((light + .05) / (dark + .05), 4.5, (foreground, background))
        self.assertGreaterEqual(1.05 / (luminance(PALETTE["primary"]) + .05), 4.5)


if __name__ == "__main__":
    unittest.main()
