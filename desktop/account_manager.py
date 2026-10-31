"""Account home with isolated study windows and one unified pending-task list."""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from account_store import AccountStore, is_running
from manual_tasks import COURSE_PORTAL, course_location
from task_repository import list_pending
from ui_theme import apply_theme
from ui_feedback import NoticeVar, Notice
from ui_widgets import WorkspaceViewport
from account_home_view import build_account_home
from app_modes import MODE_LABELS
from window_launch import account_python, gui_launch


def profile_status(profile, active):
    try:
        data = json.loads((profile.directory / "runtime_state.json").read_text(encoding="utf-8"))
        status = data.get("status")
        if status == "needs_attention":
            return "等待人工处理" if active else "待处理（已停止）"
        if status == "completed":
            return "本轮已结束"
        if not active and status == "stopped":
            return "已停止"
    except (OSError, ValueError):
        pass
    return "运行中" if active else "未运行"


class AccountManager:
    def __init__(self, window, store):
        self.window, self.store = window, store
        self.processes = {}
        self.quiz_processes = {}
        self.study_sessions = {}
        self.quiz_sessions = {}
        self.login_processes = {}
        self.account_rows = []
        build_account_home(self)
        self.refresh()
        self.timer = window.after(1500, self.tick)

    def update_selected_account(self):
        selection = self.tree.selection()
        text = self.tree.item(selection[0], "values")[0] if selection else "请先选择账户"
        self.selected_account.set(text)
        for button in self.account_actions:
            button.configure(state="normal" if selection else "disabled")

    def render_accounts(self):
        """Filter cached rows; typing never reads account files or starts processes."""
        selected = self.tree.selection()
        query, wanted = self.search_text.get().strip().casefold(), self.status_filter.get()
        rows = [row for row in self.account_rows if query in row[1].casefold()
                and (wanted == "全部账户" or (wanted == "运行中" and row[3])
                     or (wanted == "待处理" and "待" in row[2]))]
        ids = {row[0] for row in rows}
        for item in self.tree.get_children():
            if item not in ids:
                self.tree.delete(item)
        for profile_id, name, status, active in rows:
            tags = ("attention",) if "待" in status else ("active",) if active else ()
            if self.tree.exists(profile_id):
                self.tree.item(profile_id, values=(name, status), tags=tags)
            else:
                self.tree.insert("", "end", iid=profile_id, values=(name, status), tags=tags)
        if selected and selected[0] in ids:
            self.tree.selection_set(selected[0])
        elif rows:
            self.tree.selection_set(rows[0][0])
        if rows:
            self.empty_label.place_forget()
        else:
            self.empty_label.configure(text="没有匹配的账户，请清除搜索或更换状态。" if self.account_rows
                                       else "还没有账户，点击右上方“新增账户”开始。")
            self.empty_label.place(relx=.5, rely=.55, anchor="center")
        self.update_selected_account()

    def selected(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("选择账户", "请先选择一个账户。", parent=self.window)
            return None
        return self.store.get(selected[0])

    def refresh(self):
        profiles = self.store.list()
        self.account_rows = []
        active_count = 0
        for profile in profiles:
            process = self.processes.get(profile.id)
            if process is not None and process.poll() is not None:
                self.processes.pop(profile.id)
                self.study_sessions.pop(profile.id, None)
                login = self.login_processes.pop(profile.id, None)
                if login is not None and process.returncode == 0:
                    self.note.set(f"“{profile.name}”登录成功。现在可以选择下方一种运行方式。")
            active = is_running(profile)
            process = self.processes.get(profile.id)
            if process is not None and process.poll() is None:
                active = True
            status = profile_status(profile, active)
            if profile.id in self.login_processes:
                status = "登录窗口已打开"
            active_count += active
            self.account_rows.append((profile.id, profile.name, status, active))
        self.account_count.set(f"我的账户 · {len(profiles)} 个 / {active_count} 个窗口运行中")
        self.render_accounts()

    def tick(self):
        self.refresh()
        self.timer = self.window.after(1500, self.tick)

    def add(self):
        name = simpledialog.askstring("新增账户", "为这个账户填写一个名称，例如：我的账户、账户二。", parent=self.window)
        if name is None:
            return
        try:
            profile = self.store.create(name)
        except (ValueError, OSError) as error:
            messagebox.showerror("创建失败", str(error), parent=self.window)
            return
        self.search_text.set("")
        self.status_filter.set("全部账户")
        self.refresh()
        self.tree.selection_set(profile.id)
        self.tree.see(profile.id)
        self.note.set("账户已创建。请点击“登录账户”，登录成功后选择运行方式。")

    def start_selected(self, fresh=False, choose_courses=False):
        # Retain the callable for existing integrations while removing the old console flow from the home screen.
        return self.open_login() if fresh else self.open_study("mixed")

    def open_login(self):
        profile = self.selected()
        if profile is None:
            return
        process = self.processes.get(profile.id)
        if is_running(profile) or (process is not None and process.poll() is None):
            messagebox.showinfo("已经在运行", "这个账户已在运行，请使用它原来的运行窗口。", parent=self.window)
            return
        helper = self.quiz_processes.get(profile.id)
        if helper is not None and helper.poll() is None:
            messagebox.showinfo("已经在运行", "请先正常关闭当前答题窗口，再登录账户。", parent=self.window)
            return
        session_id = uuid.uuid4().hex
        launch = gui_launch(self.store.root, profile, "login_window.py", session_id,
                            python=account_python(self.store.root), arguments=())
        try:
            process = subprocess.Popen(launch.argv, cwd=launch.cwd, env=launch.env,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            messagebox.showerror("启动失败", "登录窗口未能启动，请重新打开管理界面后重试。", parent=self.window)
            return
        self.processes[profile.id] = process
        self.study_sessions[profile.id] = session_id
        self.login_processes[profile.id] = process
        self.note.set(f"“{profile.name}”的登录窗口已打开，登录完成后返回此页选择运行方式。")
        self.refresh()
        return process

    def open_study(self, mode):
        if mode not in MODE_LABELS:
            return
        profile = self.selected()
        if profile is None:
            return
        process = self.processes.get(profile.id)
        helper = self.quiz_processes.get(profile.id)
        if is_running(profile) or any(value is not None and value.poll() is None for value in (process, helper)):
            messagebox.showinfo("账户窗口已打开", "请先正常停止并关闭此账户当前的窗口，再切换运行方式。", parent=self.window)
            return
        session_id = uuid.uuid4().hex
        launch = gui_launch(self.store.root, profile, "study_window.py", session_id,
                            python=account_python(self.store.root), arguments=("--mode", mode))
        try:
            process = subprocess.Popen(launch.argv, cwd=launch.cwd, env=launch.env,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            messagebox.showerror("启动失败", "运行窗口未能启动，请重新打开管理界面后重试。", parent=self.window)
            return
        self.processes[profile.id] = process
        self.study_sessions[profile.id] = session_id
        self.note.set(f"“{profile.name}”的{MODE_LABELS[mode]}窗口已打开。请选择课程后开始。")
        self.refresh()
        return process

    def stop(self):
        profile = self.selected()
        if profile is None:
            return
        session_id = self.study_sessions.get(profile.id)
        process = self.processes.get(profile.id)
        if session_id and process is not None and process.poll() is None:
            (profile.directory / f"study_stop_{session_id}.request").touch()
            self.note.set(f"已请求“{profile.name}”安全停止，请等待当前操作结束；切换模式前关闭其运行窗口。")
            return
        process = self.processes.get(profile.id)
        if process is None or process.poll() is not None:
            process = self.quiz_processes.get(profile.id)
        if process is None or process.poll() is not None:
            text = "这个账户没有在运行。"
            if is_running(profile):
                text = "请在这个账户原来的运行窗口按 Ctrl+C 停止。"
            messagebox.showinfo("停止运行", text, parent=self.window)
            return
        quiz_session = getattr(self, "quiz_sessions", {}).get(profile.id)
        if quiz_session:
            (profile.directory / f"study_stop_{quiz_session}.request").touch()
            self.note.set(f"已请求“{profile.name}”安全停止答题，请等待结果返回。")
        else:
            messagebox.showinfo("停止运行", "请回到该账户的运行窗口正常停止；正在交卷时请等待结果。", parent=self.window)

    def open_config(self):
        profile = self.selected()
        if profile is not None:
            subprocess.Popen(["notepad.exe", str(profile.directory / "config.ini")])

    def open_directory(self):
        profile = self.selected()
        if profile is not None:
            os.startfile(profile.directory)

    def open_ai_settings(self):
        profile = self.selected()
        if profile is not None:
            from quiz_dialog import open_settings
            open_settings(self.window, profile)

    def open_pending_quiz(self, profile, task, *, parent):
        feedback = getattr(parent, "pending_feedback", self.note)
        previous = self.quiz_processes.get(profile.id)
        if previous is not None and previous.poll() is None:
            feedback.set("该账户的答题窗口已打开，请使用原窗口；关闭后可打开另一项。", tone="error")
            return None
        active = self.processes.get(profile.id)
        if is_running(profile) or (active is not None and active.poll() is None):
            feedback.set("该账户正在运行。请先停止该账户，再打开待处理答题。", tone="error")
            return None
        from quiz_dialog import ensure_ai_config
        if not ensure_ai_config(parent, profile):
            feedback.set("请先配置并启用该账户的 AI 答题，再打开任务。", tone="error")
            return None
        session_id = uuid.uuid4().hex
        launch = gui_launch(self.store.root, profile, "pending_quiz_worker.py", session_id,
                            python=account_python(self.store.root), arguments=("--course", task["course_id"], "--class-id", task["class_id"], "--chapter", task["chapter_id"]))
        try:
            process = subprocess.Popen(launch.argv, cwd=launch.cwd, env=launch.env,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            feedback.set("答题窗口未能启动，请重新打开管理界面后重试。", tone="error")
            return None
        self.quiz_processes[profile.id] = process
        if not hasattr(self, "quiz_sessions"):
            self.quiz_sessions = {}
        self.quiz_sessions[profile.id] = session_id
        feedback.set("正在打开所选章节的答题页…")

        def pending_stamp():
            values = []
            for name in ("pending_tasks.json", "study_state_quizzes.json", "study_state_videos.json", "study_state_mixed.json"):
                try:
                    values.append((name, (profile.directory / name).stat().st_mtime_ns))
                except OSError:
                    values.append((name, None))
            return tuple(values)

        stamp = pending_stamp()

        def watch():
            nonlocal stamp
            if not parent.winfo_exists():
                return
            current = pending_stamp()
            ended = process.poll() is not None
            if current != stamp or ended:
                stamp = current
                parent.pending_reload()
            if ended:
                self.quiz_processes.pop(profile.id, None)
                self.quiz_sessions.pop(profile.id, None)
                return
            parent.after(1000, watch)

        parent.after(1000, watch)
        return process

    def open_pending(self):
        profile = self.selected()
        if profile is None:
            return
        read_error = [""]
        def read_tasks():
            try:
                rows = list_pending(profile)
                read_error[0] = ""
                return rows
            except (OSError, ValueError) as error:
                read_error[0] = str(error)
                return []

        tasks = read_tasks()
        if not tasks:
            if read_error[0]:
                messagebox.showerror("待处理记录无法读取", read_error[0], parent=self.window)
            else:
                messagebox.showinfo("待处理任务", "目前没有待处理记录。课程最终状态以学习通为准。", parent=self.window)
            return
        window = tk.Toplevel(self.window)
        palette = apply_theme(window)
        window.title("待处理任务 · " + profile.name)
        window.geometry("880x650")
        window.minsize(800, 600)
        viewport = WorkspaceViewport(window, base_height=600, padding=20)
        viewport.pack(fill="both", expand=True)
        frame = viewport.content
        ttk.Label(frame, text="待处理任务", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(frame, text="已汇总三个运行模式及旧版记录。单击任务可进入程序内答题页。",
                  style="Muted.TLabel", wraplength=700).pack(anchor="w", pady=(6, 12))
        listing = tk.Listbox(frame, font=("Microsoft YaHei UI", 11), exportselection=False,
                             bg=palette["surface"], fg=palette["text"], selectbackground=palette["selection"],
                             selectforeground=palette["on_selection"],
                             relief="flat", highlightthickness=1, highlightbackground=palette["border"])
        listing.pack(fill="both", expand=True)
        location = tk.StringVar()
        ttk.Label(frame, textvariable=location, style="Muted.TLabel", wraplength=700).pack(anchor="w", pady=(10, 0))
        feedback = NoticeVar(value="点击测验可进入程序内答题；外部任务可从课程入口处理。")
        Notice(frame, feedback, compact=True).pack(fill="x", pady=(12, 0))
        window.pending_listing = listing
        window.pending_feedback = feedback
        window.pending_tasks = tasks

        def select_location(event=None):
            selected = listing.curselection()
            if not selected:
                return ""
            data = tasks[selected[0]]
            text = course_location(data.get("course_title"), data.get("chapter_title"))
            if data.get("reason"):
                text += "\n原因：" + str(data["reason"])
            location.set(text)
            return text
        def copy_selected():
            text = select_location()
            if text:
                window.clipboard_clear()
                window.clipboard_append(text)

        def task_key(task):
            return tuple(str(task.get(name, "")) for name in ("course_id", "class_id", "chapter_id"))

        def reload_pending():
            selected = listing.curselection()
            index = selected[0] if selected else 0
            key = task_key(tasks[index]) if tasks and index < len(tasks) else None
            tasks[:] = read_tasks()
            listing.delete(0, "end")
            for task in tasks:
                names = task.get("manual_tasks", [])
                titles = str(task.get("reason") or "、".join(str(name) for name in names)) if isinstance(names, list) else str(task.get("reason") or "")
                listing.insert("end", f"{task.get('course_title', '')} / {task.get('chapter_title', '')}"
                               + ("：" + titles if titles else ""))
            if tasks:
                index = next((i for i, task in enumerate(tasks) if task_key(task) == key),
                             min(index, len(tasks) - 1))
                listing.selection_set(index)
                listing.activate(index)
                select_location()
            else:
                location.set("")
                feedback.set(read_error[0] or "目前没有待处理章节，课程最终状态以学习通为准。", tone="error" if read_error[0] else "info")

        last_opened = {"index": None, "time": 0.0, "event_time": None}

        def open_selected(event=None):
            selected = listing.curselection()
            if not selected or selected[0] >= len(tasks):
                return "break"
            index = selected[0]
            now = time.monotonic()
            event_time = getattr(event, "time", None) or None
            previous_event = last_opened["event_time"]
            same_click = (event_time is not None and previous_event is not None
                          and 0 <= event_time - previous_event <= 500)
            if last_opened["index"] == index and (now - last_opened["time"] <= 0.5 or same_click):
                return "break"
            last_opened.update(index=index, time=now, event_time=event_time)
            task = tasks[index]
            if not all(str(task.get(name, "")).strip() and task.get(name) is not None
                       for name in ("course_id", "class_id", "chapter_id")):
                feedback.set("这条旧记录缺少任务编号。请先启动该账户刷新任务，再从待处理列表打开答题。", tone="error")
                return "break"
            self.open_pending_quiz(profile, task, parent=window)
            return "break"

        def click_task(event):
            if not tasks or not 0 <= event.x < listing.winfo_width():
                return "break"
            index = listing.nearest(event.y)
            bounds = listing.bbox(index)
            if bounds is None or not bounds[1] <= event.y < bounds[1] + bounds[3]:
                return "break"
            listing.selection_clear(0, "end")
            listing.selection_set(index)
            listing.activate(index)
            select_location()
            return open_selected(event)

        listing.bind("<<ListboxSelect>>", select_location)
        listing.bind("<Button-1>", click_task)
        listing.bind("<Double-Button-1>", click_task)
        listing.bind("<Return>", open_selected)
        window.pending_reload = reload_pending
        window.pending_open_selected = open_selected
        window.pending_click = click_task
        reload_pending()
        actions = ttk.Frame(frame, style="Page.TFrame")
        actions.pack(fill="x", pady=(12, 0))
        window.pending_open_button = ttk.Button(actions, text="打开选中答题", command=open_selected, style="Primary.TButton")
        window.pending_open_button.pack(fill="x", pady=(0, 8))
        legacy_actions = ttk.Frame(frame, style="Page.TFrame")
        legacy_actions.pack(fill="x")
        ttk.Button(legacy_actions, text="打开课程入口", command=lambda: webbrowser.open(COURSE_PORTAL)).pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(legacy_actions, text="复制选中课程 / 章节名称", command=copy_selected).pack(
            side="left", fill="x", expand=True)


def entry(argv=None):
    # Keep diagnostic CLI behavior; every normal shortcut opens the unified UI.
    arguments = sys.argv[1:] if argv is None else argv
    if "--check" not in arguments:
        from workspace import entry as workspace_entry
        return workspace_entry(arguments)
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--ui-check", action="store_true")
    args = parser.parse_args(argv)
    store = AccountStore(args.root)
    store.ensure_default()
    if args.check:
        for profile in store.list():
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("run_account.py")),
                                     "--root", str(store.root), "--profile", profile.id, "--check"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=30,
                                    env={**os.environ, "PYTHONUTF8": "1"})
            if result.returncode != 0:
                print(result.stdout + result.stderr)
                return result.returncode
            print(result.stdout.strip())
        return 0
    window = tk.Tk()
    if args.ui_check:
        window.withdraw()
    manager = AccountManager(window, store)
    if args.ui_check:
        window.geometry("800x600+10000+10000")
        window.deiconify()
        window.update()
        assert len(manager.tree.get_children()) == len(store.list())
        for button in manager.action_buttons:
            assert button.winfo_ismapped(), button.cget("text")
            assert 0 <= button.winfo_rootx() - window.winfo_rootx()
            assert button.winfo_rootx() + button.winfo_width() <= window.winfo_rootx() + window.winfo_width()
            assert button.winfo_rooty() + button.winfo_height() <= window.winfo_rooty() + window.winfo_height()
        window.after_cancel(manager.timer)
        window.destroy()
        print(json.dumps({"ui_initialized": True, "accounts": len(store.list())}))
    else:
        window.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(entry())
    except Exception as error:
        if sys.stderr is not None:
            print(f"账户管理启动失败：{error}", file=sys.stderr)
        if not any(flag in sys.argv for flag in ("--check", "--ui-check")):
            try:
                messagebox.showerror("启动失败", f"账户管理启动失败：{error}")
            except Exception:
                pass
        raise SystemExit(1)
