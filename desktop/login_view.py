"""Native sign-in form; password remains in memory only during authentication."""
import tkinter as tk
from tkinter import ttk
from ui_feedback import Notice, NoticeVar
from ui_widgets import wrap_to_width, WorkspaceViewport


def build_login_view(self):
    window = self.window
    window.title("登录学习通 · " + self.profile.name)
    window.geometry("660x680")
    window.minsize(620, 640)
    header = ttk.Frame(window, style="Sidebar.TFrame", padding=(24, 20))
    header.pack(fill="x")
    ttk.Label(header, text="登录学习通", style="SidebarTitle.TLabel").pack(anchor="w")
    label = ttk.Label(header, text="账户：" + self.profile.name, style="SidebarMuted.TLabel")
    label.pack(fill="x", pady=(6, 0))
    wrap_to_width(label, header, 48)
    viewport = WorkspaceViewport(window, base_height=520)
    viewport.pack(fill="both", expand=True)
    page = viewport.content
    card = ttk.Frame(page, style="Panel.TFrame", padding=20)
    card.pack(fill="x")
    ttk.Label(card, text="账户信息", style="Subheading.TLabel").pack(anchor="w", pady=(0, 16))
    self.phone, self.password = tk.StringVar(), tk.StringVar()
    ttk.Label(card, text="手机号", style="Card.TLabel").pack(anchor="w")
    self.phone_entry = ttk.Entry(card, textvariable=self.phone)
    self.phone_entry.pack(fill="x", pady=(6, 16))
    ttk.Label(card, text="密码", style="Card.TLabel").pack(anchor="w")
    self.password_entry = ttk.Entry(card, textvariable=self.password, show="●")
    self.password_entry.pack(fill="x", pady=(6, 8))
    self.show_password = tk.BooleanVar(value=False)
    self.show_button = ttk.Checkbutton(card, text="显示密码", variable=self.show_password,
                                       command=self.toggle_password, style="Card.TCheckbutton")
    self.show_button.pack(anchor="w")
    self.status = NoticeVar(value="登录成功后返回课程页，选择运行方式并读取课程。")
    self.status_notice = Notice(page, self.status, compact=True)
    self.status_notice.pack(fill="x", pady=(16, 8))
    self.status_label = self.status_notice.message
    self.activity = tk.StringVar()
    ttk.Label(page, textvariable=self.activity, style="Muted.TLabel").pack(anchor="w")
    actions = ttk.Frame(page, style="Page.TFrame")
    actions.pack(side="bottom", fill="x", pady=(16, 0))
    self.cancel_button = ttk.Button(actions, text="取消", command=self.close)
    self.cancel_button.pack(side="left")
    self.login_button = ttk.Button(actions, text="登录", command=self.login, style="Primary.TButton")
    self.login_button.pack(side="right")
    self.password_entry.bind("<Return>", lambda event: self.login())
    self.phone_entry.bind("<Return>", lambda event: self.password_entry.focus_set())
