"""What the examiner is doing during a turn, read from the agent's own event stream.

The page shows it while the candidate waits. Only the kind of action and the
folder it touches are kept: reading the brief, reading the candidate's code,
writing private notes, running checks, writing the candidate's files. File
contents, command text and the examiner's own words never leave this module,
so nothing private (a reference, a later part) can reach the candidate.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

# The steps each kind of turn is expected to pass through, in order.
STEPS = {
    "prepare": ("brief", "design", "reference", "checks", "files", "reply"),
    "reply": ("read", "checks", "reply"),
    "mark": ("read", "checks", "report", "reply"),
}
LINES = {
    "brief": "Reading the brief and your past rounds",
    "design": "Designing the problem",
    "reference": "Writing a private reference solution",
    "checks": "Running its checks",
    "files": "Writing your files",
    "read": "Reading your code",
    "notes": "Updating its notes",
    "report": "Writing your marks",
    "reply": "Writing the reply",
    "think": "Thinking it through",
}
FIRST_LINE = {"prepare": LINES["brief"], "reply": LINES["read"], "mark": LINES["read"]}

RUNS_PYTHON = re.compile(r"(?:^|[\s;&|(`'\"/])(?:python(?:3(?:\.\d+)?)?|py|pytest|unittest)(?=$|[\s'\";)])")
READS = re.compile(r"(?:^|[;&|]\s*|\n)\s*(?:cat|sed|head|tail|less|nl|rg|grep|ls|find|tree|wc|stat|diff|pwd)\b(?![^;&|\n]*>)")
BRIEF = re.compile(r"(?:^|/)(?:history-context\.json|session-settings\.json|context/interviewer(?:/|$))")
# Where a shell command writes: `> path`, `>> path`, `tee path`. Not 2>&1 or /dev/null.
WRITES = re.compile(r"(?:(?<![0-9&])>{1,2}|\btee(?:\s+-a)?)\s*['\"]?([A-Za-z0-9_./-]+)")
MENTIONS = re.compile(r"[A-Za-z0-9_./-]*(?:workspace/|\.examiner/|context/interviewer|history-context\.json|session-settings\.json)[A-Za-z0-9_./-]*")


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def where(path: str, root: str = "") -> str:
    """The part of the round a path belongs to: brief, workspace, report, private or other."""
    path = str(path or "").replace("\\", "/")
    root = str(root or "").replace("\\", "/").rstrip("/")
    if root and path.startswith(root + "/"):
        path = path[len(root) + 1:]
    while path.startswith("./"):
        path = path[2:]
    if BRIEF.search(path):
        return "brief"
    if path.startswith("workspace/") or path == "workspace":
        return "workspace"
    if path.endswith(".examiner/report.json") or path == ".examiner/report.json":
        return "report"
    if path.startswith(".examiner/") or "/.examiner/" in path:
        return "private"
    return "other"


Action = tuple[str, list[str], str]


def command_actions(command: str, listed=()) -> list[Action]:
    """What one shell command does, in order: reads, writes (by their targets), then a Python run.

    Examiners write files with heredocs and scripts as often as with patch tools,
    and read several files in one compound command, so the command's own text is
    read for paths and redirections. Only their folders are kept.
    """
    command = str(command or "")
    listed = [action for action in listed or [] if isinstance(action, dict)]
    targets = [target for target in WRITES.findall(command) if target != "/dev/null"]
    mentioned = [path for path in MENTIONS.findall(command) if path not in targets]
    actions: list[Action] = []
    if READS.search(command) or any(action.get("type") in ("read", "listFiles", "search") for action in listed):
        actions.append(("read", [str(action.get("path") or "") for action in listed] + mentioned, command))
    if targets:
        actions.append(("write", targets, command))
    if RUNS_PYTHON.search(command):
        actions.append(("run", mentioned, command))
    return actions


def codex_action(item: dict) -> list[Action]:
    """The actions in one Codex app-server ThreadItem; an empty list to ignore it."""
    kind = item.get("type")
    if kind == "reasoning":
        return [("think", [], "")]
    if kind == "agentMessage":
        return [("answer", [], "")] if item.get("phase") == "final_answer" else []
    if kind == "fileChange":
        return [("write", [change.get("path", "") for change in item.get("changes") or [] if isinstance(change, dict)], "")]
    if kind == "commandExecution":
        return command_actions(item.get("command"), item.get("commandActions"))
    return []


def claude_action(block: dict) -> list[Action]:
    """The actions in one content block of a Claude Code stream-json assistant event."""
    kind = block.get("type")
    if kind in ("thinking", "redacted_thinking"):
        return [("think", [], "")]
    if kind != "tool_use":
        return []
    name, given = block.get("name"), block.get("input") or {}
    if name == "StructuredOutput":
        return [("answer", [], "")]
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return [("write", [str(given.get("file_path") or given.get("notebook_path") or "")], "")]
    if name in ("Read", "Glob", "Grep", "LS"):
        return [("read", [str(given.get("file_path") or given.get("path") or "")], "")]
    if name == "Bash":
        return command_actions(given.get("command"))
    return []


class TurnProgress:
    """The steps of one turn as the page sees them: `state["turn"]`, and `state["activity"]` for older pages."""

    def __init__(self, kind: str, state: dict, save, root: str = "", clock=time.monotonic, min_gap: float = 0.5):
        self.kind, self.state, self.save, self.root, self.clock, self.min_gap = kind, state, save, root, clock, min_gap
        self.data = {"kind": kind, "since": stamp(), "steps": [], "checks": 0}
        self.last_save = None
        state["turn"] = self.data
        state["activity"] = FIRST_LINE[kind]

    def step_for(self, action: str, places: set[str]) -> str | None:
        if action == "answer":
            return "reply"
        if action == "run":
            return "checks"
        if action == "think":
            # Before it has written anything, a preparing examiner is designing the problem.
            return "design" if self.kind == "prepare" and not ({"reference", "files"} & self.seen()) else None
        if action == "read":
            if self.kind == "prepare":
                return "brief" if ("brief" in places or not self.seen()) else None
            return "read"
        if action == "write":
            if "report" in places:
                return "report"
            if "workspace" in places:
                return "files"
            if "private" in places:
                return "reference" if self.kind == "prepare" else "notes"
        return None

    def seen(self) -> set[str]:
        return {step["id"] for step in self.data["steps"]}

    def observe(self, actions) -> None:
        for action in actions or []:
            self.observe_one(action)

    def observe_one(self, action: Action) -> None:
        kind, paths, _command = action
        step = self.step_for(kind, {where(path, self.root) for path in paths})
        if not step:
            return
        new = step not in self.seen()
        if new:
            self.data["steps"].append({"id": step, "at": stamp()})
        if step == "checks":
            self.data["checks"] += 1
        line = LINES[step]
        changed = new or line != self.state.get("activity") or step == "checks"
        self.state["activity"] = line
        now = self.clock()
        if changed and (new or self.last_save is None or now - self.last_save >= self.min_gap):
            self.last_save = now
            self.save()

    def finish(self) -> None:
        self.state.pop("turn", None)
