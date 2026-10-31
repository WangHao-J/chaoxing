"""Build from portable source paths; supply a clean relocatable CPython runtime."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
 p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--iscc',type=Path,required=True);a=p.parse_args()
 runtime=a.runtime.resolve();stage=ROOT/'dist/build';payload=stage/'payload'
 if stage.exists():raise SystemExit('Build staging already exists; archive it or choose a clean checkout before rebuilding.')
 for n in ['python.exe','pythonw.exe','python313.dll','Lib']:
  if not (runtime/n).exists():raise SystemExit('Not a complete portable Python 3.13 runtime: '+n)
 stage.mkdir(parents=True);payload.mkdir()
 source=ROOT/'desktop'
 for f in source.glob('*.py'):
  if not f.name.startswith('test_') and f.name!='full_upgrade.py':shutil.copy2(f,payload/f.name)
 for name in ['start.cmd','requirements.lock.txt','installation-details.json','使用说明.md']:shutil.copy2(source/name,payload/name)
 shutil.copytree(source/'app',payload/'app',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.log','cookies.txt','cache.json'))
 shutil.copytree(runtime,payload/'python',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.log'))
 manifest={f.relative_to(payload).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in payload.rglob('*') if f.is_file()}
 if any(Path(n).name in ['cookies.txt','ai_quiz.json','profile.json','account_groups.json'] for n in manifest):raise SystemExit('Runtime data found in build material')
 (stage/'full-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
 shutil.copy2(source/'full_upgrade.py',stage/'full_upgrade.py')
 shutil.copy2(ROOT/'packaging/升级说明.txt',stage/'升级说明.txt')
 template=(ROOT/'packaging/full-upgrade.iss.in').read_text('utf-8-sig').replace('@OUTPUT_DIR@',str(ROOT/'dist'))
 (stage/'full-upgrade.iss').write_text(template,'utf-8-sig')
 subprocess.run([str(a.iscc.resolve()),str(stage/'full-upgrade.iss')],cwd=stage,check=True)
 for f in (ROOT/'dist').glob('*.exe'):
  f.with_suffix(f.suffix+'.sha256').write_text(hashlib.sha256(f.read_bytes()).hexdigest()+'  '+f.name+'\n','utf-8')
 print('Build complete. Validate in isolated old-version directories before publishing.')
if __name__=='__main__':main()
