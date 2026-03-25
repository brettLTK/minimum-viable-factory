"""Thin wrappers that call run_agent with the correct skill file."""

from orchestrator.state import FactoryState
from orchestrator.agent_runner import run_agent


async def pm_agent(state: FactoryState) -> FactoryState:
    return await run_agent(state, "spec-writing/SKILL.md", "Spec")


async def architect_agent(state: FactoryState) -> FactoryState:
    return await run_agent(state, "architecture/SKILL.md", "Architecture Decision")


async def review_agent(state: FactoryState) -> FactoryState:
    return await run_agent(state, "code-review/SKILL.md", "Code Review")


async def test_agent(state: FactoryState) -> FactoryState:
    return await run_agent(state, "test-writing/SKILL.md", "Test Results")


async def deploy_agent(state: FactoryState) -> FactoryState:
    return await run_agent(
        state, "deploy-checklist/SKILL.md", "Deploy Log", next_linear_state="Done"
    )
