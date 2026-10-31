"""Open one saved chapter's native quiz UI in its own account-isolated process."""
import argparse
import json
import io
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from ui_feedback import NoticeVar, Notice

from account_store import AccountStore, AlreadyRunning, ProfileLock
from card_reader import flag_is_true
from pending_quiz import (PendingQuizError, load_selected_task, resolve_selected_quizzes,
                          confirm_selected_task, task_key)
from quiz_assistant import (QuizError, read_settings, reviewed_quiz,
                            remember_pending_quiz, report_quiz_completed)
from quiz_dialog import ReviewDialog, ai_configured
from ui_theme import apply_theme


def run_selected_quizzes(root, profile, requested, reviewer, status, cancelled):
    """Read and review only the selected chapter; never start the course scheduler."""
    from run_account import prepare
    from runtime_safety import install_runtime_safety, AccessRestricted

    with ProfileLock(profile.directory):
        row = load_selected_task(profile, *task_key(requested))
        if not ai_configured(read_settings(profile)):
            raise PendingQuizError("请在管理界面配置并启用该账户的 AI 答题，然后重新打开任务。")
        if cancelled.is_set():
            return "已取消打开，待处理记录已保留。"
        status("正在读取所选章节的测验…")
        if sys.stdout is None:
            sys.stdout = io.StringIO()
        if sys.stderr is None:
            sys.stderr = io.StringIO()
        main = prepare(root, profile)
        coordinator = install_runtime_safety(main, profile)
        coordinator.cancelled = cancelled
        original_state = coordinator.state

        def state(kind, message):
            original_state(kind, message)
            status(message)

        coordinator.state = state
        try:
            from api.base import SessionManager
            if not SessionManager.get_session().cookies.get("_uid"):
                raise PendingQuizError("该账户没有可用的登录记录，请在管理界面重新登录后打开任务。")
            # Use the existing cookie session. A GUI worker never asks for terminal input.
            cx = main.Chaoxing(account=main.Account("", ""), tiku=None)
            course, point, quizzes = resolve_selected_quizzes(cx, row, cancelled=cancelled.is_set)
            coordinator.quiz_chapter_title = point.get("title", "章节测验")
            remember_pending_quiz(coordinator, course, {"name": "章节测验"}, {"knowledgeid": point["id"]})
            if cancelled.is_set():
                return "已取消打开，待处理记录已保留。"
            if flag_is_true(point.get("has_finished", False)):
                if confirm_selected_task(cx, profile, course, point):
                    report_quiz_completed(coordinator, course, point)
                    return "平台已确认该章节完成，已更新待处理列表。"
                return "平台尚未确认该章节完成，待处理记录已保留。"
            if not quizzes:
                raise PendingQuizError("该章节未读取到可在程序内打开的测验，任务仍保留待处理。")
            automatic_errors = []
            for job, info in quizzes:
                if cancelled.is_set():
                    return "已取消打开，待处理记录已保留。"

                def show_review(account, course_title, questions, generate, submit):
                    return reviewer(account, f"{course_title} / {point.get('title', '')}",
                                    questions, generate, submit)

                try:
                    outcome = reviewed_quiz(cx, course, job, info, profile, coordinator, reviewer=show_review)
                except QuizError as error:
                    if not coordinator.auto_quizzes or cancelled.is_set():
                        raise
                    automatic_errors.append(str(error))
                    status("本测验保留待处理，继续后续自动答题：" + str(error))
                    continue
                if outcome not in ("submitted", "auto_submitted", "pending_confirmation"):
                    return "本次未继续交卷，待处理记录已保留。"
            status("正在核对平台中的章节完成状态…")
            if confirm_selected_task(cx, profile, course, point):
                report_quiz_completed(coordinator, course, point)
                return "平台已确认该章节完成，已更新待处理列表。"
            if automatic_errors:
                raise PendingQuizError("部分测验自动答题未完成，已保留待处理：" + "；".join(automatic_errors))
            return "测验已处理，平台尚未确认章节完成，待处理记录已保留。"
        except AccessRestricted as error:
            raise PendingQuizError(str(error)) from None
        finally:
            coordinator.state("stopped", "所选待处理测验窗口已结束；其他任务进度保留")


class PendingQuizWindow:
    def __init__(self, window, root, profile, task, *, operation=run_selected_quizzes, autostart=True, session_id=None):
        if session_id is not None and re.fullmatch(r"[a-f0-9]{32}", session_id) is None:
            raise ValueError("运行窗口编号无效")
        self.window, self.root, self.profile, self.task = window, root, profile, task
        self.stop_request = profile.directory / f"study_stop_{session_id}.request" if session_id else None
        self.destroyed = False
        apply_theme(window)
        self.operation = operation
        self.events = queue.Queue()
        self.cancelled = threading.Event()
        self.busy, self.timer, self.review_dialog, self.review_packet = False, None, None, None
        self.open_error = ""
        window.title("待处理答题 · " + profile.name)
        window.geometry("620x290")
        window.minsize(580, 270)
        self.build_progress("正在打开所选测验…")
        window.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()
        if autostart:
            window.after(50, self.start)

    def build_progress(self, message):
        self.window.geometry("720x480")
        self.window.minsize(680, 440)
        self.body = ttk.Frame(self.window, padding=24, style="Page.TFrame")
        self.body.pack(fill="both", expand=True)
        ttk.Label(self.body, text="打开待处理测验", style="Heading.TLabel").pack(anchor="w", pady=(0, 16))
        for text in ("账户：" + self.profile.name, "课程：" + str(self.task.get("course_title", "")),
                     "章节：" + str(self.task.get("chapter_title", ""))):
            ttk.Label(self.body, text=text, wraplength=550).pack(anchor="w", pady=(5, 0))
        self.feedback = NoticeVar(value=message, tone="error" if self.open_error else "busy")
        self.feedback_notice = Notice(self.body, self.feedback, compact=True)
        self.feedback_notice.pack(fill="x", pady=(20, 12))
        self.close_button = ttk.Button(self.body, text="取消打开", command=self.close)
        self.close_button.pack(anchor="e")

    def start(self):
        if self.busy or self.cancelled.is_set():
            return
        self.busy = True

        def run():
            failed = False
            try:
                message = self.operation(self.root, self.profile, self.task, self.request_review,
                                         lambda text: self.events.put(("status", text)), self.cancelled)
            except AlreadyRunning:
                failed = True
                message = "该账户正在运行。请先停止该账户，再打开待处理答题。"
            except QuizError as error:
                failed = True
                message = str(error)
            except Exception:
                failed = True
                message = "测验读取未成功，请稍后重试；待处理记录已保留。"
            self.events.put(("failed" if failed else "done", message))

        threading.Thread(target=run, daemon=True).start()

    def request_review(self, account, course, questions, generate, submit):
        if self.cancelled.is_set():
            return "defer"
        packet = {"args": (account, course, questions, generate, submit),
                  "finished": threading.Event(), "result": "defer"}
        self.events.put(("review", packet))
        # Keep account ownership until the UI has finished an in-flight submit.
        packet["finished"].wait()
        return packet["result"]

    def show_review(self, packet):
        if self.cancelled.is_set():
            packet["finished"].set()
            return
        self.review_packet = packet
        self.body.destroy()
        try:
            self.review_dialog = ReviewDialog(self.window, *packet["args"])
        except Exception:
            self.review_dialog = self.review_packet = None
            for child in self.window.winfo_children():
                child.destroy()
            self.open_error = "答题界面未能打开，请关闭后重试；待处理记录已保留。"
            self.build_progress(self.open_error)
            self.window.protocol("WM_DELETE_WINDOW", self.close)
            packet["finished"].set()
            return
        # ReviewDialog normally ends a standalone event loop. Here the helper
        # keeps its loop alive to confirm status and release the account lock.
        self.review_dialog.finish = self.finish_review

    def finish_review(self):
        dialog, packet = self.review_dialog, self.review_packet
        if dialog is None or packet is None:
            return
        dialog.store_current()
        dialog.cancel_generation.set()
        if dialog.timer is not None:
            self.window.after_cancel(dialog.timer)
            dialog.timer = None
        packet["result"] = dialog.result
        self.review_dialog = self.review_packet = None
        for child in self.window.winfo_children():
            child.destroy()
        self.window.geometry("620x290")
        self.window.minsize(580, 270)
        self.build_progress("正在处理所选测验结果…")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        packet["finished"].set()

    def poll(self):
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None
        if self.destroyed:
            return
        if self.stop_request is not None:
            try:
                if self.stop_request.is_file():
                    self.stop_request.unlink()
                    self.close()
                    if self.destroyed:
                        return
            except OSError:
                pass
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "review":
                self.show_review(value)
            elif kind == "status" and self.review_dialog is None:
                self.feedback.set(self.open_error or value, tone="error" if self.open_error else "busy")
            elif kind in ("done", "failed"):
                self.busy = False
                self.feedback.set(self.open_error or value, tone="error" if self.open_error or kind == "failed" else "info")
                self.close_button.configure(text="关闭")
                if self.cancelled.is_set():
                    self.destroy()
                    return
        if self.cancelled.is_set() and self.review_dialog is not None:
            self.review_dialog.cancel_generation.set()
            if not self.review_dialog.busy:
                self.review_dialog.defer()
        self.timer = self.window.after(100, self.poll)

    def close(self):
        self.cancelled.set()
        if self.review_dialog is not None:
            self.review_dialog.cancel_generation.set()
            self.review_dialog.defer()
            return
        if self.busy:
            self.feedback.set("正在停止读取，待处理记录会保留…")
            self.close_button.configure(state="disabled")
        else:
            self.destroy()

    def destroy(self):
        self.destroyed = True
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None
        self.window.destroy()


def entry(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--course", required=True)
    parser.add_argument("--class-id", required=True)
    parser.add_argument("--chapter", required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--ui-check", action="store_true")
    args = parser.parse_args(argv)
    profile = AccountStore(args.root).get(args.profile)
    try:
        task = load_selected_task(profile, args.course, args.class_id, args.chapter)
    except QuizError:
        task = {"course_id": args.course, "class_id": args.class_id, "chapter_id": args.chapter}
    window = tk.Tk()
    if args.ui_check:
        window.withdraw()
    controller = PendingQuizWindow(window, args.root.resolve(), profile, task, autostart=not args.ui_check,
                                   session_id=args.session_id)
    if args.ui_check:
        window.geometry("620x290+10000+10000")
        window.deiconify()
        window.update()
        button = controller.close_button
        assert button.winfo_ismapped()
        assert button.winfo_rooty() + button.winfo_height() <= window.winfo_rooty() + window.winfo_height()
        controller.destroy()
        print(json.dumps({"pending_quiz_ui_initialized": True, "network_called": False}))
    else:
        window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(entry())
