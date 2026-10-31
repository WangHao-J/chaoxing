import json,unittest,tempfile
from pathlib import Path
from unittest.mock import patch
import full_upgrade as u

class UpgradeTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
  self.root=Path(self.t.name)/'old';self.p=Path(self.t.name)/'payload';self.root.mkdir();self.p.mkdir()
  for n,v in {'account_manager.py':b'# old','app/main.py':b'# old app','app/config.ini':b'private configuration','accounts/a/profile.json':b'{"id":"a","name":"A"}','accounts/a/cookies.txt':b'private cookies','accounts/a/study_state_mixed.json':b'private progress','ai_quiz.json':b'private API','reports/a/latest.md':b'private report','old-unknown-data.txt':b'unknown private data'}.items():
   f=self.root/n;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(v)
  for n,v in {'account_manager.py':b'# new','app/main.py':b'# new app','app/config.ini':b'blank defaults','workspace.py':b'# new workspace'}.items():
   f=self.p/n;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(v)
  (self.p.parent/'full-manifest.json').write_text(json.dumps({f.relative_to(self.p).as_posix():u.sha(f) for f in self.p.rglob('*') if f.is_file()}),'utf-8')
  self.original={f.relative_to(self.root).as_posix():f.read_bytes() for f in self.root.rglob('*') if f.is_file()}
 def run_upgrade(self,**kwargs):return u.perform(self.root,self.p,check=lambda root:None,**kwargs)
 def test_ancient_versions_preserve_all_private_data(self):
  for version in ['1.0.0','1.3.10','1.7.7']:
   (self.root/'installation-details.json').write_text(json.dumps({'version':version}),'utf-8')
   r=self.run_upgrade();self.assertTrue(r['ok'])
   for n,b in self.original.items():
    if n not in ['account_manager.py','app/main.py']:self.assertEqual((self.root/n).read_bytes(),b)
   u.restore(self.root,r['backup'],check=lambda _:None)
 def test_failure_restores_every_replaced_file(self):
  with self.assertRaises(OSError):self.run_upgrade(fail_after=2)
  for n,b in self.original.items():self.assertEqual((self.root/n).read_bytes(),b)
  self.assertFalse((self.root/'workspace.py').exists())
 def test_corrupt_payload_is_rejected_before_backup(self):
  (self.p/'workspace.py').write_bytes(b'tampered')
  with self.assertRaises(ValueError):self.run_upgrade()
  self.assertFalse((self.root/'backups').exists())
 def test_backup_can_restore_old_program_without_reverting_new_data(self):
  r=self.run_upgrade();(self.root/'accounts/a/new-record.json').write_bytes(b'new data')
  u.restore(self.root,r['backup'],check=lambda _:None)
  self.assertEqual((self.root/'account_manager.py').read_bytes(),b'# old')
  self.assertEqual((self.root/'accounts/a/new-record.json').read_bytes(),b'new data')
 def test_locked_account_refuses_update(self):
  with u.lock(self.root/'accounts/a/.run.lock'),self.assertRaises(ValueError):self.run_upgrade()
  self.assertEqual((self.root/'account_manager.py').read_bytes(),b'# old')
 def test_locked_workspace_refuses_update(self):
  with u.lock(self.root/'.workspace.lock'),self.assertRaises(ValueError):self.run_upgrade()
 def test_backup_failure_never_overwrites_program(self):
  with patch.object(u.zipfile.ZipFile,'write',side_effect=OSError('disk failure')),self.assertRaises(OSError):self.run_upgrade()
  self.assertEqual((self.root/'account_manager.py').read_bytes(),b'# old')
 def test_later_program_edit_prevents_rollback(self):
  r=self.run_upgrade();(self.root/'workspace.py').write_bytes(b'later edit')
  with self.assertRaises(ValueError):u.restore(self.root,r['backup'],check=lambda _:None)

if __name__=='__main__':unittest.main()
