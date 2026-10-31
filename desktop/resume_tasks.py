"""Persist exact course/class selections and restart ordering without inventing progress."""
import json
from datetime import datetime, timezone
from card_reader import flag_is_true


class ResumeError(ValueError):
    pass


def course_key(course):
    return str(course.get("courseId", "")), str(course.get("clazzId", ""))


def task_key(task):
    return (*course_key(task.course), str(task.point.get("id", "")))


def row_key(row):
    return str(row.get("course_id", "")), str(row.get("class_id", "")), str(row.get("chapter_id", ""))


class ResumeStore:
    def __init__(self, profile):
        self.profile = profile
        self.path = profile.directory / "resume_state.json"

    def read(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("courses"), list) \
                        or not isinstance(data.get("pending"), list):
                    raise ValueError()
                if any(not isinstance(row, dict) or not all(isinstance(row.get(name), str) and row[name]
                        for name in ("course_id", "class_id")) for row in data["courses"]):
                    raise ValueError()
                if any(not isinstance(row, dict) or not all(isinstance(row.get(name), str) and row[name]
                        for name in ("course_id", "class_id", "chapter_id")) for row in data["pending"]):
                    raise ValueError()
                return data
            except (OSError, ValueError, TypeError):
                raise ResumeError("续接记录无法读取，请保留原文件，在管理界面点击“重新选课”。") from None
        legacy = self.profile.directory / "pending_tasks.json"
        if not legacy.exists():
            return {"version": 1, "courses": [], "pending": []}
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            pending = data["tasks"]
            if not isinstance(pending, list) or any(not isinstance(row, dict) for row in pending):
                raise ValueError()
            # Older pending files may contain incomplete setup records; these cannot select a course.
            pending = [{name: str(row.get(name, "")) for name in
                       ("course_id", "class_id", "chapter_id", "course_title", "chapter_title")}
                       for row in pending if all(row.get(name) for name in ("course_id", "class_id", "chapter_id"))]
            courses = {}
            for row in pending:
                courses.setdefault((row["course_id"], row["class_id"]),
                    {"course_id": row["course_id"], "class_id": row["class_id"], "title": row["course_title"]})
            return {"version": 1, "courses": list(courses.values()), "pending": pending}
        except (OSError, ValueError, KeyError, TypeError):
            raise ResumeError("旧版待处理记录无法读取，请在管理界面点击“重新选课”；已上传的进度仍由学习通保存。") from None

    def write(self, data):
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def select(self, courses):
        saved = self.read()["courses"]
        if not saved:
            return None
        available = {course_key(course): course for course in courses}
        keys = [(row["course_id"], row["class_id"]) for row in saved]
        if any(key not in available for key in keys):
            raise ResumeError("上次选择的课程或班级已不在当前账户列表中，请点击“重新选课”；不会自动扩大课程范围。")
        return [available[key] for key in dict.fromkeys(keys)]

    def save_selection(self, courses, reset=False):
        data = {"version": 1, "courses": [], "pending": []} if reset else self.read()
        previous = {(row["course_id"], row["class_id"]) for row in data["courses"]}
        chosen = {course_key(course) for course in courses}
        if previous != chosen:
            data["pending"] = []
        data["courses"] = [{"course_id": course_key(course)[0], "class_id": course_key(course)[1],
                            "title": str(course.get("title", ""))} for course in courses]
        self.write(data)

    def remember(self, rows):
        data = self.read()
        data["pending"] = [{name: str(row.get(name, "")) for name in
                            ("course_id", "class_id", "chapter_id", "course_title", "chapter_title")}
                           for row in rows]
        if not data["courses"]:
            unique = {}
            for row in data["pending"]:
                unique.setdefault((row["course_id"], row["class_id"]),
                    {"course_id": row["course_id"], "class_id": row["class_id"], "title": row["course_title"]})
            data["courses"] = list(unique.values())
        self.write(data)

    def order(self, tasks):
        rank = {}
        for row in self.read()["pending"]:
            rank.setdefault(row_key(row), len(rank))
        available = {task_key(task) for task in tasks}
        if set(rank) - available:
            raise ResumeError("上次待处理的部分章节未出现在平台返回的目录中，请核对课程权限或点击“重新选课”。原记录已保留。")
        # Refresh all task metadata from the server first. Local success never overrides the server.
        return [task for task in tasks if not flag_is_true(task.point.get("has_finished", False))]
