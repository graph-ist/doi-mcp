#!/usr/bin/env python3
"""Check doi-mcp service and API credentials. Run: sudo python3 ~/doi-mcp/check_creds.py

Prints only outcomes and HTTP codes, never credential values.
"""
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

ENV_FILE = "/etc/doi-mcp/env"
env = {}
for line in open(ENV_FILE):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip("\"'")


def call(url, headers=None, data=None):
    # Retry 429 and 5xx: transient, not a verdict on the credential
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})},
                                     data=data)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if (e.code < 500 and e.code != 429) or attempt == 3:
                return e.code, e.read()
        except Exception as e:
            if attempt == 3:
                return None, type(e).__name__.encode()
        time.sleep(3)


def report(name, ok, detail):
    print(f"{name:<26} {'OK' if ok else 'PROBLEMA':<9} {detail}")


def have(*names):
    missing = [n for n in names if not env.get(n)]
    for n in missing:
        print(f"{n:<26} {'MANCA':<9} non presente in {ENV_FILE}")
    return not missing


# Service
active = subprocess.run(["systemctl", "is-active", "doi-mcp"], capture_output=True, text=True).stdout.strip()
report("Servizio doi-mcp", active == "active", active)
port = env.get("PORT", "3002")
s, _ = call(f"http://127.0.0.1:{port}/mcp")
report("Auth senza token", s == 401, f"HTTP {s} (atteso 401)")

# Credentials
if have("CONTACT_EMAIL"):
    report("CONTACT_EMAIL", True, "presente")

if have("OPENALEX_API_KEY"):
    s, _ = call("https://api.openalex.org/works?per-page=1&api_key="
                + urllib.parse.quote(env["OPENALEX_API_KEY"]))
    report("OpenAlex", s == 200, f"HTTP {s} (401/403 = chiave non valida)")

if have("SEMANTIC_SCHOLAR_API_KEY"):
    s, _ = call("https://api.semanticscholar.org/graph/v1/paper/DOI:10.1145/3025453.3025461?fields=title",
                {"x-api-key": env["SEMANTIC_SCHOLAR_API_KEY"]})
    report("Semantic Scholar", s == 200, f"HTTP {s} (403 = chiave non valida, 429 = limite)")

if have("GOOGLE_BOOKS_API_KEY"):
    # Volume lookup: Google's search endpoint returns 503 backendFailed at times
    s, _ = call("https://www.googleapis.com/books/v1/volumes/zyTCAlFPjgYC?projection=lite",
                {"x-goog-api-key": env["GOOGLE_BOOKS_API_KEY"]})
    report("Google Books", s == 200, f"HTTP {s} (400/403 = chiave non valida o API non abilitata)")

if have("ORCID_CLIENT_ID", "ORCID_CLIENT_SECRET"):
    body = urllib.parse.urlencode({
        "client_id": env["ORCID_CLIENT_ID"], "client_secret": env["ORCID_CLIENT_SECRET"],
        "grant_type": "client_credentials", "scope": "/read-public"}).encode()
    s, raw = call("https://orcid.org/oauth/token", data=body)
    token = False
    if s == 200:
        try:
            token = bool(json.loads(raw).get("access_token"))
        except ValueError:
            pass
    report("ORCID (token)", token, f"HTTP {s} (401 = ID o secret errati)")
