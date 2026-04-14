"""Prototype flow nodes — entry, generator fanout, eval gates, selection, graduation.

Implements:
  prototype_flow_entry        — classify task, route to prototype or direct SDLC
  prototype_generator_fanout  — 3 parallel Claude Code generator sessions
  prototype_eval_gate         — Tier 1 hard checks + Tier 2 PM scoring
  prototype_selection_gate    — human interrupt gate via Linear custom field
  graduation_trigger          — spawn new Linear issue for production build
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import yaml
from langsmith import traceable
from langgraph.types import interrupt

from orchestrator.config import (
    DELTA_DIR,
    GRADUATION_MAX_CONCURRENT,
    GENERATOR_STALL_WINDOW_SEC,
    MEMORY_DIR,
    SKILLS_DIR,
    TIER1_MAX_RETRIES,
    WORKSPACE_DIR,
    logger,
)
from orchestrator.state import FactoryState
from orchestrator.audit import audit_log
from orchestrator.memory import append_memory
from orchestrator.discord_notify import post_discord as post_slack
from orchestrator.linear import (
    comment_on_issue,
    create_linear_issue,
    get_issue_id,
    update_linear_state,
)
from orchestrator.agent_runner import run_agent
from orchestrator.prototype_memory import (
    load_bounded_deltas,
    sanitize_for_langsmith,
    write_selection_delta,
)

# ---------------------------------------------------------------------------
# Module-level graduation rate-limit semaphore (Sentinel F6)
# ---------------------------------------------------------------------------

GRADUATION_SEMAPHORE = asyncio.Semaphore(GRADUATION_MAX_CONCURRENT)


# ---------------------------------------------------------------------------
# Helper: delta context injection
# ---------------------------------------------------------------------------


def _build_delta_context(deltas: list[dict]) -> str:
    """Format delta history for PM agent prompt injection."""
    if not deltas:
        return "## Selection History\n\n_(No prior selections — this is the first prototype task.)_\n\n"
    delta_yaml = yaml.dump(deltas, default_flow_style=False, allow_unicode=True)
    return (
        f"## Selection History (last {len(deltas)} decisions — bounded to 50 max / 30 days)\n\n"
        f"```yaml\n{delta_yaml}\n```\n\n"
        f"Use this history to calibrate your prototype ranking. "
        f"Pay attention to brett_override=true records — these are your most valuable signal.\n\n"
    )


# ---------------------------------------------------------------------------
# prototype_flow_entry
# ---------------------------------------------------------------------------


async def prototype_flow_entry(state: FactoryState) -> FactoryState:
    """Entry node for In Prototype Linear state.

    1. Load last 50 selection deltas (bounded window)
    2. Call pm-classify/SKILL.md to classify task type
    3. Set flow_type in state
    4. If direct_sdlc: update Linear to In Spec, hand off to pm_agent
    5. If prototype: proceed to prototype_generator_fanout
    """
    ticket_id = state["ticket_id"]
    audit_log(ticket_id, "prototype_flow_entry:start", "loading delta context")

    # Load bounded delta context
    deltas = load_bounded_deltas(n=50, days=30)
    delta_context = _build_delta_context(deltas)

    # Call PM classify agent
    extra_prompt = (
        f"{delta_context}"
        f"## Classification Task\n\n"
        f"Classify this ticket using the pm-classify skill instructions above.\n"
        f"Output ONLY a YAML block (no extra text) matching this schema:\n\n"
        f"```yaml\n"
        f"classification:\n"
        f"  flow_type: \"prototype\"          # \"prototype\" | \"direct_sdlc\"\n"
        f"  task_type: \"feature\"            # \"feature\" | \"research\" | \"bug\" | \"fleet\" | \"ltk\"\n"
        f"  rationale: \"...\"\n"
        f"  fleet_touching: false\n"
        f"  ltk_deliverable: false\n"
        f"```\n"
    )

    try:
        result_state = await run_agent(
            state,
            "pm-classify/SKILL.md",
            "Classification",
            extra_prompt=extra_prompt,
        )
        # Parse classification from memory
        memory_text = (MEMORY_DIR / f"{ticket_id}.md").read_text(encoding="utf-8")
        # Extract YAML block from memory output
        classification = _parse_classification(memory_text)
    except Exception as exc:
        logger.warning("prototype_flow_entry: classification failed (%s) — defaulting to prototype", exc)
        audit_log(ticket_id, "prototype_flow_entry:classify_error", str(exc))
        classification = {"flow_type": "prototype", "task_type": "feature"}

    flow_type = classification.get("flow_type", "prototype")
    task_type = classification.get("task_type", "feature")

    audit_log(ticket_id, "prototype_flow_entry:classified", f"flow_type={flow_type} task_type={task_type}")

    if flow_type == "direct_sdlc":
        await update_linear_state(ticket_id, "In Spec")
        issue_info = await get_issue_id(ticket_id)
        if issue_info:
            await comment_on_issue(
                issue_info["id"],
                f"🔵 **Classification**: `direct_sdlc` — routing to standard SDLC pipeline.\n\n"
                f"Rationale: {classification.get('rationale', 'N/A')}",
            )
        return {**state, "flow_type": flow_type, "current_state": "In Spec"}

    # Prototype path
    issue_info = await get_issue_id(ticket_id)
    if issue_info:
        await comment_on_issue(
            issue_info["id"],
            f"🟡 **Classification**: `prototype` (task_type: `{task_type}`) — launching 3 parallel generators.\n\n"
            f"Rationale: {classification.get('rationale', 'N/A')}",
        )

    return {**state, "flow_type": flow_type, "current_state": "In Prototype"}


def _parse_classification(memory_text: str) -> dict:
    """Extract and parse the YAML classification block from PM agent output."""
    import re
    # Look for ```yaml ... ``` block containing "flow_type"
    match = re.search(r"```yaml\s*(.*?)```", memory_text, re.DOTALL)
    if match:
        try:
            parsed = yaml.safe_load(match.group(1))
            if isinstance(parsed, dict) and "classification" in parsed:
                return parsed["classification"]
            if isinstance(parsed, dict) and "flow_type" in parsed:
                return parsed
        except Exception:
            pass
    # Fallback: safe default
    return {"flow_type": "prototype", "task_type": "feature"}


# ---------------------------------------------------------------------------
# Single generator with stall detection
# ---------------------------------------------------------------------------


async def _run_single_generator(
    state: FactoryState, prototype_id: str, idx: int
) -> dict | None:
    """Run one prototype generator Claude Code session.

    Returns a prototype dict on success, None on failure.
    """
    ticket_id = state["ticket_id"]
    branch_name = f"{ticket_id}/prototype-{idx + 1}"
    proto_workspace = WORKSPACE_DIR / ticket_id / "prototypes" / f"proto-{idx + 1}"
    proto_workspace.mkdir(parents=True, exist_ok=True)

    extra_prompt = (
        f"## Prototype Generator Context\n\n"
        f"You are generator {idx + 1} of 3 building a prototype for ticket {ticket_id}.\n\n"
        f"**Branch**: `{branch_name}`\n"
        f"**Workspace**: `{proto_workspace}`\n\n"
        f"Implement the prototype in your assigned workspace. "
        f"Commit all changes to branch `{branch_name}`.\n"
        f"If you are blocked, post a comment with `BLOCKED: [reason]` immediately.\n"
    )

    try:
        result_state = await run_agent(
            {**state, "workspace_path": str(proto_workspace)},
            "prototype-coding/SKILL.md",
            f"Prototype-{idx + 1}",
            extra_prompt=extra_prompt,
        )
        return {
            "id": prototype_id,
            "workspace_path": str(proto_workspace),
            "repo_branch": branch_name,
            "eval_tier1_pass": None,  # filled by eval gate
            "eval_scores": {},
        }
    except Exception as exc:
        audit_log(ticket_id, f"generator_error:{prototype_id}", str(exc))
        logger.warning("Generator %s failed: %s", prototype_id, exc)
        return None


async def _run_single_generator_with_stall_detection(
    state: FactoryState, prototype_id: str, idx: int
) -> dict | None:
    """Wrap _run_single_generator with stall detection.

    Returns None if generator stalls and retry is exhausted.
    Retry budget: TIER1_MAX_RETRIES attempts after the first run.
    """
    ticket_id = state["ticket_id"]
    retry_counts = dict(state.get("prototype_retry_counts") or {})

    for attempt in range(TIER1_MAX_RETRIES + 1):  # first run + retries
        try:
            result = await asyncio.wait_for(
                _run_single_generator(state, prototype_id, idx),
                timeout=GENERATOR_STALL_WINDOW_SEC,
            )
            if result is not None:
                return result
        except asyncio.TimeoutError:
            audit_log(ticket_id, f"stall:detected:{prototype_id}", f"attempt {attempt + 1}")
            logger.warning("Stall detected for %s (attempt %d)", prototype_id, attempt + 1)

        current_retries = retry_counts.get(prototype_id, 0)
        if current_retries >= TIER1_MAX_RETRIES:
            audit_log(ticket_id, f"stall:drop:{prototype_id}", "retry cap exceeded")
            logger.warning("Dropping prototype %s — retry cap exceeded", prototype_id)
            return None

        retry_counts[prototype_id] = current_retries + 1
        audit_log(ticket_id, f"stall:retry:{prototype_id}", f"attempt {attempt + 1} → retrying")

    return None


# ---------------------------------------------------------------------------
# prototype_generator_fanout
# ---------------------------------------------------------------------------


async def prototype_generator_fanout(state: FactoryState) -> FactoryState:
    """Launch 3 parallel prototype generator sessions via asyncio.gather."""
    ticket_id = state["ticket_id"]
    prototype_ids = [f"{ticket_id}-proto-{i + 1}" for i in range(3)]
    retry_counts: dict[str, int] = dict(state.get("prototype_retry_counts") or {})

    audit_log(ticket_id, "prototype_generator_fanout:start", "launching 3 generators")
    await post_slack(f":building_construction: `{ticket_id}` — launching 3 prototype generators in parallel.")

    tasks = [
        _run_single_generator_with_stall_detection(state, pid, i)
        for i, pid in enumerate(prototype_ids)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    passing = []
    for r in results:
        if isinstance(r, dict):
            passing.append(r)
        elif isinstance(r, Exception):
            audit_log(ticket_id, "generator_exception", str(r))

    if not passing:
        error_msg = "All 3 prototype generators failed — task-level failure"
        audit_log(ticket_id, "prototype_generator_fanout:all_failed", error_msg)

        issue_info = await get_issue_id(ticket_id)
        if issue_info:
            await comment_on_issue(
                issue_info["id"],
                "🔴 **Prototype task failed** — all 3 prototypes failed to generate after retry. "
                "No prototypes passed to evaluation.",
            )
        return {**state, "current_state": "Blocked", "error": error_msg, "prototype_retry_counts": retry_counts}

    audit_log(ticket_id, "prototype_generator_fanout:done", f"{len(passing)}/3 generators succeeded")
    return {**state, "prototypes": passing, "prototype_retry_counts": retry_counts}


# ---------------------------------------------------------------------------
# Tier 1 check wrapper (fail-closed)
# ---------------------------------------------------------------------------


async def _run_tier1_check(
    name: str,
    cmd: list[str],
    cwd: str,
    ticket_id: str,
    timeout_sec: int = 120,
) -> tuple[bool, str]:
    """Run a single Tier 1 check. Returns (passed, reason).

    Fail-closed: ANY exception, timeout, or non-zero exit = FAIL.
    Audit logs the result regardless of outcome (Sentinel F1).
    """
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout_sec,
        )
        stdout, stderr = await proc.communicate()
        passed = proc.returncode == 0
        reason = (stdout.decode(errors="replace") + stderr.decode(errors="replace"))[:500]
    except asyncio.TimeoutError:
        passed = False
        reason = f"Tier 1 tool timed out after {timeout_sec}s — treated as gate failure (fail-closed)"
    except Exception as exc:
        passed = False
        reason = f"Tier 1 tool unavailable or failed to execute: {exc} — treated as gate failure (fail-closed)"

    # Audit log every Tier 1 result including tool failures (Sentinel F1)
    audit_log(ticket_id, f"tier1:{name}", f"pass={passed} | {reason[:200]}")
    return passed, reason


async def _run_tier1_checks_for_prototype(
    proto: dict, ticket_id: str, factory_root: str
) -> tuple[bool, dict[str, str]]:
    """Run all 4 Tier 1 checks for one prototype. Returns (all_passed, reasons)."""
    workspace = proto["workspace_path"]
    checks = [
        ("lint", ["npx", "eslint", ".", "--max-warnings", "0"]),
        ("tests", ["pytest"]),
        ("frontmatter", ["python3", f"{factory_root}/scripts/check_frontmatter.py", workspace]),
        ("schema", ["python3", f"{factory_root}/scripts/validate_schema.py", workspace]),
    ]

    all_passed = True
    reasons: dict[str, str] = {}
    for name, cmd in checks:
        passed, reason = await _run_tier1_check(name, cmd, workspace, ticket_id)
        reasons[name] = reason
        if not passed:
            all_passed = False

    return all_passed, reasons


# ---------------------------------------------------------------------------
# prototype_eval_gate
# ---------------------------------------------------------------------------


async def prototype_eval_gate(state: FactoryState) -> FactoryState:
    """Run Tier 1 hard checks (script) then Tier 2 PM scoring (agent).

    Tier 1: fail-closed, no agent judgment.
    Tier 2: runs only on Tier-1-passing prototypes.
    """
    ticket_id = state["ticket_id"]
    prototypes: list[dict] = list(state.get("prototypes") or [])
    retry_counts: dict[str, int] = dict(state.get("prototype_retry_counts") or {})

    # Determine factory root for script paths
    import os
    factory_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    audit_log(ticket_id, "prototype_eval_gate:start", f"evaluating {len(prototypes)} prototypes")

    # ----- Tier 1 -----
    tier1_tasks = [
        _run_tier1_checks_for_prototype(proto, ticket_id, factory_root)
        for proto in prototypes
    ]
    tier1_results = await asyncio.gather(*tier1_tasks, return_exceptions=True)

    updated_prototypes = []
    for proto, result in zip(prototypes, tier1_results):
        pid = proto["id"]
        if isinstance(result, Exception):
            proto = {**proto, "eval_tier1_pass": False, "tier1_reasons": {"error": str(result)}}
            audit_log(ticket_id, f"tier1:exception:{pid}", str(result))
        else:
            all_passed, reasons = result
            proto = {**proto, "eval_tier1_pass": all_passed, "tier1_reasons": reasons}

        updated_prototypes.append(proto)

    # Retry logic: re-run generators that failed Tier 1
    final_prototypes = []
    for proto in updated_prototypes:
        pid = proto["id"]
        if proto["eval_tier1_pass"]:
            final_prototypes.append(proto)
            continue

        current_retries = retry_counts.get(pid, 0)
        if current_retries < TIER1_MAX_RETRIES:
            retry_counts[pid] = current_retries + 1
            audit_log(ticket_id, f"tier1:retry:{pid}", f"attempt {current_retries + 1}")
            # Re-run generator for this prototype
            idx = int(pid.split("-proto-")[-1]) - 1
            retry_result = await _run_single_generator_with_stall_detection(
                {**state, "prototype_retry_counts": retry_counts}, pid, idx
            )
            if retry_result:
                # Re-check Tier 1 after retry
                all_passed, reasons = await _run_tier1_checks_for_prototype(retry_result, ticket_id, factory_root)
                retry_result = {**retry_result, "eval_tier1_pass": all_passed, "tier1_reasons": reasons}
                if all_passed:
                    final_prototypes.append(retry_result)
                    continue
            audit_log(ticket_id, f"tier1:drop:{pid}", "retry failed Tier 1 again")
        else:
            audit_log(ticket_id, f"tier1:drop:{pid}", "retry cap exceeded")
            # Notify PM agent via memory (Brett NOT notified per spec)
            append_memory(ticket_id, "Prototype-Eval", f"Prototype {pid} dropped after Tier 1 failure (retry cap).")

    if not final_prototypes:
        error_msg = "All prototypes dropped after Tier 1 evaluation and retries"
        audit_log(ticket_id, "prototype_eval_gate:all_dropped", error_msg)
        issue_info = await get_issue_id(ticket_id)
        if issue_info:
            await comment_on_issue(
                issue_info["id"],
                "🔴 **Prototype task failed** — all 3 prototypes failed Tier 1 evaluation after retry. "
                "No prototypes passed to selection.",
            )
        return {**state, "current_state": "Blocked", "error": error_msg, "prototypes": final_prototypes, "prototype_retry_counts": retry_counts}

    # ----- Tier 2 (agent scoring — runs only on Tier-1-passing prototypes) -----
    deltas = load_bounded_deltas(n=50, days=30)
    delta_context = _build_delta_context(deltas)

    tier1_summaries = "\n".join(
        f"- {p['id']}: tier1_pass={p['eval_tier1_pass']}"
        for p in final_prototypes
    )

    extra_prompt = (
        f"{delta_context}"
        f"## Tier 2 Scoring Task\n\n"
        f"Score each of the following prototypes using the pm-score skill rubric.\n\n"
        f"**Tier 1 results (passing prototypes only):**\n{tier1_summaries}\n\n"
        f"Output ONLY a YAML block matching the schema in your skill instructions.\n"
    )

    try:
        scored_state = await run_agent(
            {**state, "prototypes": final_prototypes, "prototype_retry_counts": retry_counts},
            "pm-score/SKILL.md",
            "Prototype-Scoring",
            extra_prompt=extra_prompt,
        )
        # Parse scores from memory
        memory_text = (MEMORY_DIR / f"{ticket_id}.md").read_text(encoding="utf-8")
        scores = _parse_tier2_scores(memory_text)
    except Exception as exc:
        logger.warning("prototype_eval_gate: Tier 2 scoring failed (%s) — using zero scores", exc)
        audit_log(ticket_id, "prototype_eval_gate:tier2_error", str(exc))
        scores = {}

    # Attach scores to prototypes
    for proto in final_prototypes:
        pid = proto["id"]
        proto["eval_scores"] = scores.get(pid, {"total": 0})

    # Sort by total score (best first)
    final_prototypes.sort(key=lambda p: p["eval_scores"].get("total", 0), reverse=True)

    audit_log(
        ticket_id,
        "prototype_eval_gate:done",
        f"{len(final_prototypes)} prototypes passed to selection gate",
    )

    return {**state, "prototypes": final_prototypes, "prototype_retry_counts": retry_counts}


def _parse_tier2_scores(memory_text: str) -> dict:
    """Extract Tier 2 score data from PM agent output."""
    import re
    match = re.search(r"```yaml\s*(.*?)```", memory_text, re.DOTALL)
    if match:
        try:
            parsed = yaml.safe_load(match.group(1))
            if isinstance(parsed, dict) and "scores" in parsed:
                return parsed["scores"]
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# prototype_selection_gate
# ---------------------------------------------------------------------------


@traceable(run_type="chain", name="prototype_selection_gate")
async def prototype_selection_gate(state: FactoryState) -> FactoryState:
    """Human interrupt gate — Brett selects winning prototype via Linear custom field."""
    ticket_id = state["ticket_id"]
    prototypes: list[dict] = list(state.get("prototypes") or [])

    audit_log(ticket_id, "prototype_selection_gate:start", f"{len(prototypes)} candidates")

    issue_info = await get_issue_id(ticket_id)
    if not issue_info:
        return {**state, "current_state": "Blocked", "error": "Could not resolve issue ID for selection gate"}

    # Build preview comment (top 2-3 prototypes, exclude failed ones)
    top_protos = prototypes[:3]  # already sorted by score from eval gate
    lines = ["🟡 **Prototype Selection Gate** — Brett action required.\n"]

    if top_protos:
        best = top_protos[0]
        best_score = best.get("eval_scores", {}).get("total", "N/A")
        lines.append(f"PM Agent recommends: **{best['id']}** (score: {best_score}/100)")
        for runner in top_protos[1:]:
            lines.append(f"Runner-up: {runner['id']} (score: {runner['eval_scores'].get('total', 'N/A')})")

    excluded = len([p for p in state.get("prototypes", []) if not p.get("eval_tier1_pass", True)])
    if excluded > 0:
        lines.append(f"\n_({len(top_protos)} of {len(top_protos) + excluded} prototypes advanced — {excluded} excluded by Tier 1 gate)_")

    lines.append(f"\n**Preview branches:**")
    for p in top_protos:
        lines.append(f"- `{p['id']}`: branch `{p['repo_branch']}`")

    lines.append(
        "\nApply a label to select the winner: `proto-winner-1`, `proto-winner-2`, `proto-winner-3`, or `proto-archived`."
        "\nThen move the ticket to any next state to proceed."
    )

    await comment_on_issue(issue_info["id"], "\n".join(lines))
    audit_log(ticket_id, "prototype_selection_gate:interrupted", "waiting for Brett")

    # Interrupt pipeline — resumes on any Linear state transition
    interrupt({"gate": "prototype_selection_gate", "ticket_id": ticket_id})

    # Read Winning Prototype from Linear labels (Option B — free tier compatible)
    from orchestrator.linear import get_issue_labels
    labels = await get_issue_labels(issue_info["id"])
    proto_winner_labels = [l for l in labels if l.startswith("proto-winner-")]
    archived_label = "proto-archived" in labels

    winning_prototype = None
    if proto_winner_labels:
        # proto-winner-1 → Proto-1, proto-winner-2 → Proto-2, etc.
        label = proto_winner_labels[0]
        n = label.split("-")[-1]  # "1", "2", "3"
        winning_prototype = f"{ticket_id}-proto-{n}"
    elif archived_label:
        winning_prototype = "Archived"

    # Safe default: Archived if no label applied
    if not winning_prototype or winning_prototype == "Archived":
        audit_log(ticket_id, "prototype_selection_gate:archived", "no winner selected")
        await comment_on_issue(
            issue_info["id"],
            "📦 **Prototype archived** — no winner selected (or `Archived` set). No graduation trigger.",
        )
        return {**state, "prototype_winner": "Archived", "current_state": "Archived"}

    # Write SelectionDelta
    pm_top_pick = top_protos[0]["id"] if top_protos else None
    brett_override = winning_prototype != pm_top_pick

    delta_record: dict = {
        "type": "SelectionDelta",
        "schema_version": "1.0",
        "task_id": ticket_id,
        "prototypes_generated": len(top_protos),
        "pm_agent_top_pick": pm_top_pick,
        "pm_agent_rationale": _extract_pm_rationale(state),
        "brett_selection": winning_prototype,
        "brett_override": brett_override,
        "delta_notes": f"Override: brett chose {winning_prototype} over PM pick {pm_top_pick}" if brett_override else "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_type": state.get("flow_type", "feature"),
    }

    delta_path = None
    try:
        delta_path = write_selection_delta(delta_record)
        # LangSmith-safe version (sanitized — CISO F4)
        _langsmith_safe = sanitize_for_langsmith(delta_record)
        # Would be passed to LangSmith run metadata here
        audit_log(ticket_id, "prototype_selection_gate:delta_written", str(delta_path))
    except Exception as exc:
        audit_log(ticket_id, "prototype_selection_gate:delta_error", str(exc))
        logger.error("Failed to write SelectionDelta: %s", exc)

    await comment_on_issue(
        issue_info["id"],
        f"✅ **Winner selected**: `{winning_prototype}`"
        + (f"\n\n_(PM Agent recommended `{pm_top_pick}` — Brett overrode)_" if brett_override else ""),
    )

    audit_log(ticket_id, "prototype_selection_gate:winner_set", winning_prototype)
    return {
        **state,
        "prototype_winner": winning_prototype,
        "selection_delta_path": str(delta_path) if delta_path is not None else "",
        "current_state": "prototype_winner_set",
    }


def _extract_pm_rationale(state: FactoryState) -> str:
    """Extract PM agent rationale from memory for delta record."""
    try:
        ticket_id = state["ticket_id"]
        memory_text = (MEMORY_DIR / f"{ticket_id}.md").read_text(encoding="utf-8")
        import re
        match = re.search(r"selection_rationale:\s*(.+?)(?:\n|$)", memory_text)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# graduation_trigger
# ---------------------------------------------------------------------------


def _format_graduation_spec(state: FactoryState, winner_id: str) -> str:
    """Format graduation spec for new Linear issue.

    IMPORTANT: This is the specification INPUT for the production build.
    Prototype code is NOT promoted directly — it informs the spec only.
    """
    winner_proto = next(
        (p for p in (state.get("prototypes") or []) if p["id"] == winner_id),
        None,
    )
    branch = winner_proto["repo_branch"] if winner_proto else "N/A"
    scores = winner_proto.get("eval_scores", {}) if winner_proto else {}

    return (
        f"## Production Build Task\n\n"
        f"**Source ticket**: {state['ticket_id']} — {state.get('title', '')}\n"
        f"**Winning prototype**: `{winner_id}`\n"
        f"**Prototype branch**: `{branch}` (reference only — DO NOT promote prototype code)\n\n"
        f"### Eval Scores (prototype reference)\n\n"
        f"```yaml\n{yaml.dump(scores, default_flow_style=False)}\n```\n\n"
        f"### Build Instructions\n\n"
        f"1. Read the prototype branch to understand the accepted approach\n"
        f"2. Build from scratch using the prototype as a specification input\n"
        f"3. Do NOT copy prototype code — implement cleanly for production\n"
        f"4. All acceptance criteria from the original ticket apply\n\n"
        f"_This task was created automatically by the Factory graduation trigger._"
    )


@traceable(run_type="chain", name="graduation_trigger")
async def graduation_trigger(state: FactoryState) -> FactoryState:
    """Spawn a new Linear issue for the production build task.

    Rate-limited by GRADUATION_SEMAPHORE (max GRADUATION_MAX_CONCURRENT concurrent).
    New issue starts in 'In Spec' — triggers pm_agent automatically via STATE_MAP.
    """
    ticket_id = state["ticket_id"]
    winner_id = state.get("prototype_winner", "")

    if not winner_id or winner_id == "Archived":
        audit_log(ticket_id, "graduation_trigger:skipped", "no winner or archived")
        return {**state, "current_state": "Done"}

    async with GRADUATION_SEMAPHORE:
        audit_log(ticket_id, "graduation_trigger:start", f"winner={winner_id}")

        spec_body = _format_graduation_spec(state, winner_id)

        try:
            new_issue_id = await create_linear_issue(
                title=f"[Production Build] {state.get('title', ticket_id)}",
                description=spec_body,
                parent_id=ticket_id,
                initial_state="In Spec",
            )
        except Exception as exc:
            audit_log(ticket_id, "graduation_trigger:create_error", str(exc))
            logger.error("graduation_trigger: failed to create Linear issue: %s", exc)
            return {**state, "current_state": "Blocked", "error": f"Graduation failed: {exc}"}

        audit_log(ticket_id, "graduation_triggered", f"new_issue={new_issue_id} winner={winner_id}")

        issue_info = await get_issue_id(ticket_id)
        if issue_info:
            await comment_on_issue(
                issue_info["id"],
                f"✅ **Graduation triggered.** Production build task created: `{new_issue_id}`\n\n"
                f"The prototype is used as the specification input — not promoted directly.\n"
                f"Full SDLC flow begins at **In Spec** for the new task.",
            )

        await post_slack(
            f":white_check_mark: `{ticket_id}` graduation complete — "
            f"production task `{new_issue_id}` created (prototype: `{winner_id}`)."
        )

    return {**state, "graduation_task_id": new_issue_id, "current_state": "Done"}
