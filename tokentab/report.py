"""Human-readable terminal output."""

from __future__ import annotations

import os
import sys
from typing import Optional

from .analyze import Report

BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RESET = "\033[0m"


def _paint(text: str, color: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{color}{text}{RESET}"


def money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 1000:
        return f"${value:,.0f}"
    return f"${value:,.2f}"


def _short_project(path: Optional[str], fallback: str) -> str:
    if not path:
        return fallback
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def _bar(share: float, width: int = 10) -> str:
    filled = max(0, min(width, round(share * width)))
    return "█" * filled + "░" * (width - filled)


def render(report: Report, top: int = 10, color: Optional[bool] = None, out=None) -> str:
    stream = out or sys.stdout
    if color is None:
        color = stream.isatty() and os.environ.get("NO_COLOR") is None

    lines = []
    lines.append(_paint("TokenTab — Claude Code spend audit", BOLD, color))
    lines.append("")
    lines.append(f"root      {report.root}")
    lines.append(
        f"sessions  {report.sessions_total} total · "
        f"{report.sessions_priced} priced · "
        f"{report.sessions_unpriced} unpriced"
    )
    if report.window_start and report.window_end:
        lines.append(
            f"window    {report.window_start.date().isoformat()} → "
            f"{report.window_end.date().isoformat()}"
        )
    lines.append(
        f"total     {_paint(money(report.total_cost_usd), BOLD, color)}"
        + (
            "  (lower bound: unknown model pricing)"
            if report.sessions_unknown_cost
            else ""
        )
    )
    if report.run_rate_usd is not None:
        lines.append(
            f"run-rate  {money(report.run_rate_usd)}/30d  "
            f"{_paint('(extrapolated)', DIM, color)}"
        )
    lines.append("")

    if report.by_project:
        lines.append(_paint("By project", BOLD, color))
        top_cost = report.by_project[0].cost_usd or 1.0
        for entry in report.by_project:
            name = _short_project(entry.path, entry.project)
            share = entry.cost_usd / top_cost
            lines.append(
                f"  {money(entry.cost_usd):>10}  {_bar(share)}  "
                f"{name}  {_paint(f'{entry.sessions} session(s)', DIM, color)}"
            )
        lines.append("")

    if report.by_model:
        lines.append(_paint("By model", BOLD, color))
        for entry in report.by_model:
            ratio = entry.cache_hit_ratio
            cache_text = f"cache {ratio * 100:.0f}%" if ratio is not None else "cache n/a"
            lines.append(
                f"  {money(entry.cost_usd):>10}  {entry.model:<28} "
                f"{entry.billed_input_tokens:>12,} in · "
                f"{entry.output_tokens:>10,} out · {cache_text}"
            )
        lines.append("")

    if report.top_sessions:
        lines.append(_paint(f"Top {len(report.top_sessions)} sessions", BOLD, color))
        for session in report.top_sessions:
            models = ", ".join(sorted(session.models)) or "unknown"
            lines.append(
                f"  {money(session.cost_usd):>10}  {session.day or 'unknown':<10}  "
                f"{_short_project(session.project_path, session.project)[:34]:<34}  "
                f"{models}"
            )
        lines.append("")

    if report.findings:
        lines.append(_paint("Findings", BOLD, color))
        for finding in report.findings:
            label = finding.severity.upper().ljust(4)
            painted = _paint(
                label,
                RED if finding.severity == "warn" else YELLOW,
                color,
            )
            lines.append(f"  {painted}  [{finding.code}] {finding.title}")
            lines.append(f"        {finding.advice}")
        lines.append("")
    else:
        lines.append(_paint("No spend anomalies detected.", GREEN, color))
        lines.append("")

    if report.sessions_unpriced:
        lines.append(
            _paint(
                f"Excluded {report.sessions_unpriced} session(s) without cost snapshots.",
                DIM,
                color,
            )
        )
    if report.malformed_lines:
        lines.append(
            _paint(
                f"Skipped {report.malformed_lines} malformed transcript line(s).",
                DIM,
                color,
            )
        )

    return "\n".join(lines).rstrip() + "\n"
