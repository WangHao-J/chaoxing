"""Offline selected-quiz regressions: exact scope, no video, honest checkpoints."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_runtime_safety as fixture
from api import decode
from manual_tasks import install_manual_tasks
from pending_quiz import (PendingQuizError, confirm_selected_task, load_selected_task,
                          resolve_selected_quizzes, task_key)


COURSE = {"courseId": "course", "clazzId": "class", "cpi": "fresh-cpi", "title": "当前课程"}
POINT = {"id": "chapter", "title": "当前测验", "has_finished": False, "need_unlock": False}
ROW = {"course_id": "course", "class_id": "class", "chapter_id": "chapter",
       "course_title": "旧课程名称", "chapter_title": "旧测验名称", "cpi": "old-cpi"}


def work_card(jobid="work-one", **changes):
    return {"type": "workid", "job": True, "jobid": jobid, "enc": "synthetic-secret-enc",
            "property": {"title": "真实测验"}, **changes}


def card_page(cards=(), **defaults):
    info = {"knowledgeid": "chapter", "ktoken": "synthetic-secret-ktoken", "cpi": "fresh-cpi", **defaults}
    return "<script>var mArg = " + json.dumps({"attachments": list(cards), "defaults": info}) + ";</script>"


class PendingQuizTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pending-quiz-test-", dir=fixture.ROOT / "temp")
        self.addCleanup(self.temporary.cleanup)
        self.profile = SimpleNamespace(directory=Path(self.temporary.name), id="test", name="测试账户")
        self.other_rows = [{**ROW, "chapter_id": "other-chapter", "extra": "retain chapter"},
                           {**ROW, "class_id": "other-class", "extra": "retain class"},
                           {**ROW, "course_id": "other-course", "extra": "retain course"}]
        self.pending = {"tasks": [self.other_rows[0], dict(ROW), *self.other_rows[1:]],
                        "extra": {"retain": True}, "updated_at": "original-time"}
        self.resume = {"version": 1, "courses": [{"course_id": "course", "class_id": "class", "title": "原选课"},
                                                 {"course_id": "other-course", "class_id": "class"}],
                       "pending": list(self.pending["tasks"]), "extra": "retain", "updated_at": "original-time"}
        self.write("pending_tasks.json", self.pending)
        self.write("resume_state.json", self.resume)
        self.write("ai_quiz.json", {"unrelated": "keep-settings"})
        self.cx = Mock()
        self.cx.get_course_list.return_value = [dict(COURSE)]
        self.cx.get_course_point.return_value = {"points": [dict(POINT)]}
        self.session = Mock()
        self.set_pages(card_page([work_card()]))
        previous = decode._process_attachment_cards, fixture.main.process_job, fixture.main.process_chapter
        missing = object()
        previous_pending = getattr(fixture.main, "process_pending_quizzes", missing)
        def restore():
            decode._process_attachment_cards, fixture.main.process_job, fixture.main.process_chapter = previous
            if previous_pending is missing:
                del fixture.main.process_pending_quizzes
            else:
                fixture.main.process_pending_quizzes = previous_pending
        self.addCleanup(restore)
        install_manual_tasks(fixture.main)
        session_patch = patch("api.base.SessionManager.get_session", return_value=self.session)
        session_patch.start()
        self.addCleanup(session_patch.stop)

    def write(self, name, data):
        (self.profile.directory / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def read(self, name):
        return json.loads((self.profile.directory / name).read_text(encoding="utf-8"))

    def snapshot(self):
        return {path.name: path.read_bytes() for path in self.profile.directory.iterdir() if path.is_file()}

    def set_pages(self, first, *rest):
        pages = [first, *rest]
        pages.extend([card_page()] * (7 - len(pages)))
        self.session.get.side_effect = [fixture.response(200, html) for html in pages]

    def finished(self, value=True):
        self.cx.get_course_point.return_value = {"points": [{**POINT, "has_finished": value}]}

    def test_load_selected_uses_exact_account_course_class_chapter_and_keeps_legacy_rows(self):
        self.pending["tasks"].insert(0, {"course_title": "无编号的旧记录"})
        self.write("pending_tasks.json", self.pending)
        self.assertEqual(load_selected_task(self.profile, "course", "class", "chapter"), ROW)
        for values in (("course", "wrong", "chapter"), ("wrong", "class", "chapter"),
                       ("course", "class", "wrong"), ("course", "", "chapter")):
            with self.subTest(values=values), self.assertRaises(PendingQuizError):
                load_selected_task(self.profile, *values)
        self.assertEqual(task_key({"course_id": 12, "class_id": 34, "chapter_id": 56}), ("12", "34", "56"))
        self.assertEqual(task_key({"course_id": True}), ("", "", ""))

    def test_resolve_refreshes_exact_scope_and_uses_current_metadata_without_processing_video(self):
        self.cx.get_course_list.return_value = [{**COURSE, "clazzId": "other-class"},
                                                {**COURSE, "courseId": "other-course"}, dict(COURSE)]
        self.cx.get_course_point.return_value = {"points": [{**POINT, "id": "other-chapter"}, dict(POINT)]}
        video = {"type": "video", "job": True, "jobid": "video", "mid": "video-mid", "property": {}}
        self.set_pages(card_page([video, work_card()]))
        before = self.snapshot()
        course, point, quizzes = resolve_selected_quizzes(self.cx, dict(ROW))
        self.assertEqual(course, COURSE)
        self.assertEqual(point, POINT)
        self.assertEqual([job["jobid"] for job, _ in quizzes], ["work-one"])
        self.cx.get_course_point.assert_called_once_with("course", "class", "fresh-cpi")
        self.assertEqual(self.session.get.call_count, 7)
        for index, call in enumerate(self.session.get.call_args_list):
            self.assertEqual(call.args[0], "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards")
            self.assertEqual(call.kwargs["params"], {"clazzid": "class", "courseid": "course",
                "knowledgeid": "chapter", "cpi": "fresh-cpi", "ut": "s", "v": "2025-0424-1038-3",
                "mooc2": 1, "num": str(index)})
            self.assertEqual(call.kwargs["timeout"], (5, 15))
        self.cx.get_job_list.assert_not_called()
        self.cx.study_video.assert_not_called()
        self.cx.study_emptypage.assert_not_called()
        self.session.post.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_resolve_reads_missing_job_marker_and_deduplicates_ordered_quizzes(self):
        first, second = work_card(job=None), work_card("work-two")
        self.set_pages(card_page([first, second]), card_page([first]))
        _, _, quizzes = resolve_selected_quizzes(self.cx, ROW)
        self.assertEqual([job["jobid"] for job, _ in quizzes], ["work-one", "work-two"])

    def test_resolve_allows_unused_blank_tail_pages(self):
        self.set_pages(card_page([work_card()]), *["<html><head></head><body></body></html>"] * 6)
        self.assertEqual(len(resolve_selected_quizzes(self.cx, ROW)[2]), 1)

    def test_all_entry_points_accept_official_unused_marg_template_tail_slots(self):
        unused = '<script>var mArg=""; if(typeof mArg=="string"){mArg = $.parseJSON(mArg);}</script>'
        before = self.snapshot()
        self.set_pages(card_page([work_card()]), *[unused] * 6)
        quizzes = resolve_selected_quizzes(self.cx, ROW)[2]
        self.assertEqual([job["jobid"] for job, _ in quizzes], ["work-one"])
        self.session.post.assert_not_called()
        self.cx.study_emptypage.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_mode_only_task_loads_and_platform_confirmation_clears_all_corresponding_queues(self):
        self.finished()
        self.write("pending_tasks.json", {**self.pending, "tasks": self.other_rows})
        self.write("resume_state.json", {**self.resume, "pending": self.other_rows})
        for mode in ("quizzes", "videos", "mixed"):
            self.write(f"study_state_{mode}.json", {"version": 1, "mode": mode, "courses": [],
                       "pending": [dict(ROW), *self.other_rows], "unknown": "preserve"})
        selected = load_selected_task(self.profile, "course", "class", "chapter")
        self.assertEqual(task_key(selected), ("course", "class", "chapter"))
        before_resume = (self.profile.directory / "resume_state.json").read_bytes()
        self.assertTrue(confirm_selected_task(self.cx, self.profile, COURSE, dict(POINT)))
        for mode in ("quizzes", "videos", "mixed"):
            self.assertEqual(self.read(f"study_state_{mode}.json")["pending"], self.other_rows)
            self.assertEqual(self.read(f"study_state_{mode}.json")["unknown"], "preserve")
        self.assertEqual((self.profile.directory / "resume_state.json").read_bytes(), before_resume)

    def test_finished_server_point_never_reads_or_opens_quizzes(self):
        self.finished("true")
        self.assertEqual(resolve_selected_quizzes(self.cx, ROW)[2], [])
        self.session.get.assert_not_called()

    def test_initial_cancellation_never_reads_course_or_cards(self):
        before = self.snapshot()
        with self.assertRaisesRegex(PendingQuizError, "已取消打开"):
            resolve_selected_quizzes(self.cx, ROW, cancelled=lambda: True)
        self.cx.get_course_list.assert_not_called()
        self.cx.get_course_point.assert_not_called()
        self.session.get.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_cancellation_during_first_card_request_never_reads_remaining_pages(self):
        before = self.snapshot()
        cancelled = threading.Event()
        def first_card(*args, **kwargs):
            cancelled.set()
            return fixture.response(200, card_page([work_card()]))
        self.session.get.side_effect = first_card
        with self.assertRaisesRegex(PendingQuizError, "已取消打开"):
            resolve_selected_quizzes(self.cx, ROW, cancelled=cancelled.is_set)
        self.assertEqual(self.session.get.call_count, 1)
        self.assertEqual(self.snapshot(), before)
        self.session.post.assert_not_called()

    def test_cancellation_after_chapter_refresh_never_reads_cards(self):
        cancelled = threading.Event()
        def chapter(*args):
            cancelled.set()
            return {"points": [dict(POINT)]}
        self.cx.get_course_point.side_effect = chapter
        with self.assertRaisesRegex(PendingQuizError, "已取消打开"):
            resolve_selected_quizzes(self.cx, ROW, cancelled=cancelled.is_set)
        self.session.get.assert_not_called()

    def test_string_false_is_not_a_completed_chapter(self):
        self.finished("false")
        self.assertEqual(len(resolve_selected_quizzes(self.cx, ROW)[2]), 1)

    def test_wrong_course_class_or_chapter_never_reaches_card_api(self):
        for courses, points in (([{**COURSE, "clazzId": "wrong"}], [POINT]),
                                ([{**COURSE, "courseId": "wrong"}], [POINT]),
                                ([COURSE], [{**POINT, "id": "wrong"}]),
                                ([COURSE], [POINT, dict(POINT)])):
            self.cx.get_course_list.return_value = courses
            self.cx.get_course_point.return_value = {"points": points}
            with self.subTest(courses=courses, points=points), self.assertRaises(PendingQuizError):
                resolve_selected_quizzes(self.cx, ROW)
        self.session.get.assert_not_called()

    def test_no_quiz_unread_first_page_and_partially_malformed_cards_retain_every_record(self):
        before = self.snapshot()
        for pages in ((card_page(),), ("<html></html>",),
                      (card_page([work_card()]), "<script>mArg = {broken;</script>"),
                      (card_page([work_card()]), "<html>用户登录 synthetic-secret</html>"),
                      (card_page([work_card(), {"type": "workid", "property": {"title": "缺失参数"}}]),)):
            self.set_pages(*pages)
            with self.subTest(pages=pages), self.assertRaises(PendingQuizError):
                resolve_selected_quizzes(self.cx, ROW)
            self.assertEqual(self.snapshot(), before)
        self.session.post.assert_not_called()

    def test_missing_or_mismatched_quiz_parameters_never_reach_answer_or_submission(self):
        for page in (card_page([work_card(enc="")]), card_page([work_card()], ktoken=""),
                     card_page([work_card()], knowledgeid="other-chapter"),
                     card_page([work_card()], cpi="other-cpi")):
            self.set_pages(page)
            with self.subTest(page=page), self.assertRaises(PendingQuizError) as caught:
                resolve_selected_quizzes(self.cx, ROW)
            for secret in ("synthetic-secret-enc", "synthetic-secret-ktoken"):
                self.assertNotIn(secret, str(caught.exception))
        self.session.post.assert_not_called()

    def test_not_open_http_error_and_network_errors_do_not_expose_private_response(self):
        self.set_pages("章节未开放")
        with self.assertRaises(PendingQuizError):
            resolve_selected_quizzes(self.cx, ROW)
        self.session.get.side_effect = None
        self.session.get.return_value = fixture.response(403, "password=synthetic-secret")
        with self.assertRaises(PendingQuizError) as caught:
            resolve_selected_quizzes(self.cx, ROW)
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.session.get.side_effect = RuntimeError("enc=synthetic-secret")
        with self.assertRaises(PendingQuizError) as caught:
            resolve_selected_quizzes(self.cx, ROW)
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_confirmation_removes_only_exact_key_and_preserves_selections_order_and_other_settings(self):
        self.finished()
        legacy = {"chapter_title": "未完整记录的旧条目"}
        self.pending["tasks"].append(legacy)
        self.write("pending_tasks.json", self.pending)
        settings = (self.profile.directory / "ai_quiz.json").read_bytes()
        point = dict(POINT)
        self.assertTrue(confirm_selected_task(self.cx, self.profile, COURSE, point))
        self.assertEqual(self.read("pending_tasks.json"), {**self.pending, "tasks": [*self.other_rows, legacy]})
        self.assertEqual(self.read("resume_state.json"), {**self.resume, "pending": self.other_rows})
        self.assertEqual((self.profile.directory / "ai_quiz.json").read_bytes(), settings)
        self.assertTrue(point["has_finished"])
        self.assertEqual({path.name for path in self.profile.directory.iterdir()},
                         {"pending_tasks.json", "resume_state.json", "ai_quiz.json"})
        self.cx.get_course_point.assert_called_once_with("course", "class", "fresh-cpi")

    def test_unconfirmed_or_string_false_server_state_does_not_modify_any_checkpoint(self):
        before = self.snapshot()
        for value in (False, "false", 0, None):
            self.finished(value)
            with self.subTest(value=value):
                self.assertFalse(confirm_selected_task(self.cx, self.profile, COURSE, dict(POINT)))
            self.assertEqual(self.snapshot(), before)

    def test_corrupt_or_mismatched_resume_is_checked_before_any_pending_deletion(self):
        self.finished()
        for data in ({"version": 2, "pending": [], "courses": []},
                     {**self.resume, "pending": self.other_rows},
                     {**self.resume, "pending": [{"course_id": "course"}]}):
            self.write("resume_state.json", data)
            before = self.snapshot()
            with self.subTest(data=data), self.assertRaises(PendingQuizError):
                confirm_selected_task(self.cx, self.profile, COURSE, dict(POINT))
            self.assertEqual(self.snapshot(), before)

    def test_corrupt_pending_and_missing_selected_record_do_not_modify_resume(self):
        self.finished()
        for data in ({"tasks": "bad"}, {"tasks": self.other_rows}, {"tasks": [None]}):
            self.write("pending_tasks.json", data)
            before = self.snapshot()
            with self.subTest(data=data), self.assertRaises(PendingQuizError):
                confirm_selected_task(self.cx, self.profile, COURSE, dict(POINT))
            self.assertEqual(self.snapshot(), before)

    def test_legacy_pending_without_resume_file_can_confirm_without_creating_or_changing_selection(self):
        self.finished()
        (self.profile.directory / "resume_state.json").unlink()
        self.assertTrue(confirm_selected_task(self.cx, self.profile, COURSE, dict(POINT)))
        self.assertEqual(self.read("pending_tasks.json"), {**self.pending, "tasks": self.other_rows})
        self.assertFalse((self.profile.directory / "resume_state.json").exists())

    def test_failed_second_atomic_replace_restores_first_checkpoint_and_keeps_pending(self):
        self.finished()
        before = self.snapshot()
        original_replace = os.replace
        calls = []
        def replace(source, target):
            calls.append(Path(target).name)
            if len(calls) == 2:
                raise OSError("synthetic write failure")
            return original_replace(source, target)
        with patch("pending_quiz.os.replace", side_effect=replace), self.assertRaises(PendingQuizError):
            confirm_selected_task(self.cx, self.profile, COURSE, dict(POINT))
        self.assertEqual(calls, ["resume_state.json", "pending_tasks.json", "resume_state.json"])
        self.assertEqual(self.snapshot(), before)

    def test_failure_staging_any_file_never_replaces_existing_checkpoint(self):
        self.finished()
        before = self.snapshot()
        import pending_quiz
        original_stage = pending_quiz._stage_bytes
        calls = []
        def stage(path, content):
            calls.append(path.name)
            if len(calls) == 2:
                raise OSError("synthetic stage failure")
            return original_stage(path, content)
        with patch("pending_quiz._stage_bytes", side_effect=stage), self.assertRaises(PendingQuizError):
            confirm_selected_task(self.cx, self.profile, COURSE, dict(POINT))
        self.assertEqual(self.snapshot(), before)

    def test_confirmation_network_failure_retains_both_files_and_sanitizes_error(self):
        before = self.snapshot()
        self.cx.get_course_point.side_effect = RuntimeError("cookie=synthetic-secret")
        with self.assertRaises(PendingQuizError) as caught:
            confirm_selected_task(self.cx, self.profile, COURSE, dict(POINT))
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
