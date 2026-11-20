"""Runs before the agent session: all deterministic startup checks and stages, then starts the
agent with a prompt that already says what was found. Saves the tokens an agent would spend on
connection checks, `fetch` and `prepare`.

Usage: python -m pipeline.preflight [--dry-run]   (agent command: $AGENT, default `claude`)

Picks local vs. remote Paperless URL, checks the token, runs `stage`, `fetch`, then `prepare`. With 0
untouched documents it exits without starting the agent. Exit codes: 0 done/started, 2 setup
problem (URL, token, unreachable), 3 a stage failed.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys

BASE_PROMPT = "Read docs/playbook.md and carry out the ingest run it describes (P1), completely."


def _stage(name: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", "pipeline.cli", name], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    print(r.stdout + r.stderr, end="")
    return r.returncode, r.stdout


def main() -> int:
    from pipeline import config

    sys.stdout.reconfigure(errors="replace")  # document titles vs. a cp1252 console
    try:
        url, why = config.resolve_url()
        config.get_token()
    except Exception as e:  # missing .env entry, no keyring entry, no keyring backend
        print(f"preflight: {e}")
        return 2
    os.environ["PAPERLESS_URL"] = url  # child processes (stages, the agent) skip the probe
    print(f"preflight: Paperless via {why}: {url}")

    rc, sout = _stage("stage")  # local intake from consume/, a no-op when empty
    if rc:
        return 3
    staged = re.findall(r"^staged (\S+)", sout, re.M)

    rc, out = _stage("fetch")
    if rc:
        return 2 if "ConnectionError" in out or "Timeout" in out else 3
    m = re.search(r"(\d+) untouched document", out)
    if not m:
        print("preflight: fetch printed no count")
        return 3
    n = int(m.group(1))
    if n == 0 and not staged:
        print("preflight: 0 untouched documents, nothing to do, agent not started")
        return 0
    docs = re.findall(r"^(?:fetched|exists) +(\d+)(?: \((.*)\))?$", out, re.M)

    rc, pout = _stage("prepare")
    failed = re.findall(r"FAILED preparing \S+", pout)
    summary = (
        f"Preflight already ran (do not repeat it): Paperless reached via {why} ({url}); `fetch` found "
        f"{n} untouched document(s): " + "; ".join(f"{i} {t}".strip() for i, t in docs)
        + (f"; `stage` took in {len(staged)} local file(s): {', '.join(staged)}" if staged else "") + ". "
        + ("`prepare` finished" + (f" with failures: {', '.join(failed)}" if failed else " cleanly")
           if not rc else "`prepare` exited non-zero, read its output by rerunning it")
        + ". Continue at P1 step 2 (`report`, then read every content.md); the last `fetch` is still yours."
    )
    prompt = f"{BASE_PROMPT}\n\n{summary}"
    if "--dry-run" in sys.argv:
        print(prompt)
        return 0
    agent = shlex.split(os.environ.get("AGENT", "claude"))
    agent[0] = shutil.which(agent[0]) or agent[0]  # claude is claude.cmd on Windows
    return subprocess.call(agent + [prompt])


if __name__ == "__main__":
    sys.exit(main())
