"""The job loop: fetch the account's requests over HTTPS, answer them, repeat.

This computer opens no port. Every request comes from the person's own signed-in
Solvewright account through the service, and every request is one of the fixed
operations below; there is no operation that runs an arbitrary command.
"""
from __future__ import annotations

import re
import sys
import time
import uuid

from . import __version__, agents, connection
from .service import ServiceError, Unavailable

ROUND = re.compile(r"/sessions/([a-f0-9-]{36})(?:/(message|files|test-runs|interrupt|checks))?")
ACTIVE_SECONDS, ACTIVE_POLL, IDLE_POLL = 180, 1.0, 4.0


class Revoked(Exception):
    """This computer was disconnected from the account; its key no longer works."""


def health(runtime, models=False) -> dict:
    described = agents.describe()
    for name, info in described.items():
        if info["available"]:
            info["isolation"] = runtime.isolation.get(name, info["isolation"])
    if models:
        if described["claude"]["available"]:
            described["claude"]["models"] = agents.CLAUDE_MODELS
            described["claude"]["efforts"] = agents.CLAUDE_EFFORTS
        if described["codex"]["available"]:
            try:
                described["codex"]["models"] = runtime.codex_models()
                described["codex"]["limits"] = runtime.rate_limits("codex")
            except (OSError, ValueError) as error:
                described["codex"]["modelsError"] = str(error)[:300]
        if described["claude"]["available"] and runtime.rate_limits("claude"):
            described["claude"]["limits"] = runtime.rate_limits("claude")
    return {"version": 2, "connector": __version__, "agents": described,
            # Kept for pages built before version 2.
            "providers": {name: info["available"] for name, info in described.items()},
            "dataDirectory": str(runtime.root)}


def dispatch(runtime, owner: str, path: str, body):
    if body is not None and not isinstance(body, dict):
        raise ValueError("Invalid interview request.")
    if path == "/health" and body is None:
        return health(runtime, models=True)
    if path == "/sync":
        if body is None:
            return runtime.export_sync(owner)
        if body.get("operation") == "manifest":
            return runtime.export_sync(owner, metadata=True)
        if body.get("operation") == "export":
            if not isinstance(body.get("ids"), list):
                raise ValueError("Choose which rounds to export.")
            return runtime.export_sync(owner, ids=body["ids"])
        if body.get("operation") is not None:
            raise ValueError("Unknown account history operation.")
        return runtime.import_sync(owner, body)
    if path == "/sessions":
        return {"sessions": runtime.sessions(owner)} if body is None else runtime.create(owner, body)
    match = ROUND.fullmatch(path)
    if not match:
        raise ValueError("Unknown interview operation.")
    session_id, operation = match.groups()
    if body is None and operation is None:
        return runtime.public(owner, runtime.load(owner, session_id), True)
    if operation == "checks":
        return runtime.checks(owner, session_id)
    if body is None:
        raise ValueError("This interview operation needs a request body.")
    if operation == "message":
        message = body.get("message", "")
        if not isinstance(message, str) or not message.strip() or len(message) > 30_000:
            raise ValueError("Send a message of at most 30,000 characters.")
        runtime.start_turn(owner, session_id, message, remark=body.get("remark") is True)
        return {"ok": True}
    if operation == "files":
        runtime.save_files(owner, session_id, body.get("files"))
        return {"ok": True}
    if operation == "test-runs":
        return runtime.record_test_run(owner, session_id, body.get("files"), body.get("run"))
    if operation == "interrupt":
        runtime.interrupt(owner, session_id)
        return {"ok": True}
    raise ValueError("Unsupported interview operation.")


def describe_job(path: str, body) -> str | None:
    """One line for the terminal, without the request's content."""
    if path == "/sessions" and body is not None:
        return None  # The runtime announces the new round itself.
    match = ROUND.fullmatch(path)
    operation = match.group(2) if match else None
    return {"files": "Saved your code", "interrupt": "Stopped the interviewer"}.get(operation)


def details(runtime) -> dict:
    return {"connector": __version__, "launcher": connection.launcher(), "agents": {name: {"version": info["version"], "isolation": info["isolation"]}
                                                 for name, info in health(runtime)["agents"].items() if info["available"]}}


def run(runtime, service, credentials: dict, say=print, sleep=time.sleep, clock=time.monotonic) -> None:
    identity = {"p_worker_id": credentials["workerId"], "p_key": credentials["key"], "p_instance_id": str(uuid.uuid4())}
    last_job = clock()
    announced = None
    started = clock()
    try:
        while True:
            try:
                jobs = service.rpc("next_interview_jobs", {**identity, "p_details": details(runtime)})
            except Unavailable:
                if announced != "offline":
                    say("Connection interrupted; retrying until Solvewright answers…")
                    announced = "offline"
                sleep(3)
                continue
            except ServiceError as error:
                if error.code == "42501":
                    raise Revoked() from error
                # A connector that was killed rather than closed still counts as running for 30 s.
                if error.code == "PT409" and clock() - started < 45:
                    if announced != "waiting":
                        say("An earlier connector on this computer is still marked as running; waiting for it to time out…")
                        announced = "waiting"
                    sleep(5)
                    continue
                raise
            if announced in ("offline", "waiting"):
                say("Reconnected.")
            announced = "online"
            for job in jobs or []:
                last_job = clock()
                result, failure = None, None
                try:
                    result = dispatch(runtime, credentials["owner"], job["path"], job.get("body"))
                    line = describe_job(job["path"], job.get("body"))
                    if line:
                        say(line)
                except (ValueError, KeyError, TypeError, OSError) as error:
                    failure = str(error) or "The request failed."
                # Retrying delivery never repeats the operation itself.
                while True:
                    try:
                        service.rpc("complete_interview_job", {**identity, "p_id": job["id"], "p_result": result, "p_error": failure})
                        break
                    except Unavailable:
                        sleep(3)
            if not jobs:
                sleep(ACTIVE_POLL if clock() - last_job < ACTIVE_SECONDS else IDLE_POLL)
    finally:
        try:
            service.rpc("disconnect_interview_worker", identity)
        except (OSError, ServiceError):
            pass


def stderr(text: str) -> None:
    print(text, file=sys.stderr, flush=True)
