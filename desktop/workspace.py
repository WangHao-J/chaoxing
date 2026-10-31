"""One visible window: account navigation, persistent detail pages and shared API."""
import argparse
import json
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

from account_store import AccountStore, is_running
from app_modes import MODE_LABELS
from quiz_assistant import QuizError, read_settings
from quiz_dialog import SettingsDialog, ai_configured
from shared_ai_settings import legacy_candidates, migrate_shared_settings, shared_profile
from task_repository import list_pending
from ui_feedback import Notice, NoticeVar
from ui_theme import apply_theme, PALETTE
from workspace_embedded import PageHost, EmbeddedStudy, EmbeddedLogin, EmbeddedPending
from account_groups import AccountGroups, UNGROUPED
from workspace_groups import GroupNavigation


class AccountPanel:
    def __init__(self, workspace, profile):
        self.workspace, self.profile = workspace, profile
        self.frame = ttk.Frame(workspace.content, style="Page.TFrame")
        self.frame.rowconfigure(2, weight=1)
        self.frame.columnconfigure(0, weight=1)
        self.study = self.login = self.pending = None
        self.after_study = None
        self.pages, self.task_host = {}, None
        self.page = "courses"
        self.mode = tk.StringVar(value="mixed")
        self.changed = False
        context = ttk.Frame(self.frame, padding=(16, 14), style="Panel.TFrame")
        context.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.stop_button = ttk.Button(context, text="停止此账号", style="Danger.TButton", command=self.stop)
        self.stop_button.pack(side="right")
        identity = ttk.Frame(context, style="Card.TFrame")
        identity.pack(side="left", fill="x", expand=True, padx=(0, 12))
        name_label = ttk.Label(identity, text=profile.name, style="Heading.TLabel", wraplength=560)
        name_label.pack(anchor="w", fill="x")
        ttk.Label(identity, text="当前操作账号", style="CardMuted.TLabel").pack(anchor="w")
        identity.bind("<Configure>", lambda event: name_label.configure(wraplength=max(160, event.width)))
        navigation = ttk.Frame(self.frame, padding=(8, 4), style="Page.TFrame")
        navigation.grid(row=1, column=0, sticky="ew")
        self.buttons = {}
        for key, label in (("courses", "课程与进度"), ("task", "任务详情"),
                           ("pending", "待处理"), ("login", "账号登录"), ("reports", "运行报告")):
            button = ttk.Button(navigation, text=label, command=lambda page=key: self.show(page))
            button.pack(side="left", padx=(0, 8), pady=4)
            self.buttons[key] = button
        self.body = ttk.Frame(self.frame, style="Page.TFrame")
        self.body.grid(row=2, column=0, sticky="nsew")
        self.body.rowconfigure(0, weight=1)
        self.body.columnconfigure(0, weight=1)
        self.create_study()
        self.show("courses")

    def host(self, key):
        host = PageHost(self.body)
        self.pages[key] = host
        return host

    def create_study(self):
        host = self.host("courses")
        options = ttk.Frame(host, padding=(14, 8), style="Page.TFrame")
        options.pack(fill="x")
        ttk.Label(options, text="运行方式").pack(side="left", padx=(0, 8))
        self.mode_choice = tk.StringVar(value=MODE_LABELS[self.mode.get()])
        self.mode_box = ttk.Combobox(options, textvariable=self.mode_choice,
            values=tuple(MODE_LABELS.values()), width=16, state="readonly")
        self.mode_box.pack(side="left")
        self.mode_box.bind("<<ComboboxSelected>>", self.change_mode)
        ttk.Button(options, text="重新打开此账号", command=self.restart_study).pack(side="right")
        study_host = PageHost(host)
        study_host.pack(fill="both", expand=True)
        self.study = EmbeddedStudy(self, study_host, self.mode.get(), engine_factory=self.workspace.engine_factory)
        self.schedule_course_load()

    def schedule_course_load(self, *, force=False):
        controller = self.study
        if self.workspace.closing or controller is None or controller.destroyed or controller.closing:
            return
        if controller.busy or controller.courses or controller.autoload_timer is not None:
            return
        if not force and (not self.workspace.auto_load_courses or not (self.profile.directory / "cookies.txt").is_file()):
            return
        controller.status.busy("正在自动加载课程…")
        controller.autoload_timer = controller.window.after(30, controller.refresh_courses)

    def show(self, page):
        if self.workspace.closing:
            return
        if page == "login":
            self.open_login()
            return
        if page == "courses" and (self.study is None or self.study.destroyed):
            if self.pending is not None and not self.pending.destroyed and self.pending.busy:
                self.workspace.note.error("此账号正在处理所选测验；课程可在它结束后重新打开。")
                return
            if self.pending is not None and not self.pending.destroyed:
                self.pending.close()
            old = self.pages.pop("courses", None)
            if old is not None:
                old.destroy()
            self.create_study()
        if page == "pending":
            self.build_pending()
        if page == "reports":
            from workspace_reports import show_reports
            show_reports(self)
        if page == "task" and (self.task_host is None or not self.task_host.winfo_exists()):
            host = self.pages.get("task") or self.host("task")
            if not host.winfo_children():
                ttk.Label(host, text="当前没有需要操作的任务。", style="Subheading.TLabel", padding=24).pack(anchor="w")
                ttk.Label(host, text="运行中的任务会在此展示，出现问题时账号列表会提示。", padding=(24, 0)).pack(anchor="w")
        self.activate(page)

    def activate(self, page):
        self.page = page
        for key, host in self.pages.items():
            host.grid_remove()
            if key == page and host.winfo_exists():
                host.grid(row=0, column=0, sticky="nsew")
        for key, button in self.buttons.items():
            button.configure(style="Primary.TButton" if key == page else "TButton")

    def new_task_host(self):
        old = self.pages.pop("task", None)
        if old is not None:
            old.destroy()
        self.task_host = self.host("task")
        return self.task_host

    def notify_task(self):
        self.buttons["task"].configure(text="任务详情 · 待处理")
        self.changed = True
        # Keep the current account and current keyboard focus stable.
        if self.workspace.current_id == self.profile.id and self.page == "task":
            self.activate("task")
        self.workspace.refresh()

    def task_finished(self):
        self.task_host = None
        self.pages.pop("task", None)
        self.buttons["task"].configure(text="任务详情")
        self.changed = False
        if self.page == "task" and not self.workspace.closing:
            self.show("courses")

    def ensure_ai(self):
        if self.workspace.fixture:
            return True
        try:
            ready = ai_configured(read_settings(self.profile))
        except QuizError:
            ready = False
        if not ready:
            self.workspace.show_settings()
            self.workspace.note.error("请先保存并启用统一 API 设置。保存后返回账号开始运行。")
        return ready

    def release_study(self, action):
        if self.study is None or self.study.destroyed:
            action()
            return
        if self.study.busy or self.study.dialog is not None:
            self.workspace.note.error("此账号正在处理任务，请先停止并等待操作结束。其他账号不受影响。")
            return
        self.after_study = action
        self.study.close()

    def study_closed(self, controller):
        if self.study is controller:
            self.study = None
        action, self.after_study = self.after_study, None
        if action is not None and not self.workspace.closing:
            self.frame.after_idle(action)

    def restart_study(self):
        if self.login is not None and not self.login.destroyed and self.login.busy:
            self.workspace.note.error("此账号正在登录，请等待当前操作结束。")
            return
        self.release_study(lambda: self.show("courses"))

    def change_mode(self, event=None):
        new_mode = next(key for key, label in MODE_LABELS.items() if label == self.mode_choice.get())
        if new_mode == self.mode.get():
            return
        if self.study is not None and (self.study.busy or self.study.dialog is not None):
            self.mode_choice.set(MODE_LABELS[self.mode.get()])
            self.workspace.note.error("请先停止此账号并等待当前任务结束，再切换运行方式。")
            return
        def switch():
            self.mode.set(new_mode)
            self.show("courses")
        self.release_study(switch)

    def open_login(self):
        if self.login is not None and not self.login.destroyed:
            self.activate("login")
            return
        if self.pending is not None and not self.pending.destroyed and self.pending.busy:
            self.workspace.note.error("此账号正在处理测验，请先停止并等待结果。")
            return
        def create():
            if self.pending is not None and not self.pending.destroyed:
                self.pending.close()
            old = self.pages.pop("login", None)
            if old is not None:
                old.destroy()
            self.login = EmbeddedLogin(self, self.host("login"))
            self.activate("login")
        self.release_study(create)

    def login_closed(self, controller, success):
        if self.login is controller:
            self.login = None
        self.pages.pop("login", None)
        if success:
            self.workspace.note.success("“" + self.profile.name + "”登录成功，正在自动加载课程。")
        if not self.workspace.closing:
            def return_to_courses():
                self.show("courses")
                if success:
                    self.schedule_course_load(force=True)
            self.frame.after_idle(return_to_courses)

    def build_pending(self):
        if "pending" in self.pages:
            self.reload_pending()
            return
        host = self.host("pending")
        frame = ttk.Frame(host, padding=18, style="Page.TFrame")
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="待处理章节", style="Subheading.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Label(frame, text="汇总本账号各运行方式的记录。课程完成情况以平台确认结果为准。",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        table = ttk.Frame(frame, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        self.pending_table = ttk.Treeview(table, columns=("course", "chapter"), show="headings", selectmode="browse")
        self.pending_table.heading("course", text="课程")
        self.pending_table.heading("chapter", text="章节")
        self.pending_table.column("course", width=250, minwidth=130)
        self.pending_table.column("chapter", width=300, minwidth=180)
        scroll = ttk.Scrollbar(table, command=self.pending_table.yview)
        scroll.pack(side="right", fill="y")
        self.pending_table.configure(yscrollcommand=scroll.set)
        self.pending_table.pack(fill="both", expand=True)
        self.pending_table.bind("<<TreeviewSelect>>", lambda event: self.pending_reason.set(
            self.pending_rows.get(next(iter(self.pending_table.selection()), ""), {}).get("reason", "")))
        self.pending_reason = tk.StringVar()
        ttk.Label(frame, textvariable=self.pending_reason, style="Muted.TLabel", wraplength=720).pack(fill="x", pady=12)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="打开选中任务", command=self.open_pending, style="Primary.TButton").pack(side="left")
        ttk.Button(buttons, text="刷新列表", command=self.reload_pending).pack(side="left", padx=8)
        self.reload_pending()

    def reload_pending(self):
        try:
            rows = list_pending(self.profile)
            self.pending_reason.set("当前没有待处理记录。" if not rows else "选择章节可查看原因，双击可进入处理页面。")
        except (OSError, ValueError):
            rows = []
            self.pending_reason.set("待处理记录无法读取，原文件已保留。")
        selected = self.pending_table.selection()
        self.pending_table.delete(*self.pending_table.get_children())
        self.pending_rows = {}
        for index, row in enumerate(rows):
            key = "pending-" + str(index)
            self.pending_rows[key] = row
            self.pending_table.insert("", "end", iid=key, values=(row.get("course_title", ""), row.get("chapter_title", "")))
        if selected and selected[0] in self.pending_rows:
            self.pending_table.selection_set(selected[0])
        self.pending_table.bind("<Double-1>", lambda event: self.open_pending())
        self.pending_table.bind("<Return>", lambda event: self.open_pending())

    def open_pending(self):
        selected = self.pending_table.selection()
        if not selected:
            self.pending_reason.set("请先选择一个待处理章节。")
            return
        if self.login is not None and not self.login.destroyed:
            self.workspace.note.error("请先完成或取消此账号的登录。")
            return
        if self.pending is not None and not self.pending.destroyed:
            self.activate("task")
            return
        if not self.ensure_ai():
            return
        task = self.pending_rows[selected[0]]
        def create():
            self.pending = EmbeddedPending(self, self.new_task_host(), task)
            self.activate("task")
        self.release_study(create)

    def pending_closed(self, controller):
        if self.pending is controller:
            self.pending = None
        self.task_host = None
        self.pages.pop("task", None)
        if not self.workspace.closing:
            self.show("pending")

    def stop(self):
        if self.study is not None and not self.study.destroyed:
            self.study.stop()
        if self.pending is not None and not self.pending.destroyed and self.pending.busy:
            self.pending.close()
        if self.login is not None and not self.login.destroyed and self.login.busy:
            self.login.close()

    def status(self):
        if self.login is not None and not self.login.destroyed:
            return "正在登录" if self.login.busy else "等待登录", self.login.busy
        if self.pending is not None and not self.pending.destroyed:
            return ("等待处理" if self.pending.review_dialog is not None else "处理中" if self.pending.busy else "待处理已结束"), self.pending.busy
        if self.study is not None and not self.study.destroyed:
            if self.study.dialog is not None:
                return "等待处理", True
            if self.study.stopping or self.study.closing:
                return "正在停止", self.study.busy
            if self.study.busy:
                return "运行中", True
            if self.study.recovered:
                return "需要重新打开", False
            if getattr(self.study, "last_status_kind", "") == "needs_attention":
                return "待处理", False
            if getattr(self.study, "last_status_kind", "") == "completed":
                return "本轮已结束", False
            if self.study.cancelled.is_set():
                return "已停止", False
            return "已就绪" if self.study.courses else "未开始", False
        return "未开始", False

    def close(self):
        self.after_study = None
        if self.study is not None and not self.study.destroyed:
            self.study.close()
        if self.pending is not None and not self.pending.destroyed:
            self.pending.close()
        if self.login is not None and not self.login.destroyed:
            self.login.close()

    def closed(self):
        return all(controller is None or controller.destroyed for controller in (self.study, self.pending, self.login))


class Workspace(GroupNavigation):
    def __init__(self, window, store, *, fixture=False, engine_factory=None, auto_load_courses=None):
        self.window, self.store, self.fixture = window, store, fixture
        self.engine_factory = engine_factory
        self.groups = AccountGroups(store.root)
        self.selected_group = UNGROUPED
        self.auto_load_courses = not fixture if auto_load_courses is None else auto_load_courses
        self.panels, self.current_id = {}, None
        self.closing, self.timer, self.settings = False, None, None
        self.destroyed = False
        self.settings_host, self.settings_profile = None, shared_profile(store.root)
        self.migration = migrate_shared_settings(store)
        self.palette = apply_theme(window)
        window.title("学习通助手 · 多账号工作台 1.7.7")
        window.geometry("1320x900")
        window.minsize(1120, 740)
        window.rowconfigure(1, weight=1)
        window.columnconfigure(0, weight=1)
        header = ttk.Frame(window, padding=(20, 14), style="Sidebar.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="学习工作台", style="SidebarTitle.TLabel").pack(side="left")
        self.summary = tk.StringVar()
        ttk.Label(header, textvariable=self.summary, style="SidebarMuted.TLabel").pack(side="left", padx=20)
        ttk.Button(header, text="统一 API 设置", command=self.show_settings).pack(side="right")
        ttk.Button(header, text="收起到任务栏", command=window.iconify).pack(side="right", padx=10)
        layout = ttk.Panedwindow(window, orient="horizontal")
        layout.grid(row=1, column=0, sticky="nsew", padx=12, pady=(12, 6))
        sidebar = ttk.Frame(layout, padding=12, width=310, style="Panel.TFrame")
        layout.add(sidebar, weight=0)
        self.content = ttk.Frame(layout, style="Page.TFrame")
        layout.add(self.content, weight=1)
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        top = ttk.Frame(sidebar, style="Card.TFrame")
        top.pack(fill="x", pady=(0, 12))
        ttk.Label(top, text="账号分类", style="Subheading.TLabel").pack(side="left")
        ttk.Button(top, text="新增账号", command=self.toggle_add).pack(side="right")
        self.search = tk.StringVar()
        ttk.Label(sidebar, text="搜索账号", style="CardMuted.TLabel").pack(anchor="w", pady=(0, 4))
        search = ttk.Entry(sidebar, textvariable=self.search)
        search.pack(fill="x", pady=(0, 8))
        self.filter = tk.StringVar(value="全部账号")
        ttk.Combobox(sidebar, textvariable=self.filter, values=("全部账号", "运行中", "需要处理"), state="readonly").pack(fill="x", pady=(0, 10))
        self.add_box = ttk.Frame(sidebar, style="Card.TFrame")
        self.new_name = tk.StringVar()
        ttk.Label(self.add_box, text="账号名称", style="Card.TLabel").pack(anchor="w")
        self.name_entry = ttk.Entry(self.add_box, textvariable=self.new_name)
        self.name_entry.pack(fill="x", pady=6)
        self.name_entry.bind("<Return>", lambda event: self.add())
        ttk.Button(self.add_box, text="创建账号", command=self.add).pack(anchor="e", pady=(0, 8))
        self.build_group_tools(sidebar)
        table = ttk.Frame(sidebar, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("status",), show="tree headings", selectmode="browse", height=12)
        self.tree.heading("#0", text="分类 / 账号")
        self.tree.heading("status", text="状态")
        self.tree.column("#0", width=185, minwidth=120, stretch=True)
        self.tree.column("status", width=108, minwidth=95, stretch=False)
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("attention", foreground=PALETTE["danger"])
        self.tree.tag_configure("active", foreground=PALETTE["primary"])
        self.tree.tag_configure("group", font=("Microsoft YaHei UI", 10, "bold"))
        self.tree.bind("<<TreeviewSelect>>", self.selected)
        self.tree.bind("<Button-3>", self.group_context)
        self.tree.bind("<<TreeviewOpen>>", self.group_toggle)
        self.tree.bind("<<TreeviewClose>>", self.group_toggle)
        ttk.Label(sidebar, text="切换账号，后台继续运行", style="CardMuted.TLabel").pack(anchor="w", pady=(12, 0))
        self.note = NoticeVar(value="登录后自动加载课程，勾选即可开始。API 设置统一位于右上角。")
        self.note_notice = Notice(window, self.note, compact=True, dismissible=True)
        self.note_notice.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        self.search.trace_add("write", lambda *_: self.render())
        self.filter.trace_add("write", lambda *_: self.render())
        window.bind("<Control-f>", lambda event: search.focus_set())
        window.bind("<Alt-Left>", lambda event: self.review_shortcut("previous"))
        window.bind("<Alt-Right>", lambda event: self.review_shortcut("next"))
        window.bind("<Control-Return>", lambda event: self.review_shortcut("submit"))
        window.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        profiles = store.list()
        if profiles:
            self.select(profiles[0].id)
        if self.migration == "choose":
            self.show_settings()
            self.note.error("检测到多套 API 配置，请在右侧选择一套并保存为所有账号的公共配置。")
        elif self.migration == "migrated":
            self.note.success("已将原有 API 配置迁移为公共配置，新增账号自动使用。")
        if self.groups.error:
            self.note.error(self.groups.error)
        self.timer = window.after(600, self.tick)

    def toggle_add(self):
        self.group_box.pack_forget()
        if self.add_box.winfo_manager():
            self.add_box.pack_forget()
        else:
            self.add_box.pack(fill="x", before=self.tree.master, pady=(0, 8))
            self.name_entry.focus_set()

    def add(self):
        if self.closing:
            return
        try:
            profile = self.store.create(self.new_name.get())
            if self.selected_group != UNGROUPED:
                self.groups.assign(profile.id, self.selected_group)
        except (ValueError, OSError) as error:
            self.note.error(str(error))
            return
        self.new_name.set("")
        self.add_box.pack_forget()
        self.search.set("")
        self.filter.set("全部账号")
        self.refresh()
        self.select(profile.id)
        self.note.success("账号已创建，自动使用统一 API 设置。点击“账号登录”完成登录。")

    def refresh(self):
        if self.closing:
            return
        self.rows = []
        for profile in self.store.list():
            panel = self.panels.get(profile.id)
            status, active = panel.status() if panel is not None else ("其他窗口运行中", True) if is_running(profile) else ("未开始", False)
            self.rows.append((profile, status, active))
        active = sum(row[2] for row in self.rows)
        attention = sum("待" in row[1] or "需要" in row[1] for row in self.rows)
        self.summary.set(f"{len(self.rows)} 个账号  ·  {active} 个运行中  ·  {attention} 个需要处理")
        self.render()

    def render(self):
        if not hasattr(self, "rows"):
            return
        query, wanted = self.search.get().strip().casefold(), self.filter.get()
        rows = [(profile, status, active) for profile, status, active in self.rows if query in profile.name.casefold()
                and (wanted == "全部账号" or wanted == "运行中" and active or wanted == "需要处理" and ("待" in status or "需要" in status))]
        visible = {profile.id for profile, _, _ in rows}
        filtering = bool(query) or wanted != "全部账号"
        groups = self.groups.groups()
        shown_groups = [group for group in groups if not filtering or any(self.groups.group_for(profile.id) == group["id"] for profile, _, _ in rows)]
        for index, group in enumerate(shown_groups):
            key, label = "group/" + group["id"], group["name"]
            count = sum(self.groups.group_for(profile.id) == group["id"] for profile, _, _ in rows)
            if not self.tree.exists(key):
                self.tree.insert("", "end", iid=key, open=group["id"] not in self.groups.data["collapsed"], tags=("group",))
            self.tree.item(key, text=f"{label}（{count}）", values=("",))
            if filtering:
                self.tree.item(key, open=True)
            self.tree.move(key, "", index)
        for key in self.account_items():
            if key not in visible:
                self.tree.delete(key)
        for profile, status, active in rows:
            parent = "group/" + self.groups.group_for(profile.id)
            tags = ("attention",) if "待" in status or "需要" in status else ("active",) if active else ()
            if self.tree.exists(profile.id):
                self.tree.item(profile.id, text=profile.name, values=(status,), tags=tags)
            else:
                self.tree.insert(parent, "end", iid=profile.id, text=profile.name, values=(status,), tags=tags)
            self.tree.move(profile.id, parent, "end")
        visible_groups = {"group/" + group["id"] for group in shown_groups}
        for key in self.tree.get_children():
            if key not in visible_groups:
                self.tree.delete(key)
        if self.current_id in visible and not self.tree.selection():
            self.tree.selection_set(self.current_id)

    def selected(self, event=None):
        selection = self.tree.selection()
        if selection and selection[0].startswith("group/"):
            self.selected_group = selection[0][6:]
            return
        if selection and (selection[0] != self.current_id or self.settings_host is not None and self.settings_host.winfo_ismapped()):
            self.select(selection[0])

    def select(self, profile_id):
        if self.closing:
            return
        if profile_id not in self.panels:
            self.panels[profile_id] = AccountPanel(self, self.store.get(profile_id))
        self.current_id = profile_id
        self.selected_group = self.groups.group_for(profile_id)
        self.hide_content()
        self.panels[profile_id].frame.grid(row=0, column=0, sticky="nsew")
        if self.tree.exists(profile_id):
            self.tree.selection_set(profile_id)
            self.tree.see(profile_id)

    def hide_content(self):
        for panel in self.panels.values():
            panel.frame.grid_remove()
        if self.settings_host is not None:
            self.settings_host.grid_remove()

    def show_settings(self):
        if self.closing:
            return
        self.hide_content()
        if self.settings_host is None:
            self.settings_host = PageHost(self.content)
            # Global API settings stay open when switching accounts, preserving edits.
            tools = ttk.Frame(self.settings_host, padding=12, style="Page.TFrame")
            tools.pack(fill="x")
            ttk.Button(tools, text="返回当前账号", command=lambda: self.select(self.current_id) if self.current_id else None).pack(side="left")
            candidates = legacy_candidates(self.store)
            if candidates:
                self.import_choice = tk.StringVar()
                choices = [profile.name for profile, _ in candidates]
                ttk.Label(tools, text="导入原有配置").pack(side="left", padx=(20, 8))
                box = ttk.Combobox(tools, textvariable=self.import_choice, values=choices, state="readonly", width=18)
                box.pack(side="left")
                def import_settings():
                    candidate = next((settings for profile, settings in candidates if profile.name == self.import_choice.get()), None)
                    if candidate is None:
                        self.note.set("请先选择原有账号的配置。")
                        return
                    self.settings.enabled.set(candidate["enabled"])
                    for key, value in self.settings.fields.items():
                        value.set(candidate[key])
                    self.settings.feedback.set("已载入原有配置，点击“保存设置”后所有账号共用。")
                ttk.Button(tools, text="载入", command=import_settings).pack(side="left", padx=8)
            body = PageHost(self.settings_host)
            body.pack(fill="both", expand=True)
            self.settings = SettingsDialog(body, self.settings_profile)
            if self.migration == "choose":
                self.settings.feedback.error("原有账号使用不同 API。请选择一套载入并保存，所有账号将统一使用。")
        self.settings_host.grid(row=0, column=0, sticky="nsew")

    def review_shortcut(self, action):
        panel = self.panels.get(self.current_id)
        if panel is None or panel.page != "task" or not panel.frame.winfo_ismapped():
            return
        dialog = panel.study.dialog if panel.study is not None else panel.pending.review_dialog if panel.pending is not None else None
        if dialog is not None and hasattr(dialog, "navigate"):
            if action == "submit":
                dialog.confirm_submit()
            else:
                dialog.navigate(-1 if action == "previous" else 1)
            return "break"

    def tick(self):
        self.timer = None
        if self.closing:
            if all(panel.closed() for panel in self.panels.values()):
                if self.settings is not None:
                    self.settings.close()
                self.destroyed = True
                self.window.destroy()
                return
        else:
            show_request = self.store.root / ".workspace-show.request"
            if show_request.exists():
                show_request.unlink(missing_ok=True)
                self.window.deiconify()
                self.window.lift()
            self.refresh()
        self.timer = self.window.after(600, self.tick)

    def close(self, *, confirm=True):
        if self.closing:
            return
        active = any(panel.status()[1] for panel in self.panels.values())
        if active and confirm and not messagebox.askyesno("停止账号并退出", "退出将安全停止工作台中的运行账号并保存进度。\n如需继续运行，请取消并使用右上角“收起到任务栏”。\n\n现在停止并退出？", parent=self.window):
            return
        self.closing = True
        self.note.set("正在等待账号安全停止并保存进度，提交中的任务会等待结果。")
        for panel in self.panels.values():
            panel.close()


def entry(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--ui-check", action="store_true")
    args = parser.parse_args(argv)
    store = AccountStore(args.root)
    store.ensure_default()
    instance_lock = None
    if not args.ui_check:
        from account_store import ProfileLock, AlreadyRunning
        instance_lock = ProfileLock(store.root)
        instance_lock.path = store.root / ".workspace.lock"
        try:
            instance_lock.acquire()
        except AlreadyRunning:
            (store.root / ".workspace-show.request").touch()
            return 0
    window = tk.Tk()
    if args.ui_check:
        window.withdraw()
    workspace = Workspace(window, store, auto_load_courses=not args.ui_check)
    if args.ui_check:
        window.geometry("1280x820+10000+10000")
        window.deiconify()
        window.update()
        assert len(workspace.account_items()) == len(store.list())
        assert not any(isinstance(widget, tk.Toplevel) for widget in window.winfo_children())
        workspace.close(confirm=False)
    try:
        window.mainloop()
    finally:
        if instance_lock is not None:
            instance_lock.release()
    if args.ui_check:
        print(json.dumps({"workspace_ui_initialized": True, "single_window": True, "network_called": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(entry())
