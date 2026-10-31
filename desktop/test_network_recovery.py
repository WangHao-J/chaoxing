"""Recover transient network failures without losing position or replaying submissions."""
import threading
from unittest.mock import Mock, patch
import requests
import test_study_runner as fixture
from runtime_safety import AccessRestricted, RequestFailed
from card_tasks import StudyError, StudyCancelled, StudyPaused


class NetworkRecoveryTests(fixture.StudyFixture):
    engine = fixture.StudyEngineTests.engine

    def test_read_retry_is_bounded_and_recovers(self):
        e = self.engine('videos')
        read = Mock(side_effect=[requests.ConnectTimeout(), requests.ReadTimeout(), {'ok': True}])
        self.assertEqual(e._with_recovery(read, '读取章节', read_only=True), {'ok': True})
        self.assertEqual(read.call_count, 3)
        read = Mock(side_effect=requests.ConnectTimeout())
        with self.assertRaisesRegex(StudyError, '已重试 3 次'):
            e._with_recovery(read, '读取章节', read_only=True)
        self.assertEqual(read.call_count, 4)

    def test_auth_rate_limit_tls_and_permanent_http_errors_are_not_network_retries(self):
        for error in (AccessRestricted(403), AccessRestricted(429), requests.exceptions.SSLError(),
                      RequestFailed('404', 404), ValueError('parse error')):
            e = self.engine('videos')
            e.coordinator.dialog = Mock(return_value='stop')
            read = Mock(side_effect=error)
            with self.assertRaises(Exception):
                e._with_recovery(read, '章节', read_only=True)
            self.assertEqual(read.call_count, 1)
            e.close()

    def test_server_failure_retries_only_for_explicit_read_operations(self):
        e = self.engine('videos')
        read = Mock(side_effect=[RequestFailed('503', 503), 'ok'])
        self.assertEqual(e._with_recovery(read, '目录', read_only=True), 'ok')
        submission = Mock(side_effect=requests.ReadTimeout())
        with self.assertRaises(requests.ReadTimeout):
            e._with_recovery(submission, '提交测验')
        submission.assert_called_once()

    def test_retry_delay_can_be_cancelled_without_another_request(self):
        e = self.engine('videos')
        e.network_retry_delays = (30,)
        read = Mock(side_effect=requests.ConnectTimeout())
        with patch.object(e.cancelled, 'wait', side_effect=lambda delay: (e.cancelled.set() or True)) as wait:
            with self.assertRaises(StudyCancelled):
                e._with_recovery(read, '目录', read_only=True)
        read.assert_called_once()
        wait.assert_called_once_with(30)

    def test_media_reloads_same_job_server_position_and_new_tokens(self):
        e = self.engine('videos')
        original = {**fixture.job('video'), 'playTime': 12000}
        refreshed = {**original, 'playTime': 583000}
        new_info = {'knowledgeid': 'chapter', 'ktoken': 'new-token'}
        e.main.process_job.side_effect = [requests.ConnectTimeout(), e.main.StudyResult.SUCCESS]
        with patch('study_runner.read_chapter_jobs', return_value=[(refreshed, new_info)]):
            result = e._process_media(fixture.task(), original, {'ktoken': 'old-token'}, 1)
        self.assertEqual(result, e.main.StudyResult.SUCCESS)
        self.assertEqual(e.main.process_job.call_count, 2)
        resumed = e.main.process_job.call_args.args
        self.assertEqual(resumed[2]['jobid'], original['jobid'])
        self.assertEqual(resumed[2]['playTime'], 583000)
        self.assertEqual(resumed[3]['ktoken'], 'new-token')
        self.assertEqual(original['playTime'], 12000)

    def test_confirmed_chapter_after_timeout_is_not_replayed(self):
        e = self.engine('videos')
        e.main.process_job.side_effect = requests.ReadTimeout()
        e.cx.get_course_point.return_value = {'points': [{**fixture.POINT, 'has_finished': True}]}
        task = fixture.task()
        with patch('study_runner.read_chapter_jobs') as read:
            self.assertEqual(e._process_media(task, fixture.job('video'), {}, 1), e.main.StudyResult.SUCCESS)
        e.main.process_job.assert_called_once()
        read.assert_not_called()
        self.assertIs(task.point['has_finished'], True)

    def test_missing_media_is_not_assumed_complete_but_explicit_pass_is_accepted(self):
        e = self.engine('videos')
        e.main.process_job.side_effect = requests.ReadTimeout()
        original = fixture.job('video')
        with patch('study_runner.read_chapter_jobs', return_value=[]), \
             patch('study_runner.read_chapter_job_states', return_value={}):
            with self.assertRaisesRegex(StudyError, '未能唯一确认'):
                e._process_media(fixture.task(), original, {}, 1)
        e.main.process_job.assert_called_once()
        e.main.process_job.reset_mock()
        with patch('study_runner.read_chapter_jobs', return_value=[]), \
             patch('study_runner.read_chapter_job_states', return_value={('video', original['jobid']): True}):
            self.assertEqual(e._process_media(fixture.task(), original, {}, 1), e.main.StudyResult.SUCCESS)
        e.main.process_job.assert_called_once()

    def test_different_media_object_cannot_replace_original_after_retry(self):
        e = self.engine('videos')
        e.main.process_job.side_effect = requests.ConnectTimeout()
        original = fixture.job('video')
        with patch('study_runner.read_chapter_jobs', return_value=[({**original, 'objectid': 'different'}, {})]):
            with self.assertRaisesRegex(StudyError, '对象与原任务不一致'):
                e._process_media(fixture.task(), original, {}, 1)
        e.main.process_job.assert_called_once()

    def test_document_and_read_side_effects_are_not_automatically_repeated(self):
        e = self.engine('videos')
        for kind in ('document', 'read'):
            e.main.process_job.reset_mock()
            e.main.process_job.side_effect = requests.ReadTimeout()
            with self.assertRaises(requests.ReadTimeout):
                e._process_media(fixture.task(), fixture.job(kind), {}, 1)
            e.main.process_job.assert_called_once()

    def test_exhausted_media_retry_continues_to_later_chapter(self):
        e = self.engine('videos')
        e.cx.get_course_point.return_value = {'points': [fixture.POINT, {**fixture.POINT, 'id': 'later'}]}
        e.main.process_job.side_effect = requests.ConnectTimeout()
        with patch('study_runner.read_chapter_jobs', return_value=[(fixture.job('video'), {})]) as read:
            result = e.run([('course', 'class')], speed=1)
        self.assertEqual(e.main.process_job.call_count, 8)
        self.assertTrue(any(call.args[2]['id'] == 'later' for call in read.call_args_list))
        pending = e.store.read()['pending']
        self.assertEqual(len(pending), 2)
        self.assertIn('网络恢复未成功', pending[0]['reason'])
        self.assertNotIn('前置章节', pending[1]['reason'])
        self.assertIn('保留待处理', result)
