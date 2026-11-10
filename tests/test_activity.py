"""What the page is told while the examiner works: kinds of steps only, never content."""
import io
import json
import threading
import unittest

from solvewright import activity, agents


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.state, self.saves, self.clock = {}, [], Clock()

    def progress(self, kind):
        return activity.TurnProgress(kind, self.state, lambda: self.saves.append(json.loads(json.dumps(self.state))),
                                     root="/rounds/r1", clock=self.clock)

    def test_a_prepared_round_walks_through_its_steps_from_codex_events(self):
        progress = self.progress("prepare")
        events = [
            {"type": "commandExecution", "command": "cat context/interviewer/interviewer.txt",
             "commandActions": [{"type": "read", "command": "cat", "name": "interviewer.txt", "path": "context/interviewer/interviewer.txt"}]},
            {"type": "reasoning", "summary": [], "content": []},
            {"type": "fileChange", "changes": [{"path": "/rounds/r1/.examiner/reference.py", "kind": "add", "diff": "secret"}]},
            {"type": "commandExecution", "command": "python3 -B .examiner/verify_setup.py", "commandActions": [{"type": "unknown", "command": "python3"}]},
            {"type": "fileChange", "changes": [{"path": "/rounds/r1/workspace/solution.py", "kind": "add", "diff": "class A: ..."}]},
            {"type": "commandExecution", "command": "python3 -m unittest discover -s tests", "commandActions": []},
            {"type": "agentMessage", "text": '{"message": "Build a ledger"}', "phase": "final_answer"},
        ]
        for event in events:
            progress.observe(activity.codex_action(event))
        self.assertEqual([step["id"] for step in self.state["turn"]["steps"]], ["brief", "design", "reference", "checks", "files", "reply"])
        self.assertEqual(self.state["turn"]["checks"], 2)
        self.assertEqual(self.state["activity"], "Writing the reply")
        # Nothing the examiner wrote or ran reaches the page.
        saved = json.dumps(self.saves)
        for private in ("secret", "verify_setup", "class A", "Build a ledger", "unittest"):
            self.assertNotIn(private, saved)

    def test_a_reply_reads_runs_and_answers_from_claude_blocks(self):
        progress = self.progress("reply")
        blocks = [
            {"type": "thinking", "thinking": "the cancel path"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/rounds/r1/workspace/solution.py"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "cd workspace && python3 -m pytest -q"}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/rounds/r1/.examiner/progress.json", "content": "{}"}},
            {"type": "text", "text": "Looks right"},
            {"type": "tool_use", "name": "StructuredOutput", "input": {"message": "Good."}},
        ]
        for block in blocks:
            progress.observe(activity.claude_action(block))
        self.assertEqual([step["id"] for step in self.state["turn"]["steps"]], ["read", "checks", "notes", "reply"])
        self.assertNotIn("the cancel path", json.dumps(self.saves))
        progress.finish()
        self.assertNotIn("turn", self.state)

    def test_repeated_actions_save_at_most_twice_a_second(self):
        progress = self.progress("reply")
        read = [("read", ["workspace/solution.py"], "cat workspace/solution.py")]
        progress.observe(read)
        progress.observe(read)
        progress.observe(read)
        self.assertEqual(len(self.saves), 1)
        self.clock.now = 0.2
        progress.observe([("run", [], "python3 x.py")])  # a new step saves at once
        progress.observe([("run", [], "python3 y.py")])  # the second run within half a second waits
        self.assertEqual(len(self.saves), 2)
        self.clock.now = 0.8
        progress.observe([("run", [], "python3 z.py")])
        self.assertEqual(len(self.saves), 3)
        self.assertEqual(self.saves[-1]["turn"]["checks"], 3)

    def test_only_python_runs_count_as_checks(self):
        kinds = lambda actions: [action[0] for action in actions]
        self.assertEqual(kinds(activity.codex_action({"type": "commandExecution", "command": "ls -la workspace", "commandActions": [{"type": "listFiles", "command": "ls", "path": "workspace"}]})), ["read"])
        self.assertEqual(activity.codex_action({"type": "commandExecution", "command": "mkdir -p .examiner/checks", "commandActions": [{"type": "unknown", "command": "mkdir"}]}), [])
        self.assertEqual(kinds(activity.claude_action({"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q workspace 2>&1 >/dev/null"}})), ["run"])
        self.assertEqual(activity.codex_action({"type": "agentMessage", "text": "I'll check", "phase": "commentary"}), [])

    def test_the_commands_a_real_codex_examiner_ran_while_preparing(self):
        # Taken from a real preparing turn: compound reads, heredoc writes, scripts that write the setup.
        commands = [
            "pwd; rg --files -g '!node_modules' -g '!venv' -g '!\\.git' | head -60",
            "cat context/interviewer/references/scoring.txt context/interviewer/references/session-format.txt; cat state.json",
            "mkdir -p .examiner/checks .examiner/build/tests\ncat > .examiner/prepare.py <<'PY'\nfrom pathlib import Path\nPY\npython3 .examiner/prepare.py",
            "cat > .examiner/verify.py <<'PY'\nimport unittest\nPY\npython3 -B .examiner/verify.py",
            "cat > workspace/solution.py <<'PY'\nclass TradeLedger:\n    pass\nPY",
        ]
        progress = self.progress("prepare")
        progress.observe(activity.codex_action({"type": "reasoning"}))
        for command in commands:
            progress.observe(activity.codex_action({"type": "commandExecution", "command": command, "commandActions": [{"type": "unknown", "command": command}]}))
        progress.observe(activity.codex_action({"type": "agentMessage", "phase": "final_answer", "text": "{}"}))
        self.assertEqual([step["id"] for step in self.state["turn"]["steps"]], ["design", "brief", "reference", "checks", "files", "reply"])
        self.assertNotIn("TradeLedger", json.dumps(self.saves))

    def test_where_a_path_belongs(self):
        self.assertEqual(activity.where("/rounds/r1/workspace/tests/test_public.py", "/rounds/r1"), "workspace")
        self.assertEqual(activity.where("./.examiner/report.json"), "report")
        self.assertEqual(activity.where(".examiner/checks/test_hidden.py"), "private")
        self.assertEqual(activity.where("history-context.json"), "brief")
        self.assertEqual(activity.where("/etc/passwd", "/rounds/r1"), "other")


class CodexWatcherTests(unittest.TestCase):
    def test_item_started_reaches_the_turns_watcher_and_limits_are_kept(self):
        client = object.__new__(agents.CodexClient)
        client.pending, client.finished, client.messages, client.watchers = {}, {}, {}, {}
        client.condition, client.closed = threading.Condition(), threading.Event()
        client.limits, client.limits_at, client.process = None, 0.0, None
        seen = []
        client.watchers["thread-1"] = seen.append
        lines = [
            {"method": "item/started", "params": {"threadId": "thread-1", "turnId": "t", "item": {"type": "reasoning"}, "startedAtMs": 1}},
            {"method": "item/started", "params": {"threadId": "thread-2", "turnId": "t", "item": {"type": "reasoning"}, "startedAtMs": 1}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {"primary": {"usedPercent": 40}}}},
        ]
        process = type("P", (), {"stdout": io.StringIO("".join(json.dumps(line) + "\n" for line in lines))})()
        client._read(process)
        self.assertEqual(seen, [{"type": "reasoning"}])
        self.assertEqual(client.limits["primary"]["usedPercent"], 40)


if __name__ == "__main__":
    unittest.main()


class InstallRootTests(unittest.TestCase):
    # Codex's sandbox and its link chain are macOS and Linux matters; symlinks need privileges on Windows.
    @unittest.skipIf(agents.WINDOWS, "symbolic links need extra privileges on Windows")
    def test_every_link_folder_on_the_way_to_codex_is_readable(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch).resolve()
            release = home / ".codex" / "packages" / "standalone" / "releases" / "0.167.0" / "bin"
            release.mkdir(parents=True)
            (release / "codex").write_text("#!/bin/sh\n")
            os.symlink(home / ".codex/packages/standalone/releases/0.167.0", home / ".codex/packages/standalone/current")
            (home / ".local" / "bin").mkdir(parents=True)
            os.symlink(home / ".codex/packages/standalone/current/bin/codex", home / ".local/bin/codex")
            self.assertEqual(agents.link_folders(home / ".local/bin/codex"), {home / ".local/bin", home / ".codex/packages/standalone"})
            client = object.__new__(agents.CodexClient)
            client.binary = str(home / ".local/bin/codex")
            with patch.object(Path, "home", return_value=home):
                roots = client._install_roots()
            # The folder holding `current` is what resuming a thread after a restart needed.
            self.assertIn(str(home / ".codex/packages/standalone"), roots)
            self.assertIn(str(home / ".codex/packages/standalone/releases/0.167.0"), roots)
