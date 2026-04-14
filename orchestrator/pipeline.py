"""Pipeline runner — starts or resumes the LangGraph pipeline for a ticket."""

import asyncio
import re

from langsmith import traceable
from langgraph.types import Command

from orchestrator.config import AGENT_TIMEOUT, GITHUB_ORG, GITHUB_BUILDS_REPO, WORKSPACE_DIR, logger
from orchestrator.state import FactoryState
from orchestrator.audit import audit_log
from orchestrator.memory import append_memory
from orchestrator.discord_notify import post_discord as post_slack
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


@traceable(run_type="chain", name="create_app_infra")
async def create_app_infra(ticket_id: str, title: str) -> tuple[str, str]:
    """Create a GitHub repo, Vercel project, and Supabase integration for the app.

    Returns (repo_full_name, workspace_path).
    """
    branch_name = f"{ticket_id.lower()}-{_slugify(title)}"
    repo_full = GITHUB_BUILDS_REPO  # Single output repo, branch per ticket
    workspace = WORKSPACE_DIR / ticket_id
    workspace.mkdir(parents=True, exist_ok=True)

    try:
        from claude_agent_sdk import query as claude_query, ClaudeAgentOptions

        options = ClaudeAgentOptions(
            cwd=str(workspace),
            permission_mode="bypassPermissions",
            allowed_tools=["Bash", "mcp__github__*", "mcp__vercel__*", "mcp__supabase__*"],
        )
        prompt = (
            f"Set up the infrastructure for a new app. Do these steps in order:\n\n"
            f"## 1. Create GitHub repo\n\n"
            f"Clone the factory-builds repo and create a branch for this ticket:\n"
            f"- Clone: `git clone https://github.com/{repo_full}.git .`\n"
            f"- Create branch: `git checkout -b {branch_name}`\n\n"
            f"## 2. Create Vercel project\n\n"
            f"Use the Vercel MCP to create a new project:\n"
            f"- Name: `factory-builds-{ticket_id.lower()}`\n"
            f"- Link it to the GitHub repo `{repo_full}` branch `{branch_name}`\n"
            f"- Framework preset: Next.js\n\n"
            f"If the project already exists, skip this step.\n\n"
            f"## 3. Provision Supabase via Vercel Marketplace\n\n"
            f"Use the Vercel MCP or run: `vercel integration add supabase`\n"
            f"This creates a Supabase project and auto-injects database env vars "
            f"(POSTGRES_URL, SUPABASE_URL, SUPABASE_ANON_KEY, etc.) into the Vercel project.\n\n"
            f"If Supabase is already provisioned (check `vercel integration list`), skip this step.\n\n"
            f"## 4. Pull env vars\n\n"
            f"Run `vercel env pull .env.local` to pull the auto-injected Supabase env vars "
            f"into the workspace so agents can use them during development.\n\n"
            f"Confirm everything is ready with `git status`."
        )
        async for _ in claude_query(prompt=prompt, options=options):
            pass
    except ImportError:
        logger.warning("claude-agent-sdk not available, stubbing infra creation")
        workspace.mkdir(parents=True, exist_ok=True)

    # Post infra creation to Linear
    issue_info = await get_issue_id(ticket_id)
    if issue_info:
        await comment_on_issue(
            issue_info["id"],
            f"⚪ **Infrastructure provisioned:**\n\n"
            f"- GitHub repo: [`{repo_full}`](https://github.com/{repo_full}) branch `{branch_name}`\n"
            f"- Vercel project: `factory-builds-{ticket_id.lower()}` (linked to repo)\n"
            f"- Supabase: provisioned via Vercel Marketplace (env vars auto-injected)",
        )

    audit_log(ticket_id, "infra_created", f"{repo_full}@{branch_name}")
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
            # New ticket — create infra, stage sub-issues, then start pipeline
            repo_full, workspace_path = await create_app_infra(ticket_id, title)
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
