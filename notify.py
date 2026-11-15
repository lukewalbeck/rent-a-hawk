#!/usr/bin/env python3
"""Send one ntfy push unless this key was already pushed (pushed.json). Stdlib only."""
import json, os, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(__file__).with_name("pushed.json")
key = os.environ["KEY"].strip().lower().split("?")[0].rstrip("/")
try:
    pushed = json.loads(STATE.read_text())
except (FileNotFoundError, json.JSONDecodeError):
    pushed = {}
if key in pushed:
    print(f"Already pushed: {key}")
    sys.exit(0)
payload = {"topic": os.environ["NTFY_TOPIC"], "title": os.environ["TITLE"][:250],
           "message": os.environ["MESSAGE"], "priority": int(os.environ.get("PRIORITY") or 4),
           "tags": ["house"]}
url = os.environ.get("URL", "").strip()
if url:
    payload["click"] = url
    payload["actions"] = [{"action": "view", "label": "Open listing", "url": url}]
req = urllib.request.Request("https://ntfy.sh", data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
urllib.request.urlopen(req, timeout=15).read()
pushed[key] = datetime.now(timezone.utc).isoformat(timespec="seconds")
if len(pushed) > 3000:
    pushed = dict(sorted(pushed.items(), key=lambda kv: kv[1], reverse=True)[:3000])
STATE.write_text(json.dumps(pushed, indent=1, sort_keys=True) + "\n")
print(f"Pushed: {key}")
