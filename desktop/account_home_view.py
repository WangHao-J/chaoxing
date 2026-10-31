"""Account workspace: a persistent navigation rail and three clear study routes."""
import tkinter as tk
from tkinter import ttk
from app_modes import MODES
from ui_theme import apply_theme
from ui_widgets import labelled_value, wrap_to_width, WorkspaceViewport
from ui_feedback import Notice, NoticeVar


def build_account_home(self):
    window = self.window
    window.title("学习通助手 · 学习工作台")
    window.geometry("1160x860")
    window.minsize(1000, 760)
    palette = apply_theme(window)
    window.rowconfigure(0, weight=1)
    window.columnconfigure(1, weight=1)
    rail = ttk.Frame(window, style="Sidebar.TFrame", padding=(18, 24), width=180)
    rail.grid(row=0, column=0, sticky="ns")
    rail.pack_propagate(False)
    ttk.Label(rail, text="学习通\n助手", style="SidebarTitle.TLabel").pack(anchor="w")
    ttk.Label(rail, text="学习工作台", style="SidebarMuted.TLabel").pack(anchor="w", pady=(8, 32))
    ttk.Label(rail, text="账户工具", style="SidebarMuted.TLabel").pack(anchor="w", pady=(0, 10))
    utilities = []
    for text, command in (("登录账户", self.open_login), ("待处理任务", self.open_pending),
                          ("AI 设置", self.open_ai_settings), ("停止运行", self.stop)):
        button = ttk.Button(rail, text=text, command=command, style="Sidebar.TButton")
        button.pack(fill="x", pady=(0, 10))
        utilities.append(button)
    more = ttk.Menubutton(rail, text="更多工具", style="Sidebar.TButton")
    menu = tk.Menu(more, tearoff=False)
    menu.add_command(label="打开账户文件", command=self.open_directory)
    menu.add_command(label="编辑课程配置", command=self.open_config)
    more.configure(menu=menu)
    more.pack(side="bottom", fill="x", pady=(0, 12))
    ttk.Label(rail, text="登录与进度\n按账户独立保存", style="SidebarMuted.TLabel").pack(side="bottom", anchor="w", pady=(0, 20))

    viewport = WorkspaceViewport(window, base_height=760)
    viewport.grid(row=0, column=1, sticky="nsew")
    page = viewport.content
    page.columnconfigure(0, weight=1)
    page.rowconfigure(1, weight=1)
    header = ttk.Frame(page, style="Page.TFrame")
    header.grid(row=0, column=0, sticky="ew", pady=(0, 20))
    titles = ttk.Frame(header, style="Page.TFrame")
    titles.pack(side="left", fill="x", expand=True)
    ttk.Label(titles, text="账户与运行", style="Heading.TLabel").pack(anchor="w")
    self.account_count = tk.StringVar(value="我的账户")
    ttk.Label(titles, textvariable=self.account_count, style="Muted.TLabel").pack(anchor="w", pady=(5, 0))
    add = ttk.Button(header, text="新增账户", command=self.add)
    add.pack(side="right")

    account_panel = ttk.Frame(page, style="Panel.TFrame", padding=16)
    account_panel.grid(row=1, column=0, sticky="nsew")
    account_panel.columnconfigure(0, weight=1)
    account_panel.rowconfigure(1, weight=1)
    filters = ttk.Frame(account_panel, style="Card.TFrame")
    filters.grid(row=0, column=0, sticky="ew", pady=(0, 12))
    ttk.Label(filters, text="搜索账户", style="Card.TLabel").pack(side="left", padx=(0, 8))
    self.search_text = tk.StringVar()
    self.search_entry = ttk.Entry(filters, textvariable=self.search_text)
    self.search_entry.pack(side="left", fill="x", expand=True)
    self.clear_search_button = ttk.Button(filters, text="清除", style="Small.TButton", command=lambda: self.search_text.set(""))
    self.clear_search_button.pack(side="left", padx=8)
    self.status_filter = tk.StringVar(value="全部账户")
    self.filter_box = ttk.Combobox(filters, textvariable=self.status_filter, width=10, state="readonly",
                                 values=("全部账户", "运行中", "待处理"))
    self.filter_box.pack(side="left")
    self.search_text.trace_add("write", lambda *_: self.render_accounts())
    self.status_filter.trace_add("write", lambda *_: self.render_accounts())
    self.search_entry.bind("<Escape>", lambda event: self.search_text.set(""))
    window.bind("<Control-f>", lambda event: self.search_entry.focus_set())
    table = ttk.Frame(account_panel, style="Card.TFrame")
    table.grid(row=1, column=0, sticky="nsew")
    self.tree = ttk.Treeview(table, columns=("name", "status"), show="headings", selectmode="browse", height=3)
    for column, label, width, minimum in (("name", "账户名称", 400, 220), ("status", "运行状态", 220, 180)):
        self.tree.heading(column, text=label)
        self.tree.column(column, width=width, minwidth=minimum)
    self.tree.bind("<Double-1>", lambda event: self.open_study("mixed") if self.tree.identify_row(event.y) else None)
    self.tree.bind("<<TreeviewSelect>>", lambda event: self.update_selected_account())
    self.tree.bind("<Return>", lambda event: self.open_study("mixed"))
    scroll = ttk.Scrollbar(table, command=self.tree.yview)
    scroll.pack(side="right", fill="y")
    self.tree.configure(yscrollcommand=scroll.set)
    self.tree.pack(fill="both", expand=True)
    self.tree.tag_configure("active", foreground=palette["primary"])
    self.tree.tag_configure("attention", foreground=palette["danger"])
    self.empty_label = ttk.Label(table, text="", style="CardMuted.TLabel", anchor="center")
    selection = ttk.Frame(account_panel, style="Card.TFrame")
    selection.grid(row=2, column=0, sticky="ew", pady=(12, 0))
    ttk.Label(selection, text="当前账户", style="CardMuted.TLabel").pack(side="left", padx=(0, 10))
    self.selected_account = tk.StringVar(value="请先选择账户")
    self.selected_value = labelled_value(selection, self.selected_account)

    routes = ttk.Frame(page, style="Page.TFrame")
    routes.grid(row=2, column=0, sticky="ew", pady=(20, 16))
    ttk.Label(routes, text="选择学习方式", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
    self.action_buttons, self.account_actions = [], list(utilities)
    for index, mode in enumerate(MODES):
        routes.columnconfigure(index, weight=1, uniform="routes")
        card = ttk.Frame(routes, style="Panel.TFrame", padding=14)
        card.grid(row=1, column=index, sticky="nsew", padx=(0, 12 if index < 2 else 0))
        ttk.Label(card, text=f"0{index + 1}  /  {mode.scope}", style="CardMuted.TLabel").pack(anchor="w")
        description = ttk.Label(card, text=mode.description, style="Card.TLabel", justify="left")
        description.pack(fill="x", pady=(10, 14))
        wrap_to_width(description, card, 30)
        button = ttk.Button(card, text=mode.title, command=lambda choice=mode.id: self.open_study(choice),
                            style="Primary.TButton" if mode.id == "mixed" else "TButton")
        button.pack(fill="x")
        self.action_buttons.append(button)
    self.account_actions += self.action_buttons[:]
    self.action_buttons.extend([add, *utilities])
    self.note = NoticeVar(value="先选择账户，再打开学习窗口。同一账户切换方式前，请先关闭当前窗口。")
    self.note_notice = Notice(page, self.note, compact=True)
    self.note_notice.grid(row=3, column=0, sticky="ew")
