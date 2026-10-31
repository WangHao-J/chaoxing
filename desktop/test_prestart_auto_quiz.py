"""Offline checks for pre-start AI consent, account isolation and worker delivery."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import test_single_workspace as ui
import test_study_runner as backend
from workspace_worker import Worker
from workspace_transport import WorkerClient


class PrestartUITests(unittest.TestCase):
    setUp = ui.WorkspaceTests.setUp
    tearDown = ui.WorkspaceTests.tearDown
    pump = ui.WorkspaceTests.pump
    load = ui.WorkspaceTests.load
    callback_error = ui.WorkspaceTests.callback_error

    def test_enable_before_run_reaches_selected_account_and_can_be_disabled(self):
        a = self.load(self.a)
        b = self.load(self.b)
        self.assertFalse(a.study.auto_quizzes_enabled)
        self.app.select(self.a.id)
        a.study.auto_quiz_button.invoke()
        self.assertTrue(a.study.auto_quizzes_enabled)
        self.assertFalse(b.study.auto_quizzes_enabled)
        self.assertIn('提交',a.study.status.get())
        a.study.start_selected()
        self.pump(lambda:self.engines[self.a.id].started.is_set())
        self.assertTrue(self.engines[self.a.id].auto_quizzes)
        self.assertEqual(str(a.study.auto_quiz_button.cget('state')),'disabled')
        a.study.stop()
        self.pump(lambda:not a.study.busy)
        a.study.auto_quiz_button.invoke()
        self.assertFalse(a.study.auto_quizzes_enabled)
        self.engines[self.a.id].started.clear()
        a.study.start_selected()
        self.pump(lambda:self.engines[self.a.id].started.is_set())
        self.assertFalse(self.engines[self.a.id].auto_quizzes)

    def test_incomplete_settings_do_not_enable_or_start(self):
        a=self.load(self.a)
        with patch.object(a,'ensure_ai',return_value=False):
            a.study.auto_quiz_button.invoke()
        self.assertFalse(a.study.auto_quizzes_enabled)
        self.assertFalse(a.study.busy)
        self.assertFalse(self.engines[self.a.id].started.is_set())

    def test_toolbar_fits_minimum_width_and_default_is_manual(self):
        a=self.load(self.a)
        self.window.geometry('1120x740+10000+10000')
        self.window.update()
        for button in (a.study.start_button,a.study.stop_button,a.study.auto_quiz_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),self.window.winfo_rootx()+self.window.winfo_width())
        a.study.start_selected()
        self.pump(lambda:self.engines[self.a.id].started.is_set())
        self.assertFalse(self.engines[self.a.id].auto_quizzes)

    def test_quiz_dialog_choice_is_reflected_in_toolbar(self):
        a=self.load(self.a)
        self.engines[self.a.id].review=True
        a.study.start_selected()
        self.pump(lambda:a.study.dialog is not None)
        a.study.finish_dialog({'action':'auto_all_quizzes','answers':{}})
        self.pump(lambda:not a.study.busy)
        self.assertTrue(a.study.auto_quizzes_enabled)
        self.assertIn('开启',a.study.auto_quiz_text.get())


class PrestartEngineTests(backend.StudyFixture):
    engine=backend.StudyEngineTests.engine

    def test_explicit_choice_applies_before_first_chapter_read_and_can_be_revoked(self):
        e=self.engine('quizzes')
        seen=[]
        e.cx.get_course_point.side_effect=lambda *args:(seen.append(e.coordinator.auto_quizzes) or {'points':[]})
        with patch('quiz_assistant.read_settings',return_value=backend.SETTINGS):
            e.run([('course','class')],auto_quizzes=True)
            e.run([('course','class')],auto_quizzes=False)
        self.assertEqual(seen,[True,False])
        e.coordinator.auto_quizzes=True
        with patch('quiz_assistant.read_settings',return_value=backend.SETTINGS):
            e.run([('course','class')])
        self.assertTrue(e.coordinator.auto_quizzes)

    def test_video_mode_cannot_enable_automatic_quizzes(self):
        e=self.engine('videos')
        e.cx.get_course_point.return_value={'points':[]}
        e.run([('course','class')],auto_quizzes=True)
        self.assertFalse(e.coordinator.auto_quizzes)

    def test_non_boolean_choice_rejected_before_platform_read(self):
        e=self.engine('quizzes')
        with patch('quiz_assistant.read_settings',return_value=backend.SETTINGS),self.assertRaises(backend.StudyError):
            e.run([('course','class')],auto_quizzes='true')
        e.cx.get_course_point.assert_not_called()


class PrestartTransportTests(unittest.TestCase):
    def test_client_and_worker_keep_explicit_false_and_true(self):
        for enabled in (True,False,None):
            client=object.__new__(WorkerClient)
            client.request=Mock()
            client.run([['course','class']],1,enabled)
            args=client.request.call_args.args[1]
            self.assertIs(args['auto_quizzes'],enabled)
            worker=SimpleNamespace(cancelled=threading.Event(),fixture=False,engine=Mock())
            Worker.operate(worker,'run',args)
            worker.engine.run.assert_called_once_with([['course','class']],1,enabled)

if __name__=='__main__': unittest.main()
