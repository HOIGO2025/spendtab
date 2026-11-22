"""Command-line entry point: ``spendtab``."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from typing import List, Optional

from . import __version__
from .analyze import build_report, filter_sessions, parse_since
from .guard import read_events
from .hook import guard_check, guard_init, handle_pretooluse
from .parse import DEFAULT_ROOT, load_sessions
from .report import render
from .team import build_team_report, render_team


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spendtab",
        description=(
            "Audit Claude Code spend and enforce budgets. "
            "Run `spendtab guard init` to set up the PreToolUse hook."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["scan"],
        default="scan",
        help="scan (default) reads local transcripts and prints a report",
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT,
        help="transcript root (default: ~/.claude/projects)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="only sessions starting after this point: 7d, 36h, 4w, or YYYY-MM-DD",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="substring match on project name or decoded path",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="number of top sessions to list (default: 10)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--csv",
        action="store_true",
        help="emit one row per session (invoice/export friendly)",
    )
    parser.add_argument(
        "--fail-over",
        type=float,
        default=None,
        metavar="USD",
        help="exit with status 3 when total spend exceeds USD",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="disable ANSI colors"
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="re-parse every transcript instead of using the local mtime cache",
    )
    parser.add_argument(
        "--version", action="version", version=f"spendtab {__version__}"
    )
    return parser


def _dispatch_hook(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="spendtab hook",
        description="Claude Code hook entry points.",
    )
    parser.add_argument("event", choices=["pretooluse"])
    parser.add_argument("--config", default=None, help="guard config path")
    parser.add_argument("--ledger", default=None, help="ledger path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the decision without writing the ledger or blocking",
    )
    args = parser.parse_args(argv)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        print("spendtab hook: invalid JSON on stdin; allowing call", file=sys.stderr)
        return 0
    if not isinstance(payload, dict):
        return 0
    return handle_pretooluse(
        payload,
        config_file=args.config,
        ledger_file=args.ledger,
        dry_run=args.dry_run,
    )


def _dispatch_guard(argv: List[str]) -> int:
    if argv and argv[0] == "report":
        parser = argparse.ArgumentParser(
            prog="spendtab guard report",
            description="Spend from the guard ledger, by developer, project, and day.",
        )
        parser.add_argument("--days", type=int, default=7, help="window length (default 7)")
        parser.add_argument("--ledger", default=None, help="ledger path")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--csv", action="store_true", help="grouped export")
        args = parser.parse_args(argv[1:])
        if args.days < 1:
            parser.error("--days must be >= 1")
        if args.json and args.csv:
            parser.error("--json and --csv are mutually exclusive")
        report = build_team_report(read_events(args.ledger), days=args.days)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        elif args.csv:
            writer = csv.writer(sys.stdout, lineterminator="\n")
            writer.writerow(["dimension", "name", "cost_usd", "sessions", "events"])
            for dimension, groups in (
                ("dev", report.by_dev),
                ("project", report.by_project),
                ("day", report.by_day),
            ):
                for group in groups:
                    writer.writerow(
                        [
                            dimension,
                            group.name,
                            f"{group.cost_usd:.6f}",
                            group.sessions,
                            group.events,
                        ]
                    )
        else:
            sys.stdout.write(render_team(report))
        return 0

    if argv and argv[0] == "init":
        parser = argparse.ArgumentParser(
            prog="spendtab guard init",
            description="Write a starter guard config and print the hook snippet.",
        )
        parser.add_argument("--config", default=None, help="config path to write")
        parser.add_argument("--force", action="store_true", help="overwrite")
        args = parser.parse_args(argv[1:])
        return guard_init(args.config, force=args.force)

    parser = argparse.ArgumentParser(
        prog="spendtab guard",
        description="Check current spend against guard budgets.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["check"],
        default="check",
    )
    parser.add_argument("--transcript", default=None, help="session transcript path")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--project", default=None, help="override project path")
    parser.add_argument("--config", default=None)
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    session_id = args.session_id or (
        os.path.basename(args.transcript).removesuffix(".jsonl")
        if args.transcript
        else ""
    )
    return guard_check(
        transcript=args.transcript,
        session_id=session_id,
        project=args.project or os.getcwd(),
        config_file=args.config,
        ledger_file=args.ledger,
        as_json=args.json,
    )


def _emit_csv(sessions, since, project) -> float:
    selected = filter_sessions(sessions, since=since, project=project)
    selected.sort(key=lambda s: -(s.cost_usd or 0.0))
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(
        [
            "date",
            "project_path",
            "session_id",
            "models",
            "priced",
            "cost_usd",
            "cache_hit_ratio",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "api_duration_ms",
        ]
    )
    total = 0.0
    for session in selected:
        usage = session.token_usage
        ratio = usage.cache_hit_ratio
        total += session.cost_usd or 0.0
        writer.writerow(
            [
                session.day or "",
                session.project_path or session.project,
                session.session_id,
                ";".join(sorted(session.models)),
                "yes" if session.priced else "no",
                f"{session.cost_usd:.6f}" if session.cost_usd is not None else "",
                f"{ratio:.4f}" if ratio is not None else "",
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_creation_tokens,
                session.api_duration_ms,
            ]
        )
    return total


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if raw_argv and raw_argv[0] == "hook":
        return _dispatch_hook(raw_argv[1:])
    if raw_argv and raw_argv[0] == "guard":
        return _dispatch_guard(raw_argv[1:])

    parser = build_parser()
    args = parser.parse_args(raw_argv)

    root = os.path.abspath(os.path.expanduser(args.root))
    if not os.path.isdir(root):
        print(f"spendtab: transcript root not found: {root}", file=sys.stderr)
        return 2

    try:
        since = parse_since(args.since)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover

    if args.top < 1:
        parser.error("--top must be >= 1")
    if args.json and args.csv:
        parser.error("--json and --csv are mutually exclusive")

    sessions = load_sessions(
        root,
        use_cache=not args.no_cache
        and os.environ.get("SPENDTAB_NO_CACHE", "").lower() not in ("1", "true", "yes"),
    )

    if args.csv:
        total = _emit_csv(sessions, since, args.project)
        if args.fail_over is not None and total > args.fail_over:
            print(
                f"spendtab: spend {total:.2f} exceeds --fail-over "
                f"{args.fail_over:.2f}",
                file=sys.stderr,
            )
            return 3
        return 0
    report = build_report(
        sessions,
        root=root,
        since=since,
        project=args.project,
        top=args.top,
        now=datetime.now(timezone.utc),
    )

    if args.json:
        json.dump(report.to_dict(top=args.top), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        color = False if args.no_color else None
        sys.stdout.write(render(report, top=args.top, color=color))

    if args.fail_over is not None and report.total_cost_usd > args.fail_over:
        print(
            f"spendtab: spend {report.total_cost_usd:.2f} exceeds --fail-over "
            f"{args.fail_over:.2f}",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
