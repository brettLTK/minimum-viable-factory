"""Slack webhook helper."""

import httpx
from langsmith import traceable

from orchestrator.config import SLACK_WEBHOOK_URL, logger


@traceable(run_type="tool", name="slack_post")
async def post_slack(message: str) -> None:
    if not SLACK_WEBHOOK_URL:
        logger.warning("SLACK_WEBHOOK_URL not set, skipping: %s", message)
        return
    async with httpx.AsyncClient() as client:
        await client.post(SLACK_WEBHOOK_URL, json={"text": message})
