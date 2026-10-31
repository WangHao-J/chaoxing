"""End-to-end local scheduling tests; all accounts, pages and results are synthetic."""
import json
from unittest.mock import patch, Mock
import test_study_runner as f
from card_tasks import StudyError, StudyPaused
from runtime_safety import AccessRestricted
from study_reports import report_directory


class RoundRetryTests(f.StudyFixture):
    def engine(self, mode='videos'):
        e=f.StudyEngineTests.engine(self, mode)
        e.max_restart_rounds=3
        return e

    def test_bad_chapter_does_not_block_good_chapter_and_four_pass_limit(self):
        e=self.engine()
        done=set(); calls=[]
        e.cx.get_course_point.side_effect=lambda *a:{'points':[
            {**f.POINT,'id':i,'has_finished':i in done} for i in ['bad','good']]}
        def read(cx,course,point,stop):
            calls.append(point['id'])
            if point['id']=='bad': raise StudyError('synthetic unreadable')
            return [(f.job('video','good'),{})]
        e.main.process_job.side_effect=lambda *a:(done.add('good') or e.main.StudyResult.SUCCESS)
        with patch('study_runner.read_chapter_jobs',side_effect=read):
            result=e.run([('course','class')])
        self.assertEqual(calls,['bad','good','bad','bad','bad'])
        e.main.process_job.assert_called_once()
        self.assertEqual(len(e.round_history),4)
        self.assertEqual([r['chapter_id'] for r in e.store.read()['pending']],['bad'])
        self.assertIn('已执行 4 轮',result)
        report=json.loads((report_directory(e.root,e.profile)/'latest.json').read_text('utf-8'))
        self.assertEqual(report['rounds']['current'],4)
        self.assertEqual(report['rounds']['restart_limit'],3)
        self.assertEqual({t['chapter_id'] for t in report['tasks'] if t['course_id']=='course'},{'bad'})
        self.assertTrue(any(c.args[1].get('done')==2 for c in e.emit.call_args_list if c.args[0]=='progress'))

    def test_second_round_reloads_directory_and_recovered_failure_is_removed(self):
        e=self.engine(); reads=[];done=False
        def directory(*a):return {'points':[{**f.POINT,'has_finished':done}]}
        def read(*a):
            reads.append(e._round_number)
            if e._round_number==1:raise StudyError('temporary parsing problem')
            return [(f.job('video'),{})]
        def process(*a):
            nonlocal done
            done=True
            return e.main.StudyResult.SUCCESS
        e.cx.get_course_point.side_effect=directory;e.main.process_job.side_effect=process
        with patch('study_runner.read_chapter_jobs',side_effect=read):result=e.run([('course','class')])
        self.assertEqual(reads,[1,2]);self.assertEqual(len(e.round_history),2)
        self.assertEqual(e.store.read()['pending'],[])
        self.assertIn('已执行 2 轮',result)
        report=json.loads((report_directory(e.root,e.profile)/'latest.json').read_text('utf-8'))
        self.assertEqual([t for t in report['tasks'] if t['course_id']=='course'],[])

    def test_cancel_prevents_next_chapter_and_all_future_rounds(self):
        e=self.engine()
        def read(*a):e.cancelled.set();raise f.StudyCancelled('stop')
        with patch('study_runner.read_chapter_jobs',side_effect=read) as r:
            result=e.run([('course','class')])
        self.assertIn('已停止',result);r.assert_called_once();self.assertEqual(e._round_number,1)

    def test_real_verification_stop_does_not_automatically_restart(self):
        e=self.engine();e.coordinator.dialog=Mock(return_value='stop')
        with patch('study_runner.read_chapter_jobs',side_effect=AccessRestricted(403,reason='challenge')) as r:
            e.run([('course','class')])
        r.assert_called_once();self.assertEqual(e._round_number,1)

    def test_confirmed_all_complete_does_not_start_extra_pass(self):
        e=self.engine();e.cx.get_course_point.return_value={'points':[{**f.POINT,'has_finished':True}]}
        with patch('study_runner.read_chapter_jobs') as r:e.run([('course','class')])
        r.assert_not_called();self.assertEqual(len(e.round_history),1)

    def test_plain_403_on_one_chapter_still_checks_authorized_next(self):
        e=self.engine(); e.max_restart_rounds=0
        e.cx.get_course_point.return_value={'points':[{**f.POINT,'id':x} for x in ('denied','authorized')]}
        def read(cx,course,point,stop):
            if point['id']=='denied':raise AccessRestricted(403)
            return [(f.job('video'),{})]
        with patch('study_runner.read_chapter_jobs',side_effect=read):e.run([('course','class')])
        e.main.process_job.assert_called_once()
        self.assertIn('403',e.store.read()['pending'][0]['reason'])

    def test_pending_from_unselected_course_does_not_cause_restarts(self):
        e=self.engine();e.store.save_selection([f.COURSE]);e.store.update([f.task('old',course={**f.COURSE,'courseId':'unselected'})])
        e.cx.get_course_point.return_value={'points':[]}
        e.run([('course','class')])
        self.assertEqual(len(e.round_history),1);self.assertEqual(len(e.store.read()['pending']),1)

    def test_missing_old_chapter_is_retained_and_attempts_remain_bounded(self):
        e=self.engine();e.store.save_selection([f.COURSE]);e.store.update([f.task('gone')])
        e.cx.get_course_point.return_value={'points':[]}
        e.run([('course','class')])
        self.assertEqual(len(e.round_history),4)
        self.assertEqual(e.store.read()['pending'][0]['chapter_id'],'gone')

    def test_one_course_directory_failure_does_not_block_other_course(self):
        e=self.engine();e.courses=[f.COURSE,{**f.COURSE,'courseId':'other'}]
        done=False
        def directory(course,*a):
            if course=='course':raise StudyError('directory malformed')
            return {'points':[{**f.POINT,'has_finished':done}]}
        def process(*a):
            nonlocal done
            done=True
            return e.main.StudyResult.SUCCESS
        e.cx.get_course_point.side_effect=directory;e.main.process_job.side_effect=process
        with patch('study_runner.read_chapter_jobs',return_value=[(f.job('video'),{})]):
            e.run([('course','class'),('other','class')])
        e.main.process_job.assert_called_once()
        self.assertEqual(len(e.round_history),4)
        self.assertEqual(e.store.read()['pending'][0]['chapter_id'],'__directory__')

    def test_directory_marker_disappears_after_successful_refresh(self):
        e=self.engine()
        e.cx.get_course_point.side_effect=lambda *a: {'bad':'response'} if e._round_number==1 else {'points':[]}
        e.run([('course','class')])
        self.assertEqual(len(e.round_history),2)
        self.assertEqual(e.store.read()['pending'],[])
