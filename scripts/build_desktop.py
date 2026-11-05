"""Build a complete, ordinary Windows installer from clean desktop source."""
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'Chaoxing-Desktop-Setup-1.7.7-20261102-win64'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--iscc', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    output = args.output.resolve()
    stage = ROOT / 'dist/setup-build'
    payload = stage / 'payload'
    if stage.exists():
        raise SystemExit('Build staging already exists; use a clean checkout or archive this staging directory.')
    for name in ('python.exe', 'pythonw.exe', 'python313.dll', 'Lib'):
        if not (runtime / name).exists():
            raise SystemExit('A complete portable Python 3.13 runtime is required: ' + name)
    payload.mkdir(parents=True)
    output.mkdir(parents=True, exist_ok=True)
    source = ROOT / 'desktop'
    for path in source.glob('*.py'):
        if not path.name.startswith('test_') and path.name != 'full_upgrade.py':
            shutil.copy2(path, payload / path.name)
    for name in ('start.cmd', 'requirements.lock.txt', '使用说明.md'):
        shutil.copy2(source / name, payload / name)
    metadata = json.loads((source / 'installation-details.json').read_text('utf-8'))
    metadata.update(package_type='full-installer', package_revision='1.7.7-setup-20261102', includes_user_data=False)
    (payload / 'installation-details.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), 'utf-8')
    shutil.copytree(source / 'app', payload / 'app', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.log', 'cookies.txt', 'cache.json'))
    shutil.copytree(runtime, payload / 'python', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.log'))
    shutil.copy2(ROOT / 'LICENSE', payload / 'LICENSE')
    shutil.copytree(ROOT / 'docs/third-party-licenses', payload / 'third-party-licenses')
    shutil.copy2(ROOT / 'docs/THIRD_PARTY_NOTICES.md', payload / '第三方说明.md')
    shutil.copy2(ROOT / 'docs/dependencies.csv', payload / 'dependencies.csv')
    manifest = {p.relative_to(payload).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in payload.rglob('*') if p.is_file()}
    for name in manifest:
        if {'accounts', 'reports', 'backups'}.intersection(Path(name).parts) or Path(name).name in {'cookies.txt', 'ai_quiz.json', 'profile.json', 'account_groups.json'}:
            raise SystemExit('Runtime user data found in build material: ' + name)
    (stage / 'setup-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    shutil.copytree(ROOT / 'packaging/languages', stage / 'languages')
    template = (ROOT / 'packaging/desktop-setup.iss.in').read_text('utf-8-sig').replace('@OUTPUT_DIR@', str(output))
    script = stage / 'desktop-setup.iss'
    script.write_text(template, 'utf-8-sig')
    subprocess.run([str(args.iscc.resolve()), str(script)], cwd=stage, check=True)
    installer = output / (PACKAGE + '.exe')
    installer.with_suffix('.exe.sha256').write_text(hashlib.sha256(installer.read_bytes()).hexdigest() + '  ' + installer.name + '\n', 'utf-8')
    print('Complete installer built: ' + str(installer))


if __name__ == '__main__':
    main()
