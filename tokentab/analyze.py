"""Aggregate sessions into a spend report and rule-based findings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from .parse import Session


@dataclass
class Finding:
    code: str
    severity: str  # "warn" | "info"
    title: str
    advice: str
    session_id: Optional[str] = None
    project: Optional[str] = None
    evidence: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "advice": self.advice,
            "session_id": self.session_id,
            "project": self.project,
            "evidence": self.evidence,
        }


@dataclass
class ProjectSpend:
    project: str
    path: Optional[str]
    cost_usd: float
    sessions: int


@dataclass
class ModelSpend:
    model: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    sessions: int

    @property
    def billed_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @property
    def cache_hit_ratio(self) -> Optional[float]:
        billed = self.billed_input_tokens
        return self.cache_read_tokens / billed if billed else None


@dataclass
class Report:
    root: str
    generated_at: datetime
    window_start: Optional[datetime]
    window_end: Optional[datetime]
    sessions_total: int
    sessions_priced: int
    sessions_unpriced: int
    sessions_unknown_cost: int
    total_cost_usd: float
    by_project: List[ProjectSpend]
    by_model: List[ModelSpend]
    by_day: List[Dict[str, object]]
    top_sessions: List[Session]
    findings: List[Finding]
    run_rate_usd: Optional[float]
    malformed_lines: int

    def to_dict(self, top: int = 10) -> Dict[str, object]:
        return {
            "tool": "tokentab",
            "schema": 1,
            "root": self.root,
            "generated_at": self.generated_at.isoformat(),
            "window": {
                "start": self.window_start.isoformat() if self.window_start else None,
                "end": self.window_end.isoformat() if self.window_end else None,
            },
            "totals": {
                "sessions": self.sessions_total,
                "priced_sessions": self.sessions_priced,
                "unpriced_sessions": self.sessions_unpriced,
                "sessions_with_unknown_model_cost": self.sessions_unknown_cost,
                "cost_usd": round(self.total_cost_usd, 6),
                "run_rate_usd_30d": (
                    round(self.run_rate_usd, 2) if self.run_rate_usd is not None else None
                ),
                "malformed_lines": self.malformed_lines,
            },
            "by_project": [
                {
                    "project": p.project,
                    "path": p.path,
                    "cost_usd": round(p.cost_usd, 6),
                    "sessions": p.sessions,
                }
                for p in self.by_project
            ],
            "by_model": [
                {
                    "model": m.model,
                    "cost_usd": round(m.cost_usd, 6),
                    "sessions": m.sessions,
                    "input_tokens": m.input_tokens,
                    "output_tokens": m.output_tokens,
                    "cache_read_tokens": m.cache_read_tokens,
                    "cache_creation_tokens": m.cache_creation_tokens,
                    "cache_hit_ratio": (
                        round(m.cache_hit_ratio, 4) if m.cache_hit_ratio is not None else None
                    ),
                }
                for m in self.by_model
            ],
            "by_day": self.by_day,
            "top_sessions": [
                session_to_dict(s) for s in self.top_sessions[:top]
            ],
            "findings": [f.to_dict() for f in self.findings],
        }


def session_to_dict(session: Session) -> Dict[str, object]:
    return {
        "session_id": session.session_id,
        "project": session.project,
        "project_path": session.project_path,
        "date": session.day,
        "cost_usd": round(session.cost_usd, 6) if session.cost_usd is not None else None,
        "priced": session.priced,
        "models": sorted(session.models.keys()),
        "cache_hit_ratio": (
            round(session.token_usage.cache_hit_ratio, 4)
            if session.token_usage.cache_hit_ratio is not None
            else None
        ),
        "api_duration_ms": session.api_duration_ms,
        "snapshots": session.snapshot_count,
    }


def parse_since(value: Optional[str]) -> Optional[datetime]:
    """Parse ``7d``, ``36h`` or an ISO date into a UTC cutoff."""
    if not value:
        return None
    text = value.strip().lower()
    now = datetime.now(timezone.utc)
    try:
        if text.endswith("d"):
            return now - timedelta(days=float(text[:-1]))
        if text.endswith("h"):
            return now - timedelta(hours=float(text[:-1]))
        if text.endswith("w"):
            return now - timedelta(weeks=float(text[:-1]))
    except ValueError as exc:
        raise ValueError(f"invalid --since value: {value!r}") from exc
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"invalid --since value: {value!r} (use 7d, 36h, or YYYY-MM-DD)"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def filter_sessions(
    sessions: Sequence[Session],
    since: Optional[datetime] = None,
    project: Optional[str] = None,
) -> List[Session]:
    selected: List[Session] = []
    needle = project.lower() if project else None
    for session in sessions:
        if since is not None:
            if session.start_time is None or session.start_time < since:
                continue
        if needle is not None:
            haystack = " ".join(
                part
                for part in (session.project, session.project_path or "")
                if part
            ).lower()
            if needle not in haystack:
                continue
        selected.append(session)
    return selected


def build_report(
    sessions: Sequence[Session],
    root: str,
    since: Optional[datetime] = None,
    project: Optional[str] = None,
    top: int = 10,
    now: Optional[datetime] = None,
) -> Report:
    now = now or datetime.now(timezone.utc)
    selected = filter_sessions(sessions, since=since, project=project)

    priced = [s for s in selected if s.priced]
    unpriced = [s for s in selected if not s.priced]
    total_cost = sum(s.cost_usd or 0.0 for s in priced)

    projects: Dict[str, ProjectSpend] = {}
    for session in priced:
        entry = projects.get(session.project)
        if entry is None:
            entry = ProjectSpend(
                project=session.project,
                path=session.project_path,
                cost_usd=0.0,
                sessions=0,
            )
            projects[session.project] = entry
        entry.cost_usd += session.cost_usd or 0.0
        entry.sessions += 1
    by_project = sorted(projects.values(), key=lambda p: -p.cost_usd)

    models: Dict[str, ModelSpend] = {}
    for session in priced:
        for name, usage in session.models.items():
            entry = models.get(name)
            if entry is None:
                entry = ModelSpend(
                    model=name,
                    cost_usd=0.0,
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cache_creation_tokens=0,
                    sessions=0,
                )
                models[name] = entry
            entry.cost_usd += usage.cost_usd
            entry.input_tokens += usage.input_tokens
            entry.output_tokens += usage.output_tokens
            entry.cache_read_tokens += usage.cache_read_tokens
            entry.cache_creation_tokens += usage.cache_creation_tokens
            entry.sessions += 1
    by_model = sorted(models.values(), key=lambda m: -m.cost_usd)

    days: Dict[str, Dict[str, object]] = {}
    for session in priced:
        day = session.day or "unknown"
        entry = days.setdefault(day, {"date": day, "cost_usd": 0.0, "sessions": 0})
        entry["cost_usd"] = float(entry["cost_usd"]) + (session.cost_usd or 0.0)
        entry["sessions"] = int(entry["sessions"]) + 1
    by_day = [
        {
            "date": key,
            "cost_usd": round(float(value["cost_usd"]), 6),
            "sessions": value["sessions"],
        }
        for key, value in sorted(days.items(), key=lambda kv: kv[0])
    ]

    top_sessions = sorted(priced, key=lambda s: -(s.cost_usd or 0.0))[:top]

    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    dated = [s.start_time for s in selected if s.start_time]
    if dated:
        window_start = min(dated)
        window_end = max(dated)
    if since is not None:
        window_start = since
        window_end = now

    run_rate: Optional[float] = None
    if window_start and window_end and window_end > window_start:
        span_days = (window_end - window_start).total_seconds() / 86400.0
        if span_days >= 1.0:
            run_rate = total_cost / span_days * 30.0

    findings = diagnose(selected, priced, total_cost, by_model)

    unknown_count = sum(1 for s in priced if s.has_unknown_model_cost)
    malformed = sum(s.malformed_lines for s in selected)

    return Report(
        root=root,
        generated_at=now,
        window_start=window_start,
        window_end=window_end,
        sessions_total=len(selected),
        sessions_priced=len(priced),
        sessions_unpriced=len(unpriced),
        sessions_unknown_cost=unknown_count,
        total_cost_usd=total_cost,
        by_project=by_project,
        by_model=by_model,
        by_day=by_day,
        top_sessions=top_sessions,
        findings=findings,
        run_rate_usd=run_rate,
        malformed_lines=malformed,
    )


def diagnose(
    selected: Sequence[Session],
    priced: Sequence[Session],
    total_cost: float,
    by_model: Sequence[ModelSpend],
) -> List[Finding]:
    findings: List[Finding] = []

    for session in priced:
        usage = session.token_usage
        ratio = usage.cache_hit_ratio
        if (
            ratio is not None
            and ratio < 0.5
            and usage.billed_input_tokens >= 100_000
            and (session.cost_usd or 0.0) >= 1.0
        ):
            findings.append(
                Finding(
                    code="low_cache_hit",
                    severity="warn",
                    session_id=session.session_id,
                    project=session.project_path or session.project,
                    title=(
                        f"Cache hit only {ratio * 100:.0f}% on a "
                        f"${(session.cost_usd or 0.0):.2f} session"
                    ),
                    advice=(
                        "Most billed input is being re-sent cold. Keep the working "
                        "set stable (avoid churning files/instructions) so later "
                        "turns read from cache."
                    ),
                    evidence={
                        "cache_hit_ratio": round(ratio, 4),
                        "billed_input_tokens": usage.billed_input_tokens,
                        "cost_usd": round(session.cost_usd or 0.0, 4),
                    },
                )
            )

        created_1h = usage.cache_creation_1h_tokens
        reused = usage.cache_read_tokens
        if created_1h >= 100_000 and reused < created_1h:
            findings.append(
                Finding(
                    code="stale_1h_cache",
                    severity="info",
                    session_id=session.session_id,
                    project=session.project_path or session.project,
                    title=(
                        f"{created_1h:,} tokens written to 1h cache, "
                        f"only {reused:,} read back"
                    ),
                    advice=(
                        "1h cache writes cost more than 5m writes. If this session "
                        "never reuses that context, prefer the 5m TTL or trim the "
                        "cached prefix."
                    ),
                    evidence={
                        "cache_creation_1h_tokens": created_1h,
                        "cache_read_tokens": reused,
                        "cost_usd": round(session.cost_usd or 0.0, 4),
                    },
                )
            )

    if total_cost > 0 and priced:
        top = max(priced, key=lambda s: s.cost_usd or 0.0)
        share = (top.cost_usd or 0.0) / total_cost
        if total_cost >= 100.0 and share >= 0.4:
            findings.append(
                Finding(
                    code="cost_concentration",
                    severity="info",
                    session_id=top.session_id,
                    project=top.project_path or top.project,
                    title=(
                        f"One session is {share * 100:.0f}% of spend "
                        f"(${top.cost_usd or 0.0:,.2f})"
                    ),
                    advice=(
                        "Start long sessions fresh (/clear) when the task changes; "
                        "replaying a huge context is the main way single sessions "
                        "run away."
                    ),
                    evidence={
                        "session_cost_usd": round(top.cost_usd or 0.0, 4),
                        "total_cost_usd": round(total_cost, 4),
                        "share": round(share, 4),
                    },
                )
            )

    unknown = [s for s in priced if s.has_unknown_model_cost]
    if unknown:
        findings.append(
            Finding(
                code="unknown_model_cost",
                severity="warn",
                title=f"{len(unknown)} session(s) include models with unknown pricing",
                advice=(
                    "Costs for those models are missing or understated. Treat the "
                    "total as a lower bound."
                ),
                evidence={"sessions": len(unknown)},
            )
        )

    unpriced = [s for s in selected if not s.priced]
    if unpriced:
        findings.append(
            Finding(
                code="unpriced_sessions",
                severity="info",
                title=f"{len(unpriced)} session(s) have no cost snapshot",
                advice=(
                    "Older transcripts do not record cost state. They are excluded "
                    "from totals; token volume is still visible per model when present."
                ),
                evidence={"sessions": len(unpriced)},
            )
        )

    for entry in by_model:
        ratio = entry.cache_hit_ratio
        if (
            ratio is not None
            and ratio < 0.3
            and entry.billed_input_tokens >= 1_000_000
            and entry.cost_usd >= 5.0
        ):
            findings.append(
                Finding(
                    code="model_low_cache_hit",
                    severity="info",
                    title=(
                        f"{entry.model}: {ratio * 100:.0f}% cache hit over "
                        f"{entry.billed_input_tokens:,} billed input tokens"
                    ),
                    advice=(
                        "Sessions on this model rarely reuse context. Batch related "
                        "work in one session or shorten the prelude."
                    ),
                    evidence={
                        "model": entry.model,
                        "cache_hit_ratio": round(ratio, 4),
                        "billed_input_tokens": entry.billed_input_tokens,
                        "cost_usd": round(entry.cost_usd, 4),
                    },
                )
            )

    order = {"warn": 0, "info": 1}
    findings.sort(key=lambda f: (order.get(f.severity, 9), f.code))
    return findings
