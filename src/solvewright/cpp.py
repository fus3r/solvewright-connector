"""Discover a native C++20 verifier and just the toolchain folders it reads."""
from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_cached: tuple[float, dict] | None = None
FLAGS = ["-std=c++20", "-fno-exceptions", "-O1", "-Wall"]
PROBE = """#include <concepts>
#include <span>
#include <string>
#include <vector>
static_assert(std::integral<int>);
int main() { std::vector<int> v{20, 22}; std::span<int> s(v);
return s[0] + s[1] == 42 && std::string(30, 'x').size() == 30 ? 0 : 1; }
"""


def query(command: list[str], **kwargs) -> str:
    result = subprocess.run(command, input="", capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=15, **kwargs)
    return result.stdout + result.stderr


def read_roots(compiler: str) -> list[str]:
    from .agents import link_folders

    paths = {Path(compiler).parent, Path(compiler).resolve().parent}
    if sys.platform == "darwin":
        # /usr/bin/c++ is an Xcode dispatcher, not the compiler it ultimately starts.
        for args in (["xcrun", "--find", "clang++"], ["xcrun", "--show-sdk-path"]):
            found = query(args).strip()
            if found and Path(found).exists():
                paths.add(Path(found).parent if args[1] == "--find" else Path(found))
    includes = query([compiler, "-std=c++20", "-E", "-x", "c++", "-", "-v"])
    inside = False
    for line in includes.splitlines():
        if "#include <...> search starts here:" in line:
            inside = True
        elif "End of search list." in line:
            inside = False
        elif inside:
            paths.add(Path(line.strip().removesuffix(" (framework directory)")))
    resource = query([compiler, "-print-resource-dir"]).strip()
    if resource and Path(resource).is_dir():
        paths.add(Path(resource))
    for line in query([compiler, "-print-search-dirs"]).splitlines():
        if re.match(r"^(?:libraries|programs): =?", line):
            paths.update(Path(item) for item in line.split(":", 1)[1].strip().lstrip("=").split(os.pathsep) if item)
    roots: set[Path] = set()
    pending = list(paths)
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        if not path.is_dir():
            continue
        roots.update((path, path.resolve()))
        roots.update(link_folders(path))
        # Keep intermediate spellings too: resolving /var/.../current all at
        # once loses the target of current behind the /var -> /private/var link.
        for link in (path, *path.parents):
            if link.is_symlink():
                roots.add(link.parent)
                target = Path(os.readlink(link))
                if not target.is_absolute():
                    target = link.parent / target
                pending.append(target / path.relative_to(link))
    roots.update(link_folders(Path(compiler)))
    # Never turn an unusual toolchain configuration into an allowance for all owner files.
    return sorted(str(path) for path in roots if path.resolve() not in (Path(path.anchor), Path.home().resolve()))


def discover(max_age: float = 300) -> dict:
    """Compile AND run a C++20 probe; mere presence on PATH is not enough."""
    global _cached
    if _cached and time.monotonic() - _cached[0] < max_age:
        return copy.deepcopy(_cached[1])
    reason = ("Install Xcode Command Line Tools (xcode-select --install), then restart the connector." if sys.platform == "darwin"
              else "Install build-essential, then restart the connector." if sys.platform.startswith("linux")
              else "Native Windows supports Codex with a working clang++ or g++ C++20 toolchain (LLVM/MinGW). MSVC cl.exe and Claude Code without a command sandbox are not supported; WSL2 is another option.")
    info = {"available": False, "compiler": None, "reason": reason, "readRoots": []}
    for name in ("c++", "g++", "clang++"):
        compiler = shutil.which(name)
        if not compiler:
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="solvewright-cpp-") as temporary:
                folder = Path(temporary)
                source, program = folder / "probe.cpp", folder / ("probe.exe" if sys.platform == "win32" else "probe")
                source.write_text(PROBE, encoding="utf-8")
                built = subprocess.run([compiler, *FLAGS, str(source), "-o", str(program)], capture_output=True, timeout=30)
                if built.returncode or subprocess.run([str(program)], capture_output=True, timeout=5).returncode:
                    continue
                roots = read_roots(compiler)
                info = {"available": True, "compiler": compiler, "reason": "", "readRoots": roots}
                break
        except (OSError, subprocess.SubprocessError):
            continue
    _cached = (time.monotonic(), info)
    return copy.deepcopy(info)


def capability() -> dict:
    return {key: value for key, value in discover().items() if key != "readRoots"}
