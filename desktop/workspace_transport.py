"""Private JSON pipes between one visible workspace and account-isolated workers."""
import base64
import dataclasses
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

from quiz_assistant import Question, QuizError


class Codec:
    def __init__(self, request=None):
        self.callbacks = {}
        self.request = request

    def encode(self, value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, bytes):
            return {"$bytes": base64.b64encode(value).decode("ascii")}
        if isinstance(value, Question):
            return {"$question": dataclasses.asdict(value)}
        if isinstance(value, (list, tuple)):
            return [self.encode(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self.encode(item) for key, item in value.items()}
        if callable(value):
            key = uuid.uuid4().hex
            self.callbacks[key] = value
            attrs = {name: self.encode(getattr(value, name)) for name in
                     ("initial_answers", "initial_explanations", "save_draft") if hasattr(value, name)}
            return {"$callback": key, "attributes": attrs}
        # The only remote object is the current account's manual verifier.
        if callable(getattr(value, "fetch", None)) and callable(getattr(value, "submit", None)):
            return {"$verifier": {name: self.encode(getattr(value, name)) for name in ("fetch", "submit")}}
        raise TypeError("Unsupported workspace message type: " + type(value).__name__)

    def decode(self, value):
        if isinstance(value, list):
            return [self.decode(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$bytes" in value:
            return base64.b64decode(value["$bytes"])
        if "$question" in value:
            return Question(**value["$question"])
        if "$callback" in value:
            key = value["$callback"]
            def remote(*args, **kwargs):
                return self.request("callback", {"key": key, "args": args, "kwargs": kwargs})
            for name, item in value.get("attributes", {}).items():
                setattr(remote, name, self.decode(item))
            return remote
        if "$verifier" in value:
            return SimpleNamespace(**{key: self.decode(item) for key, item in value["$verifier"].items()})
        return {key: self.decode(item) for key, item in value.items()}


class WorkerClient:
    """A reader always stays free while UI callbacks and network work wait."""
    def __init__(self, root, profile, mode, emit, call_ui, cancelled, *, kind="study", fixture=False):
        self.root, self.profile = Path(root), profile
        self.emit, self.call_ui, self.cancelled = emit, call_ui, cancelled
        self.pending, self.lock, self.write_lock = {}, threading.Lock(), threading.Lock()
        self.codec = Codec(self.request)
        self.closed = False
        python = self.root / "python" / "python.exe"
        if not python.exists():
            python = Path(sys.executable)
        script = Path(__file__).with_name("workspace_worker.py")
        argv = [str(python), "-B", str(script), "--root", str(self.root),
                "--profile", profile.id, "--mode", mode, "--kind", kind]
        if fixture:
            argv.append("--fixture")
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=profile.directory,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def send(self, message):
        raw = (json.dumps(self.codec.encode(message), ensure_ascii=False) + "\n").encode("utf-8")
        with self.write_lock:
            if self.closed:
                raise QuizError("账户后台已结束，请重新打开当前账户页面。")
            try:
                self.process.stdin.write(raw)
                self.process.stdin.flush()
            except (OSError, ValueError):
                raise QuizError("与账户后台的连接已中断，进度记录已保留。") from None

    def _read(self):
        try:
            for raw in self.process.stdout:
                packet = self.codec.decode(json.loads(raw))
                if packet["type"] == "result":
                    with self.lock:
                        waiting = self.pending.get(packet["id"])
                    if waiting:
                        waiting[1].update(packet)
                        waiting[0].set()
                elif packet["type"] == "event":
                    self.emit(packet["kind"], packet.get("value"))
                elif packet["type"] == "ui":
                    threading.Thread(target=self._ui, args=(packet,), daemon=True).start()
        except (OSError, ValueError, KeyError, TypeError):
            pass
        finally:
            self.closed = True
            with self.lock:
                for done, result in self.pending.values():
                    if not done.is_set():
                        result["error"] = "账户后台已退出，任务记录已保留；请重新打开当前账户页面。"
                        done.set()
            self.process.stdout.close()
            self.process.stdin.close()

    def _ui(self, packet):
        try:
            value = self.call_ui(packet["kind"], *packet["args"], **packet["kwargs"])
            self.send({"type": "ui_result", "id": packet["id"], "value": value})
        except Exception:
            try:
                self.send({"type": "ui_result", "id": packet["id"],
                           "value": "defer" if packet["kind"] == "quiz" else "stop"})
            except QuizError:
                pass

    def request(self, operation, arguments=None):
        key, done, result = uuid.uuid4().hex, threading.Event(), {}
        with self.lock:
            self.pending[key] = (done, result)
        try:
            self.send({"type": "request", "id": key, "operation": operation, "arguments": arguments or {}})
            stop_sent = False
            while not done.wait(.1):
                if self.cancelled.is_set() and not stop_sent and operation not in ("callback", "close"):
                    self.send({"type": "cancel"})
                    stop_sent = True
            if result.get("error"):
                raise QuizError(result["error"])
            return result.get("value")
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def load_courses(self):
        return self.request("load")

    def saved_selection(self):
        return self.request("selection")

    def run(self, selected, speed=1.0, auto_quizzes=None):
        return self.request("run", {"selected": selected, "speed": speed, "auto_quizzes": auto_quizzes})

    def authenticate(self, username, password, cancelled):
        self.cancelled = cancelled
        return self.request("login", {"username": username, "password": password})

    def run_pending(self, task):
        return self.request("pending", {"task": task})

    def close(self):
        if not self.closed:
            self.request("close")
        self.process.wait(timeout=10)
        if threading.current_thread() is not self.reader:
            self.reader.join(timeout=2)

