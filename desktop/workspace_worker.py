"""No Tk root: each account retains its own upstream modules and session lock."""
import argparse
import io
import json
import os
import queue
import sys
import threading
from pathlib import Path

from account_store import AccountStore
from quiz_assistant import QuizError, Question
from workspace_transport import Codec


class Worker:
    def __init__(self, root, profile, mode, kind, *, fixture=False):
        self.root, self.profile, self.mode, self.kind = root, profile, mode, kind
        self.fixture = fixture
        self.output, self.input = sys.stdout.buffer, sys.stdin.buffer
        # Upstream prints cannot corrupt the JSON protocol or expose credentials.
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        self.codec = Codec()
        self.write_lock, self.callback_lock = threading.Lock(), threading.Lock()
        self.cancelled, self.commands = threading.Event(), queue.Queue()
        self.ui, self.engine, self.login = {}, None, None

    def send(self, value):
        raw = (json.dumps(self.codec.encode(value), ensure_ascii=False) + "\n").encode("utf-8")
        with self.write_lock:
            self.output.write(raw)
            self.output.flush()

    def emit(self, kind, value):
        self.send({"type": "event", "kind": kind, "value": value})

    def call_ui(self, kind, *args, **kwargs):
        import uuid
        key, done, result = uuid.uuid4().hex, threading.Event(), {}
        self.ui[key] = (done, result)
        try:
            self.send({"type": "ui", "id": key, "kind": kind, "args": args, "kwargs": kwargs})
            done.wait()
            return result.get("value", "defer" if kind == "quiz" else "stop")
        finally:
            self.ui.pop(key, None)
            self.codec.callbacks.clear()

    def callback(self, packet):
        # Draft writes, AI calls and submissions in this account are serialized.
        with self.callback_lock:
            self.respond(packet, lambda args: self.codec.callbacks[args["key"]](
                *args["args"], **args["kwargs"]))

    def read(self):
        try:
            # An unbuffered OS read lets this daemon exit without holding the
            # interpreter's stdin-buffer lock during orderly worker shutdown.
            for raw in self.lines():
                packet = self.codec.decode(json.loads(raw))
                if packet["type"] == "cancel":
                    self.cancelled.set()
                elif packet["type"] == "ui_result":
                    waiting = self.ui.get(packet["id"])
                    if waiting:
                        waiting[1]["value"] = packet["value"]
                        waiting[0].set()
                elif packet["type"] == "request":
                    if packet["operation"] == "callback":
                        threading.Thread(target=self.callback, args=(packet,), daemon=True).start()
                    else:
                        self.commands.put(packet)
        finally:
            self.cancelled.set()
            # If the workspace disappears during a submit, keep this account's
            # lock until the callback has returned and its ledger is durable.
            with self.callback_lock:
                for done, _ in list(self.ui.values()):
                    done.set()
            self.commands.put(None)

    def lines(self):
        buffered = b""
        while True:
            chunk = os.read(self.input.fileno(), 65536)
            if not chunk:
                return
            buffered += chunk
            while b"\n" in buffered:
                line, buffered = buffered.split(b"\n", 1)
                yield line

    def respond(self, packet, action):
        try:
            value = action(packet.get("arguments", {}))
            self.send({"type": "result", "id": packet["id"], "value": value})
        except Exception as error:
            from account_store import AlreadyRunning
            known = isinstance(error, (QuizError, AlreadyRunning, ValueError)) or type(error).__name__ == "StudyError"
            message = str(error) if known else "账户操作未成功（" + type(error).__name__ + "），进度已保留。"
            self.send({"type": "result", "id": packet["id"], "error": message})

    def operate(self, operation, args):
        if operation == "close":
            if self.engine is not None:
                self.engine.close()
            return None
        self.cancelled.clear()
        if self.fixture:
            return self.offline(operation, args)
        if operation == "login":
            from login_window import LoginBackend
            if self.login is None:
                self.login = LoginBackend(self.root, self.profile)
            return self.login.authenticate(args["username"], args["password"], self.cancelled)
        if operation == "pending":
            from pending_quiz_worker import run_selected_quizzes
            try:
                return run_selected_quizzes(self.root, self.profile, args["task"],
                    lambda *items: self.call_ui("quiz", *items),
                    lambda message: self.emit("status", message), self.cancelled)
            finally:
                from study_reports import write_report
                try:
                    write_report(self.root, self.profile, mode="quizzes", state="pending_review_finished")
                except (OSError, ValueError, TypeError):
                    self.emit("status", "报告暂未保存成功，原任务记录已保留，可在“运行报告”中重新汇总。")
        if self.engine is None:
            from study_runner import StudyEngine
            self.engine = StudyEngine(self.root, self.profile, self.mode, self.emit, self.call_ui, self.cancelled)
        if operation == "load":
            return self.engine.load_courses()
        if operation == "selection":
            return self.engine.saved_selection()
        if operation == "run":
            return self.engine.run(args["selected"], args["speed"], args.get("auto_quizzes"))
        raise ValueError("未知的账户操作。")

    def offline(self, operation, args):
        """Explicit test-only backend: no network, no user-profile writes."""
        if operation == "load":
            return [{"courseId": "demo", "clazzId": "class-1", "title": "示例课程 · " + self.profile.name}]
        if operation == "selection":
            return [["demo", "class-1"]]
        if operation == "login":
            return True, "离线登录测试通过"
        if operation in ("run", "pending"):
            if args.get("speed") == 2:
                notes = {"initial_answers": {}, "initial_explanations": {}}
                def generate(question):
                    return ["离线答案"], "离线说明"
                generate.initial_answers, generate.initial_explanations = {}, {}
                generate.save_draft = lambda answers, explanations: notes.update(answers=answers)
                return self.call_ui("quiz", self.profile.name, "离线测验",
                    [Question("q1", "离线测试题目", "shortanswer", "", ["q1"])],
                    generate, lambda answers: "submitted")
            for done in range(40):
                if self.cancelled.wait(.05):
                    return "已停止，课程进度已保留。"
                self.emit("progress", {"done": done, "total": 40, "title": "离线后台任务"})
            return "离线任务已结束。"

    def run(self):
        threading.Thread(target=self.read, daemon=True).start()
        try:
            while True:
                packet = self.commands.get()
                if packet is None:
                    break
                operation = packet["operation"]
                self.respond(packet, lambda args: self.operate(operation, args))
                if operation == "close":
                    break
        finally:
            if self.engine is not None:
                self.engine.close()


def entry():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--mode", choices=("mixed", "quizzes", "videos"), required=True)
    parser.add_argument("--kind", choices=("study", "login", "pending"), required=True)
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args()
    profile = AccountStore(args.root).get(args.profile)
    Worker(args.root, profile, args.mode, args.kind, fixture=args.fixture).run()


if __name__ == "__main__":
    entry()
