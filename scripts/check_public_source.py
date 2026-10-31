"""Reject runtime artifacts and obvious credentials before publishing (not a complete secret audit)."""
from pathlib import Path
import configparser,re,subprocess
ROOT=Path(__file__).resolve().parents[1]
def check():
 tracked=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT).decode('utf-8').split('\0')
 errors=[]
 for name in filter(None,tracked):
  p=ROOT/name
  if not p.is_file():continue
  parts=Path(name).parts
  if any(part in {'accounts','reports','backups','__pycache__','.venv','.test-temp'} for part in parts):errors.append(name+': runtime directory')
  if p.name in {'cookies.txt','ai_quiz.json','profile.json','account_groups.json','quiz_submissions.json'} or p.suffix in {'.exe','.zip','.log','.pyc'}:errors.append(name+': runtime/binary artifact')
  try:text=p.read_text('utf-8-sig')
  except UnicodeError:continue
  if re.search(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-[A-Za-z0-9]{24,})',text):errors.append(name+': possible access token')
  if re.search(r'[A-Za-z]:[\\/]Users[\\/](?!Public[\\/])[^\\/\s]+[\\/]',text):errors.append(name+': local user profile path')
 cfg=configparser.ConfigParser(interpolation=None);cfg.read(ROOT/'desktop/app/config.ini',encoding='utf-8')
 for field in ['username','password','course_list']:
  if cfg.get('common',field,fallback='').strip():errors.append('desktop/app/config.ini: nonblank '+field)
 if errors:raise SystemExit('\n'.join(errors))
 print('Public-source checks passed; no runtime artifacts or matching tokens found.')
if __name__=='__main__':check()
