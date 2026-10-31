"""Offline regressions for independent video/quiz windows and scoped checkpoints."""
import json
import tempfile
import threading
import unittest
from quiz_assistant import QuizError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import test_runtime_safety as fixture
from account_store import AlreadyRunning, ProfileLock
from api import decode
from resume_tasks import task_key
from runtime_safety import AccessRestricted, SafetyCoordinator
from study_runner import (MODE_LABELS, CancellableClock, ModeStore, StudyCancelled, StudyEngine,
                          StudyError, read_chapter_jobs)
from test_quiz_assistant import SETTINGS


COURSE = {"courseId": "course", "clazzId": "class", "cpi": "fresh-cpi", "title": "测试课程"}
POINT = {"id": "chapter", "title": "混合任务章节", "has_finished": False,
         "need_unlock": False, "jobCount": 2}


def page(cards=(), **defaults):
    info = {"knowledgeid": "chapter", "cpi": "fresh-cpi", "ktoken": "synthetic-ktoken", **defaults}
    return "<script>var mArg = " + json.dumps({"attachments": list(cards), "defaults": info}) + ";</script>"


def job(kind, identity=None):
    identity = identity or kind
    return {"type": kind, "job": True, "jobid": "synthetic-" + identity,
            "mid": "synthetic-mid-" + identity, "objectid": "synthetic-object-" + identity,
            "name": "测试任务 " + identity, "enc": "synthetic-enc", "property": {}}


def task(chapter="chapter", *, course=None, index=0, finished=False):
    return SimpleNamespace(course=dict(course or COURSE), index=index,
                           point={**POINT, "id": str(chapter), "has_finished": finished})


class StudyFixture(unittest.TestCase):
    def setUp(self):
        directory = fixture.ROOT / "temp"
        directory.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="study-mode-test-", dir=directory)
        self.addCleanup(temporary.cleanup)
        self.profile = SimpleNamespace(directory=Path(temporary.name), id="test", name="独立模式测试")
        self.original_files = {
            "pending_tasks.json": b'{"tasks": [{"chapter_id": "mixed-only"}], "extra": 1}',
            "resume_state.json": b'{"version": 1, "courses": [], "pending": [], "extra": 2}',
            "cookies.txt": b"synthetic-cookie-preserve",
            "ai_quiz.json": b'{"synthetic": "preserve-ai-settings"}',
        }
        for name, content in self.original_files.items():
            (self.profile.directory / name).write_bytes(content)

    def assert_original_preserved(self):
        for name, content in self.original_files.items():
            with self.subTest(file=name):
                self.assertEqual((self.profile.directory / name).read_bytes(), content)


class ReadChapterJobsTests(StudyFixture):
    def setUp(self):
        super().setUp()
        self.cx = Mock()
        self.session = Mock()
        self.cancelled = threading.Event()
        processor = patch.object(decode, "_process_attachment_cards", side_effect=lambda cards: list(cards))
        self.process_cards = processor.start()
        self.addCleanup(processor.stop)

    def pages(self, *texts):
        self.session.get.side_effect = [fixture.response(200, text)
                                        for text in [*texts, *([page()] * (7 - len(texts)))]]

    def read(self, point=None):
        return read_chapter_jobs(self.cx, dict(COURSE), dict(point or POINT),
                                 self.cancelled, session=self.session)

    def assert_read_only(self):
        self.cx.get_job_list.assert_not_called()
        self.cx.study_emptypage.assert_not_called()
        self.cx.study_video.assert_not_called()
        self.cx.study_work.assert_not_called()
        self.session.post.assert_not_called()
        self.assert_original_preserved()

    def test_reads_all_seven_card_slots_with_exact_scope_and_no_progress_side_effects(self):
        self.pages(page([job("video")]), page([job("workid")]))
        result = self.read()
        self.assertEqual([entry[0]["type"] for entry in result], ["video", "workid"])
        self.assertEqual(self.session.get.call_count, 7)
        for number, call in enumerate(self.session.get.call_args_list):
            self.assertEqual(call.args[0], "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards")
            self.assertEqual({key: str(call.kwargs["params"][key])
                              for key in ("courseid", "clazzid", "knowledgeid", "cpi", "num")},
                             {"courseid": "course", "clazzid": "class", "knowledgeid": "chapter",
                              "cpi": "fresh-cpi", "num": str(number)})
            self.assertIn("timeout", call.kwargs)
        self.assert_read_only()

    def test_each_job_retains_its_own_card_token_and_metadata(self):
        self.pages(page([job("workid", "one")], ktoken="token-first", cardid="card-first"),
                   page([job("workid", "two")], ktoken="token-second", cardid="card-second"))
        result = self.read()
        self.assertEqual([(item[0]["jobid"], item[1]["ktoken"], item[1]["cardid"]) for item in result],
                         [("synthetic-one", "token-first", "card-first"),
                          ("synthetic-two", "token-second", "card-second")])
        self.assertIsNot(result[0][1], result[1][1])
        self.assert_read_only()

    def test_official_empty_initialization_before_real_cards_is_accepted(self):
        self.pages('<script>var mArg="";</script>' + page([job("video")]) +
                   '<script>mArg = $.parseJSON(mArg);</script>')
        self.assertEqual([entry[0]["type"] for entry in self.read()], ["video"])
        self.assert_read_only()

    def test_unused_template_slots_after_valid_cards_are_not_false_unread_errors(self):
        unused = '<script>var mArg=""; if(typeof mArg=="string"){mArg = $.parseJSON(mArg);}</script>'
        self.pages(page([job("video")]), *([unused] * 6))
        self.assertEqual(len(self.read()), 1)
        self.assert_read_only()

    def test_malformed_real_card_after_valid_card_cannot_be_treated_as_empty(self):
        self.pages(page([job("video")]), '<script>mArg=""; mArg={broken};</script>')
        with self.assertRaises(StudyError):
            self.read()
        self.assert_read_only()

    def test_html_login_page_is_not_an_empty_chapter(self):
        self.pages('<html><form action="/login">请重新登录</form></html>')
        with self.assertRaises(StudyError):
            self.read()
        self.assert_read_only()

    def test_mismatched_chapter_or_class_metadata_never_reaches_processing(self):
        for changes in ({"knowledgeid": "wrong-chapter"}, {"cpi": "wrong-cpi"}):
            self.pages(page([job("workid")], **changes))
            with self.subTest(changes=changes), self.assertRaises(StudyError):
                self.read()
        self.assert_read_only()

    def test_missing_chapter_or_class_metadata_never_reaches_processing(self):
        for changes in ({"knowledgeid": ""}, {"cpi": ""}):
            self.pages(page([job("workid")], **changes))
            with self.subTest(changes=changes), self.assertRaises(StudyError):
                self.read()
        self.assert_read_only()

    def test_duplicate_quiz_with_conflicting_enc_or_card_token_is_rejected(self):
        for changed_job, changed_info in (({**job("workid"), "enc": "conflicting-enc"}, {}),
                                          (job("workid"), {"ktoken": "conflicting-ktoken"})):
            self.pages(page([job("workid")]), page([changed_job], **changed_info))
            with self.subTest(job=changed_job, info=changed_info), self.assertRaises(StudyError):
                self.read()
        self.assert_read_only()

    def test_access_restriction_propagates_without_becoming_no_tasks(self):
        issue = AccessRestricted(403)
        self.session.get.side_effect = issue
        with self.assertRaises(AccessRestricted) as error:
            self.read()
        self.assertIs(error.exception, issue)
        self.assertEqual(self.session.get.call_count, 1)
        self.assert_read_only()

    def test_initial_cancellation_prevents_any_card_request(self):
        self.cancelled.set()
        with self.assertRaises(StudyCancelled):
            self.read()
        self.session.get.assert_not_called()
        self.assert_read_only()

    def test_cancellation_during_read_prevents_later_pages_and_tasks(self):
        def first(*args, **kwargs):
            self.cancelled.set()
            return fixture.response(200, page([job("video")]))
        self.session.get.side_effect = first
        with self.assertRaises(StudyCancelled):
            self.read()
        self.assertEqual(self.session.get.call_count, 1)
        self.assert_read_only()

    def test_http_error_does_not_leak_private_response(self):
        self.session.get.side_effect = None
        self.session.get.return_value = fixture.response(500, "synthetic-private-token")
        with self.assertRaises((StudyError, requests.RequestException)) as error:
            self.read()
        self.assertNotIn("synthetic-private-token", str(error.exception))
        self.assert_read_only()


class ModeStoreTests(StudyFixture):
    def test_modes_start_empty_and_never_migrate_mixed_resume_or_pending(self):
        self.assertEqual(set(MODE_LABELS), {"quizzes", "videos", "mixed"})
        for mode in MODE_LABELS:
            with self.subTest(mode=mode):
                store = ModeStore(self.profile, mode)
                self.assertEqual(store.saved_selection(), [])
                self.assertEqual(store.read(), {"version": 1, "mode": mode, "courses": [], "pending": []})
        self.assert_original_preserved()

    def test_video_and_quiz_selections_and_pending_queues_are_isolated(self):
        other = {**COURSE, "clazzId": "other-class"}
        quizzes, videos = ModeStore(self.profile, "quizzes"), ModeStore(self.profile, "videos")
        quizzes.save_selection([COURSE])
        videos.save_selection([other])
        quizzes.update([task("quiz-chapter")])
        videos.update([task("video-chapter", course=other)])
        self.assertEqual(ModeStore(self.profile, "quizzes").saved_selection(), [("course", "class")])
        self.assertEqual(ModeStore(self.profile, "videos").saved_selection(), [("course", "other-class")])
        self.assertEqual([row["chapter_id"] for row in quizzes.read()["pending"]], ["quiz-chapter"])
        self.assertEqual([row["chapter_id"] for row in videos.read()["pending"]], ["video-chapter"])
        self.assertTrue((self.profile.directory / "study_state_quizzes.json").is_file())
        self.assertTrue((self.profile.directory / "study_state_videos.json").is_file())
        self.assert_original_preserved()

    def test_restart_keeps_directory_order_despite_rotated_pending_rows(self):
        first = task("same", index=0)
        second = task("same", course={**COURSE, "clazzId": "other-class"}, index=1)
        third = task("later", index=2)
        store = ModeStore(self.profile, "videos")
        store.save_selection([COURSE, second.course])
        store.update([second, first])
        ordered = ModeStore(self.profile, "videos").plan([first, second, third])
        self.assertEqual([task_key(item) for item in ordered],
                         [task_key(first), task_key(second), task_key(third)])
        self.assert_original_preserved()

    def test_server_string_false_does_not_skip_unfinished_chapters(self):
        first, second = task("one", finished="false"), task("two", index=1, finished="true")
        self.assertEqual([task_key(item) for item in ModeStore(self.profile, "quizzes").plan([first, second])],
                         [task_key(first)])

    def test_checkpoint_later_chapter_does_not_hide_earlier_unrecorded_gaps(self):
        for mode in MODE_LABELS:
            with self.subTest(mode=mode):
                store = ModeStore(self.profile, mode)
                store.save_selection([COURSE])
                store.update([task("third")])
                points = [task("first"), task("second", finished=True), task("third")]
                self.assertEqual([item.point["id"] for item in store.plan(points)], ["first", "third"])

    def test_course_directory_groups_stay_together_despite_interleaved_checkpoint(self):
        other = {**COURSE, "clazzId": "another-class"}
        tasks = [task("first"), task("second"), task("first", course=other), task("second", course=other)]
        store = ModeStore(self.profile, "videos")
        store.save_selection([COURSE, other])
        store.update([tasks[3], tasks[1], tasks[2], tasks[0]])
        self.assertEqual([task_key(item) for item in store.plan(tasks)], [task_key(item) for item in tasks])

    def test_missing_old_chapter_stays_pending_without_blocking_valid_chapters(self):
        store = ModeStore(self.profile, "quizzes")
        store.save_selection([COURSE])
        missing, valid = task("removed"), task("valid", index=1)
        store.update([missing, valid])
        before = store.read()
        before["pending"][0]["custom"] = {"preserve": True}
        store.write(before)
        planned = store.plan([valid])
        self.assertEqual(planned, [valid])
        self.assertEqual(store.unavailable, {task_key(missing)})
        store.update(planned)
        self.assertEqual(store.read()["pending"][0], before["pending"][0])
        store.update([])
        self.assertEqual(store.read()["pending"], [before["pending"][0]])
        self.assert_original_preserved()

    def test_checkpoint_updates_keep_unknown_fields_on_remaining_rows(self):
        store = ModeStore(self.profile, "videos")
        store.save_selection([COURSE])
        current = task()
        store.update([current])
        data = store.read()
        data["pending"][0]["custom"] = ["keep"]
        data["custom_root"] = {"keep": True}
        store.write(data)
        store.update([current], {task_key(current): "新原因"})
        self.assertEqual(store.read()["pending"][0]["custom"], ["keep"])
        self.assertEqual(store.read()["pending"][0]["reason"], "新原因")
        self.assertEqual(store.read()["custom_root"], {"keep": True})
        self.assert_original_preserved()

    def test_checkpoint_records_identifiers_and_reason_but_never_live_tokens(self):
        current = task()
        current.course.update(enc="synthetic-auth-enc", ktoken="synthetic-auth-ktoken")
        current.point.update(dtoken="synthetic-auth-dtoken", cookies="synthetic-auth-cookie")
        store = ModeStore(self.profile, "quizzes")
        store.save_selection([current.course])
        store.update([current], {task_key(current): "平台尚未确认"})
        data = store.read()
        self.assertEqual(data["pending"][0]["chapter_id"], "chapter")
        content = (self.profile.directory / "study_state_quizzes.json").read_text(encoding="utf-8")
        self.assertIn("平台尚未确认", content)
        self.assertNotIn("synthetic-auth-", content)
        self.assert_original_preserved()

    def test_corrupt_mode_file_is_not_replaced_or_migrated_silently(self):
        path = self.profile.directory / "study_state_videos.json"
        path.write_bytes(b"{broken-synthetic-state")
        with self.assertRaises(StudyError):
            ModeStore(self.profile, "videos").read()
        self.assertEqual(path.read_bytes(), b"{broken-synthetic-state")
        self.assert_original_preserved()

    def test_clearing_one_mode_pending_keeps_other_modes_and_original_files(self):
        videos, quizzes = ModeStore(self.profile, "videos"), ModeStore(self.profile, "quizzes")
        for store in (videos, quizzes):
            store.save_selection([COURSE])
            store.update([task()])
        before = (self.profile.directory / "study_state_quizzes.json").read_bytes()
        videos.update([])
        self.assertEqual(videos.read()["pending"], [])
        self.assertEqual(videos.saved_selection(), [("course", "class")])
        self.assertEqual((self.profile.directory / "study_state_quizzes.json").read_bytes(), before)
        self.assert_original_preserved()


class StudyEngineTests(StudyFixture):
    def engine(self, mode):
        cancelled = threading.Event()
        engine = StudyEngine(fixture.ROOT, self.profile, mode, Mock(), Mock(), cancelled)
        engine.confirmation_delays = (0, 0, 0)
        engine.max_restart_rounds = 0
        engine.network_retry_delays = (0, 0, 0)
        status_reader = patch("study_runner.read_chapter_job_states", return_value={})
        status_reader.start()
        self.addCleanup(status_reader.stop)
        self.addCleanup(engine.close)
        engine.main = SimpleNamespace(process_job=Mock(return_value=fixture.main.StudyResult.SUCCESS),
                                      process_chapter=Mock(), JobProcessor=Mock(), main=Mock(),
                                      ChapterTask=fixture.main.ChapterTask, StudyResult=fixture.main.StudyResult,
                                      ChapterResult=fixture.main.ChapterResult, logger=Mock(),
                                      Notification=Mock())
        engine.cx = Mock()
        engine.cx.get_course_list.return_value = [dict(COURSE)]
        engine.cx.get_course_point.return_value = {"points": [dict(POINT)]}
        engine.coordinator = SafetyCoordinator(self.profile, dialog=Mock(return_value="stop"))
        engine.courses = [dict(COURSE)]
        engine._estimate_course = Mock()
        engine.lock = ProfileLock(self.profile.directory).acquire()
        return engine

    def run_with_jobs(self, engine, jobs, *, review=None, speed=1):
        info = {"knowledgeid": "chapter", "cpi": "fresh-cpi", "ktoken": "synthetic-ktoken"}
        review = review or Mock(return_value="submitted")
        with patch("study_runner.read_chapter_jobs", return_value=[(entry, dict(info)) for entry in jobs]), \
             patch("study_runner.reviewed_quiz", review, create=True), \
             patch("quiz_assistant.reviewed_quiz", review), \
             patch("quiz_assistant.read_settings", return_value=SETTINGS):
            try:
                result = engine.run([("course", "class")], speed=speed)
            except StudyCancelled:
                result = None
        return result, review

    def assert_no_mixed_scheduler(self, engine):
        engine.main.process_chapter.assert_not_called()
        engine.main.JobProcessor.assert_not_called()
        engine.main.main.assert_not_called()
        engine.cx.get_job_list.assert_not_called()
        engine.cx.study_emptypage.assert_not_called()
        engine.main.Notification.assert_not_called()
        self.assert_original_preserved()

    def test_quiz_mode_never_starts_video_read_document_or_live_work(self):
        engine = self.engine("quizzes")
        _, review = self.run_with_jobs(engine, [job(kind) for kind in ("video", "workid", "document", "read", "live")])
        self.assertEqual(review.call_count, 1)
        self.assertEqual(review.call_args.args[2]["type"], "workid")
        engine.main.process_job.assert_not_called()
        self.assert_no_mixed_scheduler(engine)

    def test_video_playback_keeps_directory_order_after_rotated_checkpoint_without_errors(self):
        engine = self.engine("videos")
        points = [{**POINT, "id": name} for name in ("first", "second", "third")]
        def confirmed_directory(*args):
            done = {call.args[2]["jobid"] for call in engine.main.process_job.call_args_list}
            return {"points": [{**point, "has_finished": "synthetic-" + point["id"] in done} for point in points]}
        engine.cx.get_course_point.side_effect = confirmed_directory
        engine.store.save_selection([COURSE])
        engine.store.update([task("third"), task("first"), task("second")])
        def read(cx, course, point, cancelled):
            return [(job("video", point["id"]), {"knowledgeid": point["id"]})]
        with patch("study_runner.read_chapter_jobs", side_effect=read):
            engine.run([("course", "class")])
        self.assertEqual([call.args[2]["jobid"] for call in engine.main.process_job.call_args_list],
                         ["synthetic-first", "synthetic-second", "synthetic-third"])
        self.assertEqual(engine.store.read()["pending"], [])
        engine.call_ui.assert_not_called()
        engine.main.logger.warning.assert_not_called()
        self.assert_no_mixed_scheduler(engine)

    def test_media_success_retains_chapter_until_platform_confirmation(self):
        engine = self.engine("videos")
        result, _ = self.run_with_jobs(engine, [job("video")])
        rows = engine.store.read()["pending"]
        self.assertEqual(len(rows), 1)
        self.assertIn("平台尚未确认", rows[0]["reason"])
        self.assertIn("保留待处理", result)
        self.assertEqual(engine.coordinator.current_status, "needs_attention")
        engine.main.process_job.assert_called_once()

    def test_late_platform_confirmation_clears_pending_without_replaying_media(self):
        engine = self.engine("videos")
        engine.cx.get_course_point.side_effect = [{"points": [dict(POINT)]},
            {"points": [dict(POINT)]}, {"points": [{**POINT, "has_finished": True}]}]
        self.run_with_jobs(engine, [job("video")])
        self.assertEqual(engine.store.read()["pending"], [])
        self.assertEqual(engine.coordinator.current_status, "completed")
        engine.main.process_job.assert_called_once()
        self.assertEqual(engine.cx.get_course_point.call_count, 3)
        engine.call_ui.assert_not_called()

    def test_failed_final_verification_never_discards_unconfirmed_media(self):
        engine = self.engine("videos")
        engine.cx.get_course_point.side_effect = [{"points": [dict(POINT)]},
            {"points": [dict(POINT)]}, requests.Timeout("synthetic private detail")]
        self.run_with_jobs(engine, [job("video")])
        self.assertEqual(len(engine.store.read()["pending"]), 1)
        self.assertEqual(engine.coordinator.current_status, "needs_attention")
        engine.main.process_job.assert_called_once()
        self.assertNotIn("synthetic private detail", str(engine.main.logger.mock_calls))

    def test_conflicting_final_directory_does_not_claim_chapter_complete(self):
        engine = self.engine("videos")
        engine.cx.get_course_point.side_effect = [{"points": [dict(POINT)]},
            {"points": [dict(POINT)]}, {"points": [dict(POINT), {**POINT, "has_finished": True}]}]
        self.run_with_jobs(engine, [job("video")])
        self.assertEqual(len(engine.store.read()["pending"]), 1)
        self.assertEqual(engine.coordinator.current_status, "needs_attention")

    def test_final_confirmation_uses_exact_course_and_class(self):
        engine = self.engine("videos")
        other = {**COURSE, "clazzId": "other-class"}
        tasks = [task(), task(course=other)]
        engine.cx.get_course_point.side_effect = [{"points": [{**POINT, "has_finished": True}]},
                                                  {"points": [dict(POINT)]}]
        remaining = engine._recheck_pending_chapters(tasks, {})
        self.assertEqual([task_key(item) for item in remaining], [task_key(tasks[1])])
        self.assertEqual([call.args[1] for call in engine.cx.get_course_point.call_args_list],
                         ["class", "other-class"])

    def test_video_mode_only_passes_media_to_original_video_audio_dispatcher(self):
        engine = self.engine("videos")
        video, audio = job("video", "video"), job("video", "audio")
        audio["name"] = "音频.mp3"
        _, review = self.run_with_jobs(engine, [video, job("workid"), audio, job("document"), job("read"), job("live")])
        review.assert_not_called()
        self.assertEqual([call.args[2]["jobid"] for call in engine.main.process_job.call_args_list],
                         [video["jobid"], audio["jobid"]])
        self.assert_no_mixed_scheduler(engine)

    def test_mixed_window_runs_supported_types_but_retains_live_and_external_tasks(self):
        engine = self.engine("mixed")
        external = {**job("manual", "external"), "kind": "hyperlink", "href": "https://example.invalid/"}
        _, review = self.run_with_jobs(engine, [job(kind) for kind in ("video", "workid", "document", "read", "live")]
                                           + [external])
        self.assertEqual(review.call_count, 1)
        self.assertEqual([call.args[2]["type"] for call in engine.main.process_job.call_args_list],
                         ["video", "document", "read"])
        self.assertTrue(ModeStore(self.profile, "mixed").read()["pending"])
        self.assert_no_mixed_scheduler(engine)

    def test_cancellation_after_first_video_prevents_later_jobs(self):
        engine = self.engine("videos")
        def process(*args, **kwargs):
            engine.cancelled.set()
            return fixture.main.StudyResult.SUCCESS
        engine.main.process_job.side_effect = process
        self.run_with_jobs(engine, [job("video", "first"), job("video", "second")])
        self.assertEqual(engine.main.process_job.call_count, 1)
        self.assertTrue(ModeStore(self.profile, "videos").read()["pending"])
        self.assert_no_mixed_scheduler(engine)

    def test_auto_choice_survives_new_run_in_same_account_window(self):
        engine = self.engine("quizzes")
        observed = []
        def review(cx, course, quiz, info, profile, coordinator, **kwargs):
            observed.append(coordinator.auto_quizzes)
            coordinator.auto_quizzes = True
            return "submitted"
        for _ in range(2):
            self.run_with_jobs(engine, [job("workid")], review=Mock(side_effect=review))
        self.assertEqual(observed, [False, True])
        self.assert_no_mixed_scheduler(engine)

    def test_failed_quiz_keeps_auto_for_next_quiz_and_media_in_same_chapter(self):
        engine = self.engine("mixed")
        engine.coordinator.auto_quizzes = True
        quiz = Mock(side_effect=[QuizError("合成 AI 连接失败"), "auto_submitted"])
        self.run_with_jobs(engine, [job("workid", "first"), job("workid", "second"), job("video")], review=quiz)
        self.assertEqual(quiz.call_count, 2)
        self.assertTrue(all(call.args[5].auto_quizzes for call in quiz.call_args_list))
        self.assertTrue(engine.coordinator.auto_quizzes)
        engine.call_ui.assert_not_called()
        engine.main.process_job.assert_called_once()
        pending = engine.store.read()["pending"]
        self.assertIn("合成 AI 连接失败", pending[0]["reason"])
        self.assert_no_mixed_scheduler(engine)

    def test_media_failure_keeps_auto_choice_and_continues_next_chapter(self):
        engine = self.engine("mixed")
        engine.coordinator.auto_quizzes = True
        engine.cx.get_course_point.return_value = {"points": [dict(POINT), {**POINT, "id": "second"}]}
        engine.main.process_job.side_effect = [RuntimeError("synthetic failure"), fixture.main.StudyResult.SUCCESS]
        _, review = self.run_with_jobs(engine, [job("video"), job("workid")])
        self.assertTrue(engine.coordinator.auto_quizzes)
        engine.call_ui.assert_not_called()
        self.assertEqual(review.call_count, 1)
        self.assertEqual(engine.main.process_job.call_count, 2)
        self.assertEqual(len(engine.store.read()["pending"]), 2)

    def test_empty_jobs_do_not_invoke_empty_page_completion_or_global_notification(self):
        engine = self.engine("videos")
        self.run_with_jobs(engine, [])
        engine.main.process_job.assert_not_called()
        self.assert_no_mixed_scheduler(engine)

    def test_video_eta_runs_once_per_course_and_quiz_mode_never_estimates_video_time(self):
        video = self.engine("videos")
        video.cx.get_course_point.return_value = {"points": [dict(POINT), {**POINT, "id": "second"}]}
        self.run_with_jobs(video, [job("video")], speed=1.5)
        self.assertEqual(video._estimate_course.call_count, 1)
        course, remaining, speed = video._estimate_course.call_args.args
        self.assertEqual(course, COURSE)
        self.assertEqual([item.point["id"] for item in remaining], ["chapter", "second"])
        self.assertEqual(speed, 1.5)
        video.close()
        quiz = self.engine("quizzes")
        self.run_with_jobs(quiz, [job("workid")])
        quiz._estimate_course.assert_not_called()
        self.assert_original_preserved()

    def test_eta_method_passes_remaining_tasks_and_speed_to_existing_read_only_estimator(self):
        engine = self.engine("videos")
        remaining = [task(), task("second", index=1)]
        with patch("course_eta.log_course_estimate") as estimate:
            StudyEngine._estimate_course(engine, COURSE, remaining, 1.5)
        estimate.assert_called_once_with(engine.cx, COURSE, remaining, 1.5, engine.main.logger)
        self.assert_original_preserved()

    def test_default_speed_comes_from_account_configuration(self):
        engine = self.engine("videos")
        (self.profile.directory / "config.ini").write_text("[common]\nspeed = 1.5\n", encoding="utf-8")
        self.run_with_jobs(engine, [job("video")], speed=None)
        self.assertEqual(engine.main.process_job.call_args.args[-1], 1.5)
        self.assertEqual(engine._estimate_course.call_args.args[-1], 1.5)
        self.assert_no_mixed_scheduler(engine)

    def test_quiz_mode_does_not_parse_broken_video_configuration(self):
        engine = self.engine("quizzes")
        config = self.profile.directory / "config.ini"
        original = b"not-an-ini-section\nspeed = invalid-video-speed\n"
        config.write_bytes(original)
        with patch("study_runner.configparser.ConfigParser", side_effect=AssertionError("No video configuration")) as parser:
            _, review = self.run_with_jobs(engine, [job("workid")], speed=None)
        parser.assert_not_called()
        self.assertEqual(review.call_count, 1)
        engine._estimate_course.assert_not_called()
        self.assertEqual(config.read_bytes(), original)
        self.assert_no_mixed_scheduler(engine)

    def test_cancelled_media_sleep_raises_immediately_without_original_sleep(self):
        original, cancelled = Mock(), threading.Event()
        cancelled.set()
        clock = CancellableClock(original, cancelled)
        with self.assertRaises(StudyCancelled):
            clock.sleep(60)
        original.sleep.assert_not_called()
        self.assertIs(clock.monotonic, original.monotonic)

    def test_media_clock_uses_cancellation_wait_and_close_restores_shared_module_attributes(self):
        from api import base
        engine = self.engine("videos")
        previous_clock, previous_progress = base.time, base.tqdm
        engine._patch_media()
        self.assertIsInstance(base.time, CancellableClock)
        self.assertIsNot(base.tqdm, previous_progress)
        progress = base.tqdm(total=60, initial=12, desc="测试视频")
        progress.refresh()
        emitted = engine.emit.call_args
        self.assertEqual(emitted.args[0], "progress")
        self.assertEqual(emitted.args[1]["unit"], "seconds")
        self.assertEqual(emitted.args[1]["done"], 12)
        engine.close()
        self.assertIs(base.time, previous_clock)
        self.assertIs(base.tqdm, previous_progress)
        cancelled = Mock()
        cancelled.wait.return_value = False
        original = Mock()
        CancellableClock(original, cancelled).sleep(2.5)
        cancelled.wait.assert_called_once_with(2.5)
        original.sleep.assert_not_called()
        self.assert_original_preserved()

    def test_quiz_submission_stays_pending_until_fresh_platform_confirmation(self):
        engine = self.engine("quizzes")
        self.run_with_jobs(engine, [job("workid")])
        self.assertTrue(ModeStore(self.profile, "quizzes").read()["pending"])
        self.assertGreaterEqual(engine.cx.get_course_point.call_count, 2)
        self.assert_no_mixed_scheduler(engine)

    def test_fresh_confirmed_quiz_can_leave_mode_queue_without_touching_mixed_records(self):
        engine = self.engine("quizzes")
        engine.cx.get_course_point.side_effect = [
            {"points": [dict(POINT)]}, {"points": [{**POINT, "has_finished": "true"}]}]
        with patch("study_runner.report_quiz_completed") as report:
            self.run_with_jobs(engine, [job("workid")])
        self.assertEqual(ModeStore(self.profile, "quizzes").read()["pending"], [])
        self.assertEqual(report.call_count, 1)
        self.assertEqual(report.call_args.args[2]["has_finished"], "true")
        self.assert_no_mixed_scheduler(engine)

    def test_fresh_confirmed_chapter_removes_matching_legacy_and_other_mode_records(self):
        engine = self.engine("quizzes")
        row = {"course_id": "course", "class_id": "class", "chapter_id": "chapter", "custom": "keep"}
        other = {**row, "class_id": "different"}
        (self.profile.directory / "pending_tasks.json").write_text(json.dumps({"tasks": [row, other]}), encoding="utf-8")
        (self.profile.directory / "resume_state.json").write_text(json.dumps({"version": 1, "courses": [],
            "pending": [row, other], "custom": "keep"}), encoding="utf-8")
        for mode in ("videos", "mixed"):
            ModeStore(self.profile, mode).write({"version": 1, "mode": mode, "courses": [],
                                               "pending": [row, other], "custom": "keep"})
        engine.cx.get_course_point.side_effect = [{"points": [dict(POINT)]},
            {"points": [{**POINT, "has_finished": True}]}]
        self.run_with_jobs(engine, [job("workid")])
        for name, field in (("pending_tasks.json", "tasks"), ("resume_state.json", "pending"),
                            ("study_state_videos.json", "pending"), ("study_state_mixed.json", "pending")):
            self.assertEqual(json.loads((self.profile.directory / name).read_text(encoding="utf-8"))[field], [other])
        self.assertEqual(ModeStore(self.profile, "quizzes").read()["pending"], [])
        self.assertEqual((self.profile.directory / "cookies.txt").read_bytes(), self.original_files["cookies.txt"])
        self.assertEqual((self.profile.directory / "ai_quiz.json").read_bytes(), self.original_files["ai_quiz.json"])

    def test_fresh_directory_completed_point_also_cleans_other_pending_queues_without_reading_cards(self):
        engine = self.engine("videos")
        row = {"course_id": "course", "class_id": "class", "chapter_id": "chapter"}
        ModeStore(self.profile, "quizzes").write({"version": 1, "mode": "quizzes", "courses": [], "pending": [row]})
        engine.cx.get_course_point.return_value = {"points": [{**POINT, "has_finished": "true"}]}
        with patch("study_runner.read_chapter_jobs") as reader:
            self.run_with_jobs(engine, [])
        self.assertEqual(ModeStore(self.profile, "quizzes").read()["pending"], [])
        engine.main.process_job.assert_not_called()
        self.assert_original_preserved()

    def test_invalid_course_class_selection_does_not_expand_scope_or_read_chapters(self):
        engine = self.engine("videos")
        for keys in ([], [("course", "wrong-class")], [("other-course", "class")]):
            with self.subTest(keys=keys), self.assertRaises(StudyError):
                engine.run(keys)
        engine.cx.get_course_point.assert_not_called()
        engine.main.process_job.assert_not_called()
        self.assert_no_mixed_scheduler(engine)

    def test_cancel_after_first_quiz_prevents_later_quizzes_or_confirmation_reads(self):
        engine = self.engine("quizzes")
        def review(*args, **kwargs):
            engine.cancelled.set()
            return "submitted"
        self.run_with_jobs(engine, [job("workid", "first"), job("workid", "second")],
                           review=Mock(side_effect=review))
        self.assertEqual(engine.cx.get_course_point.call_count, 1)
        self.assertTrue(ModeStore(self.profile, "quizzes").read()["pending"])
        self.assert_no_mixed_scheduler(engine)

    def test_stopping_verification_does_not_read_following_chapters(self):
        engine = self.engine("videos")
        engine.cx.get_course_point.return_value = {"points": [dict(POINT), {**POINT, "id": "later-chapter"}]}
        with patch("study_runner.read_chapter_jobs", side_effect=AccessRestricted(403, reason="challenge")) as reader:
            try:
                engine.run([("course", "class")])
            except StudyError:
                pass
        self.assertEqual(reader.call_count, 1, "用户停止验证后不得继续请求本账户后续章节")
        self.assertEqual(engine.coordinator.dialog.call_count, 1)
        self.assertEqual(len(ModeStore(self.profile, "videos").read()["pending"]), 2)
        self.assert_no_mixed_scheduler(engine)

    def test_successful_course_loading_holds_account_lock_until_window_closes(self):
        engine = self.engine("videos")
        session = Mock()
        session.cookies.get.return_value = "synthetic-uid"
        with patch("api.base.SessionManager.get_session", return_value=session):
            self.assertEqual(engine.load_courses(), [COURSE])
        with self.assertRaises(AlreadyRunning):
            with ProfileLock(self.profile.directory):
                pass
        other_directory = self.profile.directory / "another-account"
        other_directory.mkdir()
        with ProfileLock(other_directory):
            pass
        engine.close()
        with ProfileLock(self.profile.directory):
            pass
        self.assert_original_preserved()

    def test_existing_account_lock_prevents_even_course_loading_or_prepare(self):
        engine = StudyEngine(fixture.ROOT, self.profile, "videos", Mock(), Mock(), threading.Event())
        self.addCleanup(engine.close)
        with ProfileLock(self.profile.directory), patch("run_account.prepare") as prepare:
            with self.assertRaises(AlreadyRunning):
                engine.load_courses()
        prepare.assert_not_called()
        self.assert_original_preserved()

    def test_failed_refresh_invalidates_old_courses_and_requires_lock_reacquisition_before_run(self):
        engine = self.engine("videos")
        session = Mock()
        session.cookies.get.return_value = "synthetic-uid"
        engine.cx.get_course_list.side_effect = [[dict(COURSE)], RuntimeError("synthetic-refresh-failure"),
                                                 [dict(COURSE)]]
        with patch("api.base.SessionManager.get_session", return_value=session):
            self.assertEqual(engine.load_courses(), [COURSE])
            with self.assertRaisesRegex(RuntimeError, "synthetic-refresh-failure"):
                engine.load_courses()
            self.assertEqual(engine.courses, [])
            self.assertIsNone(engine.lock)
            with ProfileLock(self.profile.directory):
                pass
            with patch("study_runner.read_chapter_jobs") as reader:
                with self.assertRaisesRegex(StudyError, "刷新"):
                    engine.run([("course", "class")])
            reader.assert_not_called()
            engine.cx.get_course_point.assert_not_called()
            engine.main.process_job.assert_not_called()
            self.assertEqual(engine.load_courses(), [COURSE])
            self.assertIsNotNone(engine.lock)
            with self.assertRaises(AlreadyRunning):
                with ProfileLock(self.profile.directory):
                    pass
            self.run_with_jobs(engine, [job("video")])
        engine.main.process_job.assert_called_once()
        self.assert_no_mixed_scheduler(engine)

    def test_closed_engine_cannot_run_using_stale_loaded_courses_without_account_lock(self):
        engine = self.engine("videos")
        engine.close()
        with patch("study_runner.read_chapter_jobs") as reader, patch("study_runner.reviewed_quiz") as review:
            with self.assertRaisesRegex(StudyError, "刷新"):
                engine.run([("course", "class")])
        reader.assert_not_called()
        review.assert_not_called()
        engine.cx.get_course_point.assert_not_called()
        engine.main.process_job.assert_not_called()
        with ProfileLock(self.profile.directory):
            pass
        self.assert_no_mixed_scheduler(engine)

    def test_partial_setup_failure_releases_lock_and_never_installs_hooks_twice(self):
        engine = StudyEngine(fixture.ROOT, self.profile, "videos", Mock(), Mock(), threading.Event())
        self.addCleanup(engine.close)
        partial_main = Mock()
        with patch("run_account.prepare", return_value=partial_main) as prepare, \
             patch("runtime_safety.install_runtime_safety", side_effect=RuntimeError("synthetic-initialize-failure")) as install:
            for attempt in range(2):
                with self.subTest(attempt=attempt), self.assertRaisesRegex(StudyError, "重新打开"):
                    engine.load_courses()
                with ProfileLock(self.profile.directory):
                    pass
            prepare.assert_called_once_with(fixture.ROOT, self.profile)
            install.assert_called_once_with(partial_main, self.profile)
        self.assertIsNone(engine.lock)
        self.assertIsNone(engine.cx)
        self.assert_original_preserved()


if __name__ == "__main__":
    unittest.main()
