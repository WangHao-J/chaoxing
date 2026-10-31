"""Native study window layout, independent from worker/event lifecycle."""
import tkinter as tk
from tkinter import ttk
from app_modes import MODE_LABELS as MODE_TITLES
from ui_theme import apply_theme
from ui_widgets import wrap_to_width, WorkspaceViewport
from ui_feedback import NoticeVar, Notice
from course_checklist import CourseChecklist


def build_study_view(self, configured_video_speed):
    title = MODE_TITLES[self.mode]
    self.palette = apply_theme(self.window)
    self.window.title(title + " · " + self.profile.name)
    self.window.geometry("1160x860")
    self.window.minsize(1020, 780)
    self.window.rowconfigure(0, weight=1)
    self.window.columnconfigure(1, weight=1)
    rail = ttk.Frame(self.window, style="Sidebar.TFrame", padding=(16, 24), width=172)
    rail.grid(row=0, column=0, sticky="ns")
    rail.pack_propagate(False)
    ttk.Label(rail, text=title, style="SidebarTitle.TLabel", wraplength=140).pack(anchor="w")
    ttk.Label(rail, text=self.profile.name, style="SidebarMuted.TLabel", wraplength=140).pack(anchor="w", pady=(12, 28))
    for step in ("01  勾选课程", "02  开始学习", "03  保存与续接"):
        ttk.Label(rail, text=step, style="Sidebar.TLabel").pack(anchor="w", pady=(0, 16))
    ttk.Label(rail, text="进度按账户保存\n随时可安全停止", style="SidebarMuted.TLabel").pack(side="bottom", anchor="w")
    viewport = WorkspaceViewport(self.window, base_height=780, padding=16)
    viewport.grid(row=0, column=1, sticky="nsew")
    frame = viewport.content
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(2, weight=1)
    heading = ttk.Frame(frame, style="Page.TFrame")
    heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))
    ttk.Label(heading, text="课程与进度", style="Heading.TLabel").pack(anchor="w")
    account_label = ttk.Label(heading, text="账户：" + self.profile.name + "　/　课程与进度",
                              style="Muted.TLabel")
    account_label.pack(anchor="w", fill="x", pady=(3, 0))
    wrap_to_width(account_label, heading)
    overview = ttk.Frame(frame, padding=16, style="Panel.TFrame")
    overview.grid(row=1, column=0, sticky="ew", pady=(0, 10))
    overview.columnconfigure(0, weight=1)
    ttk.Label(overview, text="学习进度", style="Subheading.TLabel").grid(row=0, column=0, sticky="w")
    self.status = NoticeVar(value="正在准备课程列表…", tone="busy")
    self.status_notice = Notice(overview, self.status, compact=True)
    self.status_notice.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 8))
    self.status_label = self.status_notice.message
    self.activity_text = tk.StringVar(value="尚未开始")
    ttk.Label(overview, textvariable=self.activity_text, style="CardMuted.TLabel").grid(row=2, column=0, columnspan=2, sticky="w")
    self.progress = ttk.Progressbar(overview, mode="determinate", maximum=1)
    self.progress.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 6))
    self.progress_text = tk.StringVar(value="请选择课程后开始")
    self.progress_label = ttk.Label(overview, textvariable=self.progress_text, style="CardMuted.TLabel", wraplength=730)
    self.progress_label.grid(row=4, column=0, columnspan=2, sticky="ew")
    self.chapter_text = tk.StringVar(value="")
    ttk.Label(overview, textvariable=self.chapter_text, style="CardMuted.TLabel").grid(row=0, column=1, sticky="e")
    actions = ttk.Frame(frame, style="Page.TFrame")
    actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
    self.start_button = ttk.Button(actions, text="开始所选课程", style="Primary.TButton", command=self.start_selected)
    self.start_button.pack(side="left")
    self.stop_button = ttk.Button(actions, text="停止", style="Danger.TButton", command=self.stop)
    self.stop_button.pack(side="left", padx=8)
    self.speed, self.speed_box = None, None
    if self.mode != "quizzes":
        ttk.Label(actions, text="倍速", style="Muted.TLabel").pack(side="left", padx=(4, 5))
        self.speed = tk.StringVar(value=f"{configured_video_speed(self.profile):g}")
        self.speed_box = ttk.Combobox(actions, textvariable=self.speed, values=("1", "1.25", "1.5", "2"),
                                      width=5, state="readonly")
        self.speed_box.pack(side="left", padx=(2, 8))
    self.settings_button = None
    if self.mode != "videos":
        self.settings_button = ttk.Button(actions, text="AI 答题设置", command=self.open_settings)
        self.settings_button.pack(side="right")
    course_panel = ttk.Frame(frame, padding=16, style="Panel.TFrame")
    course_panel.grid(row=2, column=0, sticky="nsew")
    course_panel.columnconfigure(0, weight=1)
    course_panel.rowconfigure(2, weight=1)
    course_header = ttk.Frame(course_panel, style="Card.TFrame")
    course_header.grid(row=0, column=0, sticky="ew")
    ttk.Label(course_header, text="选择课程", style="Subheading.TLabel").pack(side="left")
    self.selection_text = tk.StringVar(value="未选择")
    ttk.Label(course_header, textvariable=self.selection_text, style="CardMuted.TLabel").pack(side="left", padx=12)
    self.refresh_button = ttk.Button(course_header, text="刷新", style="Quiet.TButton", command=self.refresh_courses)
    self.refresh_button.pack(side="right")
    self.clear_courses_button = ttk.Button(course_header, text="取消全选", style="Small.TButton",
                                           command=lambda: self.listing.uncheck_all())
    self.clear_courses_button.pack(side="right", padx=8)
    self.all_courses_button = ttk.Button(course_header, text="全选", style="Small.TButton",
                                         command=lambda: self.listing.check_all())
    self.all_courses_button.pack(side="right")
    help_label = ttk.Label(course_panel, text="直接点击课程前的方框勾选，多门课程可连续选择；空格键也可切换。",
                           style="CardMuted.TLabel")
    help_label.grid(row=1, column=0, sticky="ew", pady=(4, 8))
    wrap_to_width(help_label, course_panel, 24)
    course_box = ttk.Frame(course_panel, style="Card.TFrame")
    course_box.grid(row=2, column=0, sticky="nsew")
    self.listing = CourseChecklist(course_box, height=4,
        can_toggle=lambda: not (self.busy or self.closing or self.configuring or self.recovered))
    self.listing.heading("title", text="课程")
    self.listing.heading("class", text="班级")
    self.listing.column("title", width=550, minwidth=240)
    self.listing.column("class", width=170, minwidth=110)
    scrollbar = ttk.Scrollbar(course_box, command=self.listing.yview)
    self.listing.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    self.listing.pack(fill="both", expand=True)
    self.listing.bind("<<TreeviewSelect>>", lambda event: self.update_controls(), add=True)
    footer = ttk.Frame(frame, style="Page.TFrame")
    footer.grid(row=4, column=0, sticky="ew", pady=(10, 0))
    self.log_button = ttk.Button(footer, text="展开运行记录", style="Quiet.TButton", command=self.toggle_logs)
    self.log_button.pack(side="left")
    self.recover_button = ttk.Button(footer, text="修复续接记录", style="Quiet.TButton", command=self.recover_mode)
    self.recover_button.pack(side="right")
    self.log_frame = ttk.Frame(frame, style="Card.TFrame", padding=6)
    self.log = tk.Text(self.log_frame, height=3, wrap="word", state="disabled", relief="flat",
                       font=("Microsoft YaHei UI", 9), background=self.palette["surface"],
                       foreground=self.palette["muted"], highlightthickness=0)
    log_scroll = ttk.Scrollbar(self.log_frame, command=self.log.yview)
    self.log.configure(yscrollcommand=log_scroll.set)
    log_scroll.pack(side="right", fill="y")
    self.log.pack(fill="both", expand=True)
    overview.bind("<Configure>", lambda event: self.resize_labels(event.width))
    self.update_controls()

