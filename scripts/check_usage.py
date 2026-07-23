#!/usr/bin/env python3
"""Read the Claude plan usage meter (five-hour + weekly utilization).

Uses the community-discovered OAuth endpoint (undocumented; may break —
tracked in weekend-wave driver as fail-safe: any failure here means callers
must NOT spend). Prints "FIVE_HOUR=<int> SEVEN_DAY=<int>" on stdout, exit 0.
Any error: exit 1, nothing spent.
"""
import json
import sys
import urllib.request

CREDS = "/home/tlemmons/.claude/.credentials.json"
URL = "https://api.anthropic.com/api/oauth/usage"

try:
    creds = json.load(open(CREDS))
    tok = creds.get("claudeAiOauth", {}).get("accessToken") or creds.get("oauth_token")
    if not tok:
        raise RuntimeError("no oauth token in credentials file")
    req = urllib.request.Request(
        URL, headers={"Authorization": f"Bearer {tok}",
                      "anthropic-beta": "oauth-2025-04-20"})
    data = json.load(urllib.request.urlopen(req, timeout=15))
    five = int(data["five_hour"]["utilization"])
    week = int(data["seven_day"]["utilization"])
    print(f"FIVE_HOUR={five} SEVEN_DAY={week}")
except Exception as e:  # noqa: BLE001 — fail-safe: no reading, no spending
    print(f"usage check failed: {e}", file=sys.stderr)
    sys.exit(1)
