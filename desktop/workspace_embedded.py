"""Account pages reuse the proven controllers inside frames, never new windows."""
import threading
import tkinter as tk
from tkinter import ttk

from course_checklist import CourseChecklist
from login_window import LoginWindow
from pending_quiz_worker import PendingQuizWindow
from quiz_assistant import QuizError, read_settings
from quiz_dialog import ReviewDialog, ai_configured
from recovery_dialog import RecoveryDialog
from study_window import StudyWindow, configured_video_speed
from ui_feedback import Notice, NoticeVar
from ui_theme import PALETTE
from ui_widgets import WorkspaceViewport
from workspace_transport import WorkerClient


class PageHost(tk.Frame):
    """Window-shaped controller surface with local lifecycle and local shortcuts."""
    embedded = True

    def __init__(self, parent):
        super().__init__(parent, background=PALETTE["bg"])
        self.handlers, self.scheduled = {}, set()
        self.caption = ""

    def title(self, text=None):
        if text is not None:
            self.caption = text
        return self.caption

    def geometry(self, *args):
        return ""

    def minsize(self, *args):
        return None

    def protocol(self, name, callback=None):
        if callback is not None:
            self.handlers[name] = callback

    def withdraw(self):
        pass

    def deiconify(self):
        pass

    def quit(self):
        pass

    def grab_set(self):
        pass

    def grab_release(self):
        pass

    def after(self, milliseconds, callback=None, *args):
        if callback is None:
            return super().after(milliseconds)
        key = None
        def run():
            self.scheduled.discard(key)
            callback(*args)
        key = super().after(milliseconds, run)
        self.scheduled.add(key)
        return key

    def after_cancel(self, key):
        self.scheduled.discard(key)
        super().after_cancel(key)

    def destroy(self):
        for key in list(self.scheduled):
            self.after_cancel(key)
        super().destroy()


def build_study(controller):
    self = controller
    viewport = WorkspaceViewport(self.window, base_height=640, padding=14)
    self.workspace_viewport = viewport
    viewport.pack(fill="both", expand=True)
    frame = viewport.content
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(2, weight=1)
    overview = ttk.Frame(frame, style="Panel.TFrame", padding=14)
    overview.grid(row=0, column=0, sticky="ew", pady=(0, 12))
    self.status = NoticeVar(value="登录后会自动加载课程；切换账号时，后台任务继续运行。")
    self.status_notice = Notice(overview, self.status, compact=True)
    self.status_notice.pack(fill="x")
    self.status_label = self.status_notice.message
    self.activity_text, self.chapter_text = tk.StringVar(), tk.StringVar()
    ttk.Label(overview, textvariable=self.activity_text, style="CardMuted.TLabel").pack(anchor="w", pady=(6, 0))
    self.progress = ttk.Progressbar(overview, maximum=1)
    self.progress.pack(fill="x", pady=(8, 6))
    self.progress_text = tk.StringVar(value="尚未开始")
    self.progress_label = ttk.Label(overview, textvariable=self.progress_text, style="CardMuted.TLabel", wraplength=700)
    self.progress_label.pack(fill="x")
    self.eta_text = tk.StringVar(value="当前课程剩余音视频用时：等待估算" if self.mode != "quizzes" else "答题用时取决于题量、服务响应和人工操作。")
    ttk.Label(overview, textvariable=self.eta_text, style="CardMuted.TLabel", wraplength=700).pack(fill="x", pady=(4, 0))
    ttk.Label(overview, textvariable=self.chapter_text, style="CardMuted.TLabel").pack(anchor="w")
    tools = ttk.Frame(frame, style="Page.TFrame")
    tools.grid(row=1, column=0, sticky="ew", pady=(0, 10))
    self.start_button = ttk.Button(tools, text="开始所选课程", style="Primary.TButton", command=self.start_selected)
    self.start_button.pack(side="left")
    self.stop_button = ttk.Button(tools, text="停止当前账号", style="Danger.TButton", command=self.stop)
    self.stop_button.pack(side="left", padx=8)
    self.speed = self.speed_box = None
    if self.mode != "quizzes":
        ttk.Label(tools, text="倍速").pack(side="left", padx=(8, 4))
        self.speed = tk.StringVar(value=f"{configured_video_speed(self.profile):g}")
        self.speed_box = ttk.Combobox(tools, textvariable=self.speed, values=("1", "1.25", "1.5", "2"), width=5, state="readonly")
        self.speed_box.pack(side="left")
    self.auto_quiz_text = tk.StringVar(value="AI 自动答题：关闭")
    self.auto_quiz_button = ttk.Button(tools, textvariable=self.auto_quiz_text, width=19,
                                      command=self.toggle_auto_quizzes)
    self.auto_quiz_button.pack(side="left", padx=(12, 0))
    self.settings_button = None
    panel = ttk.Frame(frame, style="Panel.TFrame", padding=14)
    panel.grid(row=2, column=0, sticky="nsew")
    panel.columnconfigure(0, weight=1)
    panel.rowconfigure(1, weight=1)
    header = ttk.Frame(panel, style="Card.TFrame")
    header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
    self.selection_text = tk.StringVar(value="未选择")
    ttk.Label(header, textvariable=self.selection_text, style="CardMuted.TLabel").pack(side="left")
    self.refresh_button = None
    self.clear_courses_button = ttk.Button(header, text="取消全选", command=lambda: self.listing.uncheck_all())
    self.clear_courses_button.pack(side="right", padx=6)
    self.all_courses_button = ttk.Button(header, text="全选", command=lambda: self.listing.check_all())
    self.all_courses_button.pack(side="right")
    table = ttk.Frame(panel, style="Card.TFrame")
    table.grid(row=1, column=0, sticky="nsew")
    self.listing = CourseChecklist(table, height=6, can_toggle=lambda: not (self.busy or self.closing or self.recovered))
    self.listing.heading("title", text="课程")
    self.listing.heading("class", text="班级")
    self.listing.column("title", width=430, minwidth=180)
    self.listing.column("class", width=130, minwidth=90)
    scroll = ttk.Scrollbar(table, command=self.listing.yview)
    scroll.pack(side="right", fill="y")
    self.listing.configure(yscrollcommand=scroll.set)
    self.listing.pack(fill="both", expand=True)
    self.listing.bind("<<TreeviewSelect>>", lambda event: self.update_controls(), add=True)
    footer = ttk.Frame(frame, style="Page.TFrame")
    footer.grid(row=3, column=0, sticky="ew", pady=(10, 0))
    self.log_button = ttk.Button(footer, text="展开运行记录", command=self.toggle_logs)
    self.log_button.pack(side="left")
    self.recover_button = ttk.Button(footer, text="修复续接记录", command=self.recover_mode)
    self.recover_button.pack(side="right")
    self.log_frame = ttk.Frame(frame, style="Card.TFrame")
    self.log = tk.Text(self.log_frame, height=12, wrap="word", state="disabled", relief="flat",
                       font=("Microsoft YaHei UI", 11), background=PALETTE["surface"], foreground=PALETTE["muted"])
    self.log_scroll = ttk.Scrollbar(self.log_frame, command=self.log.yview)
    self.log_scroll.pack(side="right", fill="y")
    self.log.configure(yscrollcommand=self.log_scroll.set)
    self.log.pack(fill="both", expand=True)
    self.update_controls()


class EmbeddedStudy(StudyWindow):
    def __init__(self, panel, host, mode, *, engine_factory=None):
        self.panel = panel
        self.auto_quizzes_enabled = False
        factory = engine_factory or (lambda *args: WorkerClient(*args, fixture=panel.workspace.fixture))
        super().__init__(host, panel.workspace.store.root, panel.profile, mode,
                         engine_factory=factory, autoload=False)

    def build(self):
        build_study(self)

    def update_controls(self):
        super().update_controls()
        unavailable = (self.busy or self.closing or self.configuring or self.recovered
                       or self.settings_window is not None or self.mode == "videos")
        self.auto_quiz_button.configure(state="disabled" if unavailable else "normal")
        self.auto_quiz_text.set("AI 自动答题：开启" if self.auto_quizzes_enabled else "AI 自动答题：关闭")

    def toggle_auto_quizzes(self):
        if self.busy or self.closing or self.configuring or self.recovered or self.mode == "videos":
            return
        enabled = not self.auto_quizzes_enabled
        if enabled and not self.panel.ensure_ai():
            return
        self.auto_quizzes_enabled = enabled
        self.update_controls()
        if enabled:
            self.status.success("AI 自动答题已开启：开始后将自动填写并提交测验，仅当前账号生效；关闭账号页面后重置。")
        else:
            self.status.set("AI 自动答题已关闭：遇到测验时等待你确认。")

    def refresh_courses(self):
        if self.panel.login is not None and not self.panel.login.destroyed:
            self.status.error("请先完成或取消此账号的登录，再读取课程。")
            return
        if self.panel.pending is not None and not self.panel.pending.destroyed and self.panel.pending.busy:
            self.status.error("请先等待此账号的待处理测验结束。")
            return
        super().refresh_courses()

    def toggle_logs(self):
        self.logs_visible = not self.logs_visible
        if self.logs_visible:
            self.log_frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        else:
            self.log_frame.grid_remove()
        self.log_button.configure(text="收起运行记录" if self.logs_visible else "展开运行记录")
        self.workspace_viewport.base_height = 960 if self.logs_visible else 640
        self.window.after_idle(self.reveal_log_area)

    def reveal_log_area(self):
        if self.destroyed:
            return
        from types import SimpleNamespace
        canvas = self.workspace_viewport.canvas
        self.workspace_viewport.resize(SimpleNamespace(width=canvas.winfo_width(), height=canvas.winfo_height()))
        canvas.yview_moveto(1 if self.logs_visible else 0)

    def start_selected(self):
        if self.busy or self.closing or self.recovered:
            return
        selected = self.selected_keys()
        if not selected:
            self.status.error("请先登录并等待课程加载，然后勾选要开始的课程。")
            return
        if self.mode != "videos" and not self.panel.ensure_ai():
            return
        self.last_status_kind = ""
        self.status.busy("正在开始所选课程…")
        self.progress.configure(value=0, maximum=1)
        self.progress_text.set("正在读取所选课程的章节")
        self.launch("run", selected, None if self.speed is None else float(self.speed.get()),
                    self.auto_quizzes_enabled if self.mode != "videos" else False)

    def show_dialog(self, packet):
        if self.cancelled.is_set() or self.dialog is not None:
            packet["finished"].set()
            return
        host = self.panel.new_task_host()
        self.dialog_window, self.dialog_packet = host, packet
        try:
            kwargs = dict(packet["kwargs"])
            if packet["kind"] == "quiz":
                self.dialog = ReviewDialog(host, *packet["args"], **kwargs)
                self.dialog.finish = self.finish_dialog
            else:
                if "on_quiz" in kwargs:
                    kwargs["on_quiz"] = lambda parent: self.panel.ensure_ai()
                self.dialog = RecoveryDialog(host, *packet["args"], **kwargs)
                self.dialog.finish = self.finish_dialog
            self.status.error("当前账号需要处理，请点击“任务详情”。其他账号继续运行。")
            # Do not move keyboard focus or steal another account's active page.
            self.panel.notify_task()
        except Exception:
            self.finish_dialog()
            self.status.error("任务详情未能打开，记录已保留。请停止后重新读取。")

    def finish_dialog(self, result=None):
        decision = result if result is not None else getattr(self.dialog, "result", None)
        action = decision.get("action") if isinstance(decision, dict) else decision
        if action == "auto_all_quizzes":
            self.auto_quizzes_enabled = True
        elif action == "skip_all_quizzes":
            self.auto_quizzes_enabled = False
        super().finish_dialog(result)
        if not self.destroyed:
            self.update_controls()
        self.panel.task_finished()

    def open_settings(self):
        self.panel.workspace.show_settings()

    def destroy(self):
        if self.destroyed:
            return
        super().destroy()
        self.panel.study_closed(self)


class EmbeddedLogin(LoginWindow):
    def __init__(self, panel, host):
        self.panel = panel
        def factory(root, profile):
            return WorkerClient(root, profile, "mixed", lambda *args: None, lambda *args, **kwargs: "stop",
                                threading.Event(), kind="login", fixture=panel.workspace.fixture)
        super().__init__(host, panel.workspace.store.root, panel.profile, backend_factory=factory)

    def destroy(self):
        if self.destroyed:
            return
        backend, success = self.backend, self.authenticated
        super().destroy()
        if backend is not None:
            threading.Thread(target=backend.close, daemon=True).start()
        self.panel.login_closed(self, success)


class EmbeddedPending(PendingQuizWindow):
    def __init__(self, panel, host, task):
        self.panel = panel
        def operation(root, profile, requested, reviewer, status, cancelled):
            client = WorkerClient(root, profile, "quizzes", lambda kind, value: status(
                value.get("message", "") if isinstance(value, dict) else str(value)),
                lambda kind, *args, **kwargs: reviewer(*args), cancelled,
                kind="pending", fixture=panel.workspace.fixture)
            try:
                return client.run_pending(requested)
            finally:
                client.close()
        super().__init__(host, panel.workspace.store.root, panel.profile, task, operation=operation)

    def destroy(self):
        if self.destroyed:
            return
        super().destroy()
        self.panel.pending_closed(self)
