"""Interview rounds on disk: the workspace, the examiner's files, and agent turns.

A round lives in one folder. The agent works with that folder as its working
directory; nothing it does is meant to reach outside it. Public tests run in
the candidate's browser, so this program never executes candidate code itself:
it only stores the files and the results the browser reports.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import agents

SYNC_FORMAT = "solvewright-interview-sync-v1"
SYNC_LIMIT = 5_000_000
PORTABLE_STATE = ("id", "at", "updatedAt", "revision", "title", "caseFamily", "concepts", "settings", "messages", "stage",
                  "part", "parts", "partTimes", "finished", "testRuns", "error", "activity")
DEFAULT_SETTINGS = {"provider": "codex", "model": "", "effort": "", "style": "quant", "track": "hybrid", "mode": "mock", "minutes": 50,
                    "difficulty": "standard", "focus": "", "ai": "off", "case": "fresh", "language": "Python"}
# What each interview style asks of the examiner, written into session-settings.json.
STYLE_BRIEFS = {
    "quant": ("A practical coding round in the style of quant firms such as HRT, Jane Street or Citadel: the candidate "
              "designs and builds a small executable system (a simulator, a matching or routing engine, an event "
              "processor with time semantics, a data structure under real load). Exactness and performance matter, "
              "and the requirements change mid-round."),
    "tech": ("A practical system-design coding round at a tech company: the candidate builds a working component of a "
             "real system (a cache, a scheduler, a rate limiter, a storage or queueing layer) with clear interfaces and "
             "tests, and the requirements change mid-round. Not a LeetCode puzzle."),
}
REMARK_REQUEST = ("The candidate asks for a re-mark. Re-read the whole record: the conversation, every snapshot in "
                  ".snapshots/ and .examiner/test-runs.json. Rewrite .examiner/report.json strictly from that evidence. "
                  "Change a mark only where the record supports it, and say in notes what changed and why.")
CHOICES = {"provider": ("codex", "claude"), "style": ("quant", "tech"), "track": ("hybrid", "research", "trading", "systems"),
           "mode": ("mock", "coached", "drill"), "difficulty": ("foundation", "standard", "stretch"),
           "ai": ("off", "assistant allowed", "audit AI-written code"), "case": ("fresh", "portals"),
           "language": ("Python", "Pseudocode")}
PRIVATE_LOGS = (".examiner/claude-runtime.log", ".examiner/codex-runtime.log")
HISTORY_ROUNDS = 25
HISTORY_MESSAGE_CHARS = 3000


class SyncConflict(ValueError):
    pass


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def portable_path(name):
    if not isinstance(name, str) or len(name) > 1000 or any(char in name for char in '\\:<>"|?*') or any(ord(char) < 32 for char in name):
        raise ValueError("Invalid portable interview file path.")
    parts = name.split("/")
    if any(part in ("", ".", "..") or part.rstrip(" .") != part or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in parts):
        raise ValueError("Choose a safe, portable interview file path.")
    if name != "session-settings.json" and (len(parts) < 2 or parts[0] not in ("workspace", ".examiner", ".snapshots")):
        raise ValueError("Only interview workspace, examiner, settings and snapshot files can be restored.")
    if name in PRIVATE_LOGS:
        raise ValueError("Agent logs are not synchronized.")
    return PurePosixPath(name)


def clean_settings(settings) -> dict:
    if not isinstance(settings, dict):
        raise ValueError("Choose the round's settings.")
    chosen = {key: settings.get(key, default) for key, default in DEFAULT_SETTINGS.items()}
    for key, values in CHOICES.items():
        if chosen[key] not in values:
            raise ValueError(f"Unsupported {key} for a new round.")
    # The model name becomes one argument of the agent's command line: it must
    # never be readable as an option, so it cannot start with a dash.
    if not isinstance(chosen["model"], str) or len(chosen["model"]) > 100 or not re.fullmatch(r"(?:[A-Za-z0-9._:/@\[\]][A-Za-z0-9._:/@\[\]-]*)?", chosen["model"].strip()):
        raise ValueError("Leave Model blank, or name one model.")
    chosen["model"] = chosen["model"].strip()
    if not isinstance(chosen["effort"], str) or not re.fullmatch(r"[a-z]{0,12}", chosen["effort"]):
        raise ValueError("Choose one of the efforts your agent offers, or leave it to the agent.")
    if not isinstance(chosen["focus"], str) or len(chosen["focus"]) > 1000:
        raise ValueError("Keep the focus under 1,000 characters.")
    if type(chosen["minutes"]) is not int or not 5 <= chosen["minutes"] <= 120:
        raise ValueError("Choose between 5 and 120 minutes.")
    return chosen


def validate_report(report, state):
    """Match the portable report contract before exposing it as scored history."""
    def text(value, limit=4000, empty=False):
        return isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip()))

    def number(value, low, high):
        return type(value) in (int, float) and low <= value <= high

    def strings(value):
        return isinstance(value, list) and len(value) <= 100 and all(text(item) for item in value)

    if not isinstance(report, dict) or report.get("format") != "interview-lab-session-v1":
        raise ValueError("The interviewer report is not an Interview Lab session record.")
    if report.get("id") != state["id"] or report.get("at") != state["at"]:
        raise ValueError("The interviewer report must use this round's session ID and starting timestamp.")
    for field in ("caseId", "caseFamily", "title"):
        if not text(report.get(field), 200):
            raise ValueError(f"The interviewer report has an invalid {field}.")
    choices = {"track": ("hybrid", "research", "trading", "systems"), "mode": ("mock", "coached", "drill"),
               "difficulty": ("foundation", "standard", "stretch"), "novelty": ("unseen", "seen", "transfer"),
               "status": ("completed", "time-limited", "stopped"),
               "verification": ("executed", "inspection", "hand-trace", "discussion", "mixed")}
    for field, values in choices.items():
        if report.get(field) not in values:
            raise ValueError(f"The interviewer report has an invalid {field}.")
    if not number(report.get("plannedMinutes"), 1, 240) or (report.get("actualMinutes") is not None and not number(report.get("actualMinutes"), 0, 1440)):
        raise ValueError("The interviewer report has invalid timing fields.")
    if "actualMinutes" not in report:
        raise ValueError("The interviewer report must include actualMinutes, or null when unknown.")
    dimensions = report.get("dimensions")
    for field in ("clarification", "modeling", "implementation", "validation", "adaptation", "communication", "tools"):
        entry = dimensions.get(field) if isinstance(dimensions, dict) else None
        if not isinstance(entry, dict) or "score" not in entry:
            raise ValueError(f"The interviewer report is missing the {field} assessment.")
        score = entry["score"]
        if score is not None and (not number(score, 0, 4) or int(score) != score):
            raise ValueError(f"The interviewer report has an invalid {field} score.")
        if entry.get("assistance") not in ("none", "hint", "revealed", "ai") or not text(entry.get("evidence"), empty=score is None):
            raise ValueError(f"The interviewer report has invalid {field} evidence or assistance.")
    hints = report.get("hints")
    if not isinstance(hints, list) or len(hints) > 100 or any(not isinstance(hint, dict) or not text(hint.get("stage"), 200) or not number(hint.get("level"), 1, 4) or int(hint["level"]) != hint["level"] or not text(hint.get("summary")) for hint in hints):
        raise ValueError("The interviewer report has invalid hints.")
    if any(not strings(report.get(field)) for field in ("reveals", "skips", "strengths", "gaps")) or not text(report.get("nextDrill"), empty=True) or not text(report.get("notes"), 8000, empty=True):
        raise ValueError("The interviewer report is missing its debrief fields.")
    verdict = report.get("verdict")
    if verdict is not None and (not isinstance(verdict, dict) or not text(verdict.get("label"), 160) or verdict.get("meetsBar") not in (True, False, None)):
        raise ValueError("The interviewer report has an invalid verdict.")
    return report


def clean_test_run(run) -> dict:
    if not isinstance(run, dict) or type(run.get("exitCode")) is not int or type(run.get("timedOut")) is not bool or not isinstance(run.get("output", ""), str):
        raise ValueError("Send the browser's test result with an exit code.")
    tests = run.get("tests", [])
    if not isinstance(tests, list) or len(tests) > 500:
        raise ValueError("Too many test rows.")
    rows = []
    for row in tests[:200]:
        if not isinstance(row, dict) or row.get("outcome") not in ("passed", "failed", "skipped") or not isinstance(row.get("name"), str):
            raise ValueError("Invalid test row.")
        rows.append({"name": row["name"][:300], "file": str(row.get("file", ""))[:300], "outcome": row["outcome"],
                     "message": str(row.get("message", ""))[:2000]})
    return {"at": stamp(), "exitCode": run["exitCode"], "timedOut": run["timedOut"], "output": run.get("output", "")[-30000:],
            "tests": rows, "runner": "browser"}


class Runtime:
    def __init__(self, root: Path, instructions: Path, logs: Path):
        self.root, self.instructions, self.logs = root.resolve(), instructions.resolve(), logs.resolve()
        if self.root.is_relative_to(self.instructions):
            raise ValueError("Keep interview history outside the connector's own files.")
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.codex: agents.CodexClient | None = None
        self.codex_lock = threading.Lock()
        self.claude_processes: dict[str, subprocess.Popen] = {}
        self.cancellations: dict[str, threading.Event] = {}
        self.read_roots: list[str] | None = None
        self.isolation = {"claude": "sandbox" if agents.claude_can_sandbox() else "no-commands", "codex": "sandbox"}
        self.on_event = None
        for path in self.root.glob("*/sessions/*/state.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("busy"):
                data.update(busy=False, error="The connector stopped during this turn. Your work was saved; send a message to continue.")
                data.update(updatedAt=stamp(), revision=str(uuid.uuid4()))
                write_json(path, data)
            elif not data.get("revision"):
                data.update(updatedAt=data.get("at", stamp()), revision=str(uuid.uuid4()))
                write_json(path, data)

    def event(self, text: str) -> None:
        if self.on_event:
            self.on_event(text)

    def scope_root(self, scope: str) -> Path:
        return self.root / hashlib.sha256(scope.encode()).hexdigest()[:20]

    def directory(self, scope: str, session_id) -> Path:
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", session_id):
            raise ValueError("Invalid round ID.")
        return self.scope_root(scope) / "sessions" / session_id

    def load(self, scope, session_id) -> dict:
        path = self.directory(scope, session_id) / "state.json"
        if not path.is_file():
            raise ValueError("That round was not found on this computer.")
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, scope, state) -> None:
        state.update(updatedAt=stamp(), revision=str(uuid.uuid4()))
        write_json(self.directory(scope, state["id"]) / "state.json", state)

    def base_revision(self, directory: Path):
        path = directory / ".sync.json"
        return json.loads(path.read_text(encoding="utf-8")).get("baseRevision") if path.is_file() else None

    def copy_context(self, directory: Path) -> None:
        target = directory / "context" / "interviewer"
        shutil.copytree(self.instructions, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), dirs_exist_ok=True)

    def portable_files(self, directory: Path) -> list[dict]:
        files, size = [], 0
        paths = [directory / "session-settings.json"]
        for folder in ("workspace", ".examiner", ".snapshots"):
            paths.extend(sorted((directory / folder).rglob("*")))
        for path in paths:
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
                continue
            name = path.relative_to(directory).as_posix()
            if name in PRIVATE_LOGS or path.suffix == ".pyc" or "__pycache__" in path.parts:
                continue
            portable_path(name)
            size += path.stat().st_size
            if size > SYNC_LIMIT:
                raise ValueError("This round exceeds the 5 MB account-history limit. Its full copy stays on this computer.")
            content = path.read_bytes()
            try:
                files.append({"path": name, "content": content.decode("utf-8")})
            except UnicodeError:
                files.append({"path": name, "content": base64.b64encode(content).decode("ascii"), "encoding": "base64"})
        return files

    def export_sync(self, scope, ids=None, metadata=False) -> dict:
        if ids is not None and (not isinstance(ids, list) or len(ids) > 1000 or any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9-]{36}", item) for item in ids)):
            raise ValueError("Choose valid round IDs to export.")
        selected = set(ids) if ids is not None else None
        with self.lock:
            sessions, pending = [], False
            for path in sorted(self.scope_root(scope).glob("sessions/*/state.json")):
                state = json.loads(path.read_text(encoding="utf-8"))
                if selected is not None and state["id"] not in selected:
                    continue
                if state.get("busy"):
                    pending = True
                    continue
                bundle = {"id": state["id"], "revision": state["revision"], "baseRevision": self.base_revision(path.parent),
                          "state": {"title": state.get("title", "Saved round")} if metadata else {key: state[key] for key in PORTABLE_STATE if key in state}}
                if not metadata:
                    bundle["files"] = self.portable_files(path.parent)
                if len(json.dumps(bundle).encode("utf-8")) > SYNC_LIMIT:
                    raise ValueError("This round exceeds the 5 MB account-history limit. Its full copy stays on this computer.")
                sessions.append(bundle)
            return {"format": SYNC_FORMAT, "sessions": sessions, "pendingBusy": pending}

    def import_sync(self, scope, body) -> dict:
        if not isinstance(body, dict) or body.get("format", SYNC_FORMAT) != SYNC_FORMAT or not isinstance(body.get("sessions", []), list) or not isinstance(body.get("acknowledgements", []), list):
            raise ValueError("Invalid account interview history.")
        if len(json.dumps(body).encode("utf-8")) > SYNC_LIMIT:
            raise ValueError("Restore one round at a time; this request exceeds 5 MB.")
        prepared, acknowledgements, seen = [], [], set()
        for bundle in body.get("sessions", []):
            if not isinstance(bundle, dict) or not isinstance(bundle.get("state"), dict) or not isinstance(bundle.get("files"), list):
                raise ValueError("Invalid portable interview round.")
            session_id, revision = bundle.get("id"), bundle.get("revision")
            if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", session_id) or session_id in seen or not isinstance(revision, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", revision):
                raise ValueError("Invalid or duplicate portable round identity.")
            seen.add(session_id)
            state = {key: bundle["state"][key] for key in PORTABLE_STATE if key in bundle["state"]}
            if state.get("id") != session_id or state.get("revision") != revision or any(not isinstance(state.get(field), str) for field in ("at", "updatedAt", "title", "caseFamily", "stage", "error", "activity")):
                raise ValueError("Invalid portable round state.")
            if not isinstance(state.get("settings"), dict) or state["settings"].get("provider") not in ("codex", "claude") or not isinstance(state["settings"].get("model", ""), str) or type(state.get("finished")) is not bool or not isinstance(state.get("concepts"), list) or any(not isinstance(item, str) for item in state["concepts"]):
                raise ValueError("Invalid portable interview settings or stage.")
            for field in ("at", "updatedAt"):
                try:
                    if datetime.fromisoformat(state[field].replace("Z", "+00:00")).tzinfo is None:
                        raise ValueError()
                except ValueError:
                    raise ValueError("Portable round dates need an ISO timestamp with a timezone.")
            if not isinstance(state.get("messages"), list) or any(not isinstance(item, dict) or item.get("role") not in ("user", "assistant") or not isinstance(item.get("text"), str) or not isinstance(item.get("at"), str) for item in state["messages"]):
                raise ValueError("Invalid portable interview conversation.")
            if not isinstance(state.get("testRuns"), list) or any(not isinstance(item, dict) or type(item.get("exitCode")) is not int or type(item.get("timedOut")) is not bool or not isinstance(item.get("output"), str) or not isinstance(item.get("at"), str) for item in state["testRuns"]):
                raise ValueError("Invalid portable test history.")
            files, names = [], set()
            for item in bundle["files"]:
                if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                    raise ValueError("Invalid portable interview file.")
                name = portable_path(item.get("path"))
                if name.as_posix().casefold() in names:
                    raise ValueError("A portable round contains duplicate file paths.")
                names.add(name.as_posix().casefold())
                if item.get("encoding") == "base64":
                    try:
                        content = base64.b64decode(item["content"], validate=True)
                    except ValueError:
                        raise ValueError("Invalid binary interview file.")
                elif item.get("encoding") in (None, "utf-8"):
                    content = item["content"].encode("utf-8")
                else:
                    raise ValueError("Unsupported portable interview file encoding.")
                files.append((name, content))
            if any(parent.as_posix().casefold() in names for name, _content in files for parent in name.parents if parent != PurePosixPath(".")):
                raise ValueError("A portable round uses the same path as a file and a folder.")
            state["busy"] = False
            prepared.append((session_id, revision, bundle.get("expectedLocalRevision"), state, files))
        for ack in body.get("acknowledgements", []):
            if not isinstance(ack, dict) or not isinstance(ack.get("id"), str) or not isinstance(ack.get("revision"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", ack["revision"]):
                raise ValueError("Invalid account history acknowledgement.")
            if ack["id"] in seen:
                raise ValueError("Restore or acknowledge a round once per synchronization request.")
            seen.add(ack["id"])
            self.directory(scope, ack["id"])
            acknowledgements.append(ack)
        imported, skipped, acknowledged = [], [], []
        with self.lock:
            for session_id, _revision, expected, _state, _files in prepared:
                directory = self.directory(scope, session_id)
                local = self.load(scope, session_id) if (directory / "state.json").is_file() else None
                if local and local.get("busy"):
                    skipped.append(session_id)
                elif (local or {}).get("revision") != expected:
                    raise SyncConflict("This round changed on this computer during the restore. Both copies were kept; sync again.")
            for ack in acknowledgements:
                directory = self.directory(scope, ack["id"])
                self.load(scope, ack["id"])
                if self.base_revision(directory) != ack.get("expectedBaseRevision"):
                    raise SyncConflict("The account acknowledgement is out of date. Sync again; local work was kept.")
            for session_id, revision, _expected, state, files in prepared:
                if session_id in skipped:
                    continue
                directory = self.directory(scope, session_id)
                staging = directory.with_name(".import-" + str(uuid.uuid4()))
                backup = directory.with_name(".prior-" + str(uuid.uuid4()))
                try:
                    (staging / "workspace" / "tests").mkdir(parents=True)
                    (staging / ".examiner").mkdir()
                    for name, content in files:
                        target = staging.joinpath(*name.parts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                    self.copy_context(staging)
                    write_json(staging / "state.json", state)
                    write_json(staging / "session-settings.json", {**state["settings"], "id": session_id, "at": state["at"]})
                    write_json(staging / ".sync.json", {"baseRevision": revision})
                    if directory.exists():
                        directory.rename(backup)
                    try:
                        staging.rename(directory)
                    except OSError:
                        if backup.exists():
                            backup.rename(directory)
                        raise
                    imported.append(session_id)
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
                    if directory.exists():
                        shutil.rmtree(backup, ignore_errors=True)
            for session_id in imported:
                write_json(self.directory(scope, session_id) / "history-context.json", self.history_context(scope, session_id))
            for ack in acknowledgements:
                write_json(self.directory(scope, ack["id"]) / ".sync.json", {"baseRevision": ack["revision"]})
                acknowledged.append(ack["id"])
        return {"imported": imported, "skipped": skipped, "acknowledged": acknowledged}

    def candidate_files(self, directory: Path) -> list[dict]:
        result = []
        workspace = (directory / "workspace").resolve()
        for path in sorted(workspace.rglob("*")):
            if any(part.startswith(".") for part in path.relative_to(workspace).parts):
                continue
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(workspace):
                continue
            if "__pycache__" in path.parts or path.stat().st_size > 500_000 or path.suffix in (".pyc", ".bin"):
                continue
            try:
                result.append({"path": path.relative_to(workspace).as_posix(), "content": path.read_text(encoding="utf-8")})
            except UnicodeError:
                continue
        return result

    def public(self, scope, state, files=False) -> dict:
        data = {key: value for key, value in state.items() if key not in ("agentSessionId", "activeTurn")}
        directory = self.directory(scope, state["id"])
        data["baseRevision"] = self.base_revision(directory)
        if files:
            data["files"] = self.candidate_files(directory)
        report = directory / ".examiner" / "report.json"
        if state.get("finished"):
            try:
                if not report.is_file():
                    raise ValueError("No assessment report was saved for this round.")
                data["report"] = validate_report(json.loads(report.read_text(encoding="utf-8")), state)
            except (ValueError, OSError) as error:
                data["reportError"] = str(error) + " Send a message to ask the interviewer to repair the report."
        return data

    def sessions(self, scope) -> list[dict]:
        rounds = [self.public(scope, json.loads(path.read_text(encoding="utf-8"))) for path in self.scope_root(scope).glob("sessions/*/state.json")]
        return sorted(rounds, key=lambda item: item["at"], reverse=True)

    def history_context(self, scope, exclude: str) -> dict:
        """Previous rounds, inside this round's folder: the agent cannot read the others."""
        prior = []
        for item in [round_ for round_ in self.sessions(scope) if round_["id"] != exclude][:HISTORY_ROUNDS]:
            entry = {key: item.get(key) for key in ("id", "at", "title", "caseFamily", "concepts", "settings", "stage", "finished", "report")}
            entry["messages"] = [{**message, "text": message["text"][:HISTORY_MESSAGE_CHARS]} for message in item.get("messages", [])[-40:]]
            entry["testRuns"] = [{key: run.get(key) for key in ("at", "exitCode", "timedOut")} for run in item.get("testRuns", [])[-10:]]
            manifest = self.directory(scope, item["id"]) / ".examiner" / "manifest.json"
            if manifest.is_file():
                try:
                    entry["privateManifest"] = json.loads(manifest.read_text(encoding="utf-8"))
                except ValueError:
                    pass
            prior.append(entry)
        return {"sessions": prior}

    def create(self, scope, settings) -> dict:
        settings = clean_settings(settings)
        agents.provider_executable("claude" if settings["provider"] == "claude" else "codex")
        with self.lock:
            if any(item.get("busy") for item in self.sessions(scope)):
                raise ValueError("Finish or stop the interviewer's current turn before starting another round.")
            session_id = str(uuid.uuid4())
            directory = self.directory(scope, session_id)
            (directory / "workspace" / "tests").mkdir(parents=True)
            (directory / ".examiner").mkdir()
            self.copy_context(directory)
            state = {"id": session_id, "at": stamp(), "title": "Preparing a fresh round", "caseFamily": "", "concepts": [],
                     "settings": settings, "messages": [], "stage": "Preparing", "busy": False, "finished": False,
                     "testRuns": [], "error": "", "activity": "Designing and checking the exercise"}
            write_json(directory / "history-context.json", self.history_context(scope, session_id))
            write_json(directory / "session-settings.json", {**settings, "styleBrief": STYLE_BRIEFS[settings["style"]], "id": session_id, "at": state["at"]})
            self.save(scope, state)
            self.start_turn(scope, session_id, "Start a new live runtime interview. Read context/interviewer/interviewer.txt, "
                            "context/interviewer/references/case-generation.txt and context/interviewer/references/curriculum.txt. "
                            "Read history-context.json and session-settings.json. Generate a fresh problem and a verified setup; "
                            "do not select from a catalogue. Use the reported portal case only if settings.case is portals. Create the "
                            "manifest and private reference, verify it, and publish candidate files before asking the opening "
                            "task. Return the required structured reply. Do not solve candidate work or expose future stages.",
                            initial=True)
            return self.public(scope, self.load(scope, session_id), True)

    def start_turn(self, scope, session_id, message, initial=False, remark=False) -> None:
        with self.lock:
            state = self.load(scope, session_id)
            if state["busy"]:
                raise ValueError("The interviewer is still working. Stop it or wait for its reply.")
            if remark and not state["finished"]:
                raise ValueError("A round is re-marked only after it has ended.")
            if state["finished"] and not remark and "reportError" not in self.public(scope, state):
                raise ValueError("This round has ended. Start a fresh round to keep practising.")
            directory = self.directory(scope, session_id)
            if not initial:
                state["messages"].append({"role": "user", "text": message, "at": stamp()})
                snapshot = directory / ".snapshots" / f"{len(state['messages']):04d}"
                for file in self.candidate_files(directory):
                    target = snapshot / file["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(file["content"], encoding="utf-8")
            state.update(busy=True, error="", activity="Designing and checking a fresh exercise" if initial else "Re-marking from the record" if remark else "Reading your work and preparing a reply")
            self.save(scope, state)
            self.cancellations[session_id] = threading.Event()
        threading.Thread(target=self._turn, args=(scope, session_id, message, initial, remark), daemon=True).start()

    def _turn(self, scope, session_id, message, initial, remark=False) -> None:
        state = self.load(scope, session_id)
        directory = self.directory(scope, session_id)
        cancelled = self.cancellations.get(session_id)
        repairing_report = bool(state["finished"])
        provider = state["settings"]["provider"]
        self.event(("Preparing a new round" if initial else "Interviewer turn") + " · " + ("Claude Code" if provider == "claude" else "Codex"))

        def checkpoint():
            with self.lock:
                self.save(scope, state)

        prompt = message if initial else (
            "Live runtime continuation. Read the current files in workspace/ and .examiner/test-runs.json if present. "
            "Preserve candidate code; change it only on an explicit assistance request. Continue the fixed case and stage "
            "from your private manifest. Candidate message:\n\n" + message + "\n\nFor end/debrief write .examiner/report.json "
            "using the session ID/date in session-settings.json and the scoring and session format references. Return the "
            "structured runtime reply only.")
        if repairing_report:
            prompt = ("This round is already over. Repair its missing or invalid .examiner/report.json from the saved evidence, "
                      "without continuing scored work or changing candidate code. Keep finished true. Candidate request:\n\n" + message)
        if remark:
            prompt = REMARK_REQUEST + " Keep finished true and do not continue the interview. Candidate's note:\n\n" + message
        if agents.WINDOWS:
            prompt = f"This computer runs native Windows. Run Python with {agents.python_command()} instead of python3.\n\n" + prompt
        if not initial and not state.get("agentSessionId"):
            prompt = ("Resume this saved interview in a fresh agent conversation. Read context/interviewer/interviewer.txt, "
                      "state.json for the full prior conversation, session-settings.json, .examiner/manifest.json and "
                      ".examiner/progress.json if present, and the current workspace. Preserve the disclosed case and stage, "
                      "assistance history and candidate work. Do not generate a new exercise or reveal future stages.\n\n" + prompt)
        try:
            if cancelled and cancelled.is_set():
                raise ValueError("The interviewer turn was stopped. Send a message to continue.")
            if provider == "codex":
                with self.codex_lock:
                    if self.codex is None or self.codex.closed.is_set() or self.codex.process.poll() is not None:
                        if self.codex:
                            self.codex.close()
                        self.codex = agents.CodexClient(self.logs, self.root)
                reply = self.codex.turn(state, directory, prompt, checkpoint, cancelled)
                self.isolation["codex"] = self.codex.isolation
            else:
                reply = self.claude_turn(state, directory, prompt, checkpoint, cancelled)
            if not isinstance(reply, dict) or any(not isinstance(reply.get(field), str) for field in ("message", "title", "caseFamily", "stage")) or type(reply.get("finished")) is not bool or not isinstance(reply.get("concepts"), list) or any(not isinstance(concept, str) for concept in reply["concepts"]):
                raise ValueError("The agent did not return a complete interview reply. Your workspace was kept; ask it to continue.")
            for field in ("title", "caseFamily", "concepts", "stage", "finished"):
                state[field] = reply[field]
            for field in ("part", "parts"):
                if type(reply.get(field)) is int and 0 <= reply[field] <= 20:
                    state[field] = reply[field]
            if type(state.get("part")) is int and state["part"] >= 1:
                # When each part was first shown: the header draws its real length.
                state.setdefault("partTimes", {}).setdefault(str(state["part"]), stamp())
            if repairing_report:
                state["finished"] = True
            state["messages"].append({"role": "assistant", "text": reply["message"], "at": stamp()})
            state["activity"] = "Round complete" if state["finished"] else "Waiting for your response"
            self.event(("Round ready · " + state["title"]) if initial else ("Round complete · " + state["title"]) if state["finished"] else "Interviewer replied")
        except Exception as error:  # Every failure becomes visible state, never a stuck turn.
            state["error"] = str(error)
            state["activity"] = "Agent turn stopped"
            self.event("Turn stopped · " + str(error)[:160])
        finally:
            with self.lock:
                state["busy"] = False
                state.pop("activeTurn", None)
                self.save(scope, state)
                self.cancellations.pop(session_id, None)

    def claude_turn(self, state, directory, prompt, checkpoint, cancelled=None) -> dict:
        if cancelled and cancelled.is_set():
            raise ValueError("The interviewer turn was stopped. Send a message to continue.")
        binary = agents.provider_executable("claude")
        if not agents.claude_supports_isolation(binary):
            raise ValueError("This Claude Code is too old for the connector's sandboxed mode. Run `claude update`, then send another message.")
        sandbox = agents.claude_can_sandbox()
        if sandbox and self.read_roots is None:
            self.read_roots = agents.python_read_roots()
        command = agents.claude_command(binary, directory, state, sandbox, self.read_roots or [])
        log = (directory / ".examiner" / "claude-runtime.log").open("a", encoding="utf-8")
        process = None
        result = None
        try:
            process = subprocess.Popen(command, cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                       text=True, encoding="utf-8", env=agents.agent_environment(), bufsize=1,
                                       **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if agents.WINDOWS else {}))
            self.claude_processes[state["id"]] = process
            if cancelled and cancelled.is_set():
                raise ValueError("The interviewer turn was stopped. Send a message to continue.")
            process.stdin.write(agents.claude_preamble(sandbox) + prompt)
            process.stdin.close()
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("session_id"):
                    state["agentSessionId"] = event["session_id"]
                    checkpoint()
                if event.get("type") == "result":
                    result = event
            process.wait()
            if process.returncode or not result or result.get("is_error") or not result.get("structured_output"):
                explanation = (result or {}).get("result") or "; ".join(str(item) for item in (result or {}).get("errors", [])) or "Claude Code did not return structured output. Check its sign-in with `claude` in a terminal."
                raise ValueError(str(explanation)[:1500])
            return result["structured_output"]
        finally:
            self.claude_processes.pop(state["id"], None)
            if process:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                for pipe in (process.stdin, process.stdout):
                    if pipe and not pipe.closed:
                        pipe.close()
            log.close()

    def _write_workspace(self, scope, session_id, files) -> dict:
        state = self.load(scope, session_id)
        if state["busy"]:
            raise ValueError("Wait for the interviewer to finish before saving new work.")
        workspace = (self.directory(scope, session_id) / "workspace").resolve()
        if not isinstance(files, list) or len(files) > 100:
            raise ValueError("Invalid candidate files.")
        checked = []
        for file in files:
            if not isinstance(file, dict):
                raise ValueError("Invalid candidate file.")
            name, content = file.get("path"), file.get("content")
            if not isinstance(name, str) or not Path(name).parts or "\\" in name or Path(name).is_absolute() or any(part.startswith(".") for part in Path(name).parts):
                raise ValueError("Choose a visible workspace-relative file path.")
            target = (workspace / name).resolve()
            if not target.is_relative_to(workspace) or not isinstance(content, str) or len(content) > 500_000:
                raise ValueError("Invalid workspace file.")
            checked.append((target, content))
        for target, content in checked:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return state

    def save_files(self, scope, session_id, files) -> None:
        with self.lock:
            state = self._write_workspace(scope, session_id, files)
            self.save(scope, state)

    def record_test_run(self, scope, session_id, files, run) -> dict:
        """Store a run the browser made on exactly these files, with a snapshot of them."""
        result = clean_test_run(run)
        with self.lock:
            state = self._write_workspace(scope, session_id, files)
            directory = self.directory(scope, session_id)
            snapshot = directory / ".snapshots" / f"test-{len(state['testRuns']) + 1:04d}"
            for file in self.candidate_files(directory):
                target = snapshot / file["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(file["content"], encoding="utf-8")
            result["snapshot"] = snapshot.relative_to(directory).as_posix()
            state["testRuns"].append(result)
            write_json(directory / ".examiner" / "test-runs.json", state["testRuns"])
            self.save(scope, state)
        passed = sum(row["outcome"] == "passed" for row in result["tests"])
        self.event(f"Tests recorded · {passed} of {len(result['tests'])} passed")
        return result

    def codex_models(self) -> list[dict]:
        """Codex's own model list, read through the same isolated app-server; cached ten minutes."""
        cached = getattr(self, "_codex_models", None)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        with self.codex_lock:
            if self.codex is None or self.codex.closed.is_set() or self.codex.process.poll() is not None:
                if self.codex:
                    self.codex.close()
                self.codex = agents.CodexClient(self.logs, self.root)
            models = self.codex.models()
        self._codex_models = (time.monotonic(), models)
        return models

    def interrupt(self, scope, session_id) -> None:
        with self.lock:
            state = self.load(scope, session_id)
            cancelled = self.cancellations.get(session_id)
            if cancelled:
                cancelled.set()
        if state["settings"]["provider"] == "codex" and self.codex:
            self.codex.interrupt(state)
        else:
            process = self.claude_processes.get(session_id)
            if process:
                agents.interrupt_process(process)

    def close(self) -> None:
        if self.codex:
            self.codex.close()
        for process in list(self.claude_processes.values()):
            process.terminate()
