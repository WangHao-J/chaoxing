"""Native settings and answer-review windows."""
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from quiz_assistant import (QuizError, Question, read_settings, save_settings,
                            generate_suggestion, generate_all, validate_values, api_url)
from ui_theme import apply_theme
from ui_feedback import NoticeVar, Notice


def start_job(events, kind, operation):
    def run():
        try:
            events.put((kind, operation(), None))
        except QuizError as error:
            events.put((kind, None, str(error)))
        except Exception:
            events.put((kind, None, "操作未成功，请稍后重试；没有自动提交答案。"))
    threading.Thread(target=run, daemon=True).start()


class SettingsDialog:
    def __init__(self, window, profile, *, close_after_save=False):
        self.window, self.profile = window, profile
        self.close_after_save, self.saved = close_after_save, False
        self.events, self.timer, self.busy = queue.Queue(), None, False
        apply_theme(window)
        try:
            settings = read_settings(profile)
            initial = ""
        except QuizError as error:
            settings = {"enabled": False, "endpoint": "", "model": "", "key": ""}
            initial = str(error)
        from settings_view import build_settings_view
        build_settings_view(self, profile, settings, initial)
        window.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()

    def settings(self):
        return {"enabled": self.enabled.get(), **{name: variable.get() for name, variable in self.fields.items()}}

    def save(self):
        settings = self.settings()
        if self.close_after_save and not settings["enabled"]:
            self.feedback.error("请勾选启用 AI 答题服务，再保存并打开答题页面。")
            return
        try:
            save_settings(self.profile, settings)
        except QuizError as error:
            self.feedback.error(str(error))
            return
        except OSError:
            self.feedback.error("设置无法保存，请检查安装目录的写入权限。")
            return
        self.saved = True
        if self.close_after_save:
            self.close()
            return
        self.feedback.success("统一 API 设置已保存，所有账号及新账号共用；新配置用于后续测验。")

    def test(self):
        if self.busy:
            return
        settings = self.settings()
        self.busy = True
        self.test_button.configure(state="disabled")
        self.feedback.busy("正在向该服务发送一条简短测试请求……")
        question = Question("test", "请回答数字2。", "shortanswer", "", ["test"])
        start_job(self.events, "test", lambda: generate_suggestion(settings, question))

    def poll(self):
        try:
            _, _, error = self.events.get_nowait()
            self.busy = False
            self.test_button.configure(state="normal")
            if error:
                self.feedback.error(error)
            else:
                self.feedback.success("AI 连接成功。请点击保存设置。")
        except queue.Empty:
            pass
        self.timer = self.window.after(150, self.poll)

    def close(self):
        if self.timer:
            self.window.after_cancel(self.timer)
        self.fields["key"].set("")
        self.window.destroy()


class ReviewDialog:
    def __init__(self, window, account, course, questions, generate, submit):
        self.window, self.questions, self.generate, self.submit_callback = window, questions, generate, submit
        self.palette = apply_theme(window)
        initial = getattr(generate, "initial_answers", {})
        notes = getattr(generate, "initial_explanations", {})
        self.answers = dict(initial) if isinstance(initial, dict) else {}
        self.explanations = dict(notes) if isinstance(notes, dict) else {}
        self.save_draft = getattr(generate, "save_draft", None)
        self.draft_timer = None
        self._last_saved, self.pending_close = None, False
        self.events, self.timer = queue.Queue(), None
        self.cancel_generation = threading.Event()
        self.busy, self.operation, self.result, self.index = False, "", "defer", 0
        self.operation_started = None
        self.variables = []
        self.reviewed = tk.BooleanVar(value=False)
        from quiz_review_view import build_review_view
        build_review_view(self, account, course)
        self.render_list()
        self.listing.selection_set(0)
        self.show_question()
        window.protocol("WM_DELETE_WINDOW", self.defer)
        self.poll()

    def current_values(self):
        values = [variable.get() for variable in self.variables]
        if self.questions[self.index].kind == "multiple" and values:
            values = list(re.sub(r"[,，\s]+", "", values[0]).upper())
        return values

    def store_current(self):
        if self.variables:
            self.answers[self.questions[self.index].id] = self.current_values()
        self.persist_draft()

    def persist_draft(self):
        if not callable(self.save_draft):
            return
        snapshot = repr((self.answers, self.explanations))
        if snapshot == self._last_saved:
            return
        try:
            self.save_draft(self.answers, self.explanations)
            self._last_saved = snapshot
        except QuizError as error:
            self.feedback.error(str(error))

    def render_list(self):
        self.listing.delete(0, "end")
        filled = 0
        names = {"completion": "填空", "single": "单选", "multiple": "多选", "judgement": "判断", "shortanswer": "简答"}
        for index, question in enumerate(self.questions):
            try:
                validate_values(question, self.answers.get(question.id))
                state = "已填"
                filled += 1
            except QuizError:
                state = "待填"
            if question.unsupported:
                state += " · 需网页"
            self.listing.insert("end", f"{index + 1}. {names.get(question.kind, '其他')} · {state}")
            self.listing.itemconfigure(index, foreground=self.palette["danger"] if question.unsupported else self.palette["text"])
        self.completion.set(f"已填 {filled} / {len(self.questions)} 题")

    def show_question(self):
        question = self.questions[self.index]
        self.question_issue.set("无法在程序内提交：" + question.unsupported if question.unsupported else "")
        self.question_position.set(f"第 {self.index + 1} / {len(self.questions)} 题")
        self.previous_button.configure(state="normal" if self.index > 0 and not self.busy else "disabled")
        self.next_button.configure(state="normal" if self.index + 1 < len(self.questions) and not self.busy else "disabled")
        self.title.configure(state="normal")
        self.title.delete("1.0", "end")
        self.title.insert("1.0", question.title + ("\n\n" + question.options if question.options else ""))
        self.title.configure(state="disabled")
        for child in self.editor.winfo_children():
            child.destroy()
        count = question.blank_count if question.kind == "completion" else 1
        values = self.answers.get(question.id, [""] * count)
        if question.kind == "multiple":
            values = [",".join(values)]
        self.variables = []
        self.editor.columnconfigure(1, weight=1)
        for index in range(count):
            label = f"第{index + 1}空" if question.kind == "completion" else "答案"
            ttk.Label(self.editor, text=label, style="Card.TLabel").grid(row=index, column=0, padx=(0, 10), pady=6)
            variable = tk.StringVar(value=values[index] if index < len(values) else "")
            variable.trace_add("write", self.edited)
            self.variables.append(variable)
            entry = ttk.Entry(self.editor, textvariable=variable)
            entry.grid(row=index, column=1, sticky="ew", pady=6)
            entry.bind("<FocusIn>", lambda event: self.reveal_answer(event.widget))
            entry.bind("<MouseWheel>", lambda event: self.scroll_answers(event.delta))
        hints = {"single": "填写选项字母，例如 A。", "multiple": "填写多个选项字母，例如 A,C。",
                 "judgement": "填写 true / false 或 对 / 错。", "completion": "按题目顺序逐空填写。"}
        self.explanation.configure(state="normal")
        self.explanation.delete("1.0", "end")
        self.explanation.insert("1.0", (question.unsupported + "\n" if question.unsupported else "") +
            hints.get(question.kind, "填写你确认的答案。") + "\n" + self.explanations.get(question.id, ""))
        self.explanation.configure(state="disabled")
        self.canvas.yview_moveto(0)
        self.update_submit()

    def reveal_answer(self, widget):
        self.editor.update_idletasks()
        total, viewport = self.editor.winfo_height(), self.canvas.winfo_height()
        top = widget.winfo_y()
        current = self.canvas.yview()[0] * total
        if top < current or top + widget.winfo_height() > current + viewport:
            self.canvas.yview_moveto(max(0, top - 8) / max(1, total))

    def toggle_explanation(self):
        self.answer_tabs.select(self.explanation_panel)

    def locate_blocked(self):
        if self.busy:
            return
        blocked = [index for index, question in enumerate(self.questions) if question.unsupported]
        if blocked:
            target = next((index for index in blocked if index > self.index), blocked[0])
            self.navigate(target - self.index)
            self.answer_tabs.select(0)
            self.listing.focus_set()

    def refresh_compatibility(self):
        blocked = [index + 1 for index, question in enumerate(self.questions) if question.unsupported]
        if blocked:
            preview = "、".join(map(str, blocked[:4])) + ("等" if len(blocked) > 4 else "")
            self.compatibility.error(f"第 {preview} 题暂不支持程序提交，本测验不能自动交卷。可用 AI 填写建议，再复制到学习通网页。")
        else:
            missing = 0
            for question in self.questions:
                try:
                    validate_values(question, self.answers.get(question.id))
                except QuizError:
                    missing += 1
            self.compatibility.set(f"还需填写 {missing} 道题。填写后逐题核对，并勾选下方确认。" if missing
                                   else "答案已填写完整，请核对后勾选确认，再提交本测验。")
        self.locate_blocked_button.configure(state="normal" if blocked and not self.busy else "disabled")
        self.auto_button.configure(state="disabled" if blocked or self.busy else "normal")

    def scroll_answers(self, delta):
        self.canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        return "break"

    def navigate(self, offset):
        target = self.index + offset
        if self.busy or not 0 <= target < len(self.questions):
            return
        self.store_current()
        self.index = target
        self.listing.selection_clear(0, "end")
        self.listing.selection_set(target)
        self.listing.see(target)
        self.show_question()

    def edited(self, *args):
        self.reviewed.set(False)
        self.store_current()
        self.render_list()
        self.listing.selection_set(self.index)
        self.update_submit()

    def select(self, event=None):
        selected = self.listing.curselection()
        if selected and selected[0] != self.index:
            if self.busy:
                self.listing.selection_clear(0, "end")
                self.listing.selection_set(self.index)
                return
            self.store_current()
            self.index = selected[0]
            self.show_question()

    def update_submit(self):
        self.store_current()
        self.refresh_compatibility()
        valid = not self.busy and self.reviewed.get() and not any(q.unsupported for q in self.questions)
        try:
            for question in self.questions:
                validate_values(question, self.answers.get(question.id))
        except QuizError:
            valid = False
        self.submit_button.configure(state="normal" if valid else "disabled")

    def begin(self, kind, operation):
        if self.busy:
            return
        self.busy, self.operation = True, kind
        self.operation_started = time.monotonic()
        self.cancel_generation.clear()
        self.cancel_button.configure(state="normal" if kind != "submit" else "disabled")
        self.generate_button.configure(state="disabled")
        self.generate_all_button.configure(state="disabled")
        self.auto_button.configure(state="disabled")
        self.copy_button.configure(state="disabled")
        self.review_checkbox.configure(state="disabled")
        self.previous_button.configure(state="disabled")
        self.next_button.configure(state="disabled")
        self.locate_blocked_button.configure(state="disabled")
        self.submit_button.configure(state="disabled")
        for child in self.editor.winfo_children():
            if child.winfo_class() == "TEntry":
                child.configure(state="disabled")
        self.feedback.busy({"suggest": "正在生成本题建议……", "all": "正在补齐未填答案，已填写内容会保留……"}.get(
            kind, "正在提交你确认的答案，请等待结果……"))
        start_job(self.events, kind, operation)

    def suggest(self):
        self.store_current()
        question = self.questions[self.index]
        self.begin("suggest", lambda: self.generate(question))

    def suggest_all(self):
        if self.busy:
            return
        self.store_current()
        self.cancel_generation.clear()
        self.reviewed.set(False)
        snapshot, notes = dict(self.answers), dict(self.explanations)
        def checkpoint(answers, explanations):
            if callable(self.save_draft):
                self.save_draft({**snapshot, **answers}, {**notes, **explanations})
        self.begin("all", lambda: generate_all(self.questions, self.generate,
            progress=lambda current, total: self.events.put(("progress", (current, total), None)),
            cancelled=self.cancel_generation.is_set, initial_answers=snapshot,
            initial_explanations=notes, on_result=checkpoint))

    def automatic(self):
        if self.busy:
            self.feedback.set("请先等待当前请求结束，再选择自动答题。")
            return
        if any(question.unsupported for question in self.questions):
            self.refresh_compatibility()
            self.feedback.error("本测验不能在程序内提交。点击“定位受阻题”查看原因；仍可用 AI 生成并复制答案。")
            return
        self.store_current()
        # Return only answers, never signed submission forms. Old reviewers may still return a string.
        self.result = {"action": "auto_all_quizzes", "answers": dict(self.answers)}
        self.finish()

    def confirm_submit(self):
        self.update_submit()
        if str(self.submit_button.cget("state")) != "normal":
            self.feedback.error("暂不能提交：请填写所有题目并勾选已核对；若顶部提示题型不兼容，请在网页完成。")
            return
        snapshot = {question.id: validate_values(question, self.answers[question.id]) for question in self.questions}
        self.begin("submit", lambda: self.submit_callback(snapshot))

    def poll(self):
        if self.timer:
            self.window.after_cancel(self.timer)
            self.timer = None
        if self.busy and self.operation_started is not None:
            elapsed = int(time.monotonic() - self.operation_started)
            phase = "确认提交结果" if self.operation == "submit" else "等待 AI 响应"
            self.wait_status.set(f"{phase} · 已等待 {elapsed} 秒")
        try:
            kind, value, error = self.events.get_nowait()
            if kind == "progress":
                self.feedback.busy(f"AI 正在填写全部题目（{value[0]}/{value[1]}）……")
                self.timer = self.window.after(150, self.poll)
                return
            self.busy = False
            self.wait_status.set("")
            self.cancel_button.configure(state="disabled")
            self.generate_button.configure(state="normal")
            self.generate_all_button.configure(state="normal")
            self.auto_button.configure(state="normal")
            self.copy_button.configure(state="normal")
            self.review_checkbox.configure(state="normal")
            self.previous_button.configure(state="normal" if self.index > 0 else "disabled")
            self.next_button.configure(state="normal" if self.index + 1 < len(self.questions) else "disabled")
            for child in self.editor.winfo_children():
                if child.winfo_class() == "TEntry":
                    child.configure(state="normal")
            if error:
                self.feedback.error(error)
                self.reviewed.set(False)
            elif kind == "suggest":
                question = self.questions[self.index]
                self.answers[question.id], self.explanations[question.id] = value
                self.reviewed.set(False)
                self.render_list()
                self.listing.selection_set(self.index)
                self.show_question()
                self.feedback.success("建议已生成，请核对并修改，尤其注意空位顺序。")
            elif kind == "submit":
                self.result = "submitted"
                self.finish()
                return
            elif kind == "all":
                self.answers.update(value["answers"])
                self.explanations.update(value["explanations"])
                self.reviewed.set(False)
                self.render_list()
                self.listing.selection_set(self.index)
                self.show_question()
                count = len(value["errors"])
                first = next(iter(value["errors"].values()), "")
                if value.get("cancelled"):
                    self.feedback.set("已停止生成，完成的答案已保留。")
                elif count:
                    self.feedback.error(f"仍有题目未能填写。{first} 已完成的答案已保留，可重试未填题。")
                else:
                    if any(question.unsupported for question in self.questions):
                        self.feedback.success("答案建议已填写。此测验需在网页提交，可点击“复制已有答案”。")
                    else:
                        self.feedback.success("全部题目已填写，请核对后勾选确认并提交。")
            if self.pending_close:
                self.finish()
                return
            self.update_submit()
        except queue.Empty:
            pass
        self.timer = self.window.after(150, self.poll)

    def copy_answers(self):
        self.store_current()
        text = "\n\n".join(f"第{index + 1}题\n" + "\n".join(
            f"{number + 1}. {value}" for number, value in enumerate(self.answers.get(question.id, [])))
            for index, question in enumerate(self.questions))
        self.window.clipboard_clear()
        self.window.clipboard_append(text)
        self.feedback.set("已有答案已复制，可粘贴到网页填写。")

    def defer(self):
        if self.busy and self.operation == "submit":
            self.feedback.set("正在确认提交结果，请等待；不会自动重复提交。")
            return
        if self.busy:
            self.pending_close = True
            self.cancel_ai()
            return
        self.store_current()
        self.finish()

    def skip(self, all_remaining=False):
        if self.busy and self.operation == "submit":
            self.feedback.set("正在确认提交结果，请等待；此时不能跳过或退出。")
            return
        self.result = "skip_all_quizzes" if all_remaining else "skip"
        self.defer()

    def cancel_ai(self):
        if self.busy and self.operation != "submit":
            self.cancel_generation.set()
            self.cancel_button.configure(state="disabled")
            self.feedback.set("已请求停止，正在等待当前 AI 请求返回；完成的答案会保留。")

    def finish(self):
        self.store_current()
        self.cancel_generation.set()
        if self.timer:
            self.window.after_cancel(self.timer)
        self.window.withdraw()
        self.window.quit()


def ask_quiz(account, course, questions, generate, submit):
    window = tk.Tk()
    try:
        dialog = ReviewDialog(window, account, course, questions, generate, submit)
        window.lift()
        window.mainloop()
        return dialog.result
    finally:
        window.destroy()


def open_settings(parent, profile):
    window = tk.Toplevel(parent)
    SettingsDialog(window, profile)


def ai_configured(settings):
    if settings.get("enabled") is not True or not all(
            str(settings.get(name, "")).strip() for name in ("endpoint", "model", "key")):
        return False
    try:
        api_url(settings["endpoint"])
    except QuizError:
        return False
    return True


def ensure_ai_config(parent, profile):
    """Configure the selected account locally; opening this flow makes no AI request."""
    try:
        if ai_configured(read_settings(profile)):
            return True
    except QuizError:
        pass
    messagebox.showinfo("配置 AI 答题", "请先填写服务地址、模型和 API Key，并启用 AI 答题服务。",
                        parent=parent)
    previous_grab = parent.grab_current()
    window = tk.Toplevel(parent)
    window.transient(parent)
    dialog = SettingsDialog(window, profile, close_after_save=True)
    window.grab_set()
    parent.wait_window(window)
    if previous_grab is not None and previous_grab.winfo_exists():
        previous_grab.grab_set()
    if dialog.saved is not True:
        return False
    try:
        return ai_configured(read_settings(profile))
    except QuizError:
        return False
