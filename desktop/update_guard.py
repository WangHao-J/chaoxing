"""Read-only process check used by the installer before it takes account locks."""
import argparse
import json
import os
import subprocess
from pathlib import Path


QUERY = r"""$ErrorActionPreference = 'Stop'
$installRoot = [IO.Path]::GetFullPath($env:CHAOXING_UPDATE_ROOT).TrimEnd('\') + '\'
$busy = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @('python.exe', 'pythonw.exe') -and
    $_.CommandLine -match '(?i)(account_manager|run_account|pending_quiz_worker|study_window|login_window)\.py' -and
    (($_.ExecutablePath -and $_.ExecutablePath.StartsWith($installRoot, [StringComparison]::OrdinalIgnoreCase)) -or
     ($_.CommandLine -and $_.CommandLine.Replace('/', '\').IndexOf($installRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0))
})
if ($busy.Count -gt 0) { exit 2 }
exit 0
"""


def check(root, run=subprocess.run):
    root = Path(root).resolve()
    if not (root / "app/main.py").is_file() or not (root / "account_store.py").is_file():
        return 3
    try:
        result = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", QUERY],
                     env={**os.environ, "CHAOXING_UPDATE_ROOT": str(root)}, capture_output=True,
                     timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode if result.returncode in (0, 2) else 3
    except (OSError, subprocess.TimeoutExpired):
        return 3


def entry(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    result = check(args.root)
    print(json.dumps({"ready": result == 0, "busy": result == 2, "check_failed": result == 3}))
    return result


if __name__ == "__main__":
    raise SystemExit(entry())
