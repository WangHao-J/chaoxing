"""Read one pending chapter's quizzes without running the chapter scheduler."""
import json
import os
import re
import tempfile
from pathlib import Path

from card_reader import flag_is_true
from card_tasks import read_chapter_jobs
from task_repository import list_pending, confirmation_records, TaskRepositoryError
from quiz_assistant import QuizError


class PendingQuizError(QuizError):
    pass


def _identifier(value):
    if isinstance(value, str):
        return value.strip()
    if type(value) is int:
        return str(value)
    return ""


def task_key(row):
    """Incomplete legacy rows get an incomplete key, never a guessed target."""
    if not isinstance(row, dict):
        return "", "", ""
    return tuple(_identifier(row.get(name)) for name in ("course_id", "class_id", "chapter_id"))


def _read_json(path, label):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (OSError, UnicodeError, ValueError, TypeError):
        raise PendingQuizError(f"{label}无法读取，原待处理记录已保留。") from None


def _pending_rows(data):
    rows = data.get("tasks")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise PendingQuizError("待处理记录格式异常，原记录已保留。")
    return rows


def load_selected_task(profile, course_id, class_id, chapter_id):
    key = tuple(_identifier(value) for value in (course_id, class_id, chapter_id))
    if not all(key):
        raise PendingQuizError("这条旧记录缺少课程或章节编号，请重新启动原课程刷新待处理列表。")
    try:
        rows = list_pending(profile)
    except TaskRepositoryError as error:
        raise PendingQuizError(str(error)) from None
    selected = next((row for row in rows if task_key(row) == key), None)
    if selected is None:
        raise PendingQuizError("所选任务已不在这个账户的待处理列表中，请刷新列表。")
    return {name: value for name, value in selected.items() if name not in ("sources", "record_error")}


def _fresh_point(cx, course, chapter_id):
    try:
        result = cx.get_course_point(course["courseId"], course["clazzId"], course["cpi"])
        points = result.get("points") if isinstance(result, dict) else None
        if not isinstance(points, list) or any(not isinstance(point, dict) for point in points):
            raise ValueError()
        matches = [point for point in points if _identifier(point.get("id")) == chapter_id]
    except Exception:
        raise PendingQuizError("读取所选课程的章节状态失败，待处理记录已保留。") from None
    if len(matches) != 1:
        raise PendingQuizError("平台目录中未找到唯一对应章节，请核对课程权限；待处理记录已保留。")
    return dict(matches[0])


def _blank_card_page(html):
    return isinstance(html, str) and not re.search(r"<(?:script|iframe|input|form|a)\b", html, re.I) \
        and not re.sub(r"<!--.*?-->|<[^>]*>", "", html, flags=re.S).strip()


def resolve_selected_quizzes(cx, row, *, cancelled=None):
    """Refresh the exact account/course/class/chapter and return only real quizzes."""
    def check_cancelled():
        if cancelled is not None and cancelled():
            raise PendingQuizError("已取消打开，待处理记录已保留。")

    key = task_key(row)
    if not all(key):
        raise PendingQuizError("所选任务缺少课程或章节编号，无法打开答题页面。")
    check_cancelled()
    try:
        courses = cx.get_course_list()
        if not isinstance(courses, list) or any(not isinstance(course, dict) for course in courses):
            raise ValueError()
        matches = [course for course in courses if (
            _identifier(course.get("courseId")), _identifier(course.get("clazzId"))) == key[:2]]
    except Exception:
        raise PendingQuizError("读取当前账户的课程列表失败，请检查登录状态；待处理记录已保留。") from None
    check_cancelled()
    if len(matches) != 1:
        raise PendingQuizError("这个账户的课程列表中没有唯一对应课程或班级，待处理记录已保留。")
    course = dict(matches[0])
    if not _identifier(course.get("cpi")):
        raise PendingQuizError("平台未返回完整课程信息，请重新登录后再试；待处理记录已保留。")
    point = _fresh_point(cx, course, key[2])
    check_cancelled()
    if flag_is_true(point.get("has_finished")):
        return course, point, []
    if flag_is_true(point.get("need_unlock")):
        raise PendingQuizError("所选章节尚未开放，待处理记录已保留。")

    class Cancellation:
        def is_set(self):
            return cancelled is not None and cancelled()
    try:
        entries = read_chapter_jobs(cx, course, point, Cancellation())
    except Exception:
        check_cancelled()
        raise PendingQuizError("所选章节的任务信息未读取完整，请重新读取题目；待处理记录已保留。") from None
    quizzes = []
    for job, info in entries:
        if job.get("type") == "manual" and job.get("kind") == "workid":
            raise PendingQuizError("测验信息未读取完整，请重新读取题目；待处理记录已保留。")
        if job.get("type") != "workid":
            continue
        if any(not isinstance(value, str) or not value.strip() for value in (
                job.get("jobid"), job.get("enc"), info.get("ktoken"))):
            raise PendingQuizError("测验参数未读取完整，请重新读取题目；待处理记录已保留。")
        quizzes.append((job, info))
    if not quizzes:
        raise PendingQuizError("所选章节未读取到可作答的测验，待处理记录已保留。")
    return course, point, quizzes


def _resume_rows(data):
    rows, courses = data.get("pending"), data.get("courses")
    if data.get("version") != 1 or not isinstance(rows, list) or not isinstance(courses, list) \
            or any(not isinstance(row, dict) or not all(task_key(row)) for row in rows) \
            or any(not isinstance(course, dict) or not all(_identifier(course.get(name))
                   for name in ("course_id", "class_id")) for course in courses):
        raise PendingQuizError("续接记录格式异常，原待处理记录已保留。")
    return rows


def _stage_bytes(path, content):
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
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _write_confirmed(records):
    staged, replaced = [], []
    try:
        for path, old_bytes, new_data in records:
            content = json.dumps(new_data, ensure_ascii=False, indent=2).encode("utf-8")
            staged.append((path, old_bytes, _stage_bytes(path, content)))
        for path, old_bytes, temporary in staged:
            os.replace(temporary, path)
            replaced.append((path, old_bytes))
    except Exception:
        for path, old_bytes in reversed(replaced):
            restore = None
            try:
                restore = _stage_bytes(path, old_bytes)
                os.replace(restore, path)
            except OSError:
                pass
            finally:
                if restore is not None:
                    try:
                        restore.unlink(missing_ok=True)
                    except OSError:
                        pass
        raise PendingQuizError("保存章节确认结果失败，请保留记录并稍后重新检查。") from None
    finally:
        for _, _, temporary in staged:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def confirm_selected_task(cx, profile, course, point):
    """Delete only this key after fresh server confirmation; retain every other row."""
    key = (_identifier(course.get("courseId")), _identifier(course.get("clazzId")),
           _identifier(point.get("id")))
    if not all(key) or not _identifier(course.get("cpi")):
        raise PendingQuizError("所选章节信息不完整，待处理记录已保留。")
    latest = _fresh_point(cx, course, key[2])
    if not flag_is_true(latest.get("has_finished")):
        return False
    try:
        records = confirmation_records(profile, key)
    except (TaskRepositoryError, OSError) as error:
        raise PendingQuizError(str(error) if isinstance(error, TaskRepositoryError) else
                               "任务记录无法读取，原记录已保留。") from None
    _write_confirmed(records)
    point.update(latest)
    return True
