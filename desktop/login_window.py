"""Account-only login in its own process; credentials never enter command arguments."""
import argparse
import io
import json
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

from account_store import AccountStore, AlreadyRunning, ProfileLock
from run_account import prepare
from runtime_safety import install_runtime_safety, AccessRestricted
from ui_theme import apply_theme


class LoginBackend:
    def __init__(self, root, profile):
        self.root, self.profile = Path(root), profile
        self.main = self.coordinator = None
        self.setup_failed = False

    def authenticate(self, username, password, cancelled):
        """Login only. Preserve existing cookies on failure or cancellation."""
        if not username or len(username) > 64 or any(ord(c) < 32 for c in username):
            raise ValueError("请输入有效的学习通手机号。")
        if not password or len(password) > 256 or any(ord(c) < 32 for c in password):
            raise ValueError("请输入有效的密码。")
        if cancelled.is_set():
            return False, "已取消登录。"
        if self.setup_failed:
            return False, "登录窗口初始化未完成，请关闭后重新打开。"
        with ProfileLock(self.profile.directory):
            cookies = self.profile.directory / "cookies.txt"
            previous = cookies.read_bytes() if cookies.exists() else None
            success = False
            try:
                if self.main is None:
                    self.setup_failed = True
                    # pythonw has no console streams; upstream logging expects one.
                    if sys.stdout is None:
                        sys.stdout = io.StringIO()
                    if sys.stderr is None:
                        sys.stderr = io.StringIO()
                    main = prepare(self.root, self.profile)
                    coordinator = install_runtime_safety(main, self.profile)
                    main.logger.remove()
                    self.main, self.coordinator = main, coordinator
                    self.setup_failed = False
                self.coordinator.issue = None
                self.coordinator.state("running", "正在登录账户")
                from api import base
                session_class = base.requests.Session

                def new_session(*args, **kwargs):
                    session = session_class(*args, **kwargs)
                    request = session.request
                    def bounded_request(method, url, **options):
                        if cancelled.is_set():
                            raise InterruptedError("登录已取消")
                        options.setdefault("timeout", (5, 15))
                        return request(method, url, **options)
                    session.request = bounded_request
                    self.coordinator.guard_session(session)
                    return session

                account = self.main.Account(username, password)
                try:
                    client = self.main.Chaoxing(account, tiku=None)
                    # Fetching the display name is unnecessary for account login
                    # and would add another request after authentication.
                    client.get_name = lambda: ""
                    with patch.object(base.requests, "Session", new_session):
                        result = client.login(login_with_cookies=False)
                finally:
                    account.password = ""
                    account.username = ""
                    password = username = ""
                if cancelled.is_set():
                    self.coordinator.state("stopped", "登录已取消，原登录状态已保留")
                    return False, "已取消登录，原登录状态已保留。"
                if self.coordinator.issue is not None:
                    raise self.coordinator.issue
                success = isinstance(result, dict) and result.get("status") is True
                if success:
                    self.coordinator.state("stopped", "登录成功，请在账户首页选择运行方式")
                    return True, "登录成功。请回到账户首页选择运行方式。"
                provider_message = str(result.get("msg", "")) if isinstance(result, dict) else ""
                if any(word in provider_message for word in ("验证码", "安全验证", "人机验证")):
                    message = "平台返回了登录验证要求，尚未登录成功。请在官方页面检查账户；网页验证不一定能同步到本程序。"
                elif any(word in provider_message for word in ("频繁", "稍后", "次数")):
                    message = "平台暂时限制登录尝试，请稍后重试。"
                else:
                    message = "平台未接受本次登录，请检查手机号和密码。"
                self.coordinator.state("needs_attention", message)
                return False, message
            except AccessRestricted as error:
                if error.status == 429:
                    message = "登录请求过于频繁（HTTP 429），请稍后再试。"
                elif error.reason == "challenge":
                    message = "登录请求遇到验证页面，尚未登录成功。请先在学习通官方页面检查账户；网页验证不一定能同步到本程序。"
                elif error.reason == "login_page":
                    message = "登录接口返回了登录网页，没有返回登录结果。请检查网络或代理后重试；这不代表要求验证码。"
                elif error.status == 401:
                    message = "登录请求未获授权（HTTP 401），请检查账户登录状态后重试。"
                else:
                    message = "登录请求被拒绝（HTTP %s），未确认有验证码。请检查网络、代理或平台访问限制后重试。" % error.status
                if self.coordinator is not None:
                    self.coordinator.state("needs_attention", message)
                return False, message
            except InterruptedError:
                return False, "已取消登录，原登录状态已保留。"
            except Exception as error:
                if self.setup_failed:
                    return False, "登录窗口初始化未完成，请关闭后重新打开。"
                if self.coordinator is not None:
                    self.coordinator.state("needs_attention", "登录连接未成功，请检查网络后重试")
                return False, "登录连接未成功（" + type(error).__name__ + "），请检查网络后重试。"
            finally:
                if not success:
                    if previous is None:
                        if cookies.exists():
                            cookies.unlink()
                    elif not cookies.exists() or cookies.read_bytes() != previous:
                        temporary = cookies.with_suffix(".login-restore.tmp")
                        temporary.write_bytes(previous)
                        temporary.replace(cookies)


class LoginWindow:
    def __init__(self, window, root, profile, *, backend_factory=LoginBackend, session_id=None):
        if session_id is not None and re.fullmatch(r"[a-f0-9]{32}", session_id) is None:
            raise ValueError("登录窗口编号无效")
        self.window, self.root, self.profile = window, Path(root), profile
        self.backend_factory, self.backend = backend_factory, None
        self.events, self.cancelled = queue.Queue(), threading.Event()
        self.busy = self.closing = self.destroyed = False
        self.authenticated = False
        self.timer, self.started = None, None
        self.last_stop_check = time.monotonic()
        self.stop_request = profile.directory / f"study_stop_{session_id}.request" if session_id else None
        self.palette = apply_theme(window)
        from login_view import build_login_view
        build_login_view(self)
        window.protocol("WM_DELETE_WINDOW", self.close)
        self.phone_entry.focus_set()
        self.poll()

    def toggle_password(self):
        self.password_entry.configure(show="" if self.show_password.get() else "●")

    def update_controls(self):
        state = "disabled" if self.busy or self.closing else "normal"
        for widget in (self.login_button, self.phone_entry, self.password_entry, self.show_button):
            widget.configure(state=state)
        self.cancel_button.configure(state="disabled" if self.closing else "normal",
                                     text="取消登录" if self.busy else "取消")

    def login(self):
        if self.busy or self.closing:
            return
        username, password = self.phone.get().strip(), self.password.get()
        if not username or not password:
            self.phone_entry.configure(style="Invalid.TEntry" if not username else "TEntry")
            self.password_entry.configure(style="Invalid.TEntry" if not password else "TEntry")
            (self.phone_entry if not username else self.password_entry).focus_set()
            self.status.error("无法登录：请填写手机号和密码。")
            return
        self.password.set("")
        self.show_password.set(False)
        self.toggle_password()
        self.cancelled.clear()
        self.busy, self.started = True, time.monotonic()
        self.phone_entry.configure(style="TEntry")
        self.password_entry.configure(style="TEntry")
        self.status.busy("正在登录，请稍候…")
        self.update_controls()
        def work():
            nonlocal username, password
            try:
                if self.backend is None:
                    self.backend = self.backend_factory(self.root, self.profile)
                success, message = self.backend.authenticate(username, password, self.cancelled)
            except (AlreadyRunning, ValueError) as error:
                success, message = False, str(error)
            except Exception as error:
                success, message = False, "登录未成功（" + type(error).__name__ + "），请稍后重试。"
            finally:
                username = password = ""
            self.events.put((success, message))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def close(self):
        if self.destroyed or self.closing:
            return
        self.closing = True
        self.cancelled.set()
        self.password.set("")
        if self.busy:
            self.status.set("正在取消，等待当前登录请求结束；原登录状态会保留。")
            self.update_controls()
        else:
            self.destroy()

    def poll(self):
        self.timer = None
        if self.destroyed:
            return
        now = time.monotonic()
        if self.stop_request is not None and now - self.last_stop_check >= .5:
            self.last_stop_check = now
            try:
                if self.stop_request.is_file():
                    self.stop_request.unlink()
                    self.close()
            except OSError:
                pass
            if self.destroyed:
                return
        try:
            success, message = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            if success:
                self.status.success(message)
            else:
                self.status.error(message)
            self.authenticated = bool(success)
            if success or self.closing:
                self.destroy()
                return
            self.update_controls()
            self.password_entry.focus_set()
        self.activity.set(f"已等待 {int(now - self.started)} 秒" if self.busy and self.started is not None else "")
        self.timer = self.window.after(75, self.poll)

    def destroy(self):
        if self.destroyed:
            return
        self.destroyed = True
        self.password.set("")
        self.phone.set("")
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None
        self.window.destroy()


def entry(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--ui-check", action="store_true")
    args = parser.parse_args(argv)
    profile = AccountStore(args.root).get(args.profile)
    window = tk.Tk()
    if args.ui_check:
        window.withdraw()
    controller = LoginWindow(window, args.root.resolve(), profile, session_id=args.session_id)
    if args.ui_check:
        window.geometry("500x470+10000+10000")
        window.deiconify()
        window.update()
        for widget in (controller.phone_entry, controller.password_entry, controller.show_button,
                       controller.login_button, controller.cancel_button):
            assert widget.winfo_ismapped()
            assert widget.winfo_rooty() + widget.winfo_height() <= window.winfo_rooty() + window.winfo_height()
        controller.destroy()
        print(json.dumps({"login_ui_initialized": True, "network_called": False}))
    else:
        window.mainloop()
    return 0 if args.ui_check or controller.authenticated else 2


if __name__ == "__main__":
    raise SystemExit(entry())
