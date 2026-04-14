#!/usr/bin/env python3
"""Tier 1 frontmatter compliance checker.

Validates that all Markdown artifact files in a prototype workspace contain
required frontmatter fields (type, title, date, status).

Exit codes:
  0  — all files pass (or no Markdown files found)
  1  — one or more files fail frontmatter validation
"""

import sys
import re
from pathlib import Path

REQUIRED_FIELDS = {"type", "title", "date", "status"}

VALID_TYPES = {
    "DocumentArtifact", "ImplementationSpec", "DesignDoc", "SessionRollup",
    "PersonRecord", "SelectionDelta", "IssueSpec", "RunbookEntry",
}

VALID_STATUSES = {"draft", "active", "archived", "review", "done", "deprecated"}


def parse_frontmatter(text: str) -> dict | None:
    """Extract YAML frontmatter from Markdown. Returns None if not present."""
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return None
    import yaml
    try:
        return yaml.safe_load(match.group(1)) or {}
    except Exception:
        return None


def check_file(path: Path) -> list[str]:
    """Return a list of violation strings for a file. Empty = pass."""
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(text)

    violations = []
    if fm is None:
        violations.append(f"{path}: missing frontmatter block (--- ... ---)")
        return violations

    missing = REQUIRED_FIELDS - set(fm.keys())
    if missing:
        violations.append(f"{path}: missing required fields: {sorted(missing)}")

    if "type" in fm and fm["type"] not in VALID_TYPES:
        violations.append(f"{path}: unrecognized type '{fm['type']}' (known: {sorted(VALID_TYPES)})")

    if "status" in fm and fm["status"] not in VALID_STATUSES:
        violations.append(f"{path}: unrecognized status '{fm['status']}' (known: {sorted(VALID_STATUSES)})")

    return violations


def main(workspace: str) -> int:
    root = Path(workspace)
    if not root.exists():
        print(f"ERROR: workspace path does not exist: {workspace}", file=sys.stderr)
        return 1

    md_files = list(root.rglob("*.md"))
    if not md_files:
        print(f"PASS: no Markdown files found in {workspace}")
        return 0

    all_violations = []
    for f in md_files:
        # Skip node_modules, __pycache__, .git
        if any(part in f.parts for part in ("node_modules", "__pycache__", ".git")):
            continue
        violations = check_file(f)
        all_violations.extend(violations)

    if all_violations:
        for v in all_violations:
            print(f"FAIL: {v}", file=sys.stderr)
        print(f"\n{len(all_violations)} frontmatter violation(s) in {len(md_files)} file(s).", file=sys.stderr)
        return 1

    print(f"PASS: {len(md_files)} Markdown file(s) — all frontmatter valid.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_frontmatter.py <workspace_path>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
