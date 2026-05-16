#!/usr/bin/env python3
"""
trigger_daily.py
----------------
Run this on a schedule (cron, GitHub Actions, etc.) to kick off the daily
Denali weather call.

Usage:
    python trigger_daily.py

Or from cron (runs at 8am Alaska time = 4pm UTC):
    0 16 * * * /usr/bin/python3 /path/to/trigger_daily.py >> /var/log/denali-weather.log 2>&1
"""

import os
import sys

import httpx

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

if not PUBLIC_BASE_URL:
    print("ERROR: PUBLIC_BASE_URL environment variable is not set.")
    sys.exit(1)


def trigger():
    url = f"{PUBLIC_BASE_URL}/trigger-call"
    print(f"Triggering call via {url} ...")
    response = httpx.post(url, timeout=15)
    response.raise_for_status()
    data = response.json()
    print(f"Success! Call SID: {data.get('call_sid')}")


if __name__ == "__main__":
    trigger()
