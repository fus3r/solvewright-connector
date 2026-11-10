"""Approving this computer from the browser, and the key that approval yields.

The flow is the one `gh auth login` uses: the terminal shows a short code, the
person signed in to Solvewright confirms it on solvewright.app/connect, and
only then does this computer receive its own key. The command line never
carries a secret, and the key never leaves this machine; the service keeps a
hash of it.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .service import Service, ServiceError, Unavailable

FORMAT = "solvewright-connection-v1"


class ApprovalDenied(Exception):
    pass


class ApprovalExpired(Exception):
    pass


def computer_name() -> str:
    name = ""
    if sys.platform == "darwin":
        try:
            name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            name = ""
    elif sys.platform == "win32":
        name = os.environ.get("COMPUTERNAME", "")
    name = name or platform.node()
    name = re.sub(r"\.(local|lan|home)$", "", name.strip())
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    return name[:64] or "This computer"


def launcher() -> str:
    """How this copy was started, so the terminal and Interview Lab can say what to run next time.

    uvx and pipx run keep the package in a cache and put no command on the PATH, which is why
    a plain `solvewright` would not be found afterwards."""
    prefix = Path(sys.prefix).as_posix().lower()
    if "/uv/" in prefix and "/archive-v" in prefix:
        return "uvx"
    # `pipx run` keeps throwaway environments in a cache whose place changed between pipx versions;
    # `pipx install` keeps them under venvs/ and puts the command on the PATH.
    if "/pipx/" in prefix and "/venvs/" not in prefix:
        return "pipx"
    return "command" if shutil.which("solvewright") else "module"


def next_command(kind: str | None = None) -> str:
    kind = kind or launcher()
    if kind == "uvx":
        return "uvx solvewright"
    if kind == "pipx":
        return "pipx run solvewright"
    if kind == "command":
        return "solvewright"
    return ("py" if sys.platform == "win32" else "python3") + " -m solvewright"


def operating_system() -> str:
    if sys.platform == "darwin":
        return "macOS " + (platform.mac_ver()[0] or "").strip()
    if sys.platform == "win32":
        return "Windows " + platform.release()
    try:
        release = platform.freedesktop_os_release()
        return release.get("PRETTY_NAME") or "Linux"
    except (OSError, AttributeError):
        return "Linux"


def load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    if not all(isinstance(data.get(field), str) and data[field] for field in ("workerId", "owner", "key", "service")):
        return None
    return data


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".connection-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump({"format": FORMAT, **data}, file, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def forget(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def request(service: Service, label: str, details: dict) -> dict:
    reply = service.rpc("start_interview_connection", {"p_label": label, "p_details": details})
    if not isinstance(reply, dict) or not all(isinstance(reply.get(key), str) for key in ("deviceCode", "userCode")):
        raise ServiceError("Solvewright returned an incomplete connection request.")
    return reply


def wait(service: Service, pending: dict, on_wait=None, sleep=time.sleep, now=time.monotonic) -> dict:
    """Poll until the browser approves or denies, or the code expires."""
    interval = max(2, int(pending.get("interval") or 3))
    deadline = now() + max(60, int(pending.get("expiresIn") or 600))
    while now() < deadline:
        try:
            reply = service.rpc("poll_interview_connection", {"p_device_code": pending["deviceCode"]})
        except Unavailable:
            reply = {"status": "pending"}
        status = reply.get("status") if isinstance(reply, dict) else None
        if status == "approved":
            if not all(isinstance(reply.get(key), str) and reply[key] for key in ("workerId", "owner", "key")):
                raise ServiceError("Solvewright approved this computer but sent an incomplete key.")
            return reply
        if status == "denied":
            raise ApprovalDenied()
        if status == "expired":
            raise ApprovalExpired()
        if on_wait:
            on_wait(max(0, int(deadline - now())))
        sleep(interval)
    raise ApprovalExpired()


def record(reply: dict, service_url: str, label: str) -> dict:
    return {
        "service": service_url,
        "workerId": reply["workerId"],
        "owner": reply["owner"],
        "key": reply["key"],
        "account": reply.get("handle") or "",
        "label": label,
        "connectedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
