"""Discord webhook helper — replaces Slack for factory gate notifications."""

import httpx

from orchestrator.config import DISCORD_WEBHOOK_URL, logger


async def post_discord(message: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL not set, skipping: %s", message)
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(DISCORD_WEBHOOK_URL, json={"content": message})
        if resp.status_code not in (200, 204):
            logger.warning("Discord webhook failed: %s %s", resp.status_code, resp.text)
