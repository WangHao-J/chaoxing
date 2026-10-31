"""Build GUI process arguments without starting a process or reading credentials."""
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WindowLaunch:
    argv: list
    cwd: Path
    env: dict


def account_python(root):
    root = Path(root)
    virtual = root / ".venv" / "Scripts" / "python.exe"
    return virtual if virtual.exists() else root / "python" / "python.exe"


def gui_launch(root, profile, script, session_id, *, python=None, arguments=()):
    if script not in ("login_window.py", "study_window.py", "pending_quiz_worker.py"):
        raise ValueError("运行窗口无效")
    if re.fullmatch(r"[a-f0-9]{32}", session_id) is None:
        raise ValueError("运行窗口编号无效")
    root = Path(root)
    python = Path(python) if python is not None else account_python(root)
    gui = python.with_name("pythonw.exe")
    if gui.exists():
        python = gui
    argv = [str(python), str(root / script), "--root", str(root), "--profile", profile.id,
            *map(str, arguments), "--session-id", session_id]
    temporary = str(profile.directory / "temp")
    env = {**os.environ, "PYTHONUTF8": "1", "TEMP": temporary, "TMP": temporary}
    return WindowLaunch(argv, profile.directory, env)
