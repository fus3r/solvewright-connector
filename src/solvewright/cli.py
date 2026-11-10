"""`solvewright`: connect this computer to Interview Lab and serve its rounds.

    solvewright               connect if needed, then wait for interview rounds
    solvewright connect       approve this computer again (for example after a reset)
    solvewright status        show the connection and the agents found here
    solvewright disconnect    remove this computer from your account
    solvewright doctor        check Python, Claude Code, Codex and the sandbox

Nothing runs in the background: closing the window stops the connector.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from . import __version__, agents, connection, settings as settings_module, worker
from .runtime import Runtime
from .service import Service, ServiceError, Unavailable

INSTRUCTIONS = Path(__file__).resolve().parent / "interviewer"
ISOLATION_TEXT = {"sandbox": "sandboxed", "writes": "writes sandboxed, reads not limited",
                  "no-commands": "no commands (sandbox unavailable here)"}


class Out:
    def __init__(self, stream=sys.stdout):
        self.stream = stream
        self.color = stream.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def good(self, text: str) -> str:
        return self._wrap("32", text)

    def warn(self, text: str) -> str:
        return self._wrap("33", text)

    def line(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def row(self, label: str, value: str) -> None:
        self.line("  " + self.dim(label.ljust(14)) + value)

    def event(self, text: str) -> None:
        self.line(self.dim(datetime.now().strftime("%H:%M")) + "  " + text)


def pretty_path(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def agent_rows(out: Out, described: dict) -> None:
    for name, label in (("claude", "Claude Code"), ("codex", "Codex")):
        info = described[name]
        if info["available"]:
            out.row(label, f"{info['version']} · {ISOLATION_TEXT.get(info['isolation'], info['isolation'])}")
        else:
            out.row(label, out.dim("not found"))


def header(out: Out, config, label: str, described: dict, account: str | None = None) -> None:
    out.line(out.bold("Solvewright connector") + " " + out.dim(__version__))
    out.line()
    out.row("This computer", f"{label} · {connection.operating_system()}")
    if account:
        out.row("Account", "@" + account)
    agent_rows(out, described)
    out.row("Interviews", pretty_path(config.interviews))
    if not any(info["available"] for info in described.values()):
        out.line()
        out.line(out.warn("No Claude Code or Codex was found on this computer."))
        out.line("Install one and sign in once in a terminal, then start the connector again:")
        out.line("  Claude Code  https://code.claude.com/docs/en/setup")
        out.line("  Codex        https://developers.openai.com/codex/cli")


def approve(out: Out, config, service: Service, label: str, open_browser: bool) -> dict:
    described = agents.describe()
    details = {"connector": __version__, "os": connection.operating_system(), "launcher": connection.launcher(),
               "python": ".".join(str(part) for part in sys.version_info[:3]),
               "agents": {name: info["version"] for name, info in described.items() if info["available"]}}
    pending = connection.request(service, label, details)
    code = pending["userCode"]
    url = f"{config.site_url}/connect?code={code}"
    out.line()
    out.line("Approve this computer in your browser:")
    out.line()
    out.line("    " + out.bold(url))
    out.line()
    out.line("  Check that the page shows  " + out.bold(code))
    out.line()
    if open_browser:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass
    shown = {"last": None}

    def waiting(seconds_left: int) -> None:
        minutes = seconds_left // 60
        if shown["last"] != minutes:
            shown["last"] = minutes
            out.event(out.dim(f"Waiting for approval ({minutes + 1} min left). Ctrl+C cancels."))

    reply = connection.wait(service, pending, on_wait=waiting)
    record = connection.record(reply, config.service_url, label)
    connection.save(config.connection_file, record)
    out.line()
    out.line(out.good("✓") + " Connected" + (f" to @{record['account']}" if record["account"] else "") + f" as “{label}”.")
    return record


def serve(out: Out, config, service: Service, record: dict) -> int:
    runtime = Runtime(config.interviews, INSTRUCTIONS, config.home / "logs")
    runtime.on_event = out.event
    out.line()
    out.line("Interview Lab is ready: " + out.bold(f"{config.site_url}/coding/interviews"))
    out.line("Keep this window open while you practise. Closing it stops the connector;")
    out.line("nothing keeps running in the background. Next time, run " + out.bold(connection.next_command()) + ": no new code is needed.")
    out.line()
    out.event("Waiting for the Interview Lab")
    if sys.platform == "win32":
        def stop_on_break(_signal, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK, stop_on_break)
    try:
        worker.run(runtime, service, record, say=out.event)
    except KeyboardInterrupt:
        out.line()
        out.line("Stopped. Nothing from Solvewright is running on this computer now.")
        return 0
    except worker.Revoked:
        connection.forget(config.connection_file)
        out.line()
        out.line(out.warn("This computer was disconnected from your Solvewright account."))
        out.line("Run `solvewright` again to connect it.")
        return 2
    except ServiceError as error:
        out.line(out.warn(str(error)))
        return 1
    finally:
        runtime.close()
    return 0


def start(out: Out, config, args) -> int:
    service = Service(config.service_url, config.service_key)
    label = (args.name or connection.computer_name()).strip()[:64]
    record = connection.load(config.connection_file)
    if record and record.get("service") != config.service_url:
        record = None
    described = agents.describe()
    header(out, config, record["label"] if record and not args.name else label, described, record.get("account") if record else None)
    if args.command == "connect" or not record:
        if record:
            try:
                service.rpc("remove_interview_worker", {"p_worker_id": record["workerId"], "p_key": record["key"]})
            except (OSError, ServiceError):
                pass
            connection.forget(config.connection_file)
        try:
            record = approve(out, config, service, label, not args.no_browser)
        except KeyboardInterrupt:
            out.line()
            out.line("Cancelled. This computer was not connected.")
            return 130
        except connection.ApprovalDenied:
            out.line(out.warn("The connection was declined in the browser. Nothing was saved."))
            return 1
        except connection.ApprovalExpired:
            out.line(out.warn("The code expired. Run `solvewright` again for a new one."))
            return 1
        except (Unavailable, ServiceError) as error:
            out.line(out.warn(str(error)))
            return 1
    return serve(out, config, service, record)


def status(out: Out, config) -> int:
    record = connection.load(config.connection_file)
    header(out, config, record["label"] if record else connection.computer_name(), agents.describe(), record.get("account") if record else None)
    out.line()
    if record:
        out.line(f"Connected since {record.get('connectedAt', 'an earlier session')}. Run `solvewright` to serve rounds.")
    else:
        out.line("Not connected. Run `solvewright` to approve this computer.")
    return 0


def disconnect(out: Out, config) -> int:
    record = connection.load(config.connection_file)
    if not record:
        out.line("This computer is not connected.")
        return 0
    try:
        Service(config.service_url, config.service_key).rpc("remove_interview_worker", {"p_worker_id": record["workerId"], "p_key": record["key"]})
    except (OSError, ServiceError) as error:
        out.line(out.warn(f"Could not reach Solvewright ({error}). Remove the computer from the Interview Lab page instead."))
    connection.forget(config.connection_file)
    out.line("Disconnected. Your interview files stay in " + pretty_path(config.interviews) + ".")
    return 0


def doctor(out: Out, config) -> int:
    out.line(out.bold("Solvewright connector") + " " + out.dim(__version__))
    out.line()
    out.row("Python", sys.version.split()[0] + ("" if sys.version_info >= (3, 10) else out.warn("  needs 3.10 or newer")))
    described = agents.describe(max_age=0)
    agent_rows(out, described)
    claude = agents.executable("claude")
    if claude:
        out.row("Claude flags", "ok" if agents.claude_supports_isolation(claude) else out.warn("too old: run `claude update`"))
        out.row("Claude sandbox", "available" if agents.claude_can_sandbox() else out.warn(
            "unavailable" + (" (install bubblewrap and socat)" if sys.platform.startswith("linux") else "")))
    try:
        with urllib.request.urlopen(config.site_url, timeout=10) as response:
            out.row("Solvewright", "reachable" if response.status == 200 else f"HTTP {response.status}")
    except OSError as error:
        out.row("Solvewright", out.warn(f"unreachable ({error})"))
    record = connection.load(config.connection_file)
    out.row("Connection", f"@{record.get('account') or '?'} · {record['label']}" if record else "not connected")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="solvewright", description="Connect your own Claude Code or Codex to Solvewright Interview Lab.",
                                     epilog="Source: https://github.com/fus3r/solvewright-connector")
    parser.add_argument("command", nargs="?", default="start", choices=("start", "connect", "status", "disconnect", "doctor"))
    parser.add_argument("--name", help="how this computer is named in your account")
    parser.add_argument("--no-browser", action="store_true", help="print the approval link without opening a browser")
    parser.add_argument("--home", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"solvewright {__version__}")
    args = parser.parse_args(argv)
    out = Out()
    config = settings_module.load(args.home)
    if args.command == "status":
        return status(out, config)
    if args.command == "disconnect":
        return disconnect(out, config)
    if args.command == "doctor":
        return doctor(out, config)
    return start(out, config, args)


if __name__ == "__main__":
    raise SystemExit(main())
