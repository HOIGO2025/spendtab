"""Read Claude Code transcript files and extract per-session cost snapshots.

Only metadata is read. Message bodies are never stored or emitted.
"""

from __future__ import annotations

import glob
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

DEFAULT_ROOT = "~/.claude/projects"
CACHE_VERSION = 2
FINGERPRINT_BYTES = 4096


@dataclass
class ModelUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_creation_5m_tokens: int = 0
    cache_creation_1h_tokens: int = 0
    web_search_requests: int = 0
    cost_usd: float = 0.0

    @classmethod
    def from_dict(cls, model: str, raw: Dict) -> "ModelUsage":
        raw = raw or {}
        return cls(
            model=model,
            input_tokens=_integer(raw.get("inputTokens", 0)),
            output_tokens=_integer(raw.get("outputTokens", 0)),
            thinking_tokens=_integer(raw.get("thinkingTokens", 0)),
            cache_read_tokens=_integer(raw.get("cacheReadInputTokens", 0)),
            cache_creation_tokens=_integer(raw.get("cacheCreationInputTokens", 0)),
            cache_creation_5m_tokens=_integer(
                raw.get("cacheCreationEphemeral5mInputTokens", 0)
            ),
            cache_creation_1h_tokens=_integer(
                raw.get("cacheCreationEphemeral1hInputTokens", 0)
            ),
            web_search_requests=_integer(raw.get("webSearchRequests", 0)),
            cost_usd=_money(raw.get("costUSD", 0.0)),
        )

    @property
    def billed_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @property
    def cache_hit_ratio(self) -> Optional[float]:
        """Share of billed input tokens served from cache."""
        billed = self.billed_input_tokens
        if billed <= 0:
            return None
        return self.cache_read_tokens / billed


@dataclass
class Session:
    session_id: str
    path: str
    project: str
    project_path: Optional[str]
    cost_usd: Optional[float] = None
    models: Dict[str, ModelUsage] = field(default_factory=dict)
    start_time: Optional[datetime] = None
    total_duration_ms: int = 0
    api_duration_ms: int = 0
    tool_duration_ms: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    has_unknown_model_cost: bool = False
    snapshot_count: int = 0
    assistant_messages: int = 0
    malformed_lines: int = 0

    @property
    def priced(self) -> bool:
        return self.cost_usd is not None

    @property
    def day(self) -> Optional[str]:
        return self.start_time.date().isoformat() if self.start_time else None

    @property
    def token_usage(self) -> "ModelUsage":
        """Aggregate usage across models (for ratio math only)."""
        total = ModelUsage(model="__all__")
        for usage in self.models.values():
            total.input_tokens += usage.input_tokens
            total.output_tokens += usage.output_tokens
            total.thinking_tokens += usage.thinking_tokens
            total.cache_read_tokens += usage.cache_read_tokens
            total.cache_creation_tokens += usage.cache_creation_tokens
            total.cache_creation_5m_tokens += usage.cache_creation_5m_tokens
            total.cache_creation_1h_tokens += usage.cache_creation_1h_tokens
            total.web_search_requests += usage.web_search_requests
            total.cost_usd += usage.cost_usd
        return total

    def to_cache_dict(self) -> Dict[str, object]:
        return {
            "session_id": self.session_id,
            "path": self.path,
            "project": self.project,
            "project_path": self.project_path,
            "cost_usd": self.cost_usd,
            "models": {
                name: {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "thinking_tokens": usage.thinking_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_creation_tokens": usage.cache_creation_tokens,
                    "cache_creation_5m_tokens": usage.cache_creation_5m_tokens,
                    "cache_creation_1h_tokens": usage.cache_creation_1h_tokens,
                    "web_search_requests": usage.web_search_requests,
                    "cost_usd": usage.cost_usd,
                }
                for name, usage in self.models.items()
            },
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "total_duration_ms": self.total_duration_ms,
            "api_duration_ms": self.api_duration_ms,
            "tool_duration_ms": self.tool_duration_ms,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "has_unknown_model_cost": self.has_unknown_model_cost,
            "snapshot_count": self.snapshot_count,
            "assistant_messages": self.assistant_messages,
            "malformed_lines": self.malformed_lines,
        }

    @classmethod
    def from_cache_dict(cls, raw: Dict) -> "Session":
        if not isinstance(raw, dict):
            raise ValueError("cache entry is not an object")
        models = {
            name: ModelUsage(
                model=name,
                input_tokens=int(values.get("input_tokens", 0) or 0),
                output_tokens=int(values.get("output_tokens", 0) or 0),
                thinking_tokens=int(values.get("thinking_tokens", 0) or 0),
                cache_read_tokens=int(values.get("cache_read_tokens", 0) or 0),
                cache_creation_tokens=int(values.get("cache_creation_tokens", 0) or 0),
                cache_creation_5m_tokens=int(
                    values.get("cache_creation_5m_tokens", 0) or 0
                ),
                cache_creation_1h_tokens=int(
                    values.get("cache_creation_1h_tokens", 0) or 0
                ),
                web_search_requests=int(values.get("web_search_requests", 0) or 0),
                cost_usd=float(values.get("cost_usd", 0.0) or 0.0),
            )
            for name, values in (raw.get("models") or {}).items()
        }
        return cls(
            session_id=str(raw.get("session_id", "")),
            path=str(raw.get("path", "")),
            project=str(raw.get("project", "")),
            project_path=raw.get("project_path"),
            cost_usd=(
                float(raw["cost_usd"]) if raw.get("cost_usd") is not None else None
            ),
            models=models,
            start_time=_parse_iso(raw.get("start_time")),
            total_duration_ms=int(raw.get("total_duration_ms", 0) or 0),
            api_duration_ms=int(raw.get("api_duration_ms", 0) or 0),
            tool_duration_ms=int(raw.get("tool_duration_ms", 0) or 0),
            lines_added=int(raw.get("lines_added", 0) or 0),
            lines_removed=int(raw.get("lines_removed", 0) or 0),
            has_unknown_model_cost=bool(raw.get("has_unknown_model_cost", False)),
            snapshot_count=int(raw.get("snapshot_count", 0) or 0),
            assistant_messages=int(raw.get("assistant_messages", 0) or 0),
            malformed_lines=int(raw.get("malformed_lines", 0) or 0),
        )


def decode_project_dir(name: str) -> Optional[str]:
    """Decode Claude Code's flattened project directory name.

    ``-Users-demo-app`` -> ``/Users/demo/app``.
    Returns None for names that do not look like a flattened absolute path.
    """
    if not name.startswith("-") or len(name) < 2:
        return None
    return "/" + name[1:].replace("-", "/")


def _integer(value) -> int:
    number = int(value or 0)
    if number < 0:
        raise ValueError("negative token count")
    return number


def _money(value) -> float:
    number = float(value or 0.0)
    if not math.isfinite(number) or number < 0:
        raise ValueError("non-finite or negative cost")
    return number


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _extract_usage_tokens(usage: Dict) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not usage:
        return out
    for target, source in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_read_tokens", "cache_read_input_tokens"),
        ("cache_creation_tokens", "cache_creation_input_tokens"),
    ):
        try:
            out[target] = out.get(target, 0) + int(usage.get(source, 0) or 0)
        except (TypeError, ValueError):
            pass
    return out


def transcript_files(root: str) -> List[str]:
    root = os.path.abspath(os.path.expanduser(root))
    pattern = os.path.join(root, "**", "*.jsonl")
    return sorted(glob.glob(pattern, recursive=True))


def load_sessions(
    root: str = DEFAULT_ROOT,
    cache_path: Optional[str] = None,
    use_cache: bool = True,
) -> List[Session]:
    """Load one Session per (transcript file, sessionId) with final cost snapshot.

    Parsed summaries are cached by transcript mtime and size, so repeat scans
    only re-read files that changed.
    """
    root = os.path.abspath(os.path.expanduser(root))
    files = transcript_files(root)
    cached = _read_cache(cache_path) if use_cache else {}
    sessions: List[Session] = []
    fresh: Dict[str, Dict[str, object]] = {}
    changed = False

    for path in files:
        key = os.path.realpath(path)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entry = cached.get(key)
        fingerprint = None
        if (
            entry
            and entry.get("mtime_ns") == stat.st_mtime_ns
            and entry.get("size") == stat.st_size
            and isinstance(entry.get("sessions"), list)
        ):
            fingerprint = _fingerprint(path)
            if entry.get("fingerprint") != fingerprint:
                entry = None
        if entry and fingerprint is not None:
            try:
                if not all(
                    isinstance(item, dict) for item in entry.get("sessions") or []
                ):
                    raise ValueError("bad cached session")
                restored = [Session.from_cache_dict(item) for item in entry["sessions"]]
            except (TypeError, ValueError, KeyError, AttributeError):
                restored = None
            if restored is not None and _unchanged(path, stat):
                sessions.extend(restored)
                fresh[key] = entry
                continue
        fingerprint = fingerprint or _fingerprint(path)
        parsed = _load_file(path)
        sessions.extend(parsed)
        changed = True
        after = _fingerprint(path)
        if _unchanged(path, stat) and fingerprint is not None and after == fingerprint:
            fresh[key] = {
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "fingerprint": fingerprint,
                "sessions": [session.to_cache_dict() for session in parsed],
            }

    if use_cache and (changed or set(fresh) != set(cached)):
        _write_cache(cache_path, fresh)
    return _dedupe_sessions(sessions)


def default_cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "tokentab", "index.json")


def _unchanged(path: str, stat) -> bool:
    try:
        current = os.stat(path)
    except OSError:
        return False
    return (
        current.st_mtime_ns == stat.st_mtime_ns and current.st_size == stat.st_size
    )


def _fingerprint(path: str) -> Optional[str]:
    """Cheap content check: head and tail bytes of the transcript.

    mtime and size alone can be preserved across a rewrite (cp -p, rsync -t,
    os.utime), which would serve stale costs. Reading 8 KB per file catches that
    without paying to hash hundreds of megabytes on every scan.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            head = handle.read(FINGERPRINT_BYTES)
            if size > FINGERPRINT_BYTES:
                handle.seek(max(FINGERPRINT_BYTES, size - FINGERPRINT_BYTES))
                tail = handle.read(FINGERPRINT_BYTES)
            else:
                tail = b""
    except OSError:
        return None
    digest = hashlib.sha256()
    digest.update(head)
    digest.update(tail)
    return digest.hexdigest()


def _dedupe_sessions(sessions: List[Session]) -> List[Session]:
    """Keep one Session per session_id.

    Copies, backups, and resumed transcripts can repeat a session id; counting
    both would double the money. Keep the entry with the most snapshots, then
    the higher cost.
    """
    best: Dict[str, Session] = {}
    passthrough: List[Session] = []
    for session in sessions:
        if not session.session_id:
            passthrough.append(session)
            continue
        current = best.get(session.session_id)
        if current is None or _session_rank(session) > _session_rank(current):
            best[session.session_id] = session
    return list(best.values()) + passthrough


def _session_rank(session: Session):
    return (
        session.snapshot_count,
        session.cost_usd if session.cost_usd is not None else -1.0,
        session.start_time.timestamp() if session.start_time else 0.0,
    )


def _read_cache(cache_path: Optional[str]) -> Dict[str, Dict[str, object]]:
    path = cache_path or default_cache_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return {}
    files = payload.get("files")
    return files if isinstance(files, dict) else {}


def _write_cache(cache_path: Optional[str], files: Dict[str, Dict[str, object]]) -> None:
    path = cache_path or default_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"version": CACHE_VERSION, "files": files}, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass


def _load_file(path: str) -> List[Session]:
    project_dir = os.path.basename(os.path.dirname(path))
    by_session: Dict[str, Session] = {}
    unpriced: Dict[str, Session] = {}
    malformed = 0

    try:
        handle = open(path, "r", encoding="utf-8", errors="ignore")
    except OSError:
        return []

    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{") or not line.endswith("}"):
                malformed += 1
                continue
            # Transcripts are majority metadata. Only cost snapshots and
            # assistant usage matter here, so skip JSON decoding for the rest.
            if (
                '"cost-state"' not in line
                and not ('"type"' in line and '"assistant"' in line)
            ):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            record_type = record.get("type")
            session_id = record.get("sessionId") or os.path.basename(path).removesuffix(".jsonl")

            if record_type == "cost-state":
                session = by_session.get(session_id) or unpriced.pop(session_id, None)
                if session is None:
                    session = Session(
                        session_id=session_id,
                        path=path,
                        project=project_dir,
                        project_path=decode_project_dir(project_dir),
                    )
                by_session[session_id] = session
                try:
                    _apply_cost_state(session, record)
                except (TypeError, ValueError, AttributeError, KeyError, OverflowError):
                    malformed += 1
            elif record_type == "assistant":
                session = by_session.get(session_id)
                if session is None:
                    session = unpriced.get(session_id)
                if session is None:
                    session = Session(
                        session_id=session_id,
                        path=path,
                        project=project_dir,
                        project_path=decode_project_dir(project_dir),
                    )
                    unpriced[session_id] = session
                session.assistant_messages += 1

    result = list(by_session.values())
    for sid, session in unpriced.items():
        if sid in by_session:
            continue
        result.append(session)
    if result:
        result[0].malformed_lines += malformed
    return result


def _apply_cost_state(session: Session, record: Dict) -> None:
    cost_usd = _money(record.get("totalCostUSD", 0.0))
    total_duration = _integer(record.get("totalDuration", 0))
    api_duration = _integer(record.get("totalAPIDuration", 0))
    tool_duration = _integer(record.get("totalToolDuration", 0))
    lines_added = _integer(record.get("totalLinesAdded", 0))
    lines_removed = _integer(record.get("totalLinesRemoved", 0))
    has_unknown = bool(record.get("hasUnknownModelCost", False))

    start_time = session.start_time
    start_ms = record.get("startTime")
    if start_ms:
        try:
            start_time = datetime.fromtimestamp(
                _money(start_ms) / 1000.0, tz=timezone.utc
            )
        except (TypeError, ValueError, OSError):
            pass
    if start_time is None:
        start_time = _parse_iso(record.get("timestamp"))

    raw_models = record.get("modelUsage") or {}
    if not isinstance(raw_models, dict):
        raise ValueError("modelUsage is not an object")
    models: Dict[str, ModelUsage] = {}
    for name, raw in raw_models.items():
        if not isinstance(raw, dict):
            raise ValueError("model usage entry is not an object")
        models[str(name)] = ModelUsage.from_dict(str(name), raw)

    session.cost_usd = cost_usd
    session.snapshot_count += 1
    session.has_unknown_model_cost = has_unknown
    session.total_duration_ms = total_duration
    session.api_duration_ms = api_duration
    session.tool_duration_ms = tool_duration
    session.lines_added = lines_added
    session.lines_removed = lines_removed
    session.start_time = start_time
    session.models = models
