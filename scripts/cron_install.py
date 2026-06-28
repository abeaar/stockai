"""Install / show / remove the daily intraday cron jobs.

Two crons:
  1. 08:30 WIB (01:30 UTC) Mon-Fri  - generate today's report
  2. 16:30 WIB (09:30 UTC) Mon-Fri  - evaluate yesterday's outcomes

Both run `uv run python scripts/intraday_push.py <mode>` from the project
root. The push script handles Discord/Telegram delivery.

Usage:
    python scripts/cron_install.py install    # add to user's crontab
    python scripts/cron_install.py show       # show current crontab
    python scripts/cron_install.py remove     # remove our lines

Note: this uses the system crontab (cron / schtasks). The user is on
Windows, so we ALSO install a Task Scheduler entry.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "intraday_push.py"

# Cron line markers so we can find our own entries to remove them.
# Schedule is in local crontab time. User is in WIB (UTC+7); local = WIB
# for Windows users running this script directly. For a Unix server in
# UTC, you'd need to shift by +7.
CRON_TAG = "# stockai-intraday-push"
CRON_LINES = [
    f"30 8 * * 1-5 cd {PROJECT_ROOT} && /usr/bin/env -i PATH=/usr/bin:/usr/local/bin HOME={os.path.expanduser('~')} /usr/bin/uv run python {SCRIPT} report {CRON_TAG}",
    f"30 16 * * 1-5 cd {PROJECT_ROOT} && /usr/bin/env -i PATH=/usr/bin:/usr/local/bin HOME={os.path.expanduser('~')} /usr/bin/uv run python {SCRIPT} evaluate {CRON_TAG}",
]


def _is_windows() -> bool:
    return platform.system().lower().startswith("win")


# ---------------------------------------------------------------------------
# Unix cron (crontab)
# ---------------------------------------------------------------------------

def unix_install() -> int:
    try:
        existing = subprocess.check_output(["crontab", "-l"], text=True)
    except subprocess.CalledProcessError:
        existing = ""
    # Strip any previous stockai-intraday lines (idempotent).
    lines = [
        ln for ln in existing.splitlines()
        if not ln.strip().endswith(CRON_TAG) and ln.strip() != CRON_TAG
    ]
    lines.extend(CRON_LINES)
    new = "\n".join(lines) + "\n"
    p = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE, text=True)
    p.communicate(new)
    if p.returncode != 0:
        print("crontab install failed.", file=sys.stderr)
        return 1
    print("Installed crontab entries:")
    for ln in CRON_LINES:
        print(" ", ln)
    return 0


def unix_show() -> int:
    try:
        out = subprocess.check_output(["crontab", "-l"], text=True)
    except subprocess.CalledProcessError as e:
        print(f"No crontab or error: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


def unix_remove() -> int:
    try:
        existing = subprocess.check_output(["crontab", "-l"], text=True)
    except subprocess.CalledProcessError:
        print("No crontab to modify.")
        return 0
    lines = [
        ln for ln in existing.splitlines()
        if not ln.strip().endswith(CRON_TAG) and ln.strip() != CRON_TAG
    ]
    new = "\n".join(lines) + "\n" if lines else ""
    p = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE, text=True)
    p.communicate(new)
    if p.returncode != 0:
        print("crontab remove failed.", file=sys.stderr)
        return 1
    print("Removed stockai-intraday crontab entries.")
    return 0


# ---------------------------------------------------------------------------
# Windows Task Scheduler
# ---------------------------------------------------------------------------

PS_HEADER = r"""$ErrorActionPreference = 'Stop'
$project = "{project}"
$script  = "{script}"
"""


def _run_ps_script(ps_body: str) -> subprocess.CompletedProcess:
    """Write a PowerShell script to a temp file and execute it.

    We use a temp file (not -Command) so $args[0] is populated correctly.
    """
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", delete=False, encoding="utf-8"
    ) as f:
        f.write(PS_HEADER.format(project=str(PROJECT_ROOT), script=str(SCRIPT)))
        f.write(ps_body)
        tmp = f.name
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp],
            capture_output=True, text=True,
        )
        return p
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def windows_install() -> int:
    # Schedule in *local* machine time. The user is in WIB (UTC+7), so
    # local time = WIB for them. If you're on a different timezone,
    # adjust the two literal times below.
    body = r"""
function Install-StockAI ([string]$Name, [string]$Time, [string]$ModeArg) {
    $action = New-ScheduledTaskAction `
        -Execute "uv" `
        -Argument "run python `"$script`" $ModeArg" `
        -WorkingDirectory $project
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $Time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "Installed: $Name  (weekly Mon-Fri at $Time local time, mode=$ModeArg)"
}
Install-StockAI 'StockAI-Intraday-Report'   '08:30' 'report'
Install-StockAI 'StockAI-Intraday-Evaluate' '16:30' 'evaluate'
"""
    p = _run_ps_script(body)
    print(p.stdout)
    if p.returncode != 0:
        print(p.stderr, file=sys.stderr)
    return p.returncode


def windows_show() -> int:
    body = r"""
Get-ScheduledTask | Where-Object { $_.TaskName -like 'StockAI-*' } | Format-Table TaskName, State
"""
    p = _run_ps_script(body)
    print(p.stdout)
    return p.returncode


def windows_remove() -> int:
    body = r"""
function Remove-StockAI ([string]$Name) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed (if existed): $Name"
}
Remove-StockAI 'StockAI-Intraday-Report'
Remove-StockAI 'StockAI-Intraday-Evaluate'
"""
    p = _run_ps_script(body)
    print(p.stdout)
    return p.returncode


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = sys.argv[1]
    if _is_windows():
        fn = {"install": windows_install, "show": windows_show, "remove": windows_remove}.get(cmd)
    else:
        fn = {"install": unix_install, "show": unix_show, "remove": unix_remove}.get(cmd)
    if fn is None:
        print(f"Unknown command: {cmd}. Use install|show|remove.")
        return 1
    return fn()


if __name__ == "__main__":
    sys.exit(main())
