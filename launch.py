#!/usr/bin/env python3
"""Launch hermes gateway + API server with ~/.hermes/.env loaded."""
import os, sys, subprocess

env_path = os.path.expanduser("~/.hermes/.env")
env = os.environ.copy()
with open(env_path, "r") as fh:
    for line in fh:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip("\"").strip("'")

print("Environment loaded from ~/.hermes/.env")
sys.stdout.flush()

proc = subprocess.Popen(
    [sys.executable, "-m", "hermes_cli.main", "gateway", "run", "--force", "--no-supervise"],
    env=env,
    stdout=sys.stdout,
    stderr=sys.stderr,
)

try:
    proc.wait()
except KeyboardInterrupt:
    proc.terminate()
    proc.wait()
