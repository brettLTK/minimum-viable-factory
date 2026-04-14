#!/usr/bin/env python3
"""Weekly convergence metric computation for PM agent self-improvement.

Reads delta files from memory/selection-deltas/ and computes:
  - override_rate_week_N: fraction of brett_override=true per calendar week
  - override_rate_by_type: override rate grouped by task_type
  - total_selections: count of all delta records

Writes output to memory/convergence-report.json.
Run on-demand or via cron.

Exit codes:
  0  — report generated successfully
  1  — error (non-fatal — report generation failure should not block pipeline)
"""

import sys
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required — pip install pyyaml", file=sys.stderr)
    sys.exit(1)

DELTA_DIR = Path("memory/selection-deltas")
REPORT_PATH = Path("memory/convergence-report.json")
ARCHIVE_DIR = DELTA_DIR / "archive"


def load_all_deltas() -> list[dict]:
    """Load all delta files (including archive) for convergence analysis."""
    records = []
    for search_dir in [DELTA_DIR, ARCHIVE_DIR]:
        if not search_dir.exists():
            continue
        for f in search_dir.glob("*.yaml"):
            try:
                record = yaml.safe_load(f.read_text(encoding="utf-8"))
                if isinstance(record, dict):
                    records.append(record)
            except Exception as e:
                print(f"WARNING: skipping {f} — {e}", file=sys.stderr)
    return records


def get_iso_week(ts_str: str) -> str:
    """Return ISO week string 'YYYY-WNN' from an ISO 8601 timestamp."""
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-W%W")
    except Exception:
        return "unknown"


def compute_report(records: list[dict]) -> dict:
    """Compute convergence metrics from delta records."""
    if not records:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_selections": 0,
            "override_rate_by_week": {},
            "override_rate_by_type": {},
            "note": "No delta records found — week 1 baseline (0 overrides is valid).",
        }

    # Group by week
    week_buckets: dict[str, dict] = defaultdict(lambda: {"total": 0, "overrides": 0})
    type_buckets: dict[str, dict] = defaultdict(lambda: {"total": 0, "overrides": 0})

    for record in records:
        ts = record.get("timestamp", "")
        week = get_iso_week(ts)
        task_type = record.get("task_type", "unknown")
        override = bool(record.get("brett_override", False))

        week_buckets[week]["total"] += 1
        type_buckets[task_type]["total"] += 1

        if override:
            week_buckets[week]["overrides"] += 1
            type_buckets[task_type]["overrides"] += 1

    override_rate_by_week = {
        week: {
            "total": v["total"],
            "overrides": v["overrides"],
            "override_rate": round(v["overrides"] / v["total"], 4) if v["total"] else 0.0,
        }
        for week, v in sorted(week_buckets.items())
    }

    override_rate_by_type = {
        task_type: {
            "total": v["total"],
            "overrides": v["overrides"],
            "override_rate": round(v["overrides"] / v["total"], 4) if v["total"] else 0.0,
        }
        for task_type, v in sorted(type_buckets.items())
    }

    total_overrides = sum(v["overrides"] for v in week_buckets.values())
    total_total = len(records)
    overall_override_rate = round(total_overrides / total_total, 4) if total_total else 0.0

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_selections": total_total,
        "total_overrides": total_overrides,
        "overall_override_rate": overall_override_rate,
        "override_rate_by_week": override_rate_by_week,
        "override_rate_by_type": override_rate_by_type,
        "alert": (
            "⚠️ Override rate ≥ 40% — PM agent policy review recommended."
            if overall_override_rate >= 0.40 else None
        ),
    }


def main() -> int:
    print(f"Loading delta records from {DELTA_DIR} ...")
    records = load_all_deltas()
    print(f"Loaded {len(records)} records.")

    report = compute_report(records)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report written to {REPORT_PATH}")

    if report.get("alert"):
        print(f"\n{report['alert']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
