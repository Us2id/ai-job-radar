"""Daily runs without a server: a macOS LaunchAgent (launchd) or a Linux cron line."""
from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .pipeline.store import default_home

LABEL = "io.github.us2id.ai-job-radar"


def _exe() -> str:
    found = shutil.which("ai-job-radar")
    return found or f"{sys.executable} -m ai_job_radar"


def install(hour: int, minute: int, run_args: list[str]) -> str:
    home = default_home()
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    exe = _exe().split()
    args = exe + ["run", "--out", str(home / "output" / "ai-jobs_{date}.xlsx")] + run_args
    if platform.system() == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "Label": LABEL,
            "ProgramArguments": [a.replace("{date}", "latest") for a in args],
            "StartCalendarInterval": {"Hour": hour, "Minute": minute},
            "StandardOutPath": str(logs / "run.log"),
            "StandardErrorPath": str(logs / "run.err.log"),
            "WorkingDirectory": str(home),
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "AI_JOB_RADAR_HOME": str(home)},
            "RunAtLoad": False,
        }
        with open(plist, "wb") as fh:
            plistlib.dump(data, fh)
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        r = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True)
        if r.returncode != 0:
            return f"wrote {plist} but launchctl load failed: {r.stderr.strip()}"
        return f"scheduled daily at {hour:02d}:{minute:02d} via launchd ({plist}); logs in {logs}"
    line = f"{minute} {hour} * * * {' '.join(a.replace('{date}', 'latest') for a in args)} >> {logs / 'run.log'} 2>&1"
    return "add this line with `crontab -e`:\n" + line


def uninstall() -> str:
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if plist.exists():
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        plist.unlink()
        return f"removed {plist}"
    return "nothing scheduled"


def status() -> str:
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if not plist.exists():
        return "not scheduled"
    r = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
    return f"{plist}\n{r.stdout.strip() or r.stderr.strip()}"
