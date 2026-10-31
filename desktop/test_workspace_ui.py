"""Regression tests for explicit blocking feedback and no-modifier course checks."""
import gc
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from tkinter import ttk

from ui_theme import PALETTE, apply_theme
from ui_feedback import Notice, NoticeVar
from course_checklist import CourseChecklist
from quiz_assistant import Question
from quiz_dialog import ReviewDialog
from study_window import StudyWindow
from ui_widgets import WorkspaceViewport


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.window = tk.Tk()
        self.original_scaling = self.window.tk.call("tk", "scaling")
        self.window.withdraw()
        self.window.geometry("1200x940+10000+10000")
        self.dialog = self.study = None
        apply_theme(self.window)

    def tearDown(self):
        if self.dialog and self.dialog.timer:
            self.window.after_cancel(self.dialog.timer)
        if self.study and self.study.timer:
            self.window.after_cancel(self.study.timer)
        self.window.tk.call("tk", "scaling", self.original_scaling)
        self.window.destroy()
        self.dialog = self.study = self.window = None
        gc.collect()

    def tree(self, can_toggle=lambda: True):
        tree = CourseChecklist(self.window, can_toggle=can_toggle)
        tree.pack(fill="both", expand=True)
        for iid in ("course-a", "course-b", "course-c"):
            tree.insert("", "end", iid=iid, values=("同名课程", iid))
        self.window.deiconify()
        self.window.update()
        return tree

    def test_clicking_two_rows_keeps_both_checked_without_ctrl(self):
        tree = self.tree()
        for iid in ("course-a", "course-b"):
            x, y, width, height = tree.bbox(iid, "check")
            tree.click(SimpleNamespace(x=x + width // 2, y=y + height // 2))
        self.assertEqual(set(tree.selection()), {"course-a", "course-b"})
        self.assertEqual(tree.set("course-a", "check"), "[√]")
        self.assertEqual(tree.set("course-c", "check"), "[  ]")
        tree.toggle("course-a")
        self.assertEqual(tree.selection(), ("course-b",))

    def test_space_all_and_clear_operate_on_checkmarks(self):
        tree = self.tree()
        tree.focus("course-c")
        tree.space()
        self.assertEqual(tree.selection(), ("course-c",))
        tree.check_all()
        self.assertEqual(len(tree.selection()), 3)
        tree.uncheck_all()
        self.assertEqual(tree.selection(), ())
        self.assertTrue(all(tree.set(iid, "check") == "[  ]" for iid in tree.get_children()))

    def test_loading_and_running_can_disable_course_toggles(self):
        tree = self.tree(can_toggle=lambda: False)
        tree.toggle("course-a")
        tree.check_all()
        tree.focus("course-a")
        tree.space()
        self.assertFalse(tree.selection())

    def test_arrow_keys_move_focus_without_clearing_other_checkmarks(self):
        tree = self.tree()
        tree.toggle("course-a")
        tree.toggle("course-b")
        tree.move_focus(1)
        self.assertEqual(tree.focus(), "course-c")
        self.assertEqual(set(tree.selection()), {"course-a", "course-b"})
        tree.space()
        self.assertEqual(len(tree.selection()), 3)

    def review(self):
        questions = [Question("q1", "正常题", "completion", "", ["answer1"]),
                     Question("q2", "受阻题", "multiple", "A.甲\nB.乙", [], unsupported="页面缺少可确认的提交字段。")]
        self.generate, self.submit = Mock(), Mock()
        self.dialog = ReviewDialog(self.window, "合成账户", "合成课程", questions, self.generate, self.submit)
        self.window.deiconify()
        self.window.update()
        return self.dialog

    def test_unsupported_question_is_red_before_any_click_and_identifies_which_question(self):
        review = self.review()
        self.assertEqual(review.compatibility.tone, "error")
        self.assertIn("2", review.compatibility.get())
        self.assertEqual(review.compatibility_notice.cget("style"), "Error.TFrame")
        self.assertEqual(str(review.auto_button.cget("state")), "disabled")
        self.assertEqual(str(review.submit_button.cget("state")), "disabled")
        self.assertEqual(review.listing.itemcget(1, "foreground"), PALETTE["danger"])
        self.generate.assert_not_called()
        self.submit.assert_not_called()

    def test_locate_blocked_uses_exact_question_and_keeps_manual_answers(self):
        review = self.review()
        review.variables[0].set("保留内容")
        review.locate_blocked()
        self.assertEqual(review.index, 1)
        self.assertIn("缺少", review.question_issue.get())
        self.assertEqual(review.answers["q1"], ["保留内容"])
        review.copy_answers()
        self.assertEqual(review.compatibility.tone, "error")

    def test_submission_failure_is_red_and_never_reports_success(self):
        review = self.review()
        review.events.put(("submit", None, "提交未成功，请在网页核对状态。"))
        review.poll()
        self.assertEqual(review.feedback.tone, "error")
        self.assertIn("未成功", review.feedback.get())
        self.assertEqual(review.result, "defer")

    def test_explicit_severity_does_not_guess_from_words(self):
        message = NoticeVar(self.window, value="正常提示")
        notice = Notice(self.window, message)
        notice.pack()
        message.error("连接异常，检查网络后重试。")
        self.assertEqual(notice.cget("style"), "Error.TFrame")
        message.success("已完成，但没有自动提交。")
        self.assertEqual(notice.cget("style"), "Success.TFrame")
        message.set("失败历史已清理")
        self.assertEqual(notice.cget("style"), "Info.TFrame")
        style = ttk.Style(self.window)
        self.assertEqual(style.lookup("Error.TLabel", "foreground"), PALETTE["danger"])

    def test_exact_saved_course_class_checks_survive_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = SimpleNamespace(id="x", name="合成账户", directory=Path(directory))
            self.study = StudyWindow(self.window, directory, profile, "videos", autoload=False)
            courses = [dict(courseId="same", clazzId="a", title="同名"), dict(courseId="same", clazzId="b", title="同名")]
            self.study.populate_courses(courses, [("same", "b")])
            self.assertEqual(self.study.selected_keys(), [("same", "b")])
            self.assertEqual(self.study.listing.set("course-1", "check"), "[√]")
            self.study.populate_courses(courses, [])
            self.assertEqual(self.study.selected_keys(), [("same", "b")])

    def test_runtime_restriction_stays_red_when_run_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = SimpleNamespace(id="x", name="合成账户", directory=Path(directory))
            self.study = StudyWindow(self.window, directory, profile, "videos", autoload=False)
            self.study.events.put(("status", {"state": "needs_attention", "message": "平台暂时拒绝访问"}))
            self.study.events.put(("finished", ("run", "尚有待处理任务", None)))
            self.study.poll()
            self.assertEqual(self.study.status.tone, "error")

    def test_explicitly_clearing_checks_does_not_restore_saved_checks_on_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = SimpleNamespace(id="x", name="合成账户", directory=Path(directory))
            self.study = StudyWindow(self.window, directory, profile, "videos", autoload=False)
            courses = [dict(courseId="same", clazzId="a", title="同名")]
            self.study.populate_courses(courses, [("same", "a")])
            self.study.listing.uncheck_all()
            self.study.populate_courses(courses, [("same", "a")])
            self.assertEqual(self.study.selected_keys(), [])

    def test_large_text_viewport_can_reveal_bottom_control(self):
        self.window.tk.call("tk", "scaling", 2.0)
        apply_theme(self.window)
        viewport = WorkspaceViewport(self.window, base_height=840)
        viewport.pack(fill="both", expand=True)
        button = ttk.Button(viewport.content, text="底部操作")
        button.pack(side="bottom")
        self.window.geometry("1060x840+10000+10000")
        self.window.deiconify()
        self.window.update()
        self.assertGreater(viewport.canvas.bbox(viewport.item)[3], viewport.canvas.winfo_height())
        viewport.focus_changed(SimpleNamespace(widget=button))
        self.window.update_idletasks()
        self.assertGreater(viewport.canvas.yview()[0], 0)
        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                             viewport.canvas.winfo_rooty() + viewport.canvas.winfo_height())


if __name__ == "__main__":
    unittest.main()
