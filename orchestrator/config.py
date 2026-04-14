"""Factory-wide configuration: env vars, paths, constants."""

import os
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Directories
MEMORY_DIR = Path("memory")
AUDIT_DIR = Path("audit")
TEMPLATE_PATH = MEMORY_DIR / "_template.md"
SKILLS_DIR = Path(".claude/skills")
DB_PATH = "/app/db/factory.sqlite"

# API keys and secrets
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY", "")
LINEAR_WEBHOOK_SECRET = os.getenv("LINEAR_WEBHOOK_SECRET", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")  # legacy, unused
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

GITHUB_ORG = os.getenv("GITHUB_ORG", "ashtilawat")
GITHUB_BUILDS_REPO = os.getenv("GITHUB_BUILDS_REPO", "brettLTK/factory-builds")  # Single output repo — one branch per ticket
WORKSPACE_DIR = Path("/app/workspace")

# Timeouts
AGENT_TIMEOUT = 1800  # 30 minutes

# Prototype flow constants (GH #362)
DELTA_DIR = "memory/selection-deltas/"
TIER1_MAX_RETRIES = int(os.getenv("TIER1_MAX_RETRIES", "2"))
GRADUATION_MAX_CONCURRENT = int(os.getenv("GRADUATION_MAX_CONCURRENT", "3"))
GENERATOR_STALL_WINDOW_SEC = int(os.getenv("GENERATOR_STALL_WINDOW_SEC", "300"))

# Logging
logger = logging.getLogger("factory")
logging.basicConfig(level=logging.INFO)
