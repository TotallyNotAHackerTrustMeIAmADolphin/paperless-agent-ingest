#!/usr/bin/env sh
# Starts an unattended ingest run. pipeline.preflight checks the connection, runs fetch + prepare and
# then starts the agent with the findings in its prompt. Set AGENT to the command that launches
# your agent with a prompt as its first argument, e.g. AGENT=claude (default), AGENT=codex,
# AGENT="gemini -p". Agents without a CLI: run `python -m pipeline.preflight --dry-run` and paste
# the prompt it prints.
set -e
cd "$(dirname "$0")"
if [ -f .venv/bin/activate ]; then . .venv/bin/activate; else . .venv/Scripts/activate; fi
python -m pipeline.preflight
