"""Course checkmarks backed by the existing exact-ID Treeview selection."""
from tkinter import ttk


class CourseChecklist(ttk.Treeview):
    def __init__(self, parent, *, can_toggle=lambda: True, **options):
        super().__init__(parent, columns=("check", "title", "class"), show="headings",
                         selectmode="extended", **options)
        self.can_toggle = can_toggle
        self.heading("check", text="勾选")
        self.column("check", width=64, minwidth=64, stretch=False, anchor="center")
        self.bind("<Button-1>", self.click)
        self.bind("<Double-Button-1>", lambda event: "break")
        self.bind("<space>", self.space)
        for key, movement in (("Up", -1), ("Down", 1), ("Home", "first"), ("End", "last")):
            self.bind("<" + key + ">", lambda event, step=movement: self.move_focus(step))
        self.bind("<Control-a>", lambda event: self.select_all_key())
        self.bind("<<TreeviewSelect>>", self.sync_checks, add=True)

    def insert(self, parent, index, iid=None, **options):
        values = options.get("values", ())
        if len(values) == 2:
            options["values"] = ("[  ]", *values)
        return super().insert(parent, index, iid=iid, **options)

    def sync_checks(self, *_):
        selected = set(self.selection())
        for iid in self.get_children():
            marker = "[√]" if iid in selected else "[  ]"
            if self.set(iid, "check") != marker:
                self.set(iid, "check", marker)

    def toggle(self, iid):
        if not iid or not self.exists(iid) or not self.can_toggle():
            return
        if iid in self.selection():
            self.selection_remove(iid)
        else:
            self.selection_add(iid)
        self.focus(iid)
        self.see(iid)
        self.sync_checks()

    def click(self, event):
        if self.identify_region(event.x, event.y) in ("cell", "tree"):
            self.focus_set()
            self.toggle(self.identify_row(event.y))
            return "break"

    def space(self, event=None):
        self.toggle(self.focus())
        return "break"

    def check_all(self):
        if self.can_toggle():
            self.selection_set(self.get_children())
            self.sync_checks()

    def select_all_key(self):
        self.check_all()
        return "break"

    def move_focus(self, movement):
        rows = self.get_children()
        if not rows:
            return "break"
        current = rows.index(self.focus()) if self.focus() in rows else -1
        target = 0 if movement == "first" else len(rows) - 1 if movement == "last" else max(0, min(len(rows) - 1, current + movement))
        self.focus(rows[target])
        self.see(rows[target])
        return "break"

    def uncheck_all(self):
        if self.can_toggle():
            self.selection_remove(self.selection())
            self.sync_checks()
