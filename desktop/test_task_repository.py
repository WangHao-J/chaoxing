"""Offline queue unification, exact deletion, recovery, and rollback regressions."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from account_store import AlreadyRunning, ProfileLock
from task_repository import (TaskRepositoryError, clear_confirmed, confirmation_records,
                             list_pending, recover_mode, write_confirmed)


ROW = {"course_id": "c", "class_id": "cl", "chapter_id": "p", "course_title": "课程",
       "chapter_title": "章节", "reason": "待处理", "unknown": {"preserve": True}}


class TaskRepositoryTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).parent / "temp"
        temp_root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="task-repo-test-", dir=temp_root)
        self.addCleanup(temporary.cleanup)
        self.profile = SimpleNamespace(directory=Path(temporary.name))
        self.other = [{**ROW, "chapter_id": "different"}, {**ROW, "class_id": "different"},
                      {**ROW, "course_id": "different"}]
        self.write("pending_tasks.json", {"tasks": [ROW, *self.other], "extra": ["preserve"]})
        self.write("resume_state.json", {"version": 1, "courses": [{"course_id": "c", "class_id": "cl"}],
                                         "pending": [ROW, *self.other], "extra": "keep"})
        for mode in ("quizzes", "videos", "mixed"):
            self.write(f"study_state_{mode}.json", {"version": 1, "mode": mode,
                "courses": [{"course_id": "c", "class_id": "cl", "extra": "keep"}],
                "pending": [{**ROW, "reason": mode}, *self.other], "extra": mode})
        self.write("ai_quiz.json", {"protected": "synthetic-key"})
        self.write("quiz_submissions.json", {"ledger": "synthetic-submitted"})
        self.write("cookies.txt", {"cookie": "synthetic-cookie"})

    def write(self, name, data):
        (self.profile.directory / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def read(self, name):
        return json.loads((self.profile.directory / name).read_text(encoding="utf-8"))

    def snapshot(self):
        return {path.name: path.read_bytes() for path in self.profile.directory.iterdir()
                if path.is_file() and path.name != ".run.lock"}

    def test_all_queues_are_merged_without_expanding_course_or_class_scope(self):
        before = self.snapshot()
        rows = list_pending(self.profile)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["sources"], ["legacy", "quizzes", "videos", "mixed"])
        self.assertEqual(rows[0]["reason"], "待处理；quizzes；videos；mixed")
        self.assertEqual(rows[0]["unknown"], ROW["unknown"])
        self.assertEqual(self.snapshot(), before)

    def test_broken_queue_is_reported_without_hiding_valid_tasks(self):
        (self.profile.directory / "study_state_videos.json").write_bytes(b"{broken")
        rows = list_pending(self.profile)
        self.assertEqual(len([row for row in rows if row.get("record_error")]), 1)
        self.assertEqual(rows[0]["sources"], ["legacy", "quizzes", "mixed"])
        self.assertIn("videos", rows[-1]["sources"] if rows[-1].get("record_error") else
                      next(row["sources"] for row in rows if row.get("record_error")))

    def test_incomplete_legacy_rows_stay_visible_and_are_not_deduplicated_by_guessed_ids(self):
        self.write("pending_tasks.json", {"tasks": [{"chapter_title": "旧条目"}, {"chapter_title": "旧条目"}]})
        self.assertEqual(len([row for row in list_pending(self.profile) if not row.get("course_id")]), 2)

    def test_confirmed_key_is_removed_from_all_five_queues_preserving_every_unknown_field(self):
        before = self.snapshot()
        clear_confirmed(self.profile, ("c", "cl", "p"))
        self.assertEqual(self.read("pending_tasks.json"), {"tasks": self.other, "extra": ["preserve"]})
        self.assertEqual(self.read("resume_state.json")["pending"], self.other)
        for mode in ("quizzes", "videos", "mixed"):
            data = self.read(f"study_state_{mode}.json")
            self.assertEqual(data["pending"], self.other)
            self.assertEqual(data["extra"], mode)
            self.assertEqual(data["courses"][0]["extra"], "keep")
        for name in ("ai_quiz.json", "quiz_submissions.json", "cookies.txt"):
            self.assertEqual((self.profile.directory / name).read_bytes(), before[name])

    def test_mode_only_task_can_confirm_when_original_resume_has_no_corresponding_key(self):
        self.write("pending_tasks.json", {"tasks": self.other})
        self.write("resume_state.json", {"version": 1, "courses": [], "pending": []})
        before_resume = (self.profile.directory / "resume_state.json").read_bytes()
        write_confirmed(confirmation_records(self.profile, ("c", "cl", "p")))
        self.assertEqual((self.profile.directory / "resume_state.json").read_bytes(), before_resume)
        self.assertEqual(len(list_pending(self.profile)), 3)

    def test_unrelated_source_file_is_byte_exact_when_it_has_no_matching_key(self):
        self.write("study_state_videos.json", {"version": 1, "mode": "videos", "courses": [], "pending": self.other})
        before = (self.profile.directory / "study_state_videos.json").read_bytes()
        clear_confirmed(self.profile, ("c", "cl", "p"))
        self.assertEqual((self.profile.directory / "study_state_videos.json").read_bytes(), before)

    def test_corrupt_or_inconsistent_original_resume_blocks_every_deletion(self):
        for data in ({"version": 4}, {"version": 1, "courses": [], "pending": []}):
            self.write("resume_state.json", data)
            before = self.snapshot()
            with self.assertRaises(TaskRepositoryError):
                clear_confirmed(self.profile, ("c", "cl", "p"))
            self.assertEqual(self.snapshot(), before)

    def test_failed_fourth_replace_restores_all_preceding_files_byte_exact(self):
        before = self.snapshot()
        original = os.replace
        calls = []
        def replace(source, destination):
            calls.append(Path(destination).name)
            if len(calls) == 4:
                raise OSError("synthetic failure")
            return original(source, destination)
        with patch("task_repository.os.replace", side_effect=replace), self.assertRaises(TaskRepositoryError):
            clear_confirmed(self.profile, ("c", "cl", "p"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(calls), 7)

    def test_recover_backs_up_exact_corrupt_file_and_changes_no_credentials_or_other_checkpoints(self):
        path = self.profile.directory / "study_state_quizzes.json"
        path.write_bytes(b"{broken\xff")
        before = self.snapshot()
        backup = recover_mode(self.profile, "quizzes")
        self.assertEqual(backup.read_bytes(), before[path.name])
        self.assertEqual(self.read(path.name), {"version": 1, "mode": "quizzes", "courses": [], "pending": []})
        after = self.snapshot()
        for name, content in before.items():
            if name != path.name:
                self.assertEqual(after[name], content)

    def test_recovery_while_account_active_cannot_touch_any_checkpoint(self):
        with ProfileLock(self.profile.directory):
            before = self.snapshot()
            with self.assertRaises(AlreadyRunning):
                recover_mode(self.profile, "videos")
            self.assertEqual(self.snapshot(), before)

    def test_unknown_mode_cannot_choose_or_overwrite_arbitrary_file(self):
        before = self.snapshot()
        for mode in ("../ai_quiz", "legacy", "resume_state", ""):
            with self.subTest(mode=mode), self.assertRaises(TaskRepositoryError):
                recover_mode(self.profile, mode)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
