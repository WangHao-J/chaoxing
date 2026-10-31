"""Folder-like account navigation and inline category management."""
import tkinter as tk
from tkinter import ttk, messagebox
from account_groups import UNGROUPED


class GroupNavigation:
    def build_group_tools(self, sidebar):
        tools = ttk.Frame(sidebar, style="Card.TFrame")
        tools.pack(fill="x", pady=(0, 10))
        ttk.Button(tools, text="新建分类", command=self.edit_group).pack(side="left")
        menu_button = ttk.Menubutton(tools, text="整理账号")
        menu_button.pack(side="right")
        self.organization_menu = tk.Menu(menu_button, tearoff=False, postcommand=self.update_group_menu)
        menu_button.configure(menu=self.organization_menu)
        self.group_box = ttk.Frame(sidebar, style="Card.TFrame")
        self.group_editing = None
        self.group_name = tk.StringVar()
        self.group_form_label = ttk.Label(self.group_box, text="分类名称", style="Card.TLabel")
        self.group_form_label.pack(anchor="w")
        self.group_name_entry = ttk.Entry(self.group_box, textvariable=self.group_name)
        self.group_name_entry.pack(fill="x", pady=6)
        self.group_name_entry.bind("<Return>", lambda event: self.save_group())
        actions = ttk.Frame(self.group_box, style="Card.TFrame")
        actions.pack(fill="x", pady=(0, 8))
        ttk.Button(actions, text="取消", command=self.group_box.pack_forget).pack(side="left")
        ttk.Button(actions, text="保存分类", command=self.save_group).pack(side="right")

    def edit_group(self, key=None):
        if self.closing:
            return
        self.add_box.pack_forget()
        self.group_editing = key
        name = next((group["name"] for group in self.groups.groups() if group["id"] == key), "")
        self.group_name.set(name)
        self.group_form_label.configure(text="重命名分类" if key else "新建分类")
        self.group_box.pack(fill="x", before=self.tree.master, pady=(0, 8))
        self.group_name_entry.focus_set()

    def save_group(self):
        try:
            if self.group_editing:
                self.groups.rename(self.group_editing, self.group_name.get())
                key = self.group_editing
            else:
                key = self.groups.create(self.group_name.get())
        except (OSError, ValueError) as error:
            self.note.error(str(error))
            return
        self.selected_group = key
        self.group_box.pack_forget()
        self.render()
        node = "group/" + key
        if self.tree.exists(node):
            self.tree.selection_set(node)
            self.tree.see(node)
        self.note.success("分类已保存，可选中账号后使用“整理账号”移入分类，也可以右键操作。")

    def update_group_menu(self):
        menu = self.organization_menu
        menu.delete(0, "end")
        menu.add_command(label="新建分类", command=self.edit_group)
        key = self.selected_group
        editable = key != UNGROUPED
        menu.add_command(label="重命名当前分类", command=lambda: self.edit_group(key), state="normal" if editable else "disabled")
        menu.add_command(label="删除当前分类…", command=lambda: self.delete_group(key), state="normal" if editable else "disabled")
        menu.add_separator()
        selected = next(iter(self.tree.selection()), "")
        move = tk.Menu(menu, tearoff=False)
        for group in self.groups.groups():
            move.add_command(label=group["name"], command=lambda target=group["id"], account=selected: self.move_account(account, target))
        menu.add_cascade(label="将选中账号移入…", menu=move, state="normal" if selected and not selected.startswith("group/") else "disabled")

    def delete_group(self, key):
        if key == UNGROUPED:
            return
        name = next((group["name"] for group in self.groups.groups() if group["id"] == key), "")
        if not messagebox.askyesno("删除分类", f"删除分类“{name}”？\n其中的账号将移入“未分类”，账号资料和任务会保留。", parent=self.window):
            return
        try:
            self.groups.remove(key)
        except (OSError, ValueError) as error:
            self.note.error(str(error))
            return
        self.selected_group = UNGROUPED
        self.render()
        self.note.success("分类已删除，原账号已移入“未分类”。")

    def move_account(self, profile_id, key):
        if self.closing:
            return
        try:
            self.store.get(profile_id)
            self.groups.assign(profile_id, key)
        except (OSError, ValueError, KeyError):
            self.note.error("账号分类未能保存，请确认选中了账号且分类记录可写。")
            return
        self.selected_group = key
        self.render()
        node = "group/" + key
        if self.tree.exists(node):
            self.tree.item(node, open=True)
            self.tree.selection_set(profile_id)
            self.tree.see(profile_id)
        self.note.success("账号分类已保存，后台任务继续运行。")

    def group_context(self, event):
        node = self.tree.identify_row(event.y)
        if not node:
            return
        self.tree.selection_set(node)
        self.selected_group = node[6:] if node.startswith("group/") else self.groups.group_for(node)
        self.organization_menu.tk_popup(event.x_root, event.y_root)
        self.organization_menu.grab_release()

    def group_toggle(self, event=None):
        node = self.tree.focus()
        if not node.startswith("group/") or self.search.get().strip() or self.filter.get() != "全部账号":
            return
        def save():
            if self.closing or not self.tree.exists(node):
                return
            try:
                self.groups.collapse(node[6:], not bool(self.tree.item(node, "open")))
            except (OSError, ValueError) as error:
                self.note.error(str(error))
        self.window.after_idle(save)

    def account_items(self):
        return tuple(account for group in self.tree.get_children() for account in self.tree.get_children(group))
