"""An account-local report page with access to its independent report folder."""
import os
import json
import tkinter as tk
from tkinter import ttk
from study_reports import write_report, report_directory, plain_text


def show_reports(panel):
    if "reports" not in panel.pages:
        host = panel.host("reports")
        tools = ttk.Frame(host, padding=14, style="Page.TFrame")
        tools.pack(fill="x")
        ttk.Label(tools, text="账号运行报告", style="Subheading.TLabel").pack(side="left")
        directory = report_directory(panel.workspace.store.root, panel.profile)
        def open_folder():
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(directory)
        ttk.Button(tools, text="打开本账号报告目录", command=open_folder).pack(side="right")
        area = ttk.Frame(host, padding=(14, 0, 14, 14), style="Page.TFrame")
        area.pack(fill="both", expand=True)
        panel.report_text = tk.Text(area, wrap="word", font=("Microsoft YaHei UI", 11), state="disabled", relief="flat")
        scroll = ttk.Scrollbar(area, command=panel.report_text.yview)
        scroll.pack(side="right", fill="y")
        panel.report_text.configure(yscrollcommand=scroll.set)
        panel.report_text.pack(fill="both", expand=True)
        def refresh(snapshot=False):
            try:
                path = directory / "latest.json"
                if snapshot or not path.exists():
                    write_report(panel.workspace.store.root, panel.profile)
                text = plain_text(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                text = "报告无法读取或保存，原记录已保留。"
            panel.report_text.configure(state="normal")
            panel.report_text.delete("1.0", "end")
            panel.report_text.insert("1.0", text)
            panel.report_text.configure(state="disabled")
        ttk.Button(tools, text="汇总当前记录", command=lambda: refresh(True)).pack(side="right", padx=8)
        ttk.Button(tools, text="刷新", command=refresh).pack(side="right")
        panel.refresh_report = refresh
    panel.refresh_report()
