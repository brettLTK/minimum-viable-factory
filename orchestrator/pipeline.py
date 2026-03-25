"""Pipeline runner — starts or resumes the LangGraph pipeline for a ticket."""

import asyncio
import re

from langsmith import traceable
from langgraph.types import Command

from orchestrator.config import AGENT_TIMEOUT, GITHUB_ORG, WORKSPACE_DIR, logger
from orchestrator.state import FactoryState
from orchestrator.audit import audit_log
from orchestrator.memory import append_memory
from orchestrator.slack import post_slack
from orchestrator.linear import (
    update_linear_state,
    ensure_stage_sub_issues,
    get_issue_id,
    comment_on_issue,
)

# Track threads with an active pipeline run to prevent concurrent updates
_active_threads: set[str] = set()

# The compiled graph — set by api.py during lifespan startup
graph = None


def _slugify(title: str) -> str:
    """Turn a ticket title into a valid GitHub repo name."""
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:60] or "app"


@traceable(run_type="chain", name="create_app_repo")
async def create_app_repo(ticket_id: str, title: str) -> tuple[str, str]:
    """Create a new GitHub repo for the app and clone it to the workspace.

    Returns (repo_full_name, workspace_path).
    """
    repo_name = _slugify(title)
    repo_full = f"{GITHUB_ORG}/{repo_name}"
    workspace = WORKSPACE_DIR / ticket_id
    workspace.mkdir(parents=True, exist_ok=True)

    try:
        from claude_agent_sdk import query as claude_query, ClaudeAgentOptions

        options = ClaudeAgentOptions(
            cwd=str(workspace),
            permission_mode="bypassPermissions",
            allowed_tools=["Bash", "mcp__github__*"],
        )
        prompt = (
            f"Create a new GitHub repo and set it up:\n\n"
            f"1. Use the GitHub MCP to create a new repository:\n"
            f"   - Owner: `{GITHUB_ORG}`\n"
            f"   - Name: `{repo_name}`\n"
            f"   - Description: `{title} (built by software factory from {ticket_id})`\n"
            f"   - Private: false\n"
            f"   - Auto-init: true (so it has a default branch)\n"
            f"2. Clone it into the current directory: `git clone https://github.com/{repo_full}.git .`\n"
            f"3. Confirm the repo is ready with `git status`.\n\n"
            f"If the repo already exists, just clone it."
        )
        async for _ in claude_query(prompt=prompt, options=options):
            pass
    except ImportError:
        logger.warning("claude-agent-sdk not available, stubbing repo creation")
        workspace.mkdir(parents=True, exist_ok=True)

    # Post repo creation to Linear
    issue_info = await get_issue_id(ticket_id)
    if issue_info:
        await comment_on_issue(
            issue_info["id"],
            f"⚪ **Repository created**: [`{repo_full}`](https://github.com/{repo_full})\n\n"
            f"All code for this app will live here.",
        )

    audit_log(ticket_id, "repo_created", repo_full)
    return repo_full, str(workspace)


async def handle_timeout(ticket_id: str) -> None:
    minutes = AGENT_TIMEOUT // 60
    error_msg = f"Agent timed out after {minutes} minutes"
    append_memory(ticket_id, "Error", error_msg)
    await update_linear_state(ticket_id, "Blocked")

    issue_info = await get_issue_id(ticket_id)
    if issue_info:
        await comment_on_issue(
            issue_info["id"],
            f"🔴 **Pipeline timed out** after {minutes} minutes.\n\n"
            f"The ticket has been moved to **Blocked**. "
            f"Check the memory file and audit log for details on where the agent stalled.",
        )

    await post_slack(
        f":warning: `{ticket_id}` — agent timed out after {minutes} minutes. Ticket moved to Blocked."
    )
    audit_log(ticket_id, "timeout", f"{minutes} minute limit exceeded")


async def handle_error(ticket_id: str, error: str) -> None:
    append_memory(ticket_id, "Error", f"Pipeline error: {error}")
    await update_linear_state(ticket_id, "Blocked")

    issue_info = await get_issue_id(ticket_id)
    if issue_info:
        await comment_on_issue(
            issue_info["id"],
            f"🔴 **Pipeline error** — ticket moved to **Blocked**.\n\n"
            f"```\n{error[:500]}\n```",
        )

    await post_slack(f":x: `{ticket_id}` — pipeline error: {error}")
    audit_log(ticket_id, "error", error)


@traceable(run_type="chain", name="run_pipeline")
async def run_pipeline(ticket_id: str, title: str, state_name: str) -> None:
    if ticket_id in _active_threads:
        audit_log(ticket_id, "pipeline_skip", f"already running, ignoring {state_name}")
        return
    _active_threads.add(ticket_id)
    config = {"configurable": {"thread_id": ticket_id}}

    try:
        existing = await graph.aget_state(config)

        if existing and existing.values and existing.tasks:
            # Resume from interrupt
            audit_log(ticket_id, "pipeline_resume", state_name)
            await asyncio.wait_for(
                graph.ainvoke(Command(resume=state_name), config),
                timeout=AGENT_TIMEOUT,
            )
        else:
            # New ticket — create repo, stage sub-issues, then start pipeline
            repo_full, workspace_path = await create_app_repo(ticket_id, title)
            stage_subs = await ensure_stage_sub_issues(ticket_id)

            initial: FactoryState = {
                "ticket_id": ticket_id,
                "title": title,
                "current_state": state_name,
                "error": "",
                "parent_issue_id": "",
                "subtasks": [],
                "stage_sub_issues": stage_subs,
                "repo_name": repo_full,
                "workspace_path": workspace_path,
            }
            audit_log(ticket_id, "pipeline_start", f"{state_name} repo={repo_full}")
            await asyncio.wait_for(
                graph.ainvoke(initial, config),
                timeout=AGENT_TIMEOUT,
            )

        audit_log(ticket_id, "pipeline_step_complete", state_name)
    except asyncio.TimeoutError:
        await handle_timeout(ticket_id)
    except Exception as e:
        await handle_error(ticket_id, str(e))
    finally:
        _active_threads.discard(ticket_id)
