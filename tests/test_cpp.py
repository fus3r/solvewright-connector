"""C++ capability, narrow toolchain reads and the live-round file contract."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from solvewright import agents, cpp, runtime as rt, worker


class CppTests(unittest.TestCase):
    def test_bundled_header_matches_page_byte_for_byte(self):
        package = Path(cpp.__file__).parent / "interviewer" / "tools" / "check.hpp"
        page = Path(__file__).resolve().parents[2] / "src" / "lib" / "cpp" / "check.hpp"
        # The standalone connector distribution has no page checkout.
        if not page.exists():
            self.skipTest("Page source is checked by the monorepo test gate")
        self.assertEqual(package.read_bytes(), page.read_bytes())

    def test_probe_requires_both_compilation_and_execution_and_tries_the_next_compiler(self):
        for statuses, expected in (([1, 0, 0], "/bin/g++"), ([0, 1, 1], None), ([0, 0], "/bin/c++")):
            with self.subTest(statuses=statuses), patch.object(cpp, "_cached", None), \
                 patch.object(cpp.shutil, "which", side_effect=lambda name: f"/bin/{name}" if name != "clang++" else None), \
                 patch.object(cpp, "read_roots", return_value=["/tools/include"]), \
                 patch.object(cpp.subprocess, "run", side_effect=[Mock(returncode=code) for code in statuses]) as run:
                info = cpp.discover()
                self.assertEqual(info["compiler"], expected)
                self.assertEqual(info["available"], expected is not None)
                self.assertIn("-std=c++20", run.call_args_list[0].args[0])
                self.assertIn("-fno-exceptions", run.call_args_list[0].args[0])
        with patch.object(cpp, "_cached", None), patch.object(cpp.shutil, "which", return_value="/bin/c++"), \
             patch.object(cpp.subprocess, "run", side_effect=subprocess.TimeoutExpired("c++", 30)):
            self.assertFalse(cpp.discover()["available"])

    # The layout below is a macOS toolchain (xcrun, an SDK behind links). Windows gives the temp folder a short
    # 8.3 alias and extended-length prefixes that this layout cannot have, so the comparison means nothing there.
    @unittest.skipIf(os.name == "nt", "simulates a macOS toolchain layout")
    def test_discovery_follows_toolchain_sdk_include_and_library_links_without_allowing_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "tool-store").mkdir()
            try:
                (home / "tools").symlink_to(home / "tool-store", target_is_directory=True)
            except OSError:
                self.skipTest("Symbolic links unavailable")
            release = home / "tools" / "releases" / "v1"
            for name in ("bin", "include/c++/v1", "lib/clang/21", "lib", "sdk"):
                (release / name).mkdir(parents=True, exist_ok=True)
            compiler = release / "bin/clang++"
            compiler.touch()
            current = home / "tools/current"
            try:
                current.symlink_to(release, target_is_directory=True)
                (home / "sdk").symlink_to(release / "sdk", target_is_directory=True)
            except OSError:
                self.skipTest("Symbolic links unavailable")
            alias = current / "bin/clang++"

            def query(command):
                if command == ["xcrun", "--find", "clang++"]:
                    return str(alias)
                if command == ["xcrun", "--show-sdk-path"]:
                    return str(home / "sdk")
                if "-print-resource-dir" in command:
                    return str(current / "lib/clang/21")
                if "-print-search-dirs" in command:
                    return f"programs: ={current / 'bin'}\nlibraries: ={current / 'lib'}\n"
                return f"#include <...> search starts here:\n {current}/include/c++/v1\n {home}\nEnd of search list.\n"

            with patch.object(cpp.sys, "platform", "darwin"), patch.object(cpp, "query", side_effect=query), \
                 patch.object(Path, "home", return_value=home):
                roots = set(cpp.read_roots(str(alias)))
            for path in (release / "bin", release / "include/c++/v1", release / "lib", release / "sdk", home / "tools"):
                self.assertIn(str(path), roots)
                self.assertIn(str(path.resolve()), roots)
            for path in (current / "bin", current / "include/c++/v1", current / "lib", home / "sdk"):
                self.assertIn(str(path), roots)
            self.assertNotIn(str(home), roots)
            self.assertNotIn(str(home.resolve()), roots)
            self.assertNotIn(str(home.parent), roots)
            self.assertNotIn("/", roots)
            self.assertNotIn(str(home / "Documents"), roots)

    def test_gcc_discovery_uses_reported_includes_and_libraries_without_xcode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("bin", "include/c++/14", "lib/gcc/14"):
                (root / name).mkdir(parents=True, exist_ok=True)
            compiler = root / "bin/g++"
            compiler.touch()

            def query(command):
                self.assertNotIn("xcrun", command)
                if "-print-resource-dir" in command:
                    return "g++: error: unrecognized command-line option '-print-resource-dir'"
                if "-print-search-dirs" in command:
                    return f"libraries: ={root / 'lib/gcc/14'}{os.pathsep}{root / 'missing'}\n"
                return f"#include <...> search starts here:\n {root}/include/c++/14\nEnd of search list.\n"

            with patch.object(cpp.sys, "platform", "linux"), patch.object(cpp, "query", side_effect=query):
                roots = set(cpp.read_roots(str(compiler)))
            expected = {root / name for name in ("bin", "include/c++/14", "lib/gcc/14")}
            # A temp root may itself be an alias (/var on macOS, 8.3 names on Windows).
            self.assertEqual(roots, {str(path) for path in expected | {path.resolve() for path in expected}})

    def test_both_sandboxes_add_only_discovered_cpp_read_roots(self):
        roots = [str(Path("/tools") / name) for name in ("compiler/bin", "sdk", "include", "lib")]
        client = agents.CodexClient.__new__(agents.CodexClient)
        client.write_root = Path("/rounds")
        with patch.object(client, "_install_roots", return_value=[]), patch.object(cpp, "discover", return_value={"readRoots": roots}):
            values = client._overrides(True)
        profile = next(value for value in values if value.startswith("permissions.solvewright.filesystem="))
        for root in roots:
            self.assertIn(f'{json.dumps(root)}="read"', profile)
        self.assertIn('"~"="deny"', profile)
        command = agents.claude_command("claude", Path("/round"), {"settings": {}}, True, roots)
        settings = json.loads(command[command.index("--settings") + 1])["sandbox"]
        self.assertEqual(settings["filesystem"], {"denyRead": ["~"], "allowRead": [str(Path("/round")), *roots]})
        self.assertEqual(settings["network"], {"allowedDomains": []})

    def test_health_reports_the_probe_and_no_internal_read_roots(self):
        info = {"available": False, "compiler": None, "reason": "Install build-essential", "readRoots": ["/secret"]}
        with patch.object(agents, "describe", return_value={}), patch.object(cpp, "discover", return_value=info):
            health = worker.health(Mock(root=Path("/rounds")))
        self.assertEqual(health["cpp"], {key: value for key, value in info.items() if key != "readRoots"})
        self.assertEqual(health["connector"], "0.1.7")

    def test_cpp_round_ships_the_header_rules_compiler_and_cpp_private_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = rt.Runtime(root, Path(rt.__file__).parent / "interviewer", root / "logs")
            with patch.object(agents, "provider_executable", return_value="codex"), \
                 patch.object(cpp, "capability", return_value={"available": True, "compiler": "/tools/c++"}), \
                 patch.object(runtime, "start_turn") as turn:
                created = runtime.create("scratch", {"language": "C++", "provider": "codex"})
            folder = runtime.directory("scratch", created["id"])
            self.assertEqual((folder / "workspace/.sw/check.hpp").read_bytes(), (folder / "context/interviewer/tools/check.hpp").read_bytes())
            self.assertEqual(json.loads((folder / "session-settings.json").read_text())["cppCompiler"], "/tools/c++")
            self.assertNotIn("test_*.py", turn.call_args.args[2])
            self.assertIn(rt.CPP_RULE, turn.call_args.args[2])
            self.assertIn("shared subgraphs", (folder / "context/interviewer/references/cpp.txt").read_text())
            self.assertIn("two viable approaches", (folder / "context/interviewer/references/case-generation.txt").read_text())
            self.assertEqual(created["files"], [])
            checks = folder / rt.CHECKS
            checks.mkdir()
            (checks / "test_core.cpp").write_text('TEST(example) {}')
            (checks / "test_wrong.py").write_text('private python')
            (checks / "checks.json").write_text(json.dumps({"checks": [{"id": "test_core.cpp::example", "label": "Example", "note": "Look at it", "part": 1}]}))
            result = runtime.checks("scratch", created["id"])
            self.assertEqual([file["path"] for file in result["files"]], ["test_core.cpp"])
            self.assertEqual(result["checks"][0]["label"], "Example")
            self.assertIn(rt.CPP_RULE, runtime.continuation(runtime.load("scratch", created["id"]), folder, "review"))
            with patch.object(cpp, "capability", return_value={"available": False, "reason": "Install compiler"}):
                with self.assertRaisesRegex(ValueError, "Install compiler"):
                    runtime.create("scratch", {"language": "C++"})
            with patch.object(cpp, "capability", return_value={"available": True}), patch.object(agents, "claude_can_sandbox", return_value=False):
                with self.assertRaisesRegex(ValueError, "command sandbox"):
                    runtime.create("scratch", {"language": "C++", "provider": "claude"})
