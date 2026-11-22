#!/usr/bin/env python3
"""Write synthetic Claude Code transcripts so SpendTab can be demoed safely.

    python3 examples/make_demo.py --dir /tmp/spendtab-demo
    spendtab --root /tmp/spendtab-demo/projects --no-cache

No real session data is read or copied. Costs and paths are invented.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def model(**overrides):
    base = {
        "inputTokens": 0,
        "outputTokens": 0,
        "thinkingTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "cacheCreationEphemeral5mInputTokens": 0,
        "cacheCreationEphemeral1hInputTokens": 0,
        "webSearchRequests": 0,
        "costUSD": 0.0,
    }
    base.update(overrides)
    return base


def cost_state(session_id, cost, days_ago, models, unknown=False):
    now_ms = int(time.time() * 1000)
    return {
        "type": "cost-state",
        "sessionId": session_id,
        "totalCostUSD": cost,
        "totalAPIDuration": 3_600_000,
        "totalDuration": 7_200_000,
        "totalToolDuration": 60_000,
        "totalLinesAdded": 120,
        "totalLinesRemoved": 40,
        "startTime": now_ms - days_ago * 86_400_000,
        "hasUnknownModelCost": unknown,
        "modelUsage": models,
    }


def assistant(session_id):
    return {"type": "assistant", "sessionId": session_id, "message": {"usage": {}}}


FIXTURES = {
    "-Users-demo-webapp": {
        "a1": [
            assistant("a1"),
            cost_state(
                "a1",
                184.20,
                2,
                {
                    "claude-opus-5-5": model(
                        inputTokens=420_000,
                        outputTokens=18_000,
                        cacheReadInputTokens=128_000_000,
                        cacheCreationInputTokens=900_000,
                        costUSD=184.20,
                    )
                },
            ),
        ],
        "a2": [
            cost_state(
                "a2",
                96.40,
                5,
                {
                    "claude-sonnet-5-5": model(
                        inputTokens=210_000,
                        outputTokens=9_000,
                        cacheReadInputTokens=64_000_000,
                        cacheCreationInputTokens=480_000,
                        costUSD=96.40,
                    )
                },
            )
        ],
        "a3": [
            cost_state(
                "a3",
                18.75,
                9,
                {
                    "claude-haiku-5-5": model(
                        inputTokens=180_000,
                        outputTokens=4_200,
                        cacheReadInputTokens=90_000,
                        cacheCreationInputTokens=210_000,
                        cacheCreationEphemeral1hInputTokens=210_000,
                        costUSD=18.75,
                    )
                },
            )
        ],
    },
    "-Users-demo-data": {
        "b1": [
            cost_state(
                "b1",
                42.10,
                1,
                {
                    "claude-sonnet-5-5": model(
                        inputTokens=90_000,
                        outputTokens=6_000,
                        cacheReadInputTokens=21_000_000,
                        cacheCreationInputTokens=320_000,
                        costUSD=42.10,
                    )
                },
            )
        ],
        "b2": [assistant("b2"), assistant("b2")],
    },
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="/tmp/spendtab-demo")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    target = os.path.abspath(os.path.expanduser(args.dir))
    if os.path.exists(target) and not args.force:
        if os.listdir(target):
            print(f"refusing to write into non-empty {target}; pass --force", file=sys.stderr)
            return 2
    os.makedirs(target, exist_ok=True)
    root = os.path.join(target, "projects")

    for project, sessions in FIXTURES.items():
        directory = os.path.join(root, project)
        os.makedirs(directory, exist_ok=True)
        for session_id, records in sessions.items():
            path = os.path.join(directory, session_id + ".jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")

    print(root)
    print()
    print("Run:")
    print(f"  spendtab --root {root} --no-cache --no-color")
    print(f"  spendtab --root {root} --no-cache --json")
    print(f"  spendtab --root {root} --no-cache --csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
