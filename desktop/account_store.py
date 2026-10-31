"""Independent local profiles sharing one Chaoxing source checkout and runtime."""
import configparser
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    directory: Path


class AccountStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.accounts = self.root / "accounts"

    def directory(self, profile_id):
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", profile_id):
            raise ValueError("账户编号无效")
        directory = (self.accounts / profile_id).resolve()
        if not directory.is_relative_to(self.accounts.resolve()):
            raise ValueError("账户目录无效")
        return directory

    def get(self, profile_id):
        directory = self.directory(profile_id)
        data = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
        if data.get("id") != profile_id:
            raise ValueError("账户资料无效")
        return Profile(profile_id, str(data["name"]), directory)

    def list(self):
        if not self.accounts.exists():
            return []
        profiles = []
        for directory in self.accounts.iterdir():
            if directory.is_dir():
                try:
                    profiles.append(self.get(directory.name))
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(profiles, key=lambda item: (item.id != "default", item.name.casefold()))

    def create(self, name, *, legacy=False, profile_id=None):
        name = name.strip()
        if not name or len(name) > 60 or any(ord(char) < 32 for char in name):
            raise ValueError("请输入 1 至 60 个字符的账户名称")
        if any(profile.name.casefold() == name.casefold() for profile in self.list()):
            raise ValueError("这个账户名称已经存在")
        profile_id = profile_id or uuid.uuid4().hex
        directory = self.directory(profile_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "temp").mkdir()
        config = configparser.ConfigParser(interpolation=None)
        config.read(self.root / "app" / "config.ini", encoding="utf-8")
        if not legacy:
            for section in ("common", "tiku", "notification"):
                if not config.has_section(section):
                    config.add_section(section)
            config["common"].update({
                "username": "", "password": "", "course_list": "", "use_cookies": "false",
                "speed": "1.0", "jobs": "1", "work_redo_enabled": "false",
                "add_learning_count": "false", "notopen_action": "retry", "retry_interval": "1.0",
            })
            config["tiku"] = {"provider": "", "submit": "false", "check_llm_connection": "false"}
            config["notification"] = {"provider": ""}
        with (directory / "config.ini").open("w", encoding="utf-8") as stream:
            config.write(stream)
        shutil.copytree(self.root / "app" / "resource", directory / "resource")
        if legacy:
            for filename in ("cookies.txt", "cache.json"):
                source = self.root / "app" / filename
                if source.is_file():
                    shutil.copy2(source, directory / filename)
        (directory / "profile.json").write_text(
            json.dumps({"id": profile_id, "name": name}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.get(profile_id)

    def ensure_default(self):
        if (self.accounts / "default" / "profile.json").is_file():
            return self.get("default")
        return self.create("默认账户", legacy=True, profile_id="default")


class ProfileLock:
    """A kernel-held file lock released automatically if the process exits."""
    def __init__(self, directory):
        self.path = Path(directory) / ".run.lock"
        self.stream = None

    def acquire(self):
        stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise AlreadyRunning("这个账户已经在运行，请使用原来的运行窗口") from error
        self.stream = stream
        return self

    def release(self):
        if self.stream is not None:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()


def is_running(profile):
    try:
        with ProfileLock(profile.directory):
            return False
    except AlreadyRunning:
        return True


def selected_courses(courses, requested):
    """Select only explicitly requested courses; never expand an invalid selection."""
    ids = [str(value).strip() for value in requested if str(value).strip()]
    if not ids:
        raise ValueError("没有选择课程，请填写课程 ID")
    available = {str(course["courseId"]) for course in courses}
    if ids != ["*"]:
        missing = sorted(set(ids) - available)
        if missing:
            raise ValueError("这些课程 ID 没有找到：" + ", ".join(missing))
    chosen, seen = [], set()
    for course in courses:
        key = (str(course["courseId"]), str(course["clazzId"]))
        if (ids == ["*"] or key[0] in ids) and key not in seen:
            chosen.append(course)
            seen.add(key)
    return chosen
