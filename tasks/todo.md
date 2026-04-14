# GH #362 — Factory SDLC: Prototype Flow, Eval Gates & PM Agent Self-Improvement

## Status: COMPLETE — Ready for Sentinel review

- [x] Create `orchestrator/prototype_memory.py` — delta I/O helpers
- [x] Create `orchestrator/nodes/prototype.py` — all prototype flow nodes
- [x] Create `scripts/check_frontmatter.py` — Tier 1 frontmatter checker
- [x] Create `scripts/validate_schema.py` — Tier 1 schema validator
- [x] Create `scripts/convergence_report.py` — convergence metrics
- [x] Create `scripts/archive_old_deltas.py` — 90-day delta archival
- [x] Create `.claude/skills/pm-classify/SKILL.md`
- [x] Create `.claude/skills/pm-score/SKILL.md`
- [x] Create `.claude/skills/prototype-coding/SKILL.md`
- [x] Modify `orchestrator/state.py` — 6 new fields + STATE_MAP entry
- [x] Modify `orchestrator/config.py` — DELTA_DIR, TIER1_MAX_RETRIES, GRADUATION_MAX_CONCURRENT, GENERATOR_STALL_WINDOW_SEC
- [x] Modify `orchestrator/linear.py` — create_linear_issue()
- [x] Modify `orchestrator/nodes/__init__.py` — export new node functions
- [x] Modify `orchestrator/graph.py` — 5 nodes + conditional edges + routing functions
- [x] Update `.gitignore` — memory/selection-deltas/ entries
- [x] Verify: py_compile all modified/new Python files — PASS
- [x] Verify: STATE_MAP includes "In Prototype" — CONFIRMED
- [x] Verify: sanitize_for_langsmith() called before LangSmith writes — CONFIRMED (line 595)
- [x] Verify: memory/selection-deltas/ in .gitignore — CONFIRMED
