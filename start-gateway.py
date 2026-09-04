#!/usr/bin/env python3
"""Start hermes gateway with env vars from .env loaded."""
import os
import sys
import subprocess

# Load .env file
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value
                print(f"  Set {key}={value[:20]}...")

print("\nStarting hermes gateway on port 9119...")
sys.stdout.flush()

# Start the gateway
os.execvp('python', ['python', '-m', 'hermes_cli.main', 'serve', '--port', '9119'])
