"""Explicit semantic messages. Failure severity is never guessed from wording."""
import tkinter as tk
from tkinter import ttk


class NoticeVar(tk.StringVar):
    def __init__(self, master=None, value="", *, tone="info"):
        self.tone = tone
        super().__init__(master, value=value)

    def set(self, value, tone="info"):
        self.tone = tone
        super().set(value)

    def error(self, value):
        self.set(value, "error")

    def success(self, value):
        self.set(value, "success")

    def busy(self, value):
        self.set(value, "busy")


class Notice(ttk.Frame):
    """Text and border accompany color; status does not steal focus on every update."""
    HEADINGS = {"error": "需要处理", "success": "已完成", "busy": "进行中", "info": "提示"}

    def __init__(self, parent, variable, *, compact=False, dismissible=False):
        super().__init__(parent, padding=(12, 8 if compact else 12), style="Info.TFrame")
        self.variable = variable
        self.dismissible, self.dismissed = dismissible, False
        self.heading_row = ttk.Frame(self, style="Info.TFrame") if dismissible else None
        if self.heading_row is not None:
            self.heading_row.pack(fill="x")
        self.title = ttk.Label(self.heading_row if dismissible else self, text="", style="InfoTitle.TLabel", takefocus=True)
        self.title.pack(anchor="w", side="left" if dismissible else "top")
        if dismissible:
            self.close_button = ttk.Button(self.heading_row, text="关闭", command=self.dismiss, style="Small.TButton")
            self.close_button.pack(side="right")
        self.message = ttk.Label(self, textvariable=variable, style="Info.TLabel", justify="left")
        self.message.pack(fill="x", pady=(3, 0))
        self.bind("<Configure>", lambda event: self.message.configure(wraplength=max(100, event.width - 28)))
        self.trace = variable.trace_add("write", self.refresh)
        self.bind("<Destroy>", self.cleanup, add=True)
        self.refresh()

    def refresh(self, *_):
        if self.dismissed:
            self.dismissed = False
            self.grid()
        previous = getattr(self, "last_tone", None)
        tone = getattr(self.variable, "tone", "info")
        tone = tone if tone in self.HEADINGS else "info"
        style = tone.capitalize()
        self.configure(style=style + ".TFrame")
        if self.heading_row is not None:
            self.heading_row.configure(style=style + ".TFrame")
        self.title.configure(text=self.HEADINGS[tone], style=style + "Title.TLabel")
        self.message.configure(style=style + ".TLabel")
        self.last_tone = tone
        if tone == "error" and previous not in (None, "error"):
            self.title.focus_set()

    def dismiss(self):
        if self.dismissible:
            self.dismissed = True
            self.grid_remove()

    def cleanup(self, event):
        if event.widget is self:
            self.variable.trace_remove("write", self.trace)
