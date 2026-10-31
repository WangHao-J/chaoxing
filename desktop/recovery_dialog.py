"""Manual recovery controls; no CAPTCHA recognition or automatic submission."""
import time
import io
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk
from ui_theme import apply_theme
from ui_feedback import NoticeVar, Notice
from ui_widgets import WorkspaceViewport
from manual_tasks import safe_link, COURSE_PORTAL, course_location


class RecoveryDialog:
    def __init__(self, window, account_name, reason, chapter, retry_in, allow_retry, clock=time.monotonic,
                 *, verifier=None, course_title="", manual_jobs=None, on_quiz=None):
        self.window, self.clock = window, clock
        self.deadline = clock() + retry_in
        self.allow_retry = allow_retry
        self.result = "stop"
        self.timer = None
        self.verifier = verifier
        self.location = course_location(course_title, chapter)
        self.manual_jobs = manual_jobs or []
        self.on_quiz = on_quiz
        self.busy = False
        self.events = queue.Queue()
        apply_theme(window)
        window.minsize(680, 620)
        window.title("需要处理 · " + account_name)
        only_quizzes = bool(self.manual_jobs) and all(job.get("kind") == "workid" for job in self.manual_jobs) and not verifier
        viewport = WorkspaceViewport(window, base_height=620 if only_quizzes else 840)
        viewport.pack(fill="both", expand=True)
        frame = viewport.content
        style = ttk.Style(window)
        style.theme_use("clam")
        style.configure("Recovery.TButton", padding=(14, 9))
        self.only_quizzes = bool(self.manual_jobs) and all(
            job.get("kind") == "workid" for job in self.manual_jobs) and not verifier
        if self.only_quizzes:
            self.build_quiz_page(frame, account_name, course_title, chapter, reason)
            window.protocol("WM_DELETE_WINDOW", self.stop)
            self.tick()
            return
        ttk.Label(frame, text="需要你处理后再继续", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        guidance = (
            "点击“打开课程入口”，在浏览器登录相同账户。进入下方课程，再选择对应章节完成任务。\n"
            "打开链接本身不会被记为完成；程序会重新读取学习通中的状态。"
            if self.manual_jobs else
            "点击“显示验证码”，直接在本窗口填写图片中的字符并提交。\n"
            "这里使用当前账户的程序会话，提交后点击“重新检查”确认是否恢复。"
            if verifier else
            "平台没有提供可在本窗口显示的图片验证码，无需在首页寻找认证入口。\n"
            "可从课程入口进入原课程检查权限；登录失效请停止当前账户，回管理界面点击“重新登录”。\n"
            "浏览器登录不会同步到程序；频率限制请等待后重新检查。")
        for text in (f"账户：{account_name}", self.location):
            ttk.Label(frame, text=text, wraplength=500, font=("Microsoft YaHei UI", 10)).pack(
                anchor="w", fill="x", pady=(10, 0))
        self.reason_message = NoticeVar(value=reason, tone="error")
        Notice(frame, self.reason_message, compact=True).pack(fill="x", pady=(12, 8))
        help_panel = ttk.Frame(frame, style="Page.TFrame")
        ttk.Label(help_panel, text=guidance, wraplength=580, style="Muted.TLabel").pack(fill="x")
        def toggle_help():
            if help_panel.winfo_manager():
                help_panel.pack_forget()
            else:
                help_panel.pack(fill="x", after=help_button, pady=(6, 8))
                window.update_idletasks()
                window.geometry(f"{window.winfo_width()}x{max(window.winfo_height(), window.winfo_reqheight())}")
        help_button = ttk.Button(frame, text="查看处理方法", style="Small.TButton", command=toggle_help)
        help_button.pack(anchor="w", pady=(0, 6))
        if not allow_retry:
            ttk.Label(frame, text="本次重新检查次数已用完。可保留待处理任务后停止，处理后再次启动。", wraplength=500).pack(
                anchor="w", pady=(10, 0))
        if verifier:
            box = ttk.LabelFrame(frame, text="当前程序会话的验证码", padding=12)
            box.pack(fill="x", pady=(12, 0))
            image_area = ttk.Frame(box, height=125)
            image_area.pack(fill="x")
            image_area.pack_propagate(False)
            self.image_label = ttk.Label(image_area, text="点击下方按钮显示验证码")
            self.image_label.pack(expand=True)
            self.answer = tk.StringVar()
            self.answer_entry = ttk.Entry(box, textvariable=self.answer, font=("Microsoft YaHei UI", 12))
            self.answer_entry.pack(fill="x", pady=8)
            controls = ttk.Frame(box)
            controls.pack(fill="x")
            self.fetch_button = ttk.Button(controls, text="显示 / 更换验证码", command=self.fetch)
            self.fetch_button.pack(side="left")
            self.submit_button = ttk.Button(controls, text="提交验证码", command=self.submit, state="disabled")
            self.submit_button.pack(side="right")
            self.feedback = NoticeVar(value="不会自动识别或提交验证码。")
            Notice(box, self.feedback, compact=True).pack(fill="x", pady=(8, 0))
        if self.manual_jobs:
            self.job_list = tk.Listbox(frame, height=min(5, len(self.manual_jobs)),
                                      font=("Microsoft YaHei UI", 10), exportselection=False)
            self.job_list.pack(fill="x", pady=(12, 6))
            for job in self.manual_jobs:
                self.job_list.insert("end", job["name"])
            self.job_list.selection_set(0)
            if any(safe_link(job.get("href")) for job in self.manual_jobs):
                ttk.Button(frame, text="打开选中的外部链接", command=self.open_external).pack(fill="x")
            if any(job.get("kind") == "workid" for job in self.manual_jobs):
                self.quiz_button = ttk.Button(frame, text="打开答题页面", command=self.open_quiz,
                                              style="Recovery.TButton")
                self.quiz_button.pack(fill="x", pady=(10, 0))
                if not hasattr(self, "feedback"):
                    self.feedback = NoticeVar()
                    Notice(frame, self.feedback, compact=True).pack(fill="x")
        navigation = ttk.Frame(frame)
        navigation.pack(fill="x", pady=(12, 10))
        self.course_button = ttk.Button(navigation, text="打开课程入口", command=self.open_course,
                   style="Recovery.TButton")
        self.course_button.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.copy_button = ttk.Button(navigation, text="复制课程 / 章节名称", command=self.copy_location,
                   style="Recovery.TButton")
        self.copy_button.pack(side="left", fill="x", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        self.retry_button = ttk.Button(buttons, text="重新检查", command=self.retry, style="Recovery.TButton")
        self.retry_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(buttons, text="停止当前账户", command=self.stop, style="Recovery.TButton").grid(
            row=0, column=1, sticky="ew")
        if self.manual_jobs:
            only_quizzes = all(job.get("kind") == "workid" for job in self.manual_jobs)
            self.skip_button = ttk.Button(frame, text="跳过本次答题" if only_quizzes else "保留待处理，继续其他章节",
                command=lambda: self.finish("skip"), style="Recovery.TButton")
            self.skip_button.pack(fill="x", pady=(10, 0))
            if any(job.get("kind") == "workid" for job in self.manual_jobs):
                self.skip_all_button = ttk.Button(frame, text="跳过后续所有答题",
                    command=lambda: self.finish("skip_all_quizzes"), style="Recovery.TButton")
                self.skip_all_button.pack(fill="x", pady=(10, 0))
                ttk.Label(frame, text="仅本账户本轮生效；跳过的测验仍保留为未完成。", wraplength=500).pack(
                    anchor="w", pady=(6, 0))
        window.protocol("WM_DELETE_WINDOW", self.stop)
        self.tick()

    def build_quiz_page(self, frame, account_name, course_title, chapter, reason):
        frame.configure(padding=18)
        self.window.title("章节测验 · " + account_name)
        ttk.Label(frame, text="章节测验", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        for text in (f"账户：{account_name}", f"课程：{course_title}", f"章节：{chapter}"):
            ttk.Label(frame, text=text, wraplength=500).pack(anchor="w", fill="x", pady=(4, 0))
        unread_quiz = all(job.get("unread") for job in self.manual_jobs)
        self.feedback = NoticeVar(value="暂未读取到测验信息。请重新读取题目；本章节仍未完成。"
                                     if unread_quiz else "打开答题页面，使用 AI 填写或自行作答。")
        Notice(frame, self.feedback, compact=True).pack(fill="x", pady=(12, 12))
        if unread_quiz:
            self.feedback.error(self.feedback.get())
            self.retry_label = "重新读取题目"
            self.retry_button = ttk.Button(frame, text="重新读取题目", command=self.retry,
                                           style="Recovery.TButton")
            self.retry_button.pack(fill="x", pady=(0, 8))
        else:
            self.quiz_button = ttk.Button(frame, text="打开答题页面", command=self.open_quiz,
                                          style="Primary.TButton")
            self.quiz_button.pack(fill="x", pady=(0, 8))
        skip_row = ttk.Frame(frame)
        skip_row.pack(fill="x")
        skip_row.columnconfigure(0, weight=1, uniform="quiz_skip")
        skip_row.columnconfigure(1, weight=1, uniform="quiz_skip")
        self.skip_button = ttk.Button(skip_row, text="跳过本次答题", command=lambda: self.finish("skip"),
                                      style="Recovery.TButton")
        self.skip_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.skip_all_button = ttk.Button(skip_row, text="跳过后续所有答题",
            command=lambda: self.finish("skip_all_quizzes"), style="Recovery.TButton")
        self.skip_all_button.grid(row=0, column=1, sticky="ew")
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(8, 0))
        if not unread_quiz and any(job.get("needs_confirmation") or job.get("unread") for job in self.manual_jobs):
            self.retry_button = ttk.Button(controls, text="重新检查", command=self.retry,
                                           style="Recovery.TButton")
            self.retry_button.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.stop_button = ttk.Button(controls, text="停止当前账户", command=self.stop,
                                      style="Recovery.TButton")
        self.stop_button.pack(side="left", fill="x", expand=True)
        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(6, 0))
        ttk.Label(footer, text="全部跳过仅本账户本轮生效；测验仍未完成。", wraplength=400).pack(side="left")
        self.details_button = ttk.Button(footer, text="查看原因", command=self.toggle_details)
        self.details_button.pack(side="right")
        self.details = ttk.Frame(frame)
        Notice(self.details, NoticeVar(value=reason, tone="error"), compact=True).pack(fill="x")
        if not self.allow_retry and hasattr(self, "retry_button"):
            ttk.Label(self.details, text="重新检查次数已用完，处理后请再次启动账户。",
                      wraplength=500).pack(anchor="w", pady=(4, 0))

    def toggle_details(self):
        if self.details.winfo_manager():
            self.details.pack_forget()
            self.details_button.configure(text="查看原因")
        else:
            self.details.pack(fill="x", pady=(8, 0))
            self.details_button.configure(text="收起原因")
            self.window.update_idletasks()
            if self.window.winfo_reqheight() > self.window.winfo_height():
                self.window.geometry(f"{self.window.winfo_width()}x{self.window.winfo_reqheight()}")

    def open_quiz(self):
        if self.busy:
            return
        if self.on_quiz is None:
            self.feedback.error("请先配置 AI，再打开答题页面。")
            return
        self.busy = True
        self.quiz_button.configure(state="disabled")
        try:
            ready = self.on_quiz(self.window)
        except Exception:
            self.feedback.error("答题页面暂时无法打开，请检查 AI 设置后重试。")
            ready = False
        self.busy = False
        self.quiz_button.configure(state="normal")
        if ready:
            self.finish("quiz")
        elif not self.feedback.get() or self.only_quizzes:
            self.feedback.error("请先完成 AI 配置，再打开答题页面。")

    def tick(self):
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None
        remaining = max(0, int(self.deadline - self.clock() + 0.999))
        self.poll_events()
        enabled = self.allow_retry and remaining == 0 and not self.busy
        if hasattr(self, "retry_button"):
            label = getattr(self, "retry_label", "重新检查")
            self.retry_button.configure(state="normal" if enabled else "disabled",
                text=f"{label}（{remaining} 秒）" if remaining and self.allow_retry else label)
        self.timer = self.window.after(250, self.tick)

    def open_course(self):
        # The browser must obtain signed chapter links using its own logged-in session.
        webbrowser.open(COURSE_PORTAL)

    def copy_location(self):
        self.window.clipboard_clear()
        self.window.clipboard_append(self.location)

    def open_external(self):
        selected = self.job_list.curselection()
        if selected:
            link = safe_link(self.manual_jobs[selected[0]].get("href"))
            if link:
                webbrowser.open(link)

    def background(self, operation, kind):
        if self.busy:
            return
        self.busy = True
        self.fetch_button.configure(state="disabled")
        self.submit_button.configure(state="disabled")
        self.retry_button.configure(state="disabled")
        self.feedback.set("正在读取验证码……" if kind == "image" else "正在提交你填写的验证码……")
        def run():
            try:
                self.events.put((kind, operation()))
            except Exception:
                self.events.put(("error", None))
        threading.Thread(target=run, daemon=True).start()

    def fetch(self):
        self.answer.set("")
        self.background(self.verifier.fetch, "image")

    def submit(self):
        answer = self.answer.get().strip()
        if not answer:
            self.feedback.error("请填写图片中的字符。")
            return
        self.answer.set("")
        self.background(lambda: self.verifier.submit(answer), "submitted")

    def poll_events(self):
        try:
            kind, value = self.events.get_nowait()
        except queue.Empty:
            return
        self.busy = False
        self.fetch_button.configure(state="normal")
        if kind == "image":
            try:
                from PIL import Image, ImageTk
                picture = Image.open(io.BytesIO(value))
                if picture.width * picture.height > 1_000_000:
                    raise ValueError("Image too large")
                picture.thumbnail((450, 120))
                self.picture = ImageTk.PhotoImage(picture, master=self.window)
                self.image_label.configure(image=self.picture, text="")
                self.submit_button.configure(state="normal")
                self.feedback.set("填写上图中的字符，然后点击“提交验证码”。")
                self.answer_entry.focus_set()
            except Exception:
                self.feedback.error("验证码图片无法显示，请更换验证码或停止后重新登录。")
        elif kind == "submitted":
            self.image_label.configure(image="", text="如需重填，请更换验证码")
            self.feedback.set("验证码已提交。点击“重新检查”，由原任务确认是否恢复。" if value else
                              "平台未确认验证码提交，请更换图片后重填；也可能需要其他验证方式。", tone="info" if value else "error")
        else:
            self.image_label.configure(image="", text="未能读取或提交验证码")
            self.feedback.error("平台未返回支持的验证码，或网络请求失败。请重试、从课程入口检查权限，或停止后重新登录。")

    def finish(self, result):
        self.result = result
        if self.verifier:
            self.answer.set("")
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None
        self.window.withdraw()
        self.window.quit()

    def retry(self):
        if self.allow_retry and not self.busy and self.clock() >= self.deadline:
            self.finish("retry")

    def stop(self):
        self.finish("stop")


def ask_recovery(account_name, reason, chapter, retry_in, allow_retry, **options):
    window = tk.Tk()
    try:
        dialog = RecoveryDialog(window, account_name, reason, chapter, retry_in, allow_retry, **options)
        window.update_idletasks()
        width, height = max(680, window.winfo_reqwidth()), max(620 if dialog.only_quizzes else 760, window.winfo_reqheight())
        window.minsize(width, height)
        left, top = max(0, (window.winfo_screenwidth()-width)//2), max(0, (window.winfo_screenheight()-height)//2)
        window.geometry(f"{width}x{height}+{left}+{top}")
        window.lift()
        window.attributes("-topmost", True)
        window.after(200, lambda: window.attributes("-topmost", False))
        window.mainloop()
        return dialog.result
    finally:
        if "dialog" in locals():
            if dialog.timer is not None:
                window.after_cancel(dialog.timer)
        window.destroy()
