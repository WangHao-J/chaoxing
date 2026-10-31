"""Offline duration/progress and read-only course startup regressions."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import test_runtime_safety as fixture

from course_eta import CourseTimeEstimator, Estimate, format_time


COURSE = {"courseId": "course", "clazzId": "class", "cpi": "cpi", "title": "测试课程"}


def task(chapter="0", count=1, **changes):
    return SimpleNamespace(course=dict(COURSE), point={"id": chapter, "jobCount": count, **changes})


def video(job_id="v1", **changes):
    return {"type": "video", "job": True, "jobid": job_id, "objectId": "obj-" + job_id,
            "playTime": 120000, **changes}


def cards(items, **changes):
    return SimpleNamespace(text="var mArg = " + json.dumps({"attachments": items, **changes}) + ";", raise_for_status=Mock())


def metadata(duration=600, **changes):
    return SimpleNamespace(json=Mock(return_value={"status": "success", "duration": duration, **changes}), raise_for_status=Mock())


class EstimateTests(unittest.TestCase):
    def estimate(self, replies, tasks=None, **options):
        self.session = Mock()
        self.session.get.side_effect = replies
        return CourseTimeEstimator(self.session, "fid", **options).estimate(COURSE, tasks or [task()])

    def test_millisecond_progress_is_subtracted_before_speed_and_minutes_round_up(self):
        result = self.estimate([cards([video(playTime=120999)]), metadata(601)])
        self.assertEqual(result.media_seconds, 481)
        self.assertIn("约5分钟", result.message(2))
        self.assertIn("已读取全部", result.message(2))
        self.session.post.assert_not_called()
        self.assertEqual(self.session.get.call_count, 2)

    def test_audio_uses_same_media_time_calculation(self):
        result = self.estimate([cards([video(property={"name": "words.mp3"})]), metadata("600")])
        self.assertEqual(result.media_seconds, 480)

    def test_resume_excludes_completed_chapters_and_other_class_and_deduplicates(self):
        done = task("done", has_finished="true")
        other = task("other")
        other.course["clazzId"] = "another-class"
        result = self.estimate([cards([video()]), metadata()], [done, other, task(), task()])
        self.assertEqual(result.chapters, 1)
        self.assertEqual(result.sampled, 1)
        self.assertEqual(self.session.get.call_count, 2)

    def test_string_false_is_not_completed_and_string_true_job_is_required(self):
        result = self.estimate([cards([video(job="true", isPassed="false")]), metadata()], [task(has_finished="false")])
        self.assertEqual(result.media_seconds, 480)

    def test_finished_and_non_task_media_do_not_request_duration(self):
        result = self.estimate([cards([video(isPassed=True), video("reference", job=False)])])
        self.assertEqual(result.media_seconds, 0)
        self.assertEqual(self.session.get.call_count, 1)
        self.assertNotIn("约", result.message(1))

    def test_duplicate_job_across_pages_counted_once(self):
        result = self.estimate([cards([video()]), metadata(), cards([video(), video("v2")]), metadata(300)], [task(count=2)])
        self.assertEqual(result.media_seconds, 660)
        self.assertEqual(result.media_count, 2)

    def test_five_samples_are_spread_across_course_not_just_first_chapters(self):
        result = self.estimate([item for _ in range(3) for item in (cards([video()]), metadata())],
                               [task(str(i)) for i in range(9)], samples=3)
        self.assertEqual(result.sampled, 3)
        self.assertEqual(result.chapters, 9)
        indices = [call.kwargs["params"]["knowledgeid"] for call in self.session.get.call_args_list if "cards" in call.args[0]]
        self.assertEqual(indices, ["0", "4", "8"])
        self.assertIn("至少还需24分钟", result.message(1))
        self.assertIn("完整读取 3/9", result.message(1))

    def test_network_failure_preserves_partial_time_and_does_not_claim_full_estimate(self):
        result = self.estimate([cards([video(), video("v2")]), metadata(), requests.ConnectTimeout()], [task(count=2)])
        self.assertEqual(result.partial_seconds, 480)
        self.assertEqual(result.sampled, 0)
        self.assertIn("至少还需8分钟", result.message(1))
        self.session.post.assert_not_called()

    def test_budget_stops_preflight_without_retries(self):
        result = self.estimate([cards([video()])], budget=6, clock=Mock(side_effect=[0, 0, 7]))
        self.assertEqual(self.session.get.call_count, 1)
        self.assertTrue(result.unknown)
        self.assertIn("暂无法估算", result.message(1))

    def test_request_timeout_is_bounded_by_remaining_budget(self):
        self.estimate([cards([video(isPassed=True)])], budget=6, clock=Mock(side_effect=[0, 5.5]))
        self.assertEqual(self.session.get.call_args.kwargs["timeout"], .5)

    def test_missing_duration_never_claims_video_has_ended(self):
        for duration in (None, "invalid", -1, float("nan"), float("inf"), True):
            with self.subTest(duration=duration):
                result = self.estimate([cards([video()]), metadata(duration)])
                self.assertIn("暂无法估算", result.message(1))
                self.assertNotIn("已到结尾", result.message(1))

    def test_negative_or_invalid_progress_is_unknown(self):
        for progress in (-5, None, "no", float("inf"), True):
            with self.subTest(progress=progress):
                result = self.estimate([cards([video(playTime=progress)])])
                self.assertEqual(self.session.get.call_count, 1)
                self.assertIn("暂无法估算", result.message(1))

    def test_end_progress_does_not_claim_platform_completion(self):
        result = self.estimate([cards([video(playTime=900000)]), metadata()])
        self.assertEqual(result.media_seconds, 0)
        self.assertIn("仍需平台确认", result.message(1))

    def test_zero_progress_starts_full_duration(self):
        result = self.estimate([cards([video(playTime=0)]), metadata(600)])
        self.assertIn("约10分钟", result.message(1))

    def test_official_empty_initializer_does_not_break_time_estimation(self):
        page = cards([video()])
        page.text = 'var mArg="";' + page.text + 'mArg = $.parseJSON(mArg);'
        result = self.estimate([page, metadata(600)])
        self.assertEqual(result.media_seconds, 480)
        self.assertIn("约8分钟", result.message(1))

    def test_unused_template_slots_do_not_hide_measured_video_time(self):
        unused = SimpleNamespace(text='var mArg="";mArg=$.parseJSON(mArg);', raise_for_status=Mock())
        result = self.estimate([cards([video()]), metadata(), *[unused] * 6], [task(count=2)])
        self.assertEqual(result.partial_seconds, 480)
        self.assertIn("至少还需8分钟", result.message(1))

    def test_quiz_and_external_links_count_as_other_work_without_opening_links(self):
        result = self.estimate([cards([{"type": "workid", "jobid": "quiz"},
            {"type": "hyperlink", "job": True, "jobid": "external", "property": {"href": "https://example.com"}}])], [task(count=2)])
        self.assertEqual(result.other_count, 2)
        self.assertEqual(self.session.get.call_count, 1)
        self.session.post.assert_not_called()
        self.assertIn("答题或其他任务", result.message(1))

    def test_locked_chapter_skipped_and_no_requests_made(self):
        result = self.estimate([], [task(need_unlock="true")])
        self.session.get.assert_not_called()
        self.assertTrue(result.unknown)

    def test_malformed_card_keeps_unknown_and_never_completes_empty_page(self):
        result = self.estimate([SimpleNamespace(text="mArg={broken}", raise_for_status=Mock())] * 7)
        self.assertEqual(result.sampled, 0)
        self.assertTrue(result.unknown)
        self.session.post.assert_not_called()
        self.assertTrue(all("knowledge/cards" in call.args[0] for call in self.session.get.call_args_list))

    def test_known_partial_media_is_lower_bound_for_extrapolation(self):
        result = Estimate(chapters=3, sampled=1, media_seconds=60, partial_seconds=1200)
        self.assertIn("至少还需21分钟", result.message(1))

    def test_large_duration_format_and_seconds_rounding(self):
        self.assertEqual(format_time(7200), "2小时")
        self.assertEqual(format_time(3601), "1小时1分钟")
        self.assertEqual(format_time(1), "1分钟")

    def test_metadata_url_cannot_change_host_or_escape_path(self):
        self.estimate([cards([video(objectId="../other?secret")]), metadata()])
        self.assertEqual(self.session.get.call_args.args[0],
                         "https://mooc1.chaoxing.com/ananas/status/..%2Fother%3Fsecret")

    def test_estimate_does_not_mutate_task_or_card_progress(self):
        current = task()
        before = dict(current.point)
        self.estimate([cards([video()]), metadata()], [current])
        self.assertEqual(current.point, before)

    def test_logging_labels_course_speed_and_excluded_time(self):
        from course_eta import log_course_estimate
        session = Mock()
        session.get.side_effect = [cards([video()]), metadata()]
        cx, logger = Mock(), Mock()
        cx.get_fid.return_value = "fid"
        with patch("api.base.SessionManager.get_session", return_value=session):
            log_course_estimate(cx, COURSE, [task()], 2, logger)
        self.assertEqual(logger.info.call_count, 2)
        self.assertIn("答题、外链、验证和网络等待用时另计", logger.info.call_args.args[0])
        self.assertIn("2倍速", logger.info.call_args.args[2])


class SchedulerEstimateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.RuntimeSafetyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def processor(self, tasks):
        from runtime_safety import make_processor
        import main
        return make_processor(main.JobProcessor, main, self.fixture.co, estimate_courses=True)(Mock(), tasks, {"jobs": 1, "speed": 1})

    def test_estimate_before_first_task_once_per_course_and_does_not_change_task_order(self):
        import main
        tasks = [self.fixture.task(i) for i in range(3)]
        tasks[1].course = {**tasks[1].course, "clazzId": "second-class"}
        events = []
        def estimate(cx, course, pending, speed, logger):
            events.append(("estimate", course["clazzId"]))
            self.assertIn(tasks[0] if course["clazzId"] == "class" else tasks[1], pending)
        def process(cx, course, point, speed):
            events.append(("process", point["id"]))
            return main.ChapterResult.SUCCESS
        with patch("course_eta.log_course_estimate", side_effect=estimate), patch.object(main, "process_chapter", side_effect=process):
            processor = self.processor(tasks)
            processor.run()
        self.assertEqual(events, [("estimate", "class"), ("process", "0"), ("estimate", "second-class"), ("process", "1"), ("process", "2")])
        self.assertEqual(processor.completed_tasks, tasks)

    def test_estimation_error_still_runs_real_chapter(self):
        import main
        with patch("course_eta.log_course_estimate", side_effect=ValueError()), patch.object(main, "process_chapter", return_value=main.ChapterResult.SUCCESS) as process:
            self.processor([self.fixture.task()]).run()
        process.assert_called_once()
        self.assertTrue(self.fixture.co.completed)

    def test_restriction_during_estimate_uses_existing_pause_and_retains_task(self):
        import main
        from runtime_safety import AccessRestricted, RunNeedsAttention
        with patch("course_eta.log_course_estimate", side_effect=AccessRestricted(403)), patch.object(main, "process_chapter") as process, self.assertRaises(RunNeedsAttention):
            self.processor([self.fixture.task()]).run()
        process.assert_not_called()
        self.assertFalse(self.fixture.co.completed)
        self.assertEqual(len(self.fixture.co.pending), 1)

    def test_no_estimate_for_already_completed_course(self):
        current = self.fixture.task()
        current.point["has_finished"] = True
        with patch("course_eta.log_course_estimate") as estimate:
            self.processor([current]).run()
        estimate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
