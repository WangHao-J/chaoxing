"""AI connection form with visible labels, clear actions and semantic feedback."""
import tkinter as tk
from tkinter import ttk
from ui_feedback import Notice, NoticeVar
from ui_widgets import wrap_to_width, WorkspaceViewport


def build_settings_view(self, profile, settings, initial):
    window = self.window
    window.title("统一 API 设置")
    window.geometry("760x760")
    window.minsize(700, 720)
    header = ttk.Frame(window, style="Sidebar.TFrame", padding=(24, 20))
    header.pack(fill="x")
    ttk.Label(header, text="统一 API 设置", style="SidebarTitle.TLabel").pack(anchor="w")
    ttk.Label(header, text="配置一次，所有账号共用；新增账号自动使用", style="SidebarMuted.TLabel").pack(anchor="w", pady=(6, 0))
    viewport = WorkspaceViewport(window, base_height=600)
    viewport.pack(fill="both", expand=True)
    page = viewport.content
    self.enabled = tk.BooleanVar(value=settings["enabled"])
    ttk.Checkbutton(page, text="启用 AI 答题服务", variable=self.enabled).pack(anchor="w", pady=(0, 12))
    card = ttk.Frame(page, style="Panel.TFrame", padding=20)
    card.pack(fill="x")
    ttk.Label(card, text="连接信息", style="Subheading.TLabel").pack(anchor="w", pady=(0, 12))
    self.fields, self.field_entries = {}, {}
    for name, label in (("endpoint", "服务地址（兼容 Chat Completions）"), ("model", "模型名称"), ("key", "API Key")):
        ttk.Label(card, text=label, style="Card.TLabel").pack(anchor="w", pady=(4, 5))
        variable = tk.StringVar(value=settings[name])
        self.fields[name] = variable
        entry = ttk.Entry(card, textvariable=variable, show="●" if name == "key" else "")
        entry.pack(fill="x", pady=(0, 8))
        self.field_entries[name] = entry
    hint = ttk.Label(page, text="所有账号共用此配置。密钥在当前 Windows 用户下加密保存；题目发送至该服务，可能产生服务费用。",
                     style="Muted.TLabel", wraplength=650)
    hint.pack(fill="x", pady=(12, 12))
    wrap_to_width(hint, page)
    self.feedback = NoticeVar(value=initial or "先测试连接，成功后保存设置。", tone="error" if initial else "info")
    self.feedback_notice = Notice(page, self.feedback, compact=True)
    self.feedback_notice.pack(fill="x")
    actions = ttk.Frame(page, style="Page.TFrame")
    actions.pack(side="bottom", fill="x", pady=(16, 0))
    self.test_button = ttk.Button(actions, text="测试 AI 连接", command=self.test)
    self.test_button.pack(side="left")
    self.save_button = ttk.Button(actions, text="保存设置", command=self.save, style="Primary.TButton")
    self.save_button.pack(side="right")

