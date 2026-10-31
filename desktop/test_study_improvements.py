"""Behavioral regressions for organization, honest ETA and chapter barriers."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from account_groups import AccountGroups, UNGROUPED
from course_eta import CourseTimeEstimator, DurationCache
from study_reports import write_report
from test_single_workspace import make_store
from test_course_eta import cards, video, metadata, task as eta_task
import test_study_runner as runner_fixture
COURSE, POINT, job = runner_fixture.COURSE, runner_fixture.POINT, runner_fixture.job


class GroupTests(unittest.TestCase):
    def test_create_move_rename_collapse_and_delete_survive_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            groups = AccountGroups(directory)
            key = groups.create("同学")
            groups.assign("account_a", key)
            groups.rename(key, "同学一组")
            groups.collapse(key, True)
            loaded = AccountGroups(directory)
            self.assertEqual(loaded.group_for("account_a"), key)
            self.assertEqual(loaded.groups()[1]["name"], "同学一组")
            self.assertIn(key, loaded.data["collapsed"])
            loaded.remove(key)
            self.assertEqual(AccountGroups(directory).group_for("account_a"), UNGROUPED)

    def test_corrupt_category_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account_groups.json"
            path.write_text("{broken", encoding="utf-8")
            groups = AccountGroups(directory)
            self.assertTrue(groups.error)
            with self.assertRaises(ValueError):
                groups.create("new")
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_failed_atomic_save_keeps_previous_category_and_assignments(self):
        with tempfile.TemporaryDirectory() as directory:
            groups = AccountGroups(directory)
            key = groups.create("原有分类")
            before = groups.path.read_bytes()
            with patch("pathlib.Path.replace", side_effect=PermissionError()):
                with self.assertRaises(PermissionError):
                    groups.assign("account_a", key)
            self.assertEqual(groups.group_for("account_a"), UNGROUPED)
            self.assertEqual(groups.path.read_bytes(), before)


class ImprovedEtaTests(unittest.TestCase):
    def test_default_reads_sixteen_unequal_chapters_instead_of_extrapolating_five(self):
        session = Mock()
        durations = [60] + [3600] * 15
        session.get.side_effect = [reply for duration in durations for reply in
                                  (cards([video(playTime=0)]), metadata(duration))]
        estimate = CourseTimeEstimator(session, "fid").estimate(COURSE, [eta_task(str(i)) for i in range(16)])
        self.assertEqual(estimate.sampled, 16)
        self.assertEqual(estimate.media_seconds, sum(durations))
        self.assertIn("已读取全部", estimate.message(1))

    def test_duration_cache_never_reuses_another_accounts_playback_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = DurationCache(directory)
            first, second = Mock(), Mock()
            first.get.side_effect = [cards([video(playTime=120000)]), metadata(600)]
            second.get.side_effect = [cards([video(playTime=300000)])]
            one = CourseTimeEstimator(first, "fid", duration_cache=cache).estimate(COURSE, [eta_task()])
            two = CourseTimeEstimator(second, "fid", duration_cache=cache).estimate(COURSE, [eta_task()])
            self.assertEqual(one.media_seconds, 480)
            self.assertEqual(two.media_seconds, 300)
            self.assertEqual(second.get.call_count, 1)


class SequentialTests(runner_fixture.StudyFixture):
    def engine(self, mode):
        configured = patch("quiz_dialog.ai_configured", return_value=True)
        configured.start()
        self.addCleanup(configured.stop)
        return runner_fixture.StudyEngineTests.engine(self, mode)

    def test_unconfirmed_first_chapter_does_not_block_later_chapter(self):
        engine = self.engine("mixed")
        points = [{**POINT, "id": "first"}, {**POINT, "id": "second"}]
        engine.cx.get_course_point.return_value = {"points": points}
        with patch("study_runner.read_chapter_jobs", side_effect=lambda cx, course, point, stop:
                   [(job("video", point["id"]), {"knowledgeid": point["id"]})]):
            result = engine.run([("course", "class")])
        self.assertEqual(engine.main.process_job.call_count, 2)
        pending = engine.store.read()["pending"]
        self.assertEqual([row["chapter_id"] for row in pending], ["first", "second"])
        self.assertNotIn("前置章节", pending[1]["reason"])
        self.assertIn("保留待处理", result)

    def test_late_confirmation_is_polled_before_next_chapter_and_never_replays_job(self):
        engine = self.engine("mixed")
        first, second = {**POINT, "id": "first"}, {**POINT, "id": "second"}
        engine.cx.get_course_point.side_effect = [
            {"points": [first, second]}, {"points": [first, second]},
            {"points": [{**first, "has_finished": True}, second]},
            {"points": [{**first, "has_finished": True}, {**second, "has_finished": True}]}]
        with patch("study_runner.read_chapter_jobs", side_effect=lambda cx, course, point, stop:
                   [(job("video", point["id"]), {"knowledgeid": point["id"]})]):
            engine.run([("course", "class")])
        self.assertEqual([call.args[2]["jobid"] for call in engine.main.process_job.call_args_list],
                         ["synthetic-first", "synthetic-second"])
        self.assertEqual(engine.store.read()["pending"], [])

    def test_confirmed_video_can_advance_in_video_mode_while_quiz_remains_pending(self):
        engine = self.engine("videos")
        engine.cx.get_course_point.return_value = {"points": [{**POINT, "id": name} for name in ("first", "second")]}
        def states(cx, course, point, cancelled):
            return {("video", "synthetic-" + point["id"]): True, ("workid", "quiz"): False}
        with patch("study_runner.read_chapter_jobs", side_effect=lambda cx, course, point, stop:
                   [(job("video", point["id"]), {"knowledgeid": point["id"]})]), \
             patch("study_runner.read_chapter_job_states", side_effect=states):
            engine.run([("course", "class")])
        self.assertEqual(engine.main.process_job.call_count, 2)
        self.assertTrue(all("其他任务" in row["reason"] for row in engine.store.read()["pending"]))

    def test_initial_media_progress_uses_saved_position_instead_of_seeking_to_end(self):
        from api.base import Chaoxing, SessionManager
        cx = Chaoxing.__new__(Chaoxing)
        cx.get_fid = Mock(return_value="fid")
        cx.video_progress_log = Mock(return_value=(True, 200))
        session = Mock()
        session.get.return_value.json.return_value = {"status": "success", "duration": 60,
            "dtoken": "test", "crc": "test", "key": "test"}
        with patch.object(SessionManager, "get_session", return_value=session):
            cx.study_video(COURSE, {"objectid": "object", "name": "视频", "playTime": 12000}, {})
        self.assertEqual(cx.video_progress_log.call_args.args[6], 12)
        self.assertEqual(cx.video_progress_log.call_args.kwargs["_isdrag"], 0)


class ReportTests(unittest.TestCase):
    def test_reports_are_separate_by_account_group_causes_and_hide_signed_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            a, b = store.create("A"), store.create("B")
            pending = {"version": 1, "mode": "mixed", "courses": [], "pending": [
                {"course_id": "course", "class_id": "class", "chapter_id": "one", "course_title": "课程",
                 "chapter_title": "第一章", "reason": "平台尚未确认完成 https://example.com/?enc=secret-token"},
                {"course_id": "course", "class_id": "class", "chapter_id": "two", "course_title": "课程",
                 "chapter_title": "第二章", "reason": "前置章节未完成，未执行"}]}
            (a.directory / "study_state_mixed.json").write_text(json.dumps(pending), encoding="utf-8")
            path_a, path_b = write_report(store.root, a), write_report(store.root, b)
            self.assertNotEqual(path_a.parent, path_b.parent)
            text = path_a.read_text(encoding="utf-8")
            self.assertIn("等待平台确认", text)
            self.assertIn("前置章节阻塞", text)
            self.assertNotIn("secret-token", text)
            write_report(store.root, a)
            self.assertEqual(len(list((path_a.parent / "history").glob("*.md"))), 2)
            self.assertEqual(json.loads((a.directory / "study_state_mixed.json").read_text()), pending)


if __name__ == "__main__":
    unittest.main()
