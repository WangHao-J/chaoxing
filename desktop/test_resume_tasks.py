"""Offline restart regressions with persisted files and fresh synthetic server state."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_runtime_safety as fixture
from resume_tasks import ResumeStore, ResumeError, task_key
from run_account import configure
from runtime_safety import SafetyCoordinator, make_processor, RunNeedsAttention, AccessRestricted


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resume-test-", dir=fixture.ROOT / "temp")
        self.addCleanup(self.temp.cleanup)
        self.profile = SimpleNamespace(directory=Path(self.temp.name), name="续接测试")
        self.store = ResumeStore(self.profile)
        self.courses = [{"courseId": "100", "clazzId": "1", "cpi": "cpi", "title": "课程一"},
                        {"courseId": "100", "clazzId": "2", "cpi": "cpi2", "title": "课程一另一班"},
                        {"courseId": "200", "clazzId": "3", "cpi": "cpi3", "title": "课程二"}]

    def main(self):
        return SimpleNamespace(init_config=Mock(), init_chaoxing=Mock(), InputFormatError=ValueError)

    def task(self, chapter, course=0, finished=False):
        return fixture.main.ChapterTask(index=chapter, course=self.courses[course],
            point={"id": str(chapter), "title": f"章节{chapter}", "has_finished": finished})

    def row(self, task):
        course, clazz, chapter = task_key(task)
        return {"course_id": course, "class_id": clazz, "chapter_id": chapter,
                "course_title": task.course["title"], "chapter_title": task.point["title"], "enc": "not-to-be-saved"}

    def test_restart_recovers_explicit_course_pairs_without_prompt_or_scope_expansion(self):
        first = self.main()
        configure(first, self.profile, False)
        with patch("builtins.input", return_value="100"), patch("builtins.print"):
            chosen = first.filter_courses(self.courses, None)
        self.assertEqual(chosen, self.courses[:2])
        second = self.main()
        configure(second, self.profile, False)
        with patch("builtins.input") as prompt, patch("builtins.print"):
            result = second.filter_courses([*self.courses, {"courseId": "100", "clazzId": "new"}], None)
        prompt.assert_not_called()
        self.assertEqual(result, self.courses[:2])

    def test_old_pending_file_migrates_without_reselecting_courses(self):
        rows = [self.row(self.task(8)), self.row(self.task(9, 2))]
        (self.profile.directory / "pending_tasks.json").write_text(json.dumps({"tasks": rows}), encoding="utf-8")
        main = self.main()
        configure(main, self.profile, False)
        with patch("builtins.input") as prompt, patch("builtins.print"):
            self.assertEqual(main.filter_courses(self.courses, None), [self.courses[0], self.courses[2]])
        prompt.assert_not_called()
        self.assertEqual([row["chapter_id"] for row in self.store.read()["pending"]], ["8", "9"])
        self.assertNotIn("not-to-be-saved", self.store.path.read_text(encoding="utf-8"))

    def test_order_preserves_directory_sequence_and_filters_server_completed(self):
        self.store.save_selection(self.courses[:1])
        self.store.remember([self.row(self.task(7)), self.row(self.task(8)), self.row(self.task(2))])
        fresh = [self.task(0, finished=True), self.task(2), self.task(7), self.task(8, finished=True), self.task(9)]
        ordered = ResumeStore(self.profile).order(fresh)
        self.assertEqual([task.point["id"] for task in ordered], ["2", "7", "9"])
        self.assertIs(ordered[0], fresh[1])

    def test_local_success_never_overrides_fresh_server_unfinished_status(self):
        self.store.save_selection(self.courses[:1])
        self.store.remember([self.row(self.task(7))])
        self.assertEqual([task.point["id"] for task in self.store.order([self.task(1), self.task(7)])], ["1", "7"])

    def test_explicit_server_false_string_is_unfinished_even_without_checkpoint(self):
        ordered = self.store.order([self.task(1, finished="false"), self.task(2, finished="true")])
        self.assertEqual([item.point["id"] for item in ordered], ["1"])

    def test_choose_courses_requires_new_selection_and_clears_old_queue_only_after_valid_input(self):
        self.store.save_selection(self.courses[:1])
        self.store.remember([self.row(self.task(7))])
        original = self.store.path.read_bytes()
        main = self.main()
        configure(main, self.profile, False, True)
        with patch("builtins.input", return_value="missing"), patch("builtins.print"), self.assertRaises(ValueError):
            main.filter_courses(self.courses, ["100"])
        self.assertEqual(self.store.path.read_bytes(), original)
        with patch("builtins.input", return_value="200"), patch("builtins.print"):
            self.assertEqual(main.filter_courses(self.courses, ["100"]), self.courses[2:])
        self.assertEqual(self.store.read()["pending"], [])

    def test_completed_session_keeps_selection_for_next_run(self):
        self.store.save_selection(self.courses[:1])
        self.store.remember([])
        self.assertEqual(ResumeStore(self.profile).select(self.courses), self.courses[:1])

    def test_missing_saved_class_stops_instead_of_using_another_class(self):
        self.store.save_selection(self.courses[:1])
        with self.assertRaises(ResumeError):
            self.store.select(self.courses[1:])

    def test_explicit_config_overrides_last_selection(self):
        self.store.save_selection(self.courses[:1])
        main = self.main()
        configure(main, self.profile, False)
        with patch("builtins.input") as prompt:
            self.assertEqual(main.filter_courses(self.courses, ["200"]), self.courses[2:])
        prompt.assert_not_called()

    def test_star_is_saved_as_exact_pairs_and_does_not_select_future_courses(self):
        main = self.main()
        configure(main, self.profile, False)
        main.filter_courses(self.courses, ["*"])
        self.assertEqual(self.store.select(self.courses + [{"courseId": "new", "clazzId": "new"}]), self.courses)

    def test_profiles_do_not_share_selection_or_queue(self):
        self.store.save_selection(self.courses[:1])
        other = self.profile.directory / "other"
        other.mkdir()
        store = ResumeStore(SimpleNamespace(directory=other))
        self.assertIsNone(store.select(self.courses))
        store.save_selection(self.courses[2:])
        self.assertEqual(self.store.select(self.courses), self.courses[:1])

    def test_corrupt_records_are_preserved_and_explicit_reselection_can_recover(self):
        for data in ('broken', '{"version":1,"courses":[{}],"pending":[]}'):
            self.store.path.write_text(data, encoding="utf-8")
            with self.assertRaises(ResumeError):
                self.store.select(self.courses)
            self.assertEqual(self.store.path.read_text(encoding="utf-8"), data)
            self.store.save_selection(self.courses[:1], reset=True)
            self.assertEqual(self.store.select(self.courses), self.courses[:1])

    def test_kernel_interrupt_restart_returns_to_earliest_unfinished_chapter(self):
        self.store.save_selection(self.courses[:1])
        self.store.remember([self.row(self.task(7)), self.row(self.task(8)), self.row(self.task(2))])
        co = SafetyCoordinator(self.profile, dialog=Mock(return_value="stop"))
        processor = make_processor(fixture.main.JobProcessor, fixture.main, co)(Mock(),
            [self.task(0, finished=True), self.task(2), self.task(7), self.task(8)],
            {"jobs": 1, "speed": 1.0, "retry_interval": 0.01})
        with patch.object(fixture.main, "process_chapter", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            processor.run()
        self.assertEqual(self.store.read()["pending"][0]["chapter_id"], "2")
        co2 = SafetyCoordinator(self.profile)
        processor2 = make_processor(fixture.main.JobProcessor, fixture.main, co2)(Mock(),
            [self.task(2), self.task(7), self.task(8, finished=True)], {"jobs": 1, "speed": 1.0})
        with patch.object(fixture.main, "process_chapter", return_value=fixture.main.ChapterResult.SUCCESS) as process:
            processor2.run()
        self.assertEqual([call.args[2]["id"] for call in process.call_args_list], ["2", "7"])
        self.assertEqual(self.store.read()["pending"], [])

    def test_missing_server_chapters_preserve_checkpoint_and_do_not_report_completion(self):
        self.store.save_selection(self.courses[:1])
        self.store.remember([self.row(self.task(7))])
        before = self.store.path.read_bytes()
        for tasks in ([], [self.task(0)]):
            with self.subTest(tasks=tasks), self.assertRaises(ResumeError):
                self.store.order(tasks)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_progress_metadata_comes_from_fresh_job_cards_without_local_time_override(self):
        # The original attachment decoder supplies server playTime in milliseconds.
        from api import decode
        cards = [{"type": "video", "job": True, "mid": "m", "jobid": "j", "objectId": "o",
                  "playTime": 39000, "property": {"name": "音视频"}}]
        job = decode._process_attachment_cards(cards)[0]
        self.assertEqual(job["playTime"], 39000)


if __name__ == "__main__":
    unittest.main()
