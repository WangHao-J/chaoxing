"""Password entry with visible masking, a reveal switch, and explicit confirmation."""
import tkinter as tk
from tkinter import ttk
from ui_theme import apply_theme
from ui_feedback import NoticeVar, Notice


class PasswordDialog:
    def __init__(self, window, account_name):
        self.window = window
        self.result = None
        self.password = tk.StringVar(window)
        self.reveal = tk.BooleanVar(window, value=False)
        self.message = NoticeVar(window, value="密码支持粘贴，确认前可选择显示检查。")
        apply_theme(window)
        window.title("输入密码 · " + account_name)
        frame = ttk.Frame(window, padding=24, style="Page.TFrame")
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="输入学习通密码", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(frame, text="当前账户：" + account_name, style="Muted.TLabel", wraplength=450).pack(anchor="w", pady=(6, 18))
        self.entry = ttk.Entry(frame, textvariable=self.password, show="●",
                               font=("Microsoft YaHei UI", 12), width=30)
        self.entry.pack(fill="x", ipady=5)
        self.checkbox = ttk.Checkbutton(frame, text="显示密码", variable=self.reveal, command=self.toggle_visibility)
        self.checkbox.pack(anchor="w", pady=(8, 0))
        Notice(frame, self.message, compact=True).pack(fill="x", pady=12)
        buttons = ttk.Frame(frame, style="Page.TFrame")
        buttons.pack(fill="x")
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        self.confirm_button = ttk.Button(buttons, text="确认", command=self.confirm, style="Primary.TButton")
        self.confirm_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(buttons, text="取消", command=self.cancel, style="TButton").grid(
            row=0, column=1, sticky="ew")
        window.bind("<Return>", lambda event: self.confirm())
        window.bind("<Escape>", lambda event: self.cancel())
        window.protocol("WM_DELETE_WINDOW", self.cancel)

    def toggle_visibility(self):
        self.entry.configure(show="" if self.reveal.get() else "●")

    def confirm(self):
        value = self.password.get()
        if not value:
            self.message.error("请输入密码。")
            self.entry.focus_set()
            return
        self.result = value
        self.password.set("")
        self.window.withdraw()
        self.window.quit()

    def cancel(self):
        self.result = None
        self.password.set("")
        self.window.withdraw()
        self.window.quit()


def ask_password(account_name):
    window = tk.Tk()
    try:
        dialog = PasswordDialog(window, account_name)
        window.update_idletasks()
        width = max(500, window.winfo_reqwidth())
        height = max(300, window.winfo_reqheight())
        window.minsize(width, height)
        left = max(0, (window.winfo_screenwidth() - width) // 2)
        top = max(0, (window.winfo_screenheight() - height) // 2)
        window.geometry(f"{width}x{height}+{left}+{top}")
        window.lift()
        window.attributes("-topmost", True)
        window.after(200, lambda: window.attributes("-topmost", False))
        window.after(50, dialog.entry.focus_force)
        window.mainloop()
        result = dialog.result
    finally:
        window.destroy()
    if result is None:
        raise RuntimeError("已取消密码输入")
    return result
