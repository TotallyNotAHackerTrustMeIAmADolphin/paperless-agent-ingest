@echo off
rem Starts an unattended ingest run. pipeline.preflight checks the connection, runs fetch + prepare and
rem then starts the agent (default claude, override with AGENT) with the findings in its prompt.
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
python -m pipeline.preflight
pause
