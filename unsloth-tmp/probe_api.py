import json
import urllib.request

BASE = "http://127.0.0.1:8888"

for path in ("/v1/models", "/v1/props", "/"):
    url = BASE + path
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "phase0-probe"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", errors="replace")
            print(f"== {path} -> HTTP {r.status} ({len(body)} bytes)")
            if len(body) > 4000:
                body = body[:4000] + "...[truncated]"
            print(body)
    except Exception as e:
        print(f"== {path} -> ERROR: {e}")
    print()
