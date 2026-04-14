"""Delta I/O helpers for PM agent self-improvement loop.

Handles:
- write_selection_delta(): atomic YAML write of SelectionDelta records
- load_bounded_deltas(): 50-event OR 30-day rolling window, both caps hard
- sanitize_for_langsmith(): strips CISO-excluded fields before any LangSmith write
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

from orchestrator.config import DELTA_DIR, logger
from orchestrator.audit import audit_log

# ---------------------------------------------------------------------------
# LangSmith field exclusion policy (CISO F4 — metadata-only)
# ---------------------------------------------------------------------------

LANGSMITH_EXCLUDED_FIELDS = frozenset({
    "pm_agent_rationale",
    "task_requirements",
    "competitive_analysis",
    "delta_notes",
})


def sanitize_for_langsmith(record: dict) -> dict:
    """Remove sensitive fields before sending to external LangSmith service.

    The 4 excluded fields must NEVER appear in external traces. Call this
    before every LangSmith API write — no exception path bypasses it.
    """
    return {k: v for k, v in record.items() if k not in LANGSMITH_EXCLUDED_FIELDS}


# ---------------------------------------------------------------------------
# Delta write (atomic)
# ---------------------------------------------------------------------------


def write_selection_delta(record: dict) -> Path:
    """Write a SelectionDelta record atomically. Returns file path.

    Atomic write: write to .tmp, rename to final path.
    Raises on failure — caller must handle (do not silently drop delta records).

    Expected record fields (SelectionDelta v1.0 schema):
      type, schema_version, task_id, prototypes_generated, pm_agent_top_pick,
      pm_agent_rationale, brett_selection, brett_override, delta_notes,
      timestamp, task_type
    """
    delta_dir = Path(DELTA_DIR)
    delta_dir.mkdir(parents=True, exist_ok=True)

    ts_unix = int(datetime.now(timezone.utc).timestamp())
    task_id = record.get("task_id", "unknown")
    filename = f"{task_id}-{ts_unix}.yaml"
    tmp_path = delta_dir / f".{filename}.tmp"
    final_path = delta_dir / filename

    tmp_path.write_text(
        yaml.dump(record, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp_path.rename(final_path)

    audit_log(task_id, "delta_written", str(final_path))
    logger.info("SelectionDelta written: %s", final_path)
    return final_path


# ---------------------------------------------------------------------------
# Delta load (bounded window)
# ---------------------------------------------------------------------------


def load_bounded_deltas(n: int = 50, days: int = 30) -> list[dict]:
    """Load at most n most-recent delta records within the days cutoff.

    Reads DELTA_DIR sorted by mtime descending. Stops when count reaches n
    OR when record timestamp is older than now - days. Whichever comes first.
    Both caps are hard — there is no override path.

    Returns an empty list if DELTA_DIR does not exist or contains no records.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    delta_dir = Path(DELTA_DIR)

    if not delta_dir.exists():
        logger.info("load_bounded_deltas: DELTA_DIR does not exist yet — returning empty list")
        return []

    files = sorted(delta_dir.glob("*.yaml"), key=lambda f: f.stat().st_mtime, reverse=True)

    results: list[dict] = []
    for f in files:
        if len(results) >= n:
            break
        try:
            record = yaml.safe_load(f.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                logger.warning("load_bounded_deltas: skipping non-dict record in %s", f)
                continue
            raw_ts = record.get("timestamp")
            if raw_ts:
                ts = datetime.fromisoformat(str(raw_ts))
                # Ensure timezone-aware for comparison
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    break  # Sorted by mtime; all subsequent files are older
            results.append(record)
        except Exception as exc:
            logger.warning("load_bounded_deltas: failed to parse %s — %s", f, exc)

    logger.info("load_bounded_deltas: returned %d records (n=%d, days=%d)", len(results), n, days)
    return results
