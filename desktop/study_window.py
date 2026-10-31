"""Independent study windows with one account owner and a responsive Tk UI."""
import argparse
import configparser
import json
import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

from account_store import AccountStore, AlreadyRunning
from quiz_assistant import QuizError
from quiz_dialog import ReviewDialog, SettingsDialog, ensure_ai_config
from recovery_dialog import RecoveryDialog
from ui_theme import apply_theme


from app_modes import MODE_LABELS as MODE_TITLES


def configured_video_speed(profile):
    settings = configparser.ConfigParser(interpolation=None)
    try:
        settings.read(profile.directory / "config.ini", encoding="utf-8")
        speed = settings.getfloat("common", "speed", fallback=1.0)
        return speed if 1.0 <= speed <= 2.0 else 1.0
    except (OSError, ValueError, configparser.Error):
        return 1.0


def media_time(seconds):
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def default_engine_factory(*args):
    # UI verification must not initialize a session or touch the network.
    from study_runner import StudyEngine
    return StudyEngine(*args)


class StudyWindow:
    def __init__(self, window, root, profile, mode, *, engine_factory=default_engine_factory,
                 autoload=True, session_id=None):
        if mode not in MODE_TITLES:
            raise ValueError("运行方式无效")
        if session_id is not None and re.fullmatch(r"[a-f0-9]{32}", session_id) is None:
            raise ValueError("运行窗口编号无效")
        self.window, self.root, self.profile, self.mode = window, Path(root), profile, mode
        self.stop_request = profile.directory / f"study_stop_{session_id}.request" if session_id else None
        self.last_stop_check = time.monotonic()
        self.engine_factory, self.engine = engine_factory, None
        self.events, self.cancelled = queue.Queue(), threading.Event()
        self.busy, self.closing, self.stopping, self.destroyed = False, False, False, False
        self.configuring = False
        self.recovered = False
        self.operation_started = None
        self.last_activity = time.monotonic()
        self.logs_visible = False
        self.timer, self.worker, self.autoload_timer = None, None, None
        self.courses, self.course_rows = [], {}
        self.dialog, self.dialog_window, self.dialog_packet = None, None, None
        self.settings_dialog, self.settings_window = None, None
        self.build()
        window.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()
        if autoload:
            self.autoload_timer = window.after(30, self.refresh_courses)

    def build(self):
        from study_view import build_study_view
        build_study_view(self, configured_video_speed)

    def resize_labels(self, width):
        self.status_label.configure(wraplength=max(300, width - 30))
        self.progress_label.configure(wraplength=max(300, width - 30))

    def toggle_logs(self):
        self.logs_visible = not self.logs_visible
        if self.logs_visible:
            self.log_frame.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        else:
            self.log_frame.grid_remove()
        self.log_button.configure(text="收起运行记录" if self.logs_visible else "展开运行记录")

    def recover_mode(self):
        if self.busy or self.closing or self.configuring or self.settings_window is not None or self.recovered:
            return
        if not messagebox.askyesno("修复续接记录", "将备份并重建当前模式的选课和续接记录。\n账户、AI 设置及平台学习进度会保留。完成后请重新打开本窗口。", parent=self.window):
            return
        self.status.busy("正在备份并重建当前模式的续接记录…")
        self.launch("recover")

    def update_controls(self):
        unavailable = self.busy or self.closing or self.configuring or self.settings_window is not None or self.recovered
        self.selection_text.set(f"已选 {len(self.listing.selection())} / {len(self.course_rows)} 门")
        if self.refresh_button is not None:
            self.refresh_button.configure(state="disabled" if unavailable else "normal")
        self.start_button.configure(state="normal" if not unavailable and self.listing.selection() else "disabled")
        self.stop_button.configure(state="normal" if self.busy and not self.stopping and not self.closing else "disabled")
        self.recover_button.configure(state="disabled" if unavailable else "normal")
        self.all_courses_button.configure(state="disabled" if unavailable else "normal")
        self.clear_courses_button.configure(state="disabled" if unavailable else "normal")
        if self.settings_button is not None:
            self.settings_button.configure(state="disabled" if unavailable else "normal")
        if self.speed_box is not None:
            self.speed_box.configure(state="disabled" if unavailable else "readonly")

    def emit(self, kind, payload):
        self.events.put((kind, payload))

    def launch(self, operation, *args):
        if self.busy or self.destroyed or (self.closing and operation != "close"):
            return False
        self.busy = True
        self.operation_started = time.monotonic()
        self.last_activity = self.operation_started
        self.stopping = False
        if operation != "close":
            self.cancelled.clear()
        self.update_controls()

        def work():
            try:
                if operation == "close":
                    if self.engine is not None:
                        self.engine.close()
                    self.engine = None
                    value = None
                elif operation == "recover":
                    if self.engine is not None:
                        self.engine.close()
                    self.engine = None
                    from task_repository import recover_mode
                    value = recover_mode(self.profile, self.mode)
                else:
                    if self.engine is None:
                        self.engine = self.engine_factory(self.root, self.profile, self.mode,
                                                         self.emit, self.call_ui, self.cancelled)
                    if operation == "load":
                        courses = self.engine.load_courses()
                        value = (courses, self.engine.saved_selection())
                    else:
                        value = self.engine.run(*args)
                self.events.put(("finished", (operation, value, None)))
            except Exception as error:
                if operation == "load" and self.engine is not None:
                    try:
                        self.engine.close()
                    except Exception:
                        pass
                if isinstance(error, (AlreadyRunning, QuizError, ValueError)) or type(error).__name__ == "StudyError":
                    message = str(error)
                else:
                    message = "操作未成功（" + type(error).__name__ + "），请稍后重试；课程进度会保留。"
                self.events.put(("finished", (operation, None, message)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        return True

    def refresh_courses(self):
        self.autoload_timer = None
        if self.settings_window is not None or self.busy or self.closing or self.recovered:
            return
        self.status.busy("正在读取课程列表…")
        self.launch("load")

    def populate_courses(self, courses, saved):
        # Preserve exact course/class identity, including same-name courses.
        chosen = set(self.selected_keys()) if self.course_rows else {(str(a), str(b)) for a, b in saved}
        self.courses, self.course_rows = [], {}
        self.listing.delete(*self.listing.get_children())
        seen = set()
        for course in courses:
            key = (str(course["courseId"]), str(course["clazzId"]))
            if key in seen:
                continue
            seen.add(key)
            iid = "course-" + str(len(self.courses))
            self.courses.append(course)
            self.course_rows[iid] = key
            self.listing.insert("", "end", iid=iid, values=(course.get("title", "课程"), key[1]))
            if key in chosen:
                self.listing.selection_add(iid)
        self.listing.sync_checks()

    def selected_keys(self):
        return [self.course_rows[iid] for iid in self.listing.selection() if iid in self.course_rows]

    def start_selected(self):
        if self.busy or self.closing or self.configuring or self.settings_window is not None or self.recovered:
            return
        selected = self.selected_keys()
        if not selected:
            self.status.error("请先选择课程：点击课程前方的方框勾选。")
            return
        if self.mode != "videos":
            self.configuring = True
            self.update_controls()
            try:
                ready = ensure_ai_config(self.window, self.profile)
            finally:
                self.configuring = False
                if not self.destroyed:
                    self.update_controls()
            if self.closing:
                self.launch("close")
                return
            if not ready:
                self.status.error("请先配置并启用 AI 答题服务，再开始所选课程。")
                return
        self.status.busy("正在开始所选课程…")
        self.progress.configure(value=0, maximum=1)
        self.progress_text.set("正在读取所选课程的章节")
        if self.speed is None:
            self.launch("run", selected)
        else:
            self.launch("run", selected, float(self.speed.get()))

    def call_ui(self, kind, *args, **kwargs):
        default = "defer" if kind == "quiz" else "stop"
        if self.cancelled.is_set():
            return default
        packet = {"kind": kind, "args": args, "kwargs": kwargs, "result": default,
                  "finished": threading.Event()}
        self.events.put(("ui", packet))
        # The main thread releases this only after an active submit has returned.
        # Returning early on cancellation could close the account lock too soon.
        packet["finished"].wait()
        return packet["result"]

    def show_dialog(self, packet):
        if self.cancelled.is_set() or self.dialog is not None:
            packet["finished"].set()
            return
        popup = tk.Toplevel(self.window)
        popup.withdraw()
        popup.transient(self.window)
        self.dialog_window, self.dialog_packet = popup, packet
        try:
            if packet["kind"] == "quiz":
                self.dialog = ReviewDialog(popup, *packet["args"], **packet["kwargs"])
                self.dialog.finish = self.finish_dialog
            elif packet["kind"] == "recovery":
                self.dialog = RecoveryDialog(popup, *packet["args"], **packet["kwargs"])
                self.dialog.finish = self.finish_dialog
                popup.update_idletasks()
                popup.geometry(f"{max(580, popup.winfo_reqwidth())}x{max(360, popup.winfo_reqheight())}")
            else:
                raise ValueError("不支持的操作，请回到账户管理界面。")
            popup.protocol("WM_DELETE_WINDOW", self.defer_dialog)
            popup.deiconify()
            popup.grab_set()
            self.status.set("正在答题，请在答题页面完成后继续。" if packet["kind"] == "quiz" else "等待处理后继续…")
        except Exception:
            self.append_log("未能打开处理页面；任务会保留待处理。")
            self.finish_dialog()
            self.status.error("处理页面未能打开。任务已保留，可从待处理列表重试。")

    def finish_dialog(self, result=None):
        dialog, popup, packet = self.dialog, self.dialog_window, self.dialog_packet
        if packet is None:
            return
        if dialog is not None:
            store_current = getattr(dialog, "store_current", None)
            if callable(store_current):
                store_current()
            if result is not None:
                dialog.result = result
            cancel = getattr(dialog, "cancel_generation", None)
            if cancel is not None:
                cancel.set()
            timer = getattr(dialog, "timer", None)
            if timer is not None:
                popup.after_cancel(timer)
                dialog.timer = None
            packet["result"] = getattr(dialog, "result", packet["result"])
        try:
            popup.grab_release()
            popup.destroy()
        except tk.TclError:
            pass
        self.dialog = self.dialog_window = self.dialog_packet = None
        self.status.set("正在核对答题结果并继续…" if packet["result"] == "submitted" else "正在继续处理课程…")
        packet["finished"].set()

    def defer_dialog(self):
        if self.dialog is None:
            return
        if self.dialog_packet["kind"] == "quiz" and callable(getattr(self.dialog, "defer", None)):
            self.dialog.defer()
            return
        if self.cancelled.is_set():
            cancel = getattr(self.dialog, "cancel_generation", None)
            if cancel is not None:
                cancel.set()
        if getattr(self.dialog, "busy", False):
            if hasattr(self.dialog, "feedback"):
                self.dialog.feedback.set("正在等待当前操作结束，请稍候。")
            return
        self.finish_dialog("defer" if self.dialog_packet["kind"] == "quiz" else "stop")

    def open_settings(self):
        if self.mode == "videos" or self.busy or self.closing or self.settings_window is not None:
            return
        popup = tk.Toplevel(self.window)
        popup.transient(self.window)
        self.settings_window = popup
        try:
            self.settings_dialog = SettingsDialog(popup, self.profile)
            popup.bind("<Destroy>", lambda event: self.settings_closed(event, popup), add=True)
            popup.grab_set()
            self.update_controls()
        except Exception:
            self.settings_window = self.settings_dialog = None
            popup.destroy()
            self.status.error("AI 设置未能打开，请回到账户管理界面配置。")
            self.update_controls()

    def settings_closed(self, event, popup):
        if event.widget is popup:
            self.settings_window = self.settings_dialog = None
            if not self.destroyed:
                self.update_controls()

    def append_log(self, message):
        follow = self.log.yview()[1] >= .999
        self.log.configure(state="normal")
        self.log.insert("end", str(message).rstrip() + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 300:
            self.log.delete("1.0", "2.0")
        if follow:
            self.log.see("end")
        self.log.configure(state="disabled")

    def stop(self):
        if not self.busy or self.closing:
            return
        self.cancelled.set()
        self.stopping = True
        self.status.set("正在停止，等待当前操作结束；进度会保留…")
        self.update_controls()
        self.defer_dialog()

    def close(self):
        if self.closing or self.destroyed:
            return
        self.closing = True
        self.cancelled.set()
        if self.autoload_timer is not None:
            self.window.after_cancel(self.autoload_timer)
            self.autoload_timer = None
        if self.settings_dialog is not None:
            self.settings_dialog.close()
        self.status.set("正在关闭，等待当前操作结束并保存进度…")
        self.update_controls()
        self.defer_dialog()
        if not self.busy and not self.configuring:
            self.launch("close")

    def poll(self):
        self.timer = None
        if self.destroyed:
            return
        if self.stop_request is not None and time.monotonic() - self.last_stop_check >= .5:
            self.last_stop_check = time.monotonic()
            try:
                if self.stop_request.is_file():
                    self.stop_request.unlink()
                    self.stop()
            except OSError:
                pass
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "finished":
                operation, value, error = payload
                self.busy = False
                if operation == "close":
                    self.destroy()
                    return
                if error:
                    if operation == "load":
                        self.courses, self.course_rows = [], {}
                        self.listing.delete(*self.listing.get_children())
                    if operation == "recover":
                        # The engine has released its process-wide upstream
                        # hooks; recreating it here would install them twice.
                        self.recovered = True
                        self.courses, self.course_rows = [], {}
                        self.listing.delete(*self.listing.get_children())
                        error += " 请关闭本窗口后重新打开，再检查续接记录。"
                    self.status.error(error)
                    self.append_log(error)
                elif operation == "load" and not self.cancelled.is_set():
                    self.populate_courses(*value)
                    self.status.set(f"已读取 {len(self.courses)} 门课程，请选择后开始。")
                elif operation == "run":
                    text = str(value or "本轮处理已结束，进度已保留。")
                    if getattr(self, "last_status_kind", "") == "needs_attention":
                        self.status.error(text)
                    else:
                        self.status.success(text)
                    self.append_log(self.status.get())
                elif operation == "recover":
                    self.recovered = True
                    self.courses, self.course_rows = [], {}
                    self.listing.delete(*self.listing.get_children())
                    self.status.set("续接记录已备份并重建。请关闭本窗口，再从账户首页重新打开。")
                    self.progress_text.set("平台中的实际学习进度会在重新打开后读取。")
                    self.append_log("当前模式的续接记录已备份；账户和原有进度资料已保留。")
                elif self.cancelled.is_set():
                    self.status.set("已停止，课程进度已保留。")
                self.stopping = False
                self.update_controls()
                if self.closing:
                    self.launch("close")
            elif kind == "ui":
                self.show_dialog(payload)
            elif kind == "log":
                self.last_activity = time.monotonic()
                message = payload.get("message", "") if isinstance(payload, dict) else payload
                self.append_log(message)
            elif kind == "status" and not self.closing and not self.stopping:
                self.last_activity = time.monotonic()
                state = payload.get("state", "") if isinstance(payload, dict) else ""
                self.last_status_kind = state
                message = str(payload.get("message", "") if isinstance(payload, dict) else payload)
                if state == "needs_attention":
                    self.status.error(message)
                elif state == "completed":
                    self.status.success(message)
                elif self.busy:
                    self.status.busy(message)
                else:
                    self.status.set(message)
            elif kind == "estimate" and isinstance(payload, dict):
                if hasattr(self, "eta_text"):
                    self.eta_text.set(str(payload.get("course_title", "")) + " · " + str(payload.get("message", "")))
            elif kind == "report" and isinstance(payload, dict):
                self.append_log("本账号运行报告已保存：" + str(payload.get("path", "")))
            elif kind == "progress" and isinstance(payload, dict):
                self.last_activity = time.monotonic()
                total, done = max(1, int(payload.get("total", 0))), max(0, int(payload.get("done", 0)))
                self.progress.configure(maximum=total, value=min(done, total))
                if payload.get("unit") == "seconds":
                    self.progress_text.set(f"{media_time(done)} / {media_time(payload.get('total', 0))}　"
                                           f"{payload.get('title', '')}")
                else:
                    self.progress_text.set(f"{done}/{payload.get('total', 0)}　{payload.get('title', '')}")
                    self.chapter_text.set(f"章节处理　{done} / {payload.get('total', 0)}")
        if self.cancelled.is_set() and self.dialog is not None:
            self.defer_dialog()
        now = time.monotonic()
        if self.busy and self.operation_started is not None:
            self.activity_text.set(f"本次操作已用 {media_time(now - self.operation_started)}　·　最近更新于 {int(now - self.last_activity)} 秒前")
        else:
            self.activity_text.set("操作已结束" if self.operation_started else "尚未开始")
        self.timer = self.window.after(75, self.poll)

    def destroy(self):
        self.destroyed = True
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None
        if self.autoload_timer is not None:
            self.window.after_cancel(self.autoload_timer)
            self.autoload_timer = None
        self.window.destroy()


def entry(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--mode", choices=MODE_TITLES, required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--ui-check", action="store_true")
    args = parser.parse_args(argv)
    profile = AccountStore(args.root).get(args.profile)
    window = tk.Tk()
    if args.ui_check:
        window.withdraw()
    controller = StudyWindow(window, args.root.resolve(), profile, args.mode, autoload=not args.ui_check,
                             session_id=args.session_id)
    if args.ui_check:
        window.geometry("860x640+10000+10000")
        window.deiconify()
        window.update()
        controls = [controller.refresh_button, controller.start_button, controller.stop_button,
                    controller.listing, controller.log_button, controller.recover_button]
        if controller.settings_button is not None:
            controls.append(controller.settings_button)
        if controller.speed_box is not None:
            controls.append(controller.speed_box)
        for widget in controls:
            assert widget.winfo_ismapped()
            assert widget.winfo_rooty() + widget.winfo_height() <= window.winfo_rooty() + window.winfo_height()
        controller.destroy()
        print(json.dumps({"study_ui_initialized": True, "mode": args.mode, "network_called": False}))
    else:
        window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(entry())
