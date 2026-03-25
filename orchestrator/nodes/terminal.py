"""Terminal nodes — done and blocked handlers."""

from orchestrator.config import MEMORY_DIR
from orchestrator.state import FactoryState
from orchestrator.audit import audit_log
from orchestrator.slack import post_slack
from orchestrator.linear import update_linear_state, get_issue_id, comment_on_issue


async def done_handler(state: FactoryState) -> FactoryState:
    ticket_id = state["ticket_id"]
    repo_name = state.get("repo_name", "")

    # Post final summary to Linear
    issue_info = await get_issue_id(ticket_id)
    if issue_info:
        # Read the deploy log from memory for the deploy URL
        memory_path = MEMORY_DIR / f"{ticket_id}.md"
        deploy_info = ""
        if memory_path.exists():
            memory_text = memory_path.read_text()
            if "Production URL:" in memory_text:
                for line in memory_text.splitlines():
                    if "Production URL:" in line:
                        deploy_info = f"\n{line.strip()}"
                        break

        await comment_on_issue(
            issue_info["id"],
            f"🟢 **Pipeline complete — deployed successfully.**\n\n"
            f"Repo: [`{repo_name}`](https://github.com/{repo_name})\n"
            f"{deploy_info}\n\n"
            f"All 6 stages (Spec, Architecture, Implementation, Code Review, Tests, Deploy) "
            f"completed. See sub-issues for details on each stage.",
        )

    await post_slack(
        f":large_green_circle: `{ticket_id}`: {state['title']} — deployed successfully!"
    )
    audit_log(ticket_id, "done", "pipeline complete")
    return {**state, "current_state": "Done"}


async def blocked_handler(state: FactoryState) -> FactoryState:
    ticket_id = state["ticket_id"]
    error = state.get("error", "unknown")

    await update_linear_state(ticket_id, "Blocked")

    # Post error details to Linear
    issue_info = await get_issue_id(ticket_id)
    if issue_info:
        await comment_on_issue(
            issue_info["id"],
            f"🔴 **Pipeline blocked.**\n\n"
            f"**Reason**: {error}\n\n"
            f"Review the issue, fix the problem, and move the ticket back to the appropriate state to retry.",
        )

    await post_slack(f":red_circle: `{ticket_id}` is blocked: {error}")
    audit_log(ticket_id, "blocked", error)
    return state
