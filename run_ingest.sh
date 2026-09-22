#!/usr/bin/env sh
# Starts an unattended ingest run with a coding agent. Set AGENT to the command that launches
# your agent with a prompt as its first argument, e.g. AGENT=claude, AGENT=codex,
# AGENT="gemini -p". Agents without a CLI: open the repo in the editor and paste the prompt
# from docs/ingest-prompt.md.
set -e
cd "$(dirname "$0")"
if [ -f .venv/bin/activate ]; then . .venv/bin/activate; else . .venv/Scripts/activate; fi
AGENT="${AGENT:-claude}"
$AGENT "Read docs/ingest-prompt.md and carry out the ingest run it describes, completely."
