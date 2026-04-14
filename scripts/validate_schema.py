#!/usr/bin/env python3
"""Tier 1 schema validator.

Validates JSON and YAML config files in a prototype workspace against
expected factory schemas.

Exit codes:
  0  — all files pass (or no applicable files found)
  1  — one or more files fail schema validation
"""

import sys
import json
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


# Minimal schema definitions: {filename_pattern: required_keys}
# Keys listed are required top-level fields. Extend as factory schemas evolve.
SCHEMA_RULES: list[tuple[str, set[str]]] = [
    ("package.json", {"name", "version"}),
    ("pyproject.toml", set()),  # existence check only
    ("docker-compose.yml", {"services"}),
    ("docker-compose.yaml", {"services"}),
]


def check_json_file(path: Path, required_keys: set[str]) -> list[str]:
    violations = []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        violations.append(f"{path}: invalid JSON — {e}")
        return violations

    if not isinstance(data, dict):
        violations.append(f"{path}: expected JSON object (dict) at root, got {type(data).__name__}")
        return violations

    missing = required_keys - set(data.keys())
    if missing:
        violations.append(f"{path}: missing required keys: {sorted(missing)}")

    return violations


def check_yaml_file(path: Path, required_keys: set[str]) -> list[str]:
    if yaml is None:
        return [f"{path}: PyYAML not installed — cannot validate YAML schema"]
    violations = []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except yaml.YAMLError as e:
        violations.append(f"{path}: invalid YAML — {e}")
        return violations

    if data is None:
        data = {}

    if not isinstance(data, dict):
        violations.append(f"{path}: expected YAML mapping at root, got {type(data).__name__}")
        return violations

    missing = required_keys - set(data.keys())
    if missing:
        violations.append(f"{path}: missing required keys: {sorted(missing)}")

    return violations


def main(workspace: str) -> int:
    root = Path(workspace)
    if not root.exists():
        print(f"ERROR: workspace path does not exist: {workspace}", file=sys.stderr)
        return 1

    all_violations = []
    checked = 0

    for pattern, required_keys in SCHEMA_RULES:
        for path in root.rglob(pattern):
            if any(part in path.parts for part in ("node_modules", "__pycache__", ".git")):
                continue
            checked += 1
            suffix = path.suffix.lower()
            if suffix == ".json":
                violations = check_json_file(path, required_keys)
            elif suffix in (".yml", ".yaml"):
                violations = check_yaml_file(path, required_keys)
            else:
                # e.g. pyproject.toml — just existence check (already found by rglob)
                violations = []
            all_violations.extend(violations)

    if all_violations:
        for v in all_violations:
            print(f"FAIL: {v}", file=sys.stderr)
        print(f"\n{len(all_violations)} schema violation(s) in {checked} file(s).", file=sys.stderr)
        return 1

    print(f"PASS: {checked} config file(s) validated — all schemas valid.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: validate_schema.py <workspace_path>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
