@echo off
rem Starts an unattended ingest run with Claude Code. For another agent, see run_ingest.sh.
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
claude "Read docs/ingest-prompt.md and carry out the ingest run it describes, completely."
pause
