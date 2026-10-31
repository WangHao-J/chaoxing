"""Shared, restrained desktop styling without external UI dependencies."""
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont


PALETTE = {
    "bg": "#f3f5f7", "surface": "#ffffff", "text": "#182230",
    "muted": "#475467", "border": "#c5cdd6", "primary": "#0f766e",
    "primary_hover": "#115e59", "danger": "#b42318", "success": "#166534",
    "accent_surface": "#eaf6f3", "selection": "#d7ede8", "on_selection": "#114b46",
    "hover": "#edf1f4", "disabled": "#e9edf1", "disabled_text": "#667085",
    "heading_surface": "#f8fafb", "track": "#e4e9ef", "focus": "#0f766e",
    "sidebar": "#172b2c", "sidebar_text": "#ffffff", "sidebar_muted": "#c6d6d3",
    "error_surface": "#fff1f0", "error_border": "#e7a59f",
    "success_surface": "#edf7ed", "info_surface": "#edf2f7",
}

SPACING = (4, 8, 12, 16, 20, 24)
FONT_FAMILY = "Microsoft YaHei UI"


def apply_theme(window):
    """Use native Tk widgets with one readable, consistent blue/white palette."""
    palette = dict(PALETTE)
    style = ttk.Style(window)
    if style.theme_use() != "clam":
        style.theme_use("clam")
    font = (FONT_FAMILY, 11)
    line_height = tkfont.Font(window, font=font).metrics("linespace")
    window.configure(background=palette["bg"])
    # A global *Font resource overrides ttk's named heading styles. Keep
    # defaults on native widgets only so titles retain their intended size.
    window.option_add("*Listbox.font", font)
    window.option_add("*Menu.font", font)
    window.option_add("*Text.highlightBackground", palette["border"])
    window.option_add("*Text.highlightColor", palette["primary"])
    window.option_add("*Listbox.background", palette["surface"])
    window.option_add("*Listbox.foreground", palette["text"])
    window.option_add("*Listbox.highlightColor", palette["focus"])
    window.option_add("*Listbox.selectBackground", palette["selection"])
    window.option_add("*Listbox.selectForeground", palette["on_selection"])
    window.option_add("*Text.foreground", palette["text"])
    style.configure(".", font=font, background=palette["bg"], foreground=palette["text"])
    style.configure("Page.TFrame", background=palette["bg"])
    style.configure("Card.TFrame", background=palette["surface"])
    style.configure("AccentCard.TFrame", background=palette["accent_surface"])
    style.configure("Sidebar.TFrame", background=palette["sidebar"])
    style.configure("Sidebar.TLabel", background=palette["sidebar"], foreground=palette["sidebar_text"])
    style.configure("SidebarTitle.TLabel", background=palette["sidebar"], foreground=palette["sidebar_text"], font=(font[0], 18, "bold"))
    style.configure("SidebarMuted.TLabel", background=palette["sidebar"], foreground=palette["sidebar_muted"])
    style.configure("Panel.TFrame", background=palette["surface"], bordercolor=palette["border"], borderwidth=1, relief="solid")
    style.configure("TLabel", background=palette["bg"], foreground=palette["text"])
    style.configure("Heading.TLabel", font=(font[0], 19, "bold"))
    style.configure("Subheading.TLabel", font=(font[0], 11, "bold"), background=palette["surface"])
    style.configure("CardTitle.TLabel", font=(font[0], 15, "bold"), background=palette["surface"])
    style.configure("Section.TLabel", font=(font[0], 11, "bold"), background=palette["bg"])
    style.configure("Muted.TLabel", foreground=palette["muted"])
    style.configure("Card.TLabel", background=palette["surface"])
    style.configure("CardMuted.TLabel", background=palette["surface"], foreground=palette["muted"])
    style.configure("Accent.TLabel", background=palette["accent_surface"])
    style.configure("AccentMuted.TLabel", background=palette["accent_surface"], foreground=palette["muted"])
    style.configure("TButton", padding=(14, 9), background=palette["surface"],
                    bordercolor=palette["border"], borderwidth=1, relief="solid", focusthickness=2,
                    focuscolor=palette["focus"])
    style.map("TButton", background=[("disabled", palette["disabled"]), ("pressed", palette["selection"]),
                                      ("active", palette["hover"])],
              bordercolor=[("focus", palette["focus"])],
              foreground=[("disabled", palette["disabled_text"])])
    style.configure("Primary.TButton", background=palette["primary"], foreground="white",
                    font=(font[0], 11, "bold"), bordercolor=palette["primary"], focuscolor="white")
    style.map("Primary.TButton", background=[("disabled", palette["disabled"]), ("active", palette["primary_hover"])],
              foreground=[("disabled", palette["disabled_text"]), ("!disabled", "white")])
    style.configure("Danger.TButton", foreground=palette["danger"])
    style.map("Danger.TButton", foreground=[("disabled", palette["disabled_text"]), ("!disabled", palette["danger"])])
    style.configure("Quiet.TButton", background=palette["bg"], bordercolor=palette["bg"], relief="flat", padding=(9, 7))
    style.configure("Small.TButton", padding=(9, 5))
    style.configure("Sidebar.TButton", background=palette["sidebar"], foreground=palette["sidebar_text"],
                    bordercolor=palette["sidebar_muted"], focuscolor="white", padding=(10, 9))
    style.map("Sidebar.TButton", background=[("disabled", palette["sidebar"]), ("active", "#244647")],
              foreground=[("disabled", "#9db3af"), ("!disabled", palette["sidebar_text"])],
              bordercolor=[("focus", "white"), ("!focus", palette["sidebar_muted"])])
    style.configure("TEntry", fieldbackground=palette["surface"], bordercolor=palette["border"], padding=7)
    style.configure("TCombobox", fieldbackground=palette["surface"], padding=5)
    for control in ("TEntry", "TCombobox", "Treeview"):
        style.map(control, bordercolor=[("focus", palette["focus"])],
                  lightcolor=[("focus", palette["focus"])], darkcolor=[("focus", palette["focus"])])
    style.map("TEntry", fieldbackground=[("readonly", palette["surface"])])
    style.configure("Treeview", background=palette["surface"], fieldbackground=palette["surface"],
                    foreground=palette["text"], borderwidth=1, bordercolor=palette["border"], rowheight=max(40, line_height + 14))
    style.configure("Treeview.Heading", background=palette["heading_surface"], foreground=palette["muted"],
                    font=(font[0], 10, "bold"), padding=(8, 8), relief="flat")
    style.map("Treeview", background=[("selected", palette["selection"])], foreground=[("selected", palette["on_selection"])])
    style.configure("Horizontal.TProgressbar", background=palette["primary"], troughcolor=palette["track"],
                    lightcolor=palette["primary"], darkcolor=palette["primary"], borderwidth=0, thickness=8)
    style.configure("Vertical.TScrollbar", background=palette["border"], troughcolor=palette["bg"],
                    borderwidth=0, arrowsize=12, relief="flat", arrowcolor=palette["muted"])
    style.configure("TCheckbutton", padding=(0, 4))
    style.configure("Card.TCheckbutton", background=palette["surface"])
    style.configure("TCheckbutton", focuscolor=palette["focus"], focusthickness=2)
    style.configure("TNotebook", background=palette["surface"], borderwidth=0)
    style.configure("TNotebook.Tab", padding=(12, 7), background=palette["heading_surface"],
                    foreground=palette["muted"], focuscolor=palette["focus"])
    style.map("TNotebook.Tab", background=[("selected", palette["surface"])],
              foreground=[("selected", palette["text"])])
    for name, background, foreground, border in (
            ("Error", palette["error_surface"], palette["danger"], palette["error_border"]),
            ("Success", palette["success_surface"], palette["success"], palette["success"]),
            ("Info", palette["info_surface"], palette["muted"], palette["border"]),
            ("Busy", palette["accent_surface"], palette["primary_hover"], palette["primary"])):
        style.configure(name + ".TFrame", background=background, bordercolor=border, borderwidth=1, relief="solid")
        style.configure(name + ".TLabel", background=background, foreground=foreground)
        style.configure(name + "Title.TLabel", background=background, foreground=foreground, font=(font[0], 11, "bold"))
    style.configure("ErrorText.TLabel", foreground=palette["danger"], background=palette["surface"])
    style.configure("Invalid.TEntry", bordercolor=palette["danger"], lightcolor=palette["danger"], darkcolor=palette["danger"])
    return palette
