"""No live platform calls: optional ETA denial and critical access guards."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_study_runner as runners
import test_course_eta as eta
import test_runtime_safety as safety
from card_tasks import StudyCancelled, StudyPaused
from course_eta import CourseTimeEstimator
from runtime_safety import AccessRestricted
from study_runner import StudyEngine, ModeStore
from study_reports import category, report_directory


class CloudAccessTests(runners.StudyFixture):
    engine = runners.StudyEngineTests.engine

    def test_optional_plain_403_does_not_open_recovery_or_poison_current_request(self):
        engine = self.engine('videos')
        engine.coordinator.recover = Mock()
        error = AccessRestricted(403, resource='章节任务页')
        def denied(*args):
            engine.coordinator.issue = error
            raise error
        with patch('course_eta.log_course_estimate', side_effect=denied) as estimate:
            StudyEngine._estimate_course(engine, runners.COURSE, [runners.task()], 1)
        self.assertEqual(estimate.call_count, 1)
        engine.coordinator.recover.assert_not_called()
        self.assertIsNone(engine.coordinator.issue)
        engine.call_ui.assert_not_called()
        self.assertEqual(engine._with_recovery(lambda: 'authorized', '当前章节'), 'authorized')
        self.assertIn('403', engine.emit.call_args.args[1]['message'])
        self.assert_original_preserved()

    def test_swallowed_eta_denial_is_also_cleared_without_retrying(self):
        engine = self.engine('videos')
        def denied(*args):
            engine.coordinator.issue = AccessRestricted(403)
            return None
        with patch('course_eta.log_course_estimate', side_effect=denied):
            StudyEngine._estimate_course(engine, runners.COURSE, [runners.task()], 1)
        self.assertIsNone(engine.coordinator.issue)
        engine.call_ui.assert_not_called()

    def test_actual_plain_403_releases_slot_without_ten_minute_interaction(self):
        engine = self.engine('videos')
        engine.coordinator.recover = Mock()
        operation = Mock(side_effect=AccessRestricted(403, resource='章节任务页'))
        with self.assertRaises(runners.StudyError) as error:
            engine._with_recovery(operation, '当前章节', runners.COURSE)
        self.assertIn('当前章节', str(error.exception))
        self.assertIn('服务器访问限制', str(error.exception))
        operation.assert_called_once()
        engine.coordinator.recover.assert_not_called()
        engine.call_ui.assert_not_called()

    def test_auth_challenge_and_frequency_denials_keep_explicit_recovery(self):
        for error in (AccessRestricted(401), AccessRestricted(429, retry_after=60),
                      AccessRestricted(403, reason='challenge'), AccessRestricted(403, verifier=Mock())):
            engine = self.engine('videos')
            engine.coordinator.recover = Mock(return_value='stop')
            with patch('course_eta.log_course_estimate', side_effect=error), self.assertRaises(StudyPaused):
                StudyEngine._estimate_course(engine, runners.COURSE, [runners.task()], 1)
            engine.coordinator.recover.assert_called_once()
            engine.close()

    def test_real_card_denial_is_recorded_per_chapter_and_next_is_checked(self):
        engine = self.engine('videos')
        engine.cx.get_course_point.return_value = {'points': [runners.POINT, {**runners.POINT, 'id': 'next'}]}
        with patch('study_runner.read_chapter_jobs', side_effect=AccessRestricted(403, resource='章节任务页')) as read:
            result = engine.run([('course', 'class')], speed=1)
        self.assertIn('保留待处理', result)
        self.assertEqual(read.call_count, 2)
        engine.main.process_job.assert_not_called()
        engine.call_ui.assert_not_called()
        pending = ModeStore(self.profile, 'videos').read()['pending']
        self.assertEqual([row['chapter_id'] for row in pending], ['chapter', 'next'])
        report = json.loads((report_directory(engine.root, self.profile) / 'latest.json').read_text('utf-8'))
        denied = next(row for row in report['tasks'] if row['chapter_id'] == 'chapter')
        self.assertIn('403', denied['reason'])
        self.assertEqual(denied['category'], '平台拒绝访问，需核对访问权限与环境')
        self.assert_original_preserved()

    def test_request_guard_exposes_only_resource_label_without_url_or_tokens(self):
        result = safety.response(403, '<html>没有权限</html>')
        raw = Mock(return_value=result)
        session = SimpleNamespace(request=raw)
        coordinator = self.engine('videos').coordinator
        coordinator.guard_session(session)
        with self.assertRaises(AccessRestricted) as error:
            session.request('GET', 'https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?enc=secret')
        self.assertEqual(error.exception.resource, '章节任务页')
        self.assertNotIn('secret', str(error.exception))
        self.assertNotIn('http', str(error.exception))
        self.assertEqual(raw.call_count, 1)

    def test_estimate_paces_every_request_and_honors_cancellation(self):
        session = Mock()
        session.get.side_effect = [eta.cards([eta.video()]), eta.metadata()]
        limiter = Mock()
        result = CourseTimeEstimator(session, 'fid', before_request=limiter).estimate(eta.COURSE, [eta.task()])
        self.assertEqual(result.sampled, 1)
        self.assertEqual(limiter.call_count, 2)
        limiter.side_effect = StudyCancelled('cancelled')
        session.get.reset_mock()
        with self.assertRaises(StudyCancelled):
            CourseTimeEstimator(session, 'fid', before_request=limiter).estimate(eta.COURSE, [eta.task()])
        session.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
