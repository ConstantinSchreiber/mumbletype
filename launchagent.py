"""Start-at-login via a launchd LaunchAgent, with restart-on-crash."""

import logging
import os
import plistlib
import subprocess
import sys

log = logging.getLogger(__name__)

LABEL = "com.mumbletype"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
_LAUNCHD_ENV = "MUMBLETYPE_LAUNCHD"


def is_installed() -> bool:
    return os.path.exists(PLIST_PATH)


def is_launchd_managed() -> bool:
    """True when this process was started by our LaunchAgent."""
    return _LAUNCHD_ENV in os.environ


def _plist_dict() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "mumbletype.py"),
        ],
        "RunAtLoad": True,
        # Restart after a crash (non-zero exit), but not after a clean Quit.
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {_LAUNCHD_ENV: "1"},
        "StandardErrorPath": os.path.expanduser("~/Library/Logs/Mumbletype.launchd.log"),
    }


def _launchctl(*args) -> subprocess.CompletedProcess:
    proc = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        log.info("launchctl %s -> %d %s", " ".join(args), proc.returncode, proc.stderr.strip())
    return proc


def install():
    """Write the plist and start the launchd-managed job.

    When called from a manually-started instance, this immediately spawns the
    launchd copy — the caller should then terminate so only one instance runs.
    """
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(_plist_dict(), f)
    if is_launchd_managed():
        log.info("LaunchAgent plist refreshed (already running under launchd)")
        return
    domain = f"gui/{os.getuid()}"
    # bootstrap fails if the label is already loaded — boot any stale job out first
    _launchctl("bootout", f"{domain}/{LABEL}")
    _launchctl("bootstrap", domain, PLIST_PATH)
    log.info("LaunchAgent installed and started: %s", PLIST_PATH)


def uninstall():
    if os.path.exists(PLIST_PATH):
        os.remove(PLIST_PATH)
    if is_launchd_managed():
        # bootout would SIGTERM this very process. Removing the plist is
        # enough: no start at next login, and the current session lives on.
        log.info("LaunchAgent removed; current session keeps running")
    else:
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
        log.info("LaunchAgent removed")
