"""Verify a standalone Windows installer using only disposable synthetic data.

The installer must honor /QAISOLATED=1: no shortcuts, external registration,
or application launch. Its directory-local uninstaller remains available.
"""
import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_report(path, report):
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    installer = args.installer.resolve(strict=True)
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError('Manifest must map relative program paths to SHA-256 values.')
    for name in manifest:
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Unsafe path in manifest: ' + name)
    report = {'installer': installer.name, 'sha256': digest(installer),
              'manifest_files': len(manifest), 'passed': False,
              'real_accounts_used': False, 'stage': 'starting'}

    def record(stage):
        report['stage'] = stage
        write_report(output, report)

    try:
        # The only directory cleaned recursively is this generated child of the
        # caller-selected report directory, never a user's existing installation.
        with tempfile.TemporaryDirectory(prefix='setup-smoke-', dir=output.parent) as directory:
            scratch = Path(directory).resolve()
            if not scratch.is_relative_to(output.parent) or scratch == output.parent:
                raise RuntimeError('Temporary test directory escaped its intended parent.')
            root = scratch / 'clean-install'

            def install(stage):
                record(stage)
                log = output.with_name(output.stem + '-' + stage + '.log')
                subprocess.run([str(installer), '/SP-', '/VERYSILENT', '/SUPPRESSMSGBOXES',
                                '/NORESTART', '/QAISOLATED=1', '/LOG=' + str(log),
                                '/DIR=' + str(root)], check=True, timeout=900)

            def verify_program(except_config=False):
                for name, expected in manifest.items():
                    if except_config and name.replace('\\', '/') == 'app/config.ini':
                        continue
                    actual = root / name
                    if not actual.is_file() or digest(actual) != expected:
                        raise RuntimeError('Installed file differs from manifest: ' + name)

            install('fresh-install')
            record('verify-clean-install')
            verify_program()
            for name in ('account_manager.py', 'workspace.py', 'app/main.py',
                         'python/python.exe', 'python/pythonw.exe', 'unins000.exe'):
                if not (root / name).is_file():
                    raise RuntimeError('Required installed file is missing: ' + name)
            for name in ('accounts', 'reports', 'ai_quiz.json', 'cookies.txt'):
                if (root / name).exists():
                    raise RuntimeError('Unexpected runtime data in clean installation: ' + name)
            config = configparser.ConfigParser(interpolation=None)
            config.read(root / 'app/config.ini', encoding='utf-8-sig')
            for field in ('username', 'password', 'course_list'):
                if config.get('common', field, fallback='').strip():
                    raise RuntimeError('Clean configuration is not blank: ' + field)
            report['fresh_install_hashes_verified'] = True
            report['blank_defaults_verified'] = True

            record('runtime-and-offline-check')
            env = dict(os.environ)
            # Do not let the build machine's Python/Tcl configuration make an
            # otherwise incomplete bundled runtime appear portable.
            for variable in ('PYTHONHOME', 'PYTHONPATH', 'TCL_LIBRARY', 'TK_LIBRARY'):
                env.pop(variable, None)
            env['PYTHONUTF8'] = '1'
            runtime_temp = scratch / 'runtime-temp'
            runtime_temp.mkdir()
            env['TEMP'] = env['TMP'] = str(runtime_temp)
            python = str(root / 'python/python.exe')
            commands = [
                [python, '-B', '-c',
                 'import tkinter as tk; import requests, lxml; '
                 'w=tk.Tk(); w.withdraw(); w.update_idletasks(); w.destroy(); '
                 'import account_manager; print("Bundled Python and Tk initialized.")'],
                # Inspected entry path: creates a local default profile and
                # checks its config/resource paths; does not log in or study.
                [python, '-B', str(root / 'account_manager.py'), '--check'],
            ]
            with output.with_name(output.stem + '-runtime.log').open('w', encoding='utf-8') as log:
                for command in commands:
                    completed = subprocess.run(command, cwd=root, env=env,
                                               capture_output=True, text=True,
                                               encoding='utf-8', errors='replace', timeout=90)
                    log.write(completed.stdout + completed.stderr)
                    log.flush()
                    completed.check_returncode()
            report['bundled_python_and_tk_initialized'] = True
            report['offline_profile_check_passed'] = True

            record('prepare-synthetic-data')
            fixtures = {
                'accounts/example/profile.json': b'{"id":"example","name":"Synthetic account"}',
                'accounts/example/cookies.txt': b'synthetic fixture, not a login cookie',
                'ai_quiz.json': b'{"fixture":true}',
                'reports/example/history.md': b'synthetic history',
                'custom-data.txt': b'synthetic unrelated user file',
                'app/config.ini': (root / 'app/config.ini').read_bytes() + b'\n; synthetic saved preference\n',
            }
            for name, content in fixtures.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            def verify_data():
                for name, content in fixtures.items():
                    path = root / name
                    if not path.is_file() or path.read_bytes() != content:
                        raise RuntimeError('Synthetic user data changed: ' + name)

            install('repeat-install')
            record('verify-repeat-install')
            verify_program(except_config=True)
            verify_data()
            report['repeat_install_preserved_data'] = True

            record('uninstall')
            uninstall_log = output.with_name(output.stem + '-uninstall.log')
            subprocess.run([str(root / 'unins000.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES',
                            '/NORESTART', '/LOG=' + str(uninstall_log)], check=True, timeout=900)
            # Inno's temporary uninstaller may finish its self-deletion just
            # after the initiating executable returns.
            deadline = time.monotonic() + 15
            program_markers = ('account_manager.py', 'workspace.py', 'app/main.py', 'python/python.exe')
            while any((root / name).exists() for name in program_markers) and time.monotonic() < deadline:
                time.sleep(0.25)
            for name in program_markers:
                if (root / name).exists():
                    raise RuntimeError('Uninstall left an installed program file: ' + name)
            verify_data()
            report['uninstall_removed_program'] = True
            report['uninstall_preserved_data'] = True
        report.update(passed=True, stage='complete')
        write_report(output, report)
        print('SETUP smoke passed: fresh install, bundled Python/Tk, offline check, repeat install and data-safe uninstall.')
    except Exception as error:
        report['error'] = str(error)
        write_report(output, report)
        raise


if __name__ == '__main__':
    main()
