#!/usr/bin/env python3
"""90-day delta archival script.

Moves SelectionDelta YAML files older than 90 days from
memory/selection-deltas/ to memory/selection-deltas/archive/.

Files are moved (not deleted) and archive directory is set to 700 permissions.
Archive files are set to 600 permissions (owner-only, restricted access).

Exit codes:
  0  — completed (zero or more files archived)
  1  — error
"""

import sys
import os
import stat
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required — pip install pyyaml", file=sys.stderr)
    sys.exit(1)

DELTA_DIR = Path("memory/selection-deltas")
ARCHIVE_DIR = DELTA_DIR / "archive"
RETENTION_DAYS = 90


def archive_old_deltas(dry_run: bool = False) -> int:
    """Archive delta files older than RETENTION_DAYS. Returns count of archived files."""
    if not DELTA_DIR.exists():
        print(f"INFO: Delta directory does not exist: {DELTA_DIR} — nothing to archive.")
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    files = list(DELTA_DIR.glob("*.yaml"))

    to_archive = []
    for f in files:
        try:
            record = yaml.safe_load(f.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                continue
            raw_ts = record.get("timestamp")
            if not raw_ts:
                # Fall back to file mtime
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    to_archive.append(f)
            else:
                ts = datetime.fromisoformat(str(raw_ts))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    to_archive.append(f)
        except Exception as e:
            print(f"WARNING: skipping {f} — {e}", file=sys.stderr)

    if not to_archive:
        print(f"INFO: No delta files older than {RETENTION_DAYS} days found.")
        return 0

    if dry_run:
        print(f"DRY RUN: would archive {len(to_archive)} file(s):")
        for f in to_archive:
            print(f"  {f}")
        return len(to_archive)

    # Create archive directory with restricted permissions
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(ARCHIVE_DIR, stat.S_IRWXU)  # 700

    archived = 0
    for f in to_archive:
        dest = ARCHIVE_DIR / f.name
        try:
            f.rename(dest)
            os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)  # 600
            print(f"ARCHIVED: {f.name}")
            archived += 1
        except Exception as e:
            print(f"ERROR: failed to archive {f}: {e}", file=sys.stderr)

    print(f"\nArchived {archived}/{len(to_archive)} delta files to {ARCHIVE_DIR}")
    return archived


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("Running in dry-run mode — no files will be moved.")

    try:
        count = archive_old_deltas(dry_run=dry_run)
        return 0
    except Exception as e:
        print(f"ERROR: archival failed — {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
