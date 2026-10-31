"""Small native-widget behaviors shared by the desktop views."""
import math
import tkinter as tk
from tkinter import ttk
from ui_theme import PALETTE


def wrap_to_width(label, container, margin=0, minimum=120):
    """Keep help/status copy readable when a native window is resized."""
    container.bind("<Configure>", lambda event: label.configure(
        wraplength=max(minimum, event.width - margin)), add=True)


def labelled_value(parent, variable):
    """A full, keyboard-selectable value instead of silently truncated text."""
    entry = ttk.Entry(parent, textvariable=variable, state="readonly", takefocus=True)
    entry.pack(side="left", fill="x", expand=True)
    return entry


class WorkspaceViewport(ttk.Frame):
    """Keep large-text layouts usable by scrolling the page when it cannot fit."""
    def __init__(self, parent, *, base_height, padding=24):
        super().__init__(parent, style="Page.TFrame")
        self.window, self.base_height = parent.winfo_toplevel(), base_height
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=PALETTE["bg"])
        scrollbar = ttk.Scrollbar(self, command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(fill="both", expand=True)
        self.content = ttk.Frame(self.canvas, style="Page.TFrame", padding=padding)
        self.item = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.bind("<Configure>", self.resize)
        self.focus_binding = self.window.bind("<FocusIn>", self.focus_changed, add=True)
        self.wheel_binding = self.window.bind("<MouseWheel>", self.mouse_wheel, add=True)
        self.bind("<Destroy>", self.cleanup, add=True)

    def resize(self, event):
        scale = max(1, float(self.window.tk.call("tk", "scaling")) / (4 / 3))
        height = max(event.height, math.ceil(self.base_height * scale))
        self.canvas.itemconfigure(self.item, width=event.width, height=height)
        self.canvas.configure(scrollregion=(0, 0, event.width, height))

    def contains(self, widget):
        return str(widget).startswith(str(self.content) + ".")

    def focus_changed(self, event):
        widget = event.widget
        if not self.contains(widget) or not widget.winfo_ismapped():
            return
        top, bottom = self.canvas.winfo_rooty(), self.canvas.winfo_rooty() + self.canvas.winfo_height()
        target_top, target_bottom = widget.winfo_rooty(), widget.winfo_rooty() + widget.winfo_height()
        offset = target_top - top - 8 if target_top < top else target_bottom - bottom + 8 if target_bottom > bottom else 0
        if offset:
            region = self.canvas.bbox(self.item)
            height = max(1, region[3] - region[1])
            self.canvas.yview_moveto(max(0, self.canvas.yview()[0] + offset / height))

    def mouse_wheel(self, event):
        if self.contains(event.widget) and event.widget.winfo_class() not in (
                "Text", "Listbox", "Treeview", "Canvas", "TScrollbar", "TCombobox"):
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def cleanup(self, event):
        if event.widget is self:
            self.window.unbind("<FocusIn>", self.focus_binding)
            self.window.unbind("<MouseWheel>", self.wheel_binding)
