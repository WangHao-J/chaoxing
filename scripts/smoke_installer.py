"""Exercise the built EXE in a disposable synthetic legacy directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('installer', type=Path)
    args = parser.parse_args()
    installer = args.installer.resolve()
    repository = Path(__file__).resolve().parents[1]
    manifest = json.loads((repository / 'dist/build/full-manifest.json').read_text('utf-8'))
    with tempfile.TemporaryDirectory(prefix='desktop-upgrade-smoke-') as directory:
        root = Path(directory) / 'legacy'
        fixtures = {
            'account_manager.py': b'# synthetic legacy entry\n',
            'app/main.py': b'# synthetic legacy app\n',
            'app/config.ini': b'[common]\nusername=synthetic\n',
            'accounts/example/profile.json': b'{"name":"Synthetic account"}',
            'accounts/example/cookies.txt': b'synthetic fixture, not a login cookie',
            'ai_quiz.json': b'{"fixture":true}',
            'reports/example/history.md': b'synthetic history',
            'custom-data.txt': b'unknown legacy data',
        }
        for name, content in fixtures.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        def run(*extra):
            log = repository / 'dist' / ('smoke-rollback.log' if extra else 'smoke-upgrade.log')
            command = [str(installer), '/SP-', '/VERYSILENT', '/SUPPRESSMSGBOXES',
                       '/NORESTART', '/QAISOLATED=1', '/LOG=' + str(log), '/DIR=' + str(root), *extra]
            subprocess.run(command, check=True, timeout=900)

        run()
        for name, expected in manifest.items():
            if name != 'app/config.ini' and digest(root / name) != expected:
                raise RuntimeError('Installed file differs: ' + name)
        for name, content in fixtures.items():
            if name not in {'account_manager.py', 'app/main.py'} and (root / name).read_bytes() != content:
                raise RuntimeError('Legacy data changed: ' + name)
        subprocess.run([str(root / 'python/python.exe'), '-B', '-c',
                        'import tkinter, requests, lxml; import account_manager'],
                       cwd=root, env={**os.environ, 'PYTHONUTF8': '1'},
                       check=True, timeout=60)
        new_record = root / 'accounts/example/after-upgrade.txt'
        new_record.write_bytes(b'new user data')
        run('/ROLLBACK=1')
        for name, content in fixtures.items():
            if (root / name).read_bytes() != content:
                raise RuntimeError('Rollback differs: ' + name)
        if new_record.read_bytes() != b'new user data':
            raise RuntimeError('Rollback lost new data')
        print('EXE smoke passed: installed hashes, data retention, relocated runtime and rollback.')


if __name__ == '__main__':
    main()
