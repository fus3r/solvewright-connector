"""Rounds on disk, the agent boundary and account-history sync. No CLI or model calls."""
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from solvewright import agents, runtime as rt

REPLY = {"message": "What is your proposed invariant?", "title": "An unseen case", "caseFamily": "events",
         "concepts": ["invariants"], "stage": "Baseline", "finished": False}
INSTRUCTIONS = Path(rt.__file__).resolve().parent / "interviewer"


class FakeProcess:
    def __init__(self, events=(), stdin=None):
        self.stdin = stdin if stdin is not None else io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = rt.Runtime(self.root / "interviews", INSTRUCTIONS, self.root / "logs")
        self.scope = "owner-a"
        self.state = {"id": str(uuid.uuid4()), "at": rt.stamp(), "settings": {**rt.DEFAULT_SETTINGS, "provider": "claude"},
                      "busy": False, "finished": False, "messages": [], "testRuns": [], "error": "", "title": "A case",
                      "caseFamily": "", "concepts": [], "stage": "Baseline", "activity": ""}
        self.directory = self.runtime.directory(self.scope, self.state["id"])
        (self.directory / "workspace" / "tests").mkdir(parents=True)
        (self.directory / ".examiner").mkdir()
        self.runtime.save(self.scope, self.state)

    def report(self):
        return {"format": "interview-lab-session-v1", "id": self.state["id"], "at": self.state["at"],
                "caseId": "generated", "caseFamily": "events", "title": "A case", "track": "systems", "mode": "mock",
                "difficulty": "standard", "novelty": "unseen", "plannedMinutes": 50, "actualMinutes": None,
                "status": "completed", "verification": "inspection", "dimensions": {
                    dimension: {"score": None, "assistance": "none", "evidence": ""} for dimension in (
                        "clarification", "modeling", "implementation", "validation", "adaptation", "communication", "tools")},
                "hints": [], "reveals": [], "skips": [], "strengths": [], "gaps": [], "nextDrill": "", "notes": ""}

    def test_new_round_copies_the_packaged_instructions_and_keeps_history_inside_the_round(self):
        older = {**self.state, "id": str(uuid.uuid4()), "messages": [{"role": "user", "text": "x" * 9000, "at": rt.stamp()}]}
        older_directory = self.runtime.directory(self.scope, older["id"])
        (older_directory / ".examiner").mkdir(parents=True)
        (older_directory / ".examiner" / "manifest.json").write_text('{"mechanism":"windows"}', encoding="utf-8")
        self.runtime.save(self.scope, older)
        with patch.object(agents, "provider_executable", return_value="codex"), patch.object(self.runtime, "start_turn") as turn:
            created = self.runtime.create(self.scope, {"provider": "codex", "minutes": 25})
        directory = self.runtime.directory(self.scope, created["id"])
        self.assertTrue((directory / "context" / "interviewer" / "interviewer.txt").is_file())
        self.assertTrue((directory / "context" / "interviewer" / "tools" / "portal_lab.py").is_file())
        history = json.loads((directory / "history-context.json").read_text(encoding="utf-8"))
        by_id = {item["id"]: item for item in history["sessions"]}
        self.assertEqual(by_id[older["id"]]["privateManifest"], {"mechanism": "windows"})
        self.assertLessEqual(len(by_id[older["id"]]["messages"][0]["text"]), rt.HISTORY_MESSAGE_CHARS)
        self.assertNotIn("artifactDirectory", json.dumps(history))
        self.assertIn("context/interviewer/interviewer.txt", turn.call_args.args[2])

    def test_settings_reject_values_the_page_never_offers(self):
        for settings in ({"provider": "gemini"}, {"minutes": 1}, {"minutes": "50"}, {"track": "sales"},
                         {"model": "--dangerously-skip-permissions"}, {"model": "-x"}, {"model": "a b"}, {"focus": "x" * 1001}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                rt.clean_settings(settings)
        self.assertEqual(rt.clean_settings({"model": " claude-opus-5-5[1m] "})["model"], "claude-opus-5-5[1m]")

    def test_candidate_files_hide_dotfiles_private_tree_and_symlinks(self):
        workspace = self.directory / "workspace"
        (workspace / "solution.py").write_text("# candidate\n")
        (workspace / ".reference.py").write_text("answer\n")
        (self.directory / ".examiner" / "reference.py").write_text("answer\n")
        try:
            (workspace / "reference.py").symlink_to(self.directory / ".examiner" / "reference.py")
        except OSError:
            pass
        self.assertEqual(self.runtime.candidate_files(self.directory), [{"path": "solution.py", "content": "# candidate\n"}])

    def test_save_rejects_hidden_traversal_absolute_and_dot_paths(self):
        for path in (".", "", ".reference.py", "tests/../answer.py", "/tmp/answer.py", "tests\\answer.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.runtime.save_files(self.scope, self.state["id"], [{"path": path, "content": "x"}])

    def test_a_browser_test_run_is_stored_with_a_snapshot_of_exactly_the_tested_files(self):
        run = {"exitCode": 1, "timedOut": False, "output": "1 failed", "tests": [
            {"name": "test_window", "file": "tests/test_public.py", "outcome": "failed", "message": "assert 2 == 3"}]}
        first = self.runtime.record_test_run(self.scope, self.state["id"], [{"path": "solution.py", "content": "first\n"}], run)
        second = self.runtime.record_test_run(self.scope, self.state["id"], [{"path": "solution.py", "content": "second\n"}],
                                              {**run, "exitCode": 0, "tests": []})
        self.assertEqual((self.directory / first["snapshot"] / "solution.py").read_text(), "first\n")
        self.assertEqual((self.directory / second["snapshot"] / "solution.py").read_text(), "second\n")
        self.assertEqual(first["runner"], "browser")
        saved = json.loads((self.directory / ".examiner" / "test-runs.json").read_text(encoding="utf-8"))
        self.assertEqual([item["exitCode"] for item in saved], [1, 0])
        self.assertEqual(saved[0]["tests"][0]["message"], "assert 2 == 3")
        for bad in ({"exitCode": "0", "timedOut": False}, {"exitCode": 0}, {"exitCode": 0, "timedOut": False, "tests": [{"outcome": "won"}]}):
            with self.subTest(run=bad), self.assertRaises(ValueError):
                self.runtime.record_test_run(self.scope, self.state["id"], [], bad)

    def test_the_connector_never_runs_candidate_code_itself(self):
        with patch.object(rt.subprocess, "run") as run, patch.object(rt.subprocess, "Popen") as popen:
            self.runtime.record_test_run(self.scope, self.state["id"], [{"path": "solution.py", "content": "import os\n"}],
                                         {"exitCode": 0, "timedOut": False, "output": ""})
        run.assert_not_called()
        popen.assert_not_called()

    def test_owner_scope_cannot_read_another_owners_round(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            self.runtime.load("owner-b", self.state["id"])

    def test_finished_missing_or_invalid_report_is_visible_and_valid_report_is_returned(self):
        self.state["finished"] = True
        self.assertIn("reportError", self.runtime.public(self.scope, self.state))
        report_path = self.directory / ".examiner" / "report.json"
        rt.write_json(report_path, self.report())
        self.assertEqual(self.runtime.public(self.scope, self.state)["report"]["id"], self.state["id"])
        report = self.report()
        report["dimensions"]["implementation"]["score"] = 3
        with self.assertRaisesRegex(ValueError, "evidence"):
            rt.validate_report(report, self.state)

    def test_invalid_structured_reply_does_not_corrupt_the_saved_state(self):
        with patch.object(self.runtime, "claude_turn", return_value={**REPLY, "finished": "false"}):
            self.runtime._turn(self.scope, self.state["id"], "Review", False)
        state = self.runtime.load(self.scope, self.state["id"])
        self.assertIs(state["finished"], False)
        self.assertIn("complete interview reply", state["error"])
        self.assertFalse(state["busy"])

    def test_stop_before_the_turn_starts_does_not_launch_the_agent(self):
        with patch.object(rt.threading, "Thread"):
            self.runtime.start_turn(self.scope, self.state["id"], "Review my code")
        self.runtime.interrupt(self.scope, self.state["id"])
        with patch.object(self.runtime, "claude_turn") as provider:
            self.runtime._turn(self.scope, self.state["id"], "Review my code", False)
            provider.assert_not_called()
        self.assertIn("stopped", self.runtime.load(self.scope, self.state["id"])["error"])

    def test_claude_runs_restricted_sandboxed_and_resumes_its_own_session(self):
        self.state["agentSessionId"] = "saved-cli-session"
        process = FakeProcess([{"type": "system", "session_id": "resumed-cli-session"},
                               {"type": "result", "is_error": False, "structured_output": REPLY}])
        with patch.object(agents, "provider_executable", return_value="claude"), \
             patch.object(agents, "claude_supports_isolation", return_value=True), \
             patch.object(agents, "claude_can_sandbox", return_value=True), \
             patch.object(agents, "python_read_roots", return_value=[]), \
             patch.object(rt.subprocess, "Popen", return_value=process) as spawn:
            result = self.runtime.claude_turn(self.state, self.directory, "Candidate question", lambda: self.runtime.save(self.scope, self.state))
        command = spawn.call_args.args[0]
        self.assertEqual(result, REPLY)
        for flag in ("--restricted", "--safe-mode", "--strict-mcp-config", "--resume", "saved-cli-session"):
            self.assertIn(flag, command)
        settings = json.loads(command[command.index("--settings") + 1])["sandbox"]
        self.assertTrue(settings["enabled"] and settings["failIfUnavailable"])
        self.assertFalse(settings["allowUnsandboxedCommands"])
        self.assertEqual(settings["filesystem"]["denyRead"], ["~"])
        self.assertEqual(settings["filesystem"]["allowRead"], [str(self.directory)])
        self.assertEqual(settings["network"]["allowedDomains"], [])
        self.assertEqual(self.runtime.load(self.scope, self.state["id"])["agentSessionId"], "resumed-cli-session")

    def test_claude_without_a_sandbox_gets_no_command_tool(self):
        command = agents.claude_command("claude", self.directory, self.state, sandbox=False, read_roots=[])
        tools = command[command.index("--tools") + 1]
        self.assertNotIn("Bash", tools)
        self.assertNotIn("--settings", command)
        self.assertIn("no command tool", agents.claude_preamble(False))

    def test_an_old_claude_is_refused_rather_than_run_unrestricted(self):
        with patch.object(agents, "provider_executable", return_value="claude"), \
             patch.object(agents, "claude_supports_isolation", return_value=False), \
             patch.object(rt.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "claude update"):
                self.runtime.claude_turn(self.state, self.directory, "Question", lambda: None)
        spawn.assert_not_called()

    def test_claude_error_array_is_reported(self):
        process = FakeProcess([{"type": "result", "is_error": True, "errors": ["Login expired. Sign in again."]}])
        with patch.object(agents, "provider_executable", return_value="claude"), \
             patch.object(agents, "claude_supports_isolation", return_value=True), \
             patch.object(agents, "claude_can_sandbox", return_value=False), \
             patch.object(rt.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "Login expired"):
                self.runtime.claude_turn(self.state, self.directory, "Candidate question", lambda: None)

    def cloud_round(self):
        self.state.update(title="An unseen case", caseFamily="events", concepts=["invariants"], agentSessionId="machine-local-id")
        self.state["messages"] = [{"role": "user", "text": "Here is my invariant.", "at": self.state["at"]}]
        self.runtime.save(self.scope, self.state)
        (self.directory / "workspace" / "solution.py").write_text("# candidate work\n", encoding="utf-8")
        (self.directory / "workspace" / "sample.bin").write_bytes(b"\x00\xff\x80")
        (self.directory / ".examiner" / "manifest.json").write_text('{"contract":"fixed"}', encoding="utf-8")
        (self.directory / ".examiner" / "claude-runtime.log").write_text("private-log-marker", encoding="utf-8")
        return self.runtime.export_sync(self.scope)["sessions"][0]

    def test_account_roundtrip_preserves_the_case_code_history_and_private_evidence(self):
        bundle = self.cloud_round()
        serialized = json.dumps(bundle)
        self.assertNotIn("machine-local-id", serialized)
        self.assertNotIn("private-log-marker", serialized)
        restored = rt.Runtime(self.root / "other-computer", INSTRUCTIONS, self.root / "logs")
        self.assertEqual(restored.import_sync(self.scope, {"sessions": [bundle]})["imported"], [self.state["id"]])
        directory = restored.directory(self.scope, self.state["id"])
        self.assertEqual((directory / "workspace" / "sample.bin").read_bytes(), b"\x00\xff\x80")
        self.assertEqual((directory / ".examiner" / "manifest.json").read_text(encoding="utf-8"), '{"contract":"fixed"}')
        self.assertTrue((directory / "context" / "interviewer" / "interviewer.txt").is_file())
        self.assertNotIn("agentSessionId", restored.load(self.scope, self.state["id"]))

    def test_stale_restore_preserves_local_work(self):
        bundle = self.cloud_round()
        bundle["expectedLocalRevision"] = bundle["revision"]
        self.runtime.save_files(self.scope, self.state["id"], [{"path": "solution.py", "content": "# newer local work\n"}])
        with self.assertRaises(rt.SyncConflict):
            self.runtime.import_sync(self.scope, {"sessions": [bundle]})
        self.assertEqual((self.directory / "workspace" / "solution.py").read_text(encoding="utf-8"), "# newer local work\n")


class StyleAndMarkingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.runtime = rt.Runtime(root / "interviews", INSTRUCTIONS, root / "logs")

    def test_the_interview_style_reaches_the_examiner_in_plain_words(self):
        with patch.object(agents, "provider_executable", return_value="claude"), patch.object(self.runtime, "start_turn"):
            created = self.runtime.create("owner", {"provider": "claude", "style": "tech", "track": "systems", "effort": "high"})
        settings = json.loads((self.runtime.directory("owner", created["id"]) / "session-settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["style"], "tech")
        self.assertIn("Not a LeetCode puzzle", settings["styleBrief"])
        self.assertEqual(settings["effort"], "high")
        with self.assertRaises(ValueError):
            rt.clean_settings({"style": "trading"})
        with self.assertRaises(ValueError):
            rt.clean_settings({"effort": "--max"})

    def test_parts_are_recorded_with_when_each_was_first_shown(self):
        state = {"id": str(uuid.uuid4()), "at": rt.stamp(), "settings": {**rt.DEFAULT_SETTINGS, "provider": "claude"}, "busy": False,
                 "finished": False, "messages": [], "testRuns": [], "error": "", "title": "", "caseFamily": "", "concepts": [], "stage": "", "activity": ""}
        directory = self.runtime.directory("owner", state["id"])
        (directory / ".examiner").mkdir(parents=True)
        self.runtime.save("owner", state)
        with patch.object(self.runtime, "claude_turn", return_value={**REPLY, "part": 1, "parts": 3}):
            self.runtime._turn("owner", state["id"], "Start", True)
        first = self.runtime.load("owner", state["id"])
        self.assertEqual((first["part"], first["parts"]), (1, 3))
        with patch.object(self.runtime, "claude_turn", return_value={**REPLY, "part": 2, "parts": 3}):
            self.runtime._turn("owner", state["id"], "Next", False)
        second = self.runtime.load("owner", state["id"])
        self.assertEqual(sorted(second["partTimes"]), ["1", "2"])
        self.assertEqual(second["partTimes"]["1"], first["partTimes"]["1"])

    def test_only_an_ended_round_can_be_re_marked(self):
        state = {"id": str(uuid.uuid4()), "at": rt.stamp(), "settings": {**rt.DEFAULT_SETTINGS, "provider": "claude"}, "busy": False,
                 "finished": False, "messages": [], "testRuns": [], "error": "", "title": "", "caseFamily": "", "concepts": [], "stage": "", "activity": ""}
        (self.runtime.directory("owner", state["id"]) / ".examiner").mkdir(parents=True)
        self.runtime.save("owner", state)
        with self.assertRaisesRegex(ValueError, "after it has ended"):
            self.runtime.start_turn("owner", state["id"], "Please re-mark", remark=True)
        state["finished"] = True
        self.runtime.save("owner", state)
        with patch.object(rt.threading, "Thread") as thread:
            self.runtime.start_turn("owner", state["id"], "Please re-mark", remark=True)
        self.assertTrue(thread.call_args.kwargs["args"][-1])
        with patch.object(self.runtime, "claude_turn", return_value={**REPLY, "finished": True}) as provider:
            self.runtime._turn("owner", state["id"], "Please re-mark", False, True)
        self.assertIn("re-mark", provider.call_args.args[2])

    def test_a_verdict_is_optional_but_checked(self):
        state = {"id": "x", "at": "2026-11-10T12:00:00Z"}
        report = {"format": "interview-lab-session-v1", "id": "x", "at": "2026-11-10T12:00:00Z", "caseId": "c", "caseFamily": "f", "title": "t",
                  "track": "hybrid", "mode": "mock", "difficulty": "standard", "novelty": "unseen", "plannedMinutes": 45, "actualMinutes": None,
                  "status": "completed", "verification": "executed", "dimensions": {name: {"score": None, "assistance": "none", "evidence": ""} for name in (
                      "clarification", "modeling", "implementation", "validation", "adaptation", "communication", "tools")},
                  "hints": [], "reveals": [], "skips": [], "strengths": [], "gaps": [], "nextDrill": "", "notes": ""}
        rt.validate_report(report, state)
        rt.validate_report({**report, "verdict": {"label": "Below the bar", "meetsBar": False}}, state)
        with self.assertRaisesRegex(ValueError, "verdict"):
            rt.validate_report({**report, "verdict": {"label": "", "meetsBar": "no"}}, state)


class CodexIsolationTests(unittest.TestCase):
    def test_codex_is_launched_with_its_reach_switched_off_and_home_reads_denied(self):
        client = agents.CodexClient.__new__(agents.CodexClient)
        client.binary = str(Path.home() / ".local" / "bin" / "codex")
        client.write_root = Path("/tmp/solvewright-interviews")
        overrides = client._overrides(profile=True, disable=["chrome-devtools", "ticktick"])
        values = [overrides[index + 1] for index in range(0, len(overrides), 2)]
        self.assertTrue(all(overrides[index] == "-c" for index in range(0, len(overrides), 2)))
        for expected in ("notify=[]", 'web_search="disabled"', "features.plugins=false", "features.apps=false",
                         "features.computer_use=false", "features.hooks=false", "mcp_servers.chrome-devtools.enabled=false",
                         'default_permissions="solvewright"'):
            self.assertIn(expected, values)
        profile = next(value for value in values if value.startswith("permissions.solvewright.filesystem="))
        self.assertIn('"~"="deny"', profile)
        self.assertIn('"/tmp/solvewright-interviews"="write"', profile)
        with self.assertRaises(ValueError):
            client._overrides(profile=True, disable=["bad name; rm"])

    def test_a_codex_that_cannot_list_its_mcp_servers_is_refused(self):
        client = agents.CodexClient.__new__(agents.CodexClient)
        client.binary = "codex"
        with patch.object(agents.subprocess, "run", return_value=Mock(returncode=2, stdout="")):
            with self.assertRaisesRegex(ValueError, "cannot list its MCP servers"):
                client._configured_mcp_servers()
        listed = json.dumps([{"name": "chrome-devtools", "enabled": True}, {"name": "ticktick", "enabled": False}])
        with patch.object(agents.subprocess, "run", return_value=Mock(returncode=0, stdout=listed)) as run:
            self.assertEqual(client._configured_mcp_servers(), ["chrome-devtools", "ticktick"])
        self.assertIn("features.plugins=false", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
