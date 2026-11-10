"""Approval, the saved key and the job loop. No network or model calls."""
import os
import stat
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from solvewright import connection, worker
from solvewright.service import Service, ServiceError, Unavailable


class FakeService:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def rpc(self, name, data):
        self.calls.append((name, data))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class ApprovalTests(unittest.TestCase):
    def test_waits_until_approved_and_survives_a_network_blip(self):
        service = FakeService([{"status": "pending"}, Unavailable("down"),
                               {"status": "approved", "workerId": "w", "owner": "o", "key": "k", "handle": "fus3r"}])
        reply = connection.wait(service, {"deviceCode": "d", "interval": 3, "expiresIn": 600}, sleep=lambda _s: None)
        self.assertEqual(reply["handle"], "fus3r")
        self.assertTrue(all(call == ("poll_interview_connection", {"p_device_code": "d"}) for call in service.calls))

    def test_declined_and_expired_codes_stop_the_wait(self):
        with self.assertRaises(connection.ApprovalDenied):
            connection.wait(FakeService([{"status": "denied"}]), {"deviceCode": "d"}, sleep=lambda _s: None)
        with self.assertRaises(connection.ApprovalExpired):
            connection.wait(FakeService([{"status": "expired"}]), {"deviceCode": "d"}, sleep=lambda _s: None)
        ticks = iter(range(0, 10_000, 400))
        with self.assertRaises(connection.ApprovalExpired):
            connection.wait(FakeService([{"status": "pending"}] * 50), {"deviceCode": "d", "expiresIn": 600},
                            sleep=lambda _s: None, now=lambda: next(ticks))

    def test_an_incomplete_approval_is_not_saved_as_a_key(self):
        with self.assertRaises(ServiceError):
            connection.wait(FakeService([{"status": "approved", "workerId": "w"}]), {"deviceCode": "d"}, sleep=lambda _s: None)

    def test_the_key_file_is_private_and_round_trips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "connection.json"
            record = connection.record({"workerId": "w", "owner": "o", "key": "k", "handle": "fus3r"}, "https://x.supabase.co", "Laptop")
            connection.save(path, record)
            self.assertEqual(connection.load(path)["key"], "k")
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            path.write_text("{}", encoding="utf-8")
            self.assertIsNone(connection.load(path))
            connection.forget(path)
            connection.forget(path)
            self.assertFalse(path.exists())

    def test_computer_names_are_cleaned(self):
        with patch.object(connection.subprocess, "run", return_value=Mock(stdout="Riad’s MacBook\x07 Pro\n")), \
             patch.object(connection.sys, "platform", "darwin"):
            self.assertEqual(connection.computer_name(), "Riad’s MacBook Pro")

    def test_the_service_address_must_use_https(self):
        with self.assertRaises(ValueError):
            Service("http://example.com", "key")
        Service("http://127.0.0.1:54321", "key")


class WorkerTests(unittest.TestCase):
    def credentials(self):
        return {"workerId": str(uuid.uuid4()), "owner": str(uuid.uuid4()), "key": "device-key"}

    def test_dispatch_scopes_everything_to_the_key_owner(self):
        runtime = Mock()
        session = str(uuid.uuid4())
        worker.dispatch(runtime, "owner", "/sessions", {"provider": "codex", "owner": "someone-else"})
        runtime.create.assert_called_once_with("owner", {"provider": "codex", "owner": "someone-else"})
        worker.dispatch(runtime, "owner", f"/sessions/{session}/test-runs", {"files": [], "run": {"exitCode": 0}})
        runtime.record_test_run.assert_called_once_with("owner", session, [], {"exitCode": 0})
        for path in (f"/sessions/{session}/tests", "/shell", f"/sessions/{session}/../x", "/sessions/x/message"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                worker.dispatch(runtime, "owner", path, {})

    def test_messages_are_bounded_before_the_agent_sees_them(self):
        runtime = Mock()
        for message in ("", " ", None, "x" * 30_001):
            with self.subTest(kind=type(message).__name__), self.assertRaises(ValueError):
                worker.dispatch(runtime, "owner", f"/sessions/{uuid.uuid4()}/message", {"message": message})
        runtime.start_turn.assert_not_called()

    def test_a_delivery_retry_never_repeats_the_operation(self):
        job = {"id": str(uuid.uuid4()), "path": "/sessions", "body": {"provider": "codex"}}
        runtime = Mock(isolation={})
        replies = [[job], Unavailable("receipt lost"), None, KeyboardInterrupt(), None]
        service = FakeService(replies)
        with patch.object(worker.agents, "describe", return_value={}):
            with self.assertRaises(KeyboardInterrupt):
                worker.run(runtime, service, self.credentials(), say=lambda _t: None, sleep=lambda _s: None)
        runtime.create.assert_called_once()
        names = [name for name, _data in service.calls]
        self.assertEqual(names, ["next_interview_jobs", "complete_interview_job", "complete_interview_job",
                                 "next_interview_jobs", "disconnect_interview_worker"])

    def test_a_killed_connector_is_waited_out_rather_than_refused(self):
        stale = ServiceError("already running", "PT409")
        service = FakeService([stale, stale, [], KeyboardInterrupt(), None])
        said = []
        with patch.object(worker.agents, "describe", return_value={}):
            with self.assertRaises(KeyboardInterrupt):
                worker.run(Mock(isolation={}), service, self.credentials(), say=said.append, sleep=lambda _s: None)
        self.assertEqual(said[0], "An earlier connector on this computer is still marked as running; waiting for it to time out…")
        self.assertIn("Reconnected.", said)

    def test_a_disconnected_computer_stops_instead_of_retrying(self):
        service = FakeService([ServiceError("no longer connected", "42501"), None])
        with patch.object(worker.agents, "describe", return_value={}):
            with self.assertRaises(worker.Revoked):
                worker.run(Mock(isolation={}), service, self.credentials(), say=lambda _t: None, sleep=lambda _s: None)


if __name__ == "__main__":
    unittest.main()


class LauncherTests(unittest.TestCase):
    def test_the_next_command_is_the_one_that_started_this_copy(self):
        # uvx and pipx run leave no `solvewright` on the PATH, so a plain `solvewright` would not be found next time.
        cases = [("/Users/a/.cache/uv/archive-v0/x1", None, "uvx", "uvx solvewright"),
                 ("/Users/a/.local/pipx/.cache/x2", None, "pipx", "pipx run solvewright"),
                 ("/Users/a/Library/Caches/pipx/x3", None, "pipx", "pipx run solvewright"),
                 ("/Users/a/.local/pipx/venvs/solvewright", "/Users/a/.local/bin/solvewright", "command", "solvewright"),
                 ("/Users/a/.local/share/uv/tools/solvewright", "/Users/a/.local/bin/solvewright", "command", "solvewright")]
        for prefix, found, kind, command in cases:
            with patch.object(connection.sys, "prefix", prefix), patch.object(connection.shutil, "which", return_value=found):
                self.assertEqual(connection.launcher(), kind)
                self.assertEqual(connection.next_command(), command)
        with patch.object(connection.sys, "prefix", "/usr/local"), patch.object(connection.shutil, "which", return_value=None):
            self.assertEqual(connection.launcher(), "module")
            self.assertTrue(connection.next_command().endswith(" -m solvewright"))
