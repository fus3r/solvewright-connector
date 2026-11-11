"""Read a frozen exercise, never the candidate workspace or conversation."""
from __future__ import annotations

import json
import re
from pathlib import Path

FORMAT = "solvewright-round-v1"
MAX_BYTES = 2_000_000


def pack_round(directory: Path, state: dict) -> dict:
    if not state.get("finished") or state.get("busy") or state.get("ending"):
        raise ValueError("Finish the round and wait for the examiner before keeping it.")
    language = {"Python": "python", "C++": "cpp"}.get(state["settings"]["language"])
    if not language:
        raise ValueError("Only Python and C++ rounds can be kept as executable rounds.")
    root = directory / ".examiner" / "pack"

    def fail(reason):
        raise ValueError("This round cannot be packed: " + reason + ". Older rounds need a frozen starter, each part's brief, tests, checks and reference, and an exercise debrief in .examiner/pack/.")

    def read(name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,120}", name) or any(piece.startswith(".") for piece in name.split("/")):
            fail("invalid exercise asset path")
        path = root / name
        if root.is_symlink() or root.parent.is_symlink() or any(p.is_symlink() for p in (path, *(root / parent for parent in Path(name).parents))) or not path.resolve().is_relative_to(root.resolve()):
            fail("exercise assets must be regular files inside .examiner/pack")
        if not path.is_file() or path.stat().st_size > 250_000:
            fail("missing or oversized exercise asset " + name)
        return path.read_text(encoding="utf-8")

    def text(value, limit=160, empty=False):
        if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
            fail("invalid manifest text")
        return value

    def number(value, low=1, high=240):
        if type(value) is not int or not low <= value <= high:
            fail("invalid manifest number")
        return value

    def filename(value):
        text(value, 121)
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,120}", value) or ".." in value or any(p.startswith(".") for p in value.split("/")) or value.endswith("/") or "//" in value or any(p in ("__proto__", "constructor", "prototype") for p in value.split("/")):
            fail("invalid workspace file name")
        return value

    def files(value):
        if not isinstance(value, dict) or len(value) > 32:
            fail("invalid exercise files")
        return {filename(path): read(source) for path, source in value.items()}

    try:
        manifest = json.loads(read("manifest.json"))
        if not isinstance(manifest, dict) or manifest.get("format") != 1:
            fail("missing version 1 pack manifest")
        parts = manifest.get("parts")
        if not isinstance(parts, list) or not 1 <= len(parts) <= 8 or (state.get("parts") and len(parts) != state["parts"]):
            fail("the manifest does not hold every planned part")
        built, paths, ids = [], set(), set()
        suffix = "cpp" if language == "cpp" else "py"
        for index, part in enumerate(parts, 1):
            if part["number"] != index:
                fail("parts must be numbered in order")
            tests, checks = part["tests"], part["checks"]
            for item, public in ((tests, True), (checks, False)):
                path = filename(item["path"])
                pattern = rf"tests/test_[A-Za-z0-9_-]+\.{suffix}" if public else rf"test_[A-Za-z0-9_-]+\.{suffix}"
                if not re.fullmatch(pattern, path) or path in paths:
                    fail("test paths must be unique and match the round language")
                paths.add(path)
            labels = checks["labels"]
            if not isinstance(labels, list) or not 1 <= len(labels) <= 100:
                fail("each part needs labelled private checks")
            clean_labels = []
            for label in labels:
                check_id = text(label["id"], 300)
                if not check_id.startswith(checks["path"] + "::") or check_id in ids or label["part"] != index:
                    fail("private check ids must be unique and belong to their part's file")
                ids.add(check_id)
                clean_labels.append({"id": check_id, "part": index, "label": text(label["label"]), "note": text(label["note"], 400)})
            hints = part.get("hints", [])
            if not isinstance(hints, list) or len(hints) > 8:
                fail("invalid hint ladder")
            built.append({"number": index, "title": text(part["title"]), "minutes": number(part["minutes"]),
                          "brief": read(part["brief"]), "rules": read(part["rules"]),
                          "tests": {"path": tests["path"], "content": read(tests["source"])},
                          "checks": {"path": checks["path"], "content": read(checks["source"]), "labels": clean_labels},
                          "solution": read(part["solution"]), "files": files(part.get("files", {})),
                          "hints": [text(hint, 8000) for hint in hints]})
        starter = files(manifest["files"])
        main = "solution.hpp" if language == "cpp" else "solution.py"
        all_paths = set(starter)
        file_count = len(starter)
        for part in built:
            for field in ("brief", "rules"):
                text(part[field], 32000, empty=field == "rules")
            text(part["solution"], 250000)
            for field in ("tests", "checks"):
                text(part[field]["content"], 250000)
            extra = part["files"]
            if main in extra or "README.md" in extra or any(path.startswith("tests/") for path in extra):
                fail("a part overwrites the solution, README or tests")
            for path in (*extra, part["tests"]["path"], part["checks"]["path"]):
                if path in all_paths:
                    fail("duplicate workspace or check file path")
                all_paths.add(path)
                file_count += 1
        if file_count > 128 or len(ids) > 200:
            fail("too many exercise files or private checks")
        if main not in starter or "README.md" in starter or any(main in part["files"] for part in built):
            fail("the frozen starter is missing or overwritten by a later part")
        settings = state["settings"]
        result = {"format": FORMAT, "round": {"id": "kept-" + state["id"], "version": 1,
                  "title": text(state["title"]), "summary": text(manifest["summary"], 4000), "language": language,
                  "style": settings.get("style", "quant"), "domain": settings.get("domain", "general"),
                  "level": settings["difficulty"], "minutes": settings["minutes"], "origin": "Kept round", "skills": [],
                  "files": starter, "parts": built, "debrief": text(read(manifest["debrief"]), 32000)}}
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
            fail("the exercise is larger than 2 MB")
        return result
    except (KeyError, TypeError, OSError, UnicodeError, json.JSONDecodeError) as error:
        fail("missing or unreadable exercise manifest or asset (" + str(error)[:160] + ")")
