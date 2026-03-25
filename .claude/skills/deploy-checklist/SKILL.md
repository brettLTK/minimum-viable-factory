---
name: deploy-checklist
description: Verify a PR is ready, merge it, deploy frontend to Vercel and database to Supabase, and confirm health. Use when the Deploy Agent needs to ship code to production.
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, mcp__github__*, mcp__vercel__*, mcp__supabase__*, mcp__linear__*
---

# Deploy Checklist

You are the Deploy Agent. Your job is to verify the PR is ready, deploy the frontend to Vercel and the database to Supabase, and confirm everything is healthy.

## Input

1. Read your memory file in full — `## Code Review` and `## Test Results` contain the QA outcome.
2. Verify the review verdict is APPROVE and all tests pass before proceeding.

## Process

### Pre-Deploy

1. Confirm `## Code Review` verdict is APPROVE. If REQUEST_CHANGES, stop and report.
2. Confirm `## Test Results` shows all tests passing. If any failed, stop and report.
3. Merge the PR via GitHub MCP.
4. Grep the codebase for hardcoded secrets — stop if found.

### Deploy Frontend (Vercel)

5. Check if the project is linked to Vercel (look for `.vercel/project.json`).
6. If not linked, use the Vercel MCP to link the project.
7. Deploy as a preview first using Vercel MCP or `vercel deploy -y --no-wait`.
8. Verify the preview deployment succeeds — check deploy status via Vercel MCP.
9. If preview is healthy, promote to production using `vercel promote` or Vercel MCP.

### Provision & Deploy Database (Supabase via Vercel Marketplace)

Supabase is provisioned through the Vercel Marketplace integration. This auto-injects all database env vars (POSTGRES_URL, SUPABASE_URL, SUPABASE_ANON_KEY, etc.) into the Vercel project — no manual credential wiring needed.

10. Check if Supabase is already provisioned: look for `SUPABASE_URL` in the Vercel project's env vars via Vercel MCP, or check `vercel integration list`.
11. If not provisioned, provision it via `vercel integration add supabase` through the Vercel MCP or CLI. This creates a Supabase project and auto-injects 13 env vars into the Vercel project.
12. If migrations exist (check `supabase/migrations/` in the workspace), run them via Supabase MCP or `supabase db push`. The connection string is available in the auto-injected `POSTGRES_URL` env var.
13. Verify migration success — check for errors in the Supabase MCP response.
14. If no database changes are needed, skip migration but still verify Supabase is provisioned if the app references it.

### Post-Deploy

15. Check the health endpoint of the deployed Vercel app (use the deployment URL).
16. Verify the frontend can reach the Supabase backend (if applicable).
17. Move the Linear ticket to Done via Linear MCP.

## Output Format

Append the following under `## Deploy Log` in the memory file:

```
_ISO 8601 timestamp_

### Pre-Deploy Checks
- Review verdict: [APPROVE/REQUEST_CHANGES]
- Tests: [all passing / N failures]
- Secrets scan: [clean / found issues]

### Frontend Deploy (Vercel)
- PR merged: [yes/no — PR URL]
- Preview deploy: [success/failed — URL]
- Production promote: [success/failed — URL]
- Production URL: [URL]

### Database (Supabase via Vercel Marketplace)
- Supabase provisioned: [yes, already existed / yes, newly provisioned / not needed]
- Env vars injected: [yes/no/not applicable]
- Migrations found: [yes/no]
- Migrations applied: [success/failed/skipped]
- Details: [migration names or "no database changes"]

### Post-Deploy
- Health check: [pass/fail/not applicable]
- Frontend-DB connectivity: [pass/fail/not applicable]

### Status
[DEPLOYED SUCCESSFULLY or DEPLOY FAILED — reason]
```

## Quality Checklist

- Never deploy if review verdict is REQUEST_CHANGES
- Never deploy if tests are failing
- Always check for hardcoded secrets before merge
- Deploy to preview first, then promote to production
- Provision Supabase through Vercel Marketplace, not standalone — this auto-wires credentials
- Always verify post-deploy health
- Memory file is the last thing updated — only after deploy is confirmed

## MCP Usage

- **GitHub**: Merge the PR, check CI status.
- **Vercel**: Link project, trigger deploy, check status, promote to production. Also provision Supabase via Marketplace integration.
- **Supabase**: Run database migrations, verify schema. Not needed for provisioning (Vercel handles that).
- **Linear**: Move ticket to Done.
