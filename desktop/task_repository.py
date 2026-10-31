"""One exact-scope view of legacy and independent-window pending checkpoints."""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from account_store import ProfileLock


from app_modes import MODE_IDS as MODES


class TaskRepositoryError(ValueError):
    pass


def identifier(value):
    return value.strip() if isinstance(value, str) else str(value) if type(value) is int else ""


def task_key(row):
    return tuple(identifier(row.get(name)) for name in ("course_id", "class_id", "chapter_id")) \
        if isinstance(row, dict) else ("", "", "")


def read_object(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (OSError, UnicodeError, ValueError, TypeError):
        raise TaskRepositoryError("任务记录无法读取，原文件已保留；可备份并重建对应窗口记录。") from None


def validate_mode(data, mode):
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("mode") != mode:
        raise TaskRepositoryError("此窗口的续接记录格式异常，原文件已保留。")
    for name, fields in (("courses", ("course_id", "class_id")),
                         ("pending", ("course_id", "class_id", "chapter_id"))):
        rows = data.get(name)
        if not isinstance(rows, list) or any(not isinstance(row, dict) or any(
                not isinstance(row.get(field), str) or not row[field] for field in fields) for row in rows):
            raise TaskRepositoryError("此窗口的续接记录格式异常，原文件已保留。")
    return data


def _sources(profile):
    directory = Path(profile.directory)
    yield "legacy", directory / "pending_tasks.json", "tasks"
    for mode in MODES:
        yield mode, directory / f"study_state_{mode}.json", "pending"


def source_rows(data, source, field):
    if source != "legacy":
        validate_mode(data, source)
    rows = data.get(field)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise TaskRepositoryError("待处理记录格式异常，原文件已保留。")
    return rows


def list_pending(profile):
    """Return all queues; a damaged queue appears as a diagnostic, never hides others."""
    result, known = [], {}
    for source, path, field in _sources(profile):
        if not path.exists():
            continue
        try:
            rows = source_rows(read_object(path), source, field)
        except TaskRepositoryError as error:
            result.append({"course_title": "记录需要修复", "chapter_title": path.name,
                           "reason": str(error), "sources": [source], "record_error": True})
            continue
        for row in rows:
            key = task_key(row)
            target = known.get(key) if all(key) else None
            reason = str(row.get("reason", "")).strip()
            if target is None:
                target = dict(row)
                target["sources"] = [source]
                if all(key):
                    target.update(zip(("course_id", "class_id", "chapter_id"), key))
                    known[key] = target
                result.append(target)
            else:
                if source not in target["sources"]:
                    target["sources"].append(source)
                for name in ("course_title", "chapter_title"):
                    if not target.get(name) and row.get(name):
                        target[name] = row[name]
                if reason and reason not in str(target.get("reason", "")).split("；"):
                    target["reason"] = "；".join(filter(None, (str(target.get("reason", "")), reason)))
    return result


def confirmation_records(profile, key, *, require_present=True):
    """Prepare all removals before writing; callers must freshly confirm platform completion."""
    records, legacy_selected, present = [], False, False
    for source, path, field in _sources(profile):
        if not path.exists():
            continue
        old = path.read_bytes()
        data = read_object(path)
        rows = source_rows(data, source, field)
        matching = any(task_key(row) == key for row in rows)
        present |= matching
        legacy_selected |= source == "legacy" and matching
        if matching:
            data[field] = [row for row in rows if task_key(row) != key]
            records.append((path, old, data))
    if require_present and not present:
        raise TaskRepositoryError("所选任务已不在当前待处理列表中，请刷新列表；原记录未改动。")
    resume_path = Path(profile.directory) / "resume_state.json"
    if resume_path.exists():
        old = resume_path.read_bytes()
        data = read_object(resume_path)
        rows, courses = data.get("pending"), data.get("courses")
        if data.get("version") != 1 or not isinstance(rows, list) or not isinstance(courses, list) \
                or any(not isinstance(row, dict) or not all(task_key(row)) for row in rows) \
                or any(not isinstance(row, dict) or not all(identifier(row.get(name))
                       for name in ("course_id", "class_id")) for row in courses):
            raise TaskRepositoryError("原课程续接记录格式异常，全部待处理记录已保留。")
        matching = any(task_key(row) == key for row in rows)
        if legacy_selected and not matching:
            raise TaskRepositoryError("原课程两份待处理记录不一致，请重新核对；原记录已保留。")
        if matching:
            data["pending"] = [row for row in rows if task_key(row) != key]
            records.insert(0, (resume_path, old, data))
    return records


def stage_bytes(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix="." + path.name + ".pending-", suffix=".tmp",
                                          dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def write_confirmed(records, *, stage=stage_bytes):
    """Stage every file, replace, and roll back exact original bytes if a replace fails."""
    staged, replaced = [], []
    try:
        for path, old, data in records:
            staged.append((path, old, stage(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))))
        for path, old, temporary in staged:
            os.replace(temporary, path)
            replaced.append((path, old))
    except Exception:
        for path, old in reversed(replaced):
            temporary = None
            try:
                temporary = stage_bytes(path, old)
                os.replace(temporary, path)
            except OSError:
                pass
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        raise TaskRepositoryError("保存确认结果失败，请保留现有记录并重新核对。") from None
    finally:
        for _, _, temporary in staged:
            temporary.unlink(missing_ok=True)


def clear_confirmed(profile, key):
    """Internal runner API: caller holds ProfileLock and has a fresh completed point."""
    write_confirmed(confirmation_records(profile, key, require_present=False))


def recover_mode(profile, mode):
    """Back up and rebuild only one mode checkpoint while the account is idle."""
    if mode not in MODES:
        raise TaskRepositoryError("只能重建独立窗口的续接记录。")
    directory = Path(profile.directory)
    path = directory / f"study_state_{mode}.json"
    with ProfileLock(directory):
        if not path.is_file():
            raise TaskRepositoryError("此窗口没有需要重建的记录。")
        content = path.read_bytes()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        backup = directory / f"{path.stem}.backup-{stamp}.json"
        with backup.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        data = {"version": 1, "mode": mode, "courses": [], "pending": []}
        temporary = stage_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return backup
