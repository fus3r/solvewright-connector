"""Running the person's own Claude Code or Codex, locked to one interview folder.

Both agents run unmodified and signed in with the person's own account. The
connector never reads or stores their credentials. What it adds is a boundary:

* Claude Code runs in `--restricted --safe-mode` with its OS sandbox for
  commands: file tools stay inside the round folder, commands cannot reach the
  network or read the rest of the home folder, and the person's own CLAUDE.md,
  hooks, plugins and MCP servers are not loaded.
* Codex runs through its documented app-server with a permission profile that
  denies the home folder except the round folders, its plugins, apps, hooks,
  web search and every configured MCP server switched off.

When a platform cannot provide that boundary, the agent loses the capability
rather than the boundary: Claude Code without its sandbox gets no command
tool at all, and the connector reports it.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

WINDOWS = sys.platform == "win32"

REPLY_SCHEMA = {"type": "object", "properties": {
    "message": {"type": "string"}, "title": {"type": "string"}, "caseFamily": {"type": "string"},
    "concepts": {"type": "array", "items": {"type": "string"}}, "stage": {"type": "string"},
    "part": {"type": "integer"}, "parts": {"type": "integer"}, "finished": {"type": "boolean"}},
    "required": ["message", "title", "caseFamily", "concepts", "stage", "part", "parts", "finished"], "additionalProperties": False}

# Claude Code resolves these aliases to the newest model of each family.
CLAUDE_MODELS = [{"id": "", "name": "Your Claude Code default"}, {"id": "fable", "name": "Fable"},
                 {"id": "opus", "name": "Opus"}, {"id": "sonnet", "name": "Sonnet"}, {"id": "haiku", "name": "Haiku"}]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Codex features that would give the interviewer reach beyond its folder.
CODEX_FEATURES_OFF = ("apps", "plugins", "plugin_hooks", "hooks", "computer_use", "browser_use", "in_app_browser",
                      "memories", "multi_agent", "multi_agent_v2", "remote_control", "image_generation", "js_repl",
                      "enable_mcp_apps", "standalone_web_search", "web_search_request", "tool_suggest",
                      "request_permissions_tool")
CLAUDE_FLAGS = ("--restricted", "--safe-mode", "--permission-prompts", "--strict-mcp-config")


def executable(name: str) -> str | None:
    if WINDOWS:
        native = shutil.which(name + ".exe")
        if native:
            return native
        candidates = [Path.home() / ".local" / "bin" / (name + ".exe")]
        if name == "codex" and os.environ.get("LOCALAPPDATA"):
            candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe")
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    found = shutil.which(name)
    fallback = Path.home() / ".local" / "bin" / name
    return found or (str(fallback) if fallback.is_file() else None)


def provider_executable(name: str) -> str:
    binary = executable(name)
    label = "Claude Code" if name == "claude" else "Codex"
    if not binary:
        raise ValueError(f"{label} is not installed on this computer. Install it, sign in once in a terminal, then restart the connector.")
    if WINDOWS and Path(binary).suffix.lower() in (".cmd", ".bat", ".ps1"):
        raise ValueError(f"The connector needs the native Windows {name}.exe. Install the native {label} and open a new terminal.")
    return binary


def run_quietly(command: list[str], timeout: float = 10) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or "") + (done.stderr or "")


def version_of(name: str) -> str | None:
    binary = executable(name)
    if not binary:
        return None
    match = re.search(r"\d+\.\d+\.\d+", run_quietly([binary, "--version"]))
    return match.group(0) if match else "unknown"


def claude_supports_isolation(binary: str) -> bool:
    help_text = run_quietly([binary, "--help"])
    return all(flag in help_text for flag in CLAUDE_FLAGS)


def claude_can_sandbox() -> bool:
    if sys.platform == "darwin":
        return Path("/usr/bin/sandbox-exec").exists()
    if sys.platform.startswith("linux"):
        return bool(shutil.which("bwrap") and shutil.which("socat"))
    return False


_described: tuple[float, dict] | None = None


def describe(max_age: float = 300) -> dict:
    """What the browser shows about this computer's agents. No network, no model call."""
    global _described
    if _described and time.monotonic() - _described[0] < max_age:
        return json.loads(json.dumps(_described[1]))
    agents = {}
    for name in ("claude", "codex"):
        version = version_of(name)
        if name == "claude":
            isolation = "sandbox" if claude_can_sandbox() else "no-commands"
        else:
            isolation = "sandbox"
        agents[name] = {"available": version is not None, "version": version, "isolation": isolation if version else None}
    _described = (time.monotonic(), agents)
    return json.loads(json.dumps(agents))


def agent_environment() -> dict:
    environment = os.environ.copy()
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    if WINDOWS:
        environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    return environment


def python_command() -> str:
    return Path(sys.executable).name if WINDOWS else "python3"


def python_read_roots() -> list[str]:
    """Where the interviewer's `python3` lives, when that is inside the home folder."""
    home = Path.home().resolve()
    python = shutil.which("python3")
    if not python:
        return []
    roots: set[Path] = set()
    found = Path(python)
    try:
        reply = json.loads(run_quietly([python, "-c", "import json,site,sys;print(json.dumps([sys.base_prefix,sys.prefix,site.getusersitepackages()]))"]).strip().splitlines()[-1])
        roots.update(Path(item) for item in reply if isinstance(item, str))
    except (ValueError, IndexError):
        pass
    if found.is_relative_to(home) and found.parent.parent != home:
        roots.add(found.parent.parent)  # A version manager's shims and versions, for example ~/.pyenv.
    return sorted(str(root.resolve()) for root in roots if root.resolve().is_relative_to(home) and root.resolve() != home)


def interrupt_process(process) -> None:
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
        if WINDOWS:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
    except ProcessLookupError:
        pass
    except OSError:
        if not WINDOWS:
            raise
        process.terminate()


def claude_command(binary: str, directory: Path, state: dict, sandbox: bool, read_roots: list[str], effort: str | None = None) -> list[str]:
    command = [binary, "--print", "--output-format", "stream-json", "--verbose", "--json-schema", json.dumps(REPLY_SCHEMA),
               "--restricted", "--safe-mode", "--strict-mcp-config",
               "--permission-mode", "acceptEdits", "--permission-prompts", "none"]
    if sandbox:
        settings = {"sandbox": {
            "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False, "autoAllowBashIfSandboxed": True,
            "filesystem": {"denyRead": ["~"], "allowRead": [str(directory), *read_roots]},
            "network": {"allowedDomains": []}}}
        command += ["--tools", "Read,Write,Edit,Glob,Grep,Bash", "--settings", json.dumps(settings)]
    else:
        command += ["--tools", "Read,Write,Edit,Glob,Grep"]
    if state.get("agentSessionId"):
        command += ["--resume", state["agentSessionId"]]
    model = state["settings"].get("model", "").strip()
    if model:
        command += ["--model", model]
    effort = (state["settings"].get("effort", "") if effort is None else effort).strip()
    if effort:
        command += ["--effort", effort]
    return command


def claude_preamble(sandbox: bool) -> str:
    python = python_command()
    if sandbox:
        return (f"Runtime tool constraint: commands run in a sandbox with no network, and can read and write only inside "
                f"this round folder. Use {python} for checks, for example `{python} .examiner/verify_setup.py`. Use "
                f"Read, Write and Edit for files. If a step is blocked, say so in your structured reply.\n\n")
    return ("Runtime tool constraint: on this computer you have no command tool, because Claude Code's sandbox is not "
            "available here. Verify the setup and candidate work by careful inspection and hand-traces, record in "
            ".examiner/manifest.json that the reference was not executed, and say so when it matters.\n\n")


def link_folders(path: Path, limit: int = 40) -> set[Path]:
    """The folder of every symbolic link met while resolving `path`, one component at a time."""
    folders: set[Path] = set()
    pending = list(Path(os.path.abspath(path)).parts)
    current = Path(pending.pop(0))
    hops = 0
    while pending and hops < limit:
        candidate = current / pending.pop(0)
        if candidate.is_symlink():
            hops += 1
            folders.add(candidate.parent)
            target = Path(os.readlink(candidate))
            if target.is_absolute():
                current, pending = Path(target.parts[0]), list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
        else:
            current = candidate
    return folders


class CodexClient:
    """Newline JSON-RPC client for Codex's documented app-server, isolated."""

    def __init__(self, logs: Path, write_root: Path):
        self.binary = provider_executable("codex")
        self.write_root = write_root
        logs.mkdir(parents=True, exist_ok=True)
        self.log = (logs / "codex-runtime.log").open("a", encoding="utf-8")
        self.lock = threading.Lock()
        self.condition = threading.Condition()
        self.pending, self.finished, self.messages = {}, {}, {}
        # Per thread: what the examiner starts doing, for the page's progress line. Never its content.
        self.watchers: dict = {}
        self.limits: dict | None = None
        self.limits_at = 0.0
        self.sequence = 0
        self.loaded: set[str] = set()
        self.closed = threading.Event()
        self.isolation = "sandbox"
        self.process = None
        try:
            # Learn the configured MCP servers without starting any, then start
            # the app-server once with every one of them already switched off.
            self.disable = self._configured_mcp_servers()
            self._launch(self._overrides(profile=True, disable=self.disable))
            still = [name for name, server in self._mcp_servers().items() if server.get("enabled", True)]
            if still:
                raise ValueError("Codex kept MCP servers enabled (" + ", ".join(still) + "); the examiner will not run with them.")
        except Exception:
            self.close()
            raise

    def _configured_mcp_servers(self) -> list[str]:
        features = [part for name in CODEX_FEATURES_OFF for part in ("-c", f"features.{name}=false")]
        try:
            done = subprocess.run([self.binary, "mcp", "list", "--json", *features], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=30, env=agent_environment())
            servers = json.loads(done.stdout) if done.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            servers = None
        if not isinstance(servers, list):
            raise ValueError("This Codex cannot list its MCP servers, so the connector cannot switch them off. Update Codex, then try again.")
        return sorted({server["name"] for server in servers if isinstance(server, dict) and isinstance(server.get("name"), str)})

    def _overrides(self, profile: bool, disable=()) -> list[str]:
        values = ["notify=[]", 'web_search="disabled"']
        values += [f"features.{name}=false" for name in CODEX_FEATURES_OFF]
        for name in disable:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name):
                raise ValueError(f"Codex has an MCP server named {name!r} that the connector cannot switch off safely.")
            values.append(f"mcp_servers.{name}.enabled=false")
        if profile:
            readable = {"~/.local/bin", *self._install_roots()}
            entries = ['"~"="deny"'] + [f'{json.dumps(path)}="read"' for path in sorted(readable)]
            entries.append(f'{json.dumps(str(self.write_root))}="write"')
            values += ['default_permissions="solvewright"', 'permissions.solvewright.extends=":workspace"',
                       "permissions.solvewright.filesystem={" + ", ".join(entries) + "}"]
        self.profile = profile
        return [part for value in values for part in ("-c", value)]

    def _install_roots(self) -> list[str]:
        home = Path.home().resolve()
        roots = []
        for path in {Path(self.binary), Path(self.binary).resolve()}:
            parent = path.parent
            if parent.resolve().is_relative_to(home):
                # The release folder, e.g. ~/.codex/packages/standalone/releases/<version>/.
                roots.append(str(parent.parent.resolve() if parent.name == "bin" else parent.resolve()))
        # Codex starts itself again inside the sandbox (to load a resumed thread's instructions), so every link on
        # the way to the binary must be readable too: ~/.local/bin/codex -> .../standalone/current/bin/codex, and
        # `current` -> releases/<version>. Without the folder holding `current`, resuming a round after the
        # connector restarts fails with "execvp() ... Operation not permitted".
        roots += [str(folder) for folder in link_folders(Path(self.binary)) if folder.resolve().is_relative_to(home)]
        return sorted(set(roots))

    def _launch(self, overrides: list[str]) -> None:
        self.closed.clear()
        self.process = subprocess.Popen([self.binary, "app-server", *overrides], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.log, text=True, encoding="utf-8", env=agent_environment(), bufsize=1,
                                        cwd=str(self.write_root))
        self.reader = threading.Thread(target=self._read, args=(self.process,), daemon=True)
        self.reader.start()
        self.rpc("initialize", {"clientInfo": {"name": "solvewright_connector", "title": "Solvewright Interview Lab", "version": "1"}})
        self.send({"method": "initialized", "params": {}})

    def _mcp_servers(self) -> dict:
        reply = self.rpc("config/read", {"includeLayers": False})
        config = reply.get("config") or reply
        servers = config.get("mcp_servers") or {}
        return servers if isinstance(servers, dict) else {}

    def _stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        reader = getattr(self, "reader", None)
        if reader and reader is not threading.current_thread():
            reader.join(timeout=5)

    def send(self, value) -> None:
        with self.lock:
            self.process.stdin.write(json.dumps(value) + "\n")
            self.process.stdin.flush()

    def rpc(self, method: str, params: dict, timeout: float = 45):
        with self.condition:
            self.sequence += 1
            key = self.sequence
            response: queue.Queue = queue.Queue()
            self.pending[key] = response
        try:
            if self.closed.is_set():
                raise ValueError("Codex disconnected. Send another message to resume this round.")
            self.send({"id": key, "method": method, "params": params})
            message = response.get(timeout=timeout)
        except queue.Empty:
            raise ValueError(f"Codex did not answer {method}. Check the connector's terminal.")
        except (BrokenPipeError, OSError) as error:
            raise ValueError("Codex disconnected. Send another message to resume this round.") from error
        finally:
            with self.condition:
                self.pending.pop(key, None)
        if "error" in message:
            raise ValueError(message["error"].get("message", "Codex request failed."))
        return message.get("result", {})

    def _read(self, process) -> None:
        try:
            for line in process.stdout:
                try:
                    data = json.loads(line)
                    if "id" in data and ("result" in data or "error" in data):
                        target = self.pending.get(data["id"])
                        if target:
                            target.put(data)
                    elif "id" in data and "method" in data:
                        # No approval is ever granted on the person's behalf.
                        self.send({"id": data["id"], "error": {"code": -32601, "message": "Approval is unavailable in Interview Lab."}})
                    elif data.get("method") == "item/started":
                        params = data["params"]
                        watcher = self.watchers.get(params.get("threadId"))
                        if watcher:
                            try:
                                watcher(params.get("item") or {})
                            except Exception:  # A progress line must never break the turn.
                                pass
                    elif data.get("method") == "account/rateLimits/updated":
                        snapshot = (data.get("params") or {}).get("rateLimits")
                        if isinstance(snapshot, dict):
                            self.limits, self.limits_at = snapshot, time.monotonic()
                    elif data.get("method") == "item/completed":
                        params = data["params"]
                        item = params.get("item", {})
                        if item.get("type") == "agentMessage":
                            self.messages[(params["threadId"], params["turnId"])] = item.get("text", "")
                    elif data.get("method") == "turn/completed":
                        params = data["params"]
                        with self.condition:
                            self.finished[(params["threadId"], params["turn"]["id"])] = params["turn"]
                            self.condition.notify_all()
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
        finally:
            if process is self.process:
                with self.condition:
                    self.closed.set()
                    for target in self.pending.values():
                        target.put({"error": {"message": "Codex disconnected. Send another message to resume this round."}})
                    self.condition.notify_all()

    def _start_thread(self, directory: Path, model: str) -> str:
        params = {"cwd": str(directory), "approvalPolicy": "never", "serviceName": "solvewright-interviews"}
        if not self.profile:
            params["sandbox"] = "workspace-write"
        if model:
            params["model"] = model
        try:
            return self.rpc("thread/start", params)["thread"]["id"]
        except ValueError as error:
            if not self.profile or "not permitted" not in str(error).lower():
                raise
        # This Codex cannot start under the read-restricting profile: keep the
        # write and network limits, and say plainly that reads are not limited.
        self._stop()
        self._launch(self._overrides(profile=False, disable=self.disable))
        self.isolation = "writes"
        params["sandbox"] = "workspace-write"
        return self.rpc("thread/start", params)["thread"]["id"]

    def rate_limits(self, max_age: float = 60) -> dict | None:
        """The account's Codex usage windows (5-hour and weekly), read without a model call."""
        if self.limits is not None and time.monotonic() - self.limits_at < max_age:
            return self.limits
        try:
            snapshot = self.rpc("account/rateLimits/read", {}, timeout=10).get("rateLimits")
        except (ValueError, OSError):
            return self.limits
        if isinstance(snapshot, dict):
            self.limits, self.limits_at = snapshot, time.monotonic()
        return self.limits

    def turn(self, state: dict, directory: Path, prompt: str, callback, cancelled=None, effort=None, watch=None) -> dict:
        if cancelled and cancelled.is_set():
            raise ValueError("The interviewer turn was stopped. Send a message to continue.")
        model = state["settings"].get("model", "").strip()
        if not state.get("agentSessionId"):
            state["agentSessionId"] = self._start_thread(directory, model)
            self.loaded.add(state["agentSessionId"])
            callback()
        thread = state["agentSessionId"]
        if thread not in self.loaded:
            resume = {"threadId": thread, "excludeTurns": True}
            if not self.profile:
                resume["sandbox"] = "workspace-write"
            self.rpc("thread/resume", resume)
            self.loaded.add(thread)
        if cancelled and cancelled.is_set():
            raise ValueError("The interviewer turn was stopped. Send a message to continue.")
        params = {"threadId": thread, "cwd": str(directory), "input": [{"type": "text", "text": prompt}],
                  "outputSchema": REPLY_SCHEMA, "approvalPolicy": "never"}
        effort = (state["settings"].get("effort", "") if effort is None else effort).strip()
        if effort:
            params["effort"] = effort
        if not self.profile:
            params["sandboxPolicy"] = {"type": "workspaceWrite", "writableRoots": [str(directory)], "networkAccess": False}
        if watch:
            self.watchers[thread] = watch
        try:
            started = self.rpc("turn/start", params)
            turn = started["turn"]["id"]
            state["activeTurn"] = turn
            callback()
            if cancelled and cancelled.is_set():
                self.interrupt(state)
            key = (thread, turn)
            with self.condition:
                while key not in self.finished:
                    if self.closed.is_set() or self.process.poll() is not None:
                        raise ValueError("Codex stopped. Send another message to resume this round.")
                    self.condition.wait(timeout=5)
                completed = self.finished.pop(key)
        finally:
            self.watchers.pop(thread, None)
        if completed["status"] != "completed":
            error = completed.get("error") or {}
            raise ValueError(error.get("message", f"Codex turn {completed['status']}. Your files and conversation were kept."))
        content = self.messages.pop(key, "")
        if not content:
            content = next((item.get("text", "") for item in reversed(completed.get("items", [])) if item.get("type") == "agentMessage"), "")
        return json.loads(content)

    def models(self) -> list[dict]:
        """The models this Codex account offers, with the efforts each one supports. No model call."""
        found, cursor = [], None
        for _page in range(5):
            reply = self.rpc("model/list", {"cursor": cursor, "limit": 50} if cursor else {"limit": 50})
            for model in reply.get("data", []):
                if model.get("hidden"):
                    continue
                found.append({"id": model.get("model") or model.get("id"), "name": model.get("displayName") or model.get("id"),
                              "default": bool(model.get("isDefault")), "defaultEffort": model.get("defaultReasoningEffort"),
                              "efforts": [option.get("reasoningEffort") for option in model.get("supportedReasoningEfforts", []) if option.get("reasoningEffort")]})
            cursor = reply.get("nextCursor")
            if not cursor:
                break
        return found

    def interrupt(self, state: dict) -> None:
        if state.get("activeTurn"):
            self.rpc("turn/interrupt", {"threadId": state["agentSessionId"], "turnId": state["activeTurn"]})

    def close(self) -> None:
        self._stop()
        self.closed.set()
        self.log.close()
