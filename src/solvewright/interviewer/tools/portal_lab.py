#!/usr/bin/env python3
"""Synthetic portal exercise infrastructure. Examiner source contains data spoilers."""

import argparse
import heapq
import json
import math
import random
import selectors
import subprocess
import sys
from pathlib import Path


def point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("A point must contain two finite numbers.")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > 1e6 for x in value):
        raise ValueError("Coordinates must be finite numbers within +/-1,000,000.")
    return tuple(float(x) for x in value)


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def route_distance(origin, destination, portals):
    """Exact shortest route: complete Euclidean walking graph + free portal edges."""
    nodes = [point(origin), point(destination)]
    mates = {}
    for portal in portals:
        i = len(nodes)
        nodes.extend([point(portal["a"]), point(portal["b"])])
        mates[i], mates[i + 1] = i + 1, i
    best = [math.inf] * len(nodes)
    best[0] = 0.0
    queue = [(0.0, 0)]
    while queue:
        cost, node = heapq.heappop(queue)
        if cost != best[node]:
            continue
        if node == 1:
            return cost
        for other, location in enumerate(nodes):
            step = 0.0 if mates.get(node) == other else distance(nodes[node], location)
            candidate = cost + step
            if candidate < best[other]:
                best[other] = candidate
                heapq.heappush(queue, (candidate, other))
    raise RuntimeError("Walking always provides a route.")


def episode(seed, count=96):
    rng = random.Random(seed)
    angle = rng.uniform(0, 2 * math.pi)
    length = rng.uniform(35, 65)
    offset = (length * math.cos(angle), length * math.sin(angle))
    x, y = rng.uniform(-20, 20), rng.uniform(-20, 20)
    arrivals = []
    for _ in range(count):
        x += rng.gauss(0.45 * math.cos(angle + 0.8), 1.8)
        y += rng.gauss(0.45 * math.sin(angle + 0.8), 1.8)
        origin = [round(x, 5), round(y, 5)]
        destination = [round(x + offset[0] + rng.gauss(0, 2), 5), round(y + offset[1] + rng.gauss(0, 2), 5)]
        arrivals.append({"origin": origin, "destination": destination})
    return {"format": "portal-episode-v1", "synthetic": True, "seed": seed, "arrivals": arrivals}


def load_episode(path):
    data = json.loads(Path(path).read_text())
    if data.get("format") != "portal-episode-v1" or not data.get("arrivals"):
        raise ValueError("Expected a nonempty portal-episode-v1 file.")
    for row in data["arrivals"]:
        point(row["origin"])
        point(row["destination"])
    return data


class Scorer:
    def __init__(self):
        self.portals = []
        self.construction = 0.0
        self.travel = 0.0
        self.baseline = 0.0

    def apply(self, arrival, builds):
        if not isinstance(builds, list) or len(self.portals) + len(builds) > 24:
            raise ValueError("build must be a list; at most 24 portals per episode.")
        checked = [{"a": list(point(p["a"])), "b": list(point(p["b"]))} for p in builds]
        self.construction += sum(5 * distance(p["a"], p["b"]) for p in checked)
        self.portals.extend(checked)
        self.travel += route_distance(arrival["origin"], arrival["destination"], self.portals)
        self.baseline += distance(arrival["origin"], arrival["destination"])

    def result(self):
        total = self.construction + self.travel
        return {"construction": self.construction, "travel": self.travel, "total": total,
                "no_portal": self.baseline, "portals": len(self.portals),
                "savings_percent": 100 * (1 - total / self.baseline) if self.baseline else None}


def score(data, plan):
    steps = {}
    for build in plan:
        index = build["step"]
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(data["arrivals"]):
            raise ValueError("A plan step must name an arrival index in this episode.")
        steps.setdefault(index, []).append(build)
    scorer = Scorer()
    for index, arrival in enumerate(data["arrivals"]):
        scorer.apply(arrival, steps.get(index, []))
    return scorer.result()


def run_policy(data, path):
    """Prefix-only stdin protocol, not a filesystem/network security sandbox."""
    process = subprocess.Popen([sys.executable, str(Path(path).resolve())], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=None, bufsize=0)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = b""
    scorer = Scorer()
    try:
        for index, arrival in enumerate(data["arrivals"]):
            request = {"step": index, "horizon": len(data["arrivals"]), **arrival, "portals": scorer.portals}
            process.stdin.write((json.dumps(request) + "\n").encode())
            process.stdin.flush()
            import time
            deadline = time.monotonic() + 10
            while b"\n" not in buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ValueError(f"Policy timed out at step {index}.")
                chunk = process.stdout.read(65536)
                if not chunk:
                    raise ValueError(f"Policy exited without a response at step {index}.")
                buffer += chunk
                if len(buffer) > 100000:
                    raise ValueError("Policy response is too large.")
            line, buffer = buffer.split(b"\n", 1)
            response = json.loads(line)
            if not isinstance(response, dict) or set(response) != {"build"}:
                raise ValueError("Each policy response must be an object with only a build list.")
            scorer.apply(arrival, response["build"])
        return scorer.result()
    finally:
        selector.close()
        if process.stdin:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def self_test():
    assert route_distance([0, 0], [3, 4], []) == 5
    portal = {"a": [0, 0], "b": [10, 0]}
    assert route_distance([10, 0], [0, 0], [portal]) == 0  # bidirectional
    chain = [portal, {"a": [10, 0], "b": [20, 0]}]
    assert route_distance([0, 0], [20, 0], chain) == 0  # two portals required
    assert route_distance([0, 0], [0, 2], [portal]) == 2  # walking remains available
    data = {"arrivals": [{"origin": [0, 0], "destination": [10, 0]}] * 6}
    early = score(data, [{"step": 0, **portal}])
    late = score(data, [{"step": 5, **portal}])
    assert early["construction"] == 50 and early["travel"] == 0 and early["total"] == 50
    assert late["total"] == 100 and late["travel"] == 50  # no retrospective saving
    assert score(data, [])["total"] == 60
    assert episode(42) == episode(42)
    print("Portal checks passed: walking, both directions, chaining, accounting, timing, repeatability.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    samples = sub.add_parser("samples")
    samples.add_argument("--out", required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("--seed", type=int, required=True)
    generate.add_argument("--out", required=True)
    for command in ("score", "run"):
        p = sub.add_parser(command)
        p.add_argument("--episode", required=True)
        if command == "score":
            p.add_argument("--plan")
        else:
            p.add_argument("--policy", required=True)
    args = parser.parse_args()
    if args.command == "self-test":
        self_test()
    elif args.command == "samples":
        destination = Path(args.out)
        destination.mkdir(parents=True, exist_ok=True)
        for index in range(1, 9):
            (destination / f"sample-{index:02d}.json").write_text(json.dumps(episode(2000 + index), indent=2) + "\n")
        print(f"Wrote eight synthetic episodes to {destination}.")
    elif args.command == "generate":
        Path(args.out).write_text(json.dumps(episode(args.seed), indent=2) + "\n")
        print(f"Wrote synthetic episode to {args.out}.")
    elif args.command == "score":
        plan = json.loads(Path(args.plan).read_text()) if args.plan else []
        print(json.dumps(score(load_episode(args.episode), plan), indent=2))
    elif args.command == "run":
        print(json.dumps(run_policy(load_episode(args.episode), args.policy), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        print(f"Portal evaluation failed: {exc}", file=sys.stderr)
        sys.exit(1)
