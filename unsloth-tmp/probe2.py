import os, urllib.request, urllib.error
BASE = "http://127.0.0.1:8888"
print("TOKEN set:", bool(os.environ.get("UNSLOTH_STUDIO_AUTH_TOKEN")))
print("APIKEY set:", bool(os.environ.get("UNSLOTH_API_KEY")))
for path in ("/v1/models", "/v1/props", "/"):
    try:
        req = urllib.request.Request(BASE + path, headers={"User-Agent": "phase2-check"})
        with urllib.request.urlopen(req, timeout=5) as r:
            print(path, "->", r.status)
    except urllib.error.HTTPError as e:
        print(path, "-> HTTP", e.code, e.reason)
    except Exception as e:
        print(path, "-> ERROR:", type(e).__name__, e)
