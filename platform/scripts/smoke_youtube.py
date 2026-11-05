#!/usr/bin/env python
"""Manual smoke test: runs the full loop against a real test channel through the REST API.

This spends real money with your LLM and TTS providers and uses YouTube quota. Run it against a
test channel:

    python platform/scripts/smoke_youtube.py --api http://localhost:8000 --topic "Saturn's rings"

The YouTube OAuth redirect URI must be http://localhost:8765/oauth/callback. The script receives
Google's redirect, posts the code to the API, then polls until the video is confirmed.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx

CALLBACK_PORT = 8765
TERMINAL = {"published", "failed", "rejected"}


def wait_for_redirect() -> dict[str, str]:
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            captured.update({key: value[0] for key, value in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"You can return to the terminal.")

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", CALLBACK_PORT), Handler)
    server.handle_request()
    server.server_close()
    return captured


def check(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        sys.exit(
            f"{response.request.method} {response.request.url}: "
            f"{response.status_code} {response.text[:500]}"
        )
    return response.json()


def poll(client, item_id, stop, timeout, interval=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = check(client.get(f"/api/v1/content/{item_id}/"))
        print(f"  status={item['status']} detail={item['status_detail']}")
        if item["status"] in stop:
            return item
        time.sleep(interval)
    sys.exit(f"Timed out waiting for {sorted(stop)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--email")
    parser.add_argument("--topic", default="Why Saturn's rings are disappearing")
    parser.add_argument(
        "--visibility", default="private", choices=["private", "unlisted", "public"]
    )
    args = parser.parse_args()

    client = httpx.Client(base_url=args.api, timeout=60)
    email = args.email or f"smoke-{secrets.token_hex(4)}@example.com"
    password = getpass.getpass("Password (blank generates one and registers a new user): ")
    if not password:
        password = secrets.token_urlsafe(16)
        check(client.post("/api/v1/auth/register/", json={"email": email, "password": password}))
        print(f"Registered {email}")
    tokens = check(client.post("/api/v1/auth/token/", json={"email": email, "password": password}))
    client.headers["Authorization"] = f"Bearer {tokens['access']}"

    print("Connecting YouTube...")
    start = check(client.post("/api/v1/social/connections/start/", json={"platform": "youtube"}))
    print(f"Open this URL if a browser does not appear:\n{start['authorization_url']}\n")
    webbrowser.open(start["authorization_url"])
    redirect = wait_for_redirect()
    if "error" in redirect:
        sys.exit(f"Google returned an error: {redirect['error']}")
    account = check(
        client.post(
            "/api/v1/social/connections/complete/",
            json={"state": redirect["state"], "code": redirect["code"]},
        )
    )
    print(f"Connected {account['display_name']}")
    print(check(client.get("/api/v1/publishing/status/")))

    niche = check(
        client.post(
            "/api/v1/niches/",
            json={"name": "Smoke test", "audience": "Curious adults", "keywords": ["science"]},
        )
    )
    item = check(client.post("/api/v1/content/", json={"niche": niche["id"], "topic": args.topic}))
    print(f"Generating item {item['id']}...")
    item = poll(client, item["id"], {"awaiting_review", "failed"}, timeout=1800)
    if item["status"] == "failed":
        sys.exit(f"Generation failed: {item['status_message']}")
    print(f"Review it here (the link expires): {item['preview_url']}")
    print(f"Title: {item['script']['title']}\nSources: {item['script']['sources']}")
    if input("Approve and upload? [y/N] ").strip().lower() != "y":
        check(client.post(f"/api/v1/content/{item['id']}/reject/", json={"reason": "smoke test"}))
        print("Rejected. Nothing was uploaded.")
        return
    check(
        client.post(
            f"/api/v1/content/{item['id']}/approve/",
            json={"account": account["id"], "visibility": args.visibility, "made_for_kids": False},
        )
    )
    item = poll(client, item["id"], TERMINAL, timeout=3600, interval=30)
    publication = item["publication"] or {}
    print(f"Final status: {item['status']}")
    print(f"URL: {publication.get('url')}  visibility: {publication.get('visibility')}")
    print(f"Visibility note: {publication.get('visibility_reason')}")
    if item["status"] != "published":
        sys.exit(f"Upload did not finish: {item['status_detail']} {item['status_message']}")
    print("Smoke test passed: videos.list confirmed the video. Check the channel too.")


if __name__ == "__main__":
    main()
