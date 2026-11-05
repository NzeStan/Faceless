#!/usr/bin/env python
"""Run the real stack end to end against a local stand-in for Google, killing workers mid-upload.

    python platform/e2e/run.py

Needs Docker and httpx. It uses real Celery workers, PostgreSQL, Redis, MinIO and FFmpeg, with the
fake AI, voice and footage providers. It spends nothing and never reads .env.

It runs as its own Compose project and deletes that project's data afterwards, so your development
data is safe. Stop any development stack first, because both use the same ports. Use --keep to leave
the stack running for debugging.

The stand-in follows Google's documentation. A pass shows that our workers and recovery logic work,
not that Google behaves the same way. Only platform/scripts/smoke_youtube.py can show that.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "faceless-e2e"
API = "http://localhost:8000/api/v1"
GOOGLE = "http://localhost:9999"
APP_SERVICES = ("api", "worker-ai", "worker-render", "worker-publish", "beat")
PAID_KEYS = ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "PEXELS_API_KEY")

client = httpx.Client(timeout=30)
failures: list[str] = []


def check(passed: bool, message: str) -> None:
    print(("  PASS " if passed else "  FAIL ") + message)
    if not passed:
        failures.append(message)


def compose(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    command = ["docker", "compose", "-p", PROJECT, "-f", "docker-compose.yml"]
    command += ["-f", "platform/e2e/compose.yml", *args]
    return subprocess.run(
        command,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        check=False,
    )


def body(response: httpx.Response, expected: int | None = None):
    if response.status_code >= 400 or (expected and response.status_code != expected):
        request = response.request
        sys.exit(f"{request.method} {request.url} -> {response.status_code}: {response.text[:700]}")
    return response.json() if response.content else None


def google_state() -> dict:
    return client.get(f"{GOOGLE}/_state").json()


def new_user() -> dict[str, str]:
    email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
    credentials = {"email": email, "password": "correct-horse-" + uuid.uuid4().hex}
    body(client.post(f"{API}/auth/register/", json=credentials), 201)
    tokens = body(client.post(f"{API}/auth/token/", json=credentials))
    return {"Authorization": f"Bearer {tokens['access']}"}


class Run:
    def __init__(self) -> None:
        self.auth = new_user()
        self.account: dict = {}
        self.niche = body(
            client.post(
                f"{API}/niches/",
                headers=self.auth,
                json={"name": "Sea", "audience": "adults", "tone": "calm", "language": "en"},
            ),
            201,
        )

    def get(self, path: str):
        return body(client.get(f"{API}{path}", headers=self.auth))

    def connect(self) -> None:
        print("\n== Connect a channel")
        start = body(
            client.post(
                f"{API}/social/connections/start/",
                headers=self.auth,
                json={"platform": "youtube"},
            )
        )
        query = parse_qs(urlsplit(start["authorization_url"]).query)
        check(query.get("code_challenge_method") == ["S256"], "OAuth uses PKCE S256")
        payload = {"state": query["state"][0], "code": "fake-code"}
        url = f"{API}/social/connections/complete/"
        self.account = body(client.post(url, headers=self.auth, json=payload))
        tokens = google_state()["secrets"]
        text = json.dumps(self.account)
        check(
            tokens["access"] not in text and tokens["refresh"] not in text,
            "tokens are not in the connect response",
        )
        replay = client.post(url, headers=self.auth, json=payload)
        check(replay.status_code >= 400, f"replayed OAuth state rejected ({replay.status_code})")

    def generate(self, topic: str) -> dict:
        created = body(
            client.post(
                f"{API}/content/",
                headers=self.auth,
                json={"niche": self.niche["id"], "topic": topic},
            ),
            201,
        )
        deadline = time.monotonic() + 400
        while time.monotonic() < deadline:
            item = self.get(f"/content/{created['id']}/")
            if item["status"] == "awaiting_review":
                return item
            if item["status"] == "failed":
                sys.exit(f"Generation failed: {item['status_detail']} {item['status_message']}")
            time.sleep(2)
        sys.exit("Timed out generating a video.")

    def check_review_access(self) -> None:
        print("\n== Generate and review")
        item = self.generate(f"Review {uuid.uuid4().hex[:6]}")
        url = item["preview_url"]
        video = client.get(url)
        check(video.status_code == 200 and len(video.content) > 1000, "signed preview downloads")
        tampered = url.split("X-Amz-Signature")[0] + "X-Amz-Signature=" + "0" * 64
        check(client.get(tampered).status_code == 403, "tampered signature gets 403")
        path = f"{API}/content/{item['id']}/"
        check(client.get(path, headers=new_user()).status_code == 404, "another user gets 404")
        check(client.get(path).status_code == 401, "no credentials gets 401")
        self.check_video_format(video.content)

    @staticmethod
    def check_video_format(video: bytes) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "preview.mp4").write_bytes(video)
            probe = compose(
                "run", "--rm", "-T", "--no-deps", "-v", f"{Path(folder).as_posix()}:/out",
                "worker-render", "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_name,width,height", "-of", "compact", "/out/preview.mp4",
            )  # fmt: skip
        out = probe.stdout
        check("width=1080" in out and "height=1920" in out, "video is 1080x1920")
        check("h264" in out and "aac" in out, "video is H.264 with AAC audio")

    def upload_scenario(self, name: str, fault: str | None) -> dict:
        print(f"\n== Upload: {name}")
        item = self.generate(f"{name} {uuid.uuid4().hex[:6]}")
        digest = hashlib.sha256(client.get(item["preview_url"]).content).hexdigest()
        sessions_before = google_state()["inserts"]
        body(client.post(f"{GOOGLE}/_mode", json={"mode": fault or "normal"}))
        body(
            client.post(
                f"{API}/content/{item['id']}/approve/",
                headers=self.auth,
                json={
                    "account": self.account["id"],
                    "visibility": "public",
                    "made_for_kids": False,
                },
            )
        )
        if fault:
            self.kill_worker_at_fault(name)
        result = self.wait_for_upload(item["id"])
        self.check_upload(name, result, digest, sessions_before)
        return result

    def kill_worker_at_fault(self, name: str) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and google_state()["hung"] == 0:
            time.sleep(0.5)
        check(google_state()["hung"] == 1, f"{name}: the worker reached the failure point")
        check(compose("kill", "-s", "SIGKILL", "worker-publish").returncode == 0, "worker killed")
        client.post(f"{GOOGLE}/_release")
        time.sleep(1)
        compose("start", "worker-publish")

    def wait_for_upload(self, item_id: str, timeout: int = 300) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        item = {}
        while time.monotonic() < deadline:
            item = self.get(f"/content/{item_id}/")
            current = (item["status"], item["status_detail"])
            if current != last:
                print(f"    {current[0]} {current[1]}")
                last = current
            if item["status"] in ("published", "failed", "rejected"):
                break
            time.sleep(2)
        return item

    @staticmethod
    def check_upload(name: str, item: dict, digest: str, sessions_before: int) -> None:
        state = google_state()
        publication = item["publication"] or {}
        check(item["status"] == "published", f"{name}: published (got {item['status']})")
        same_bytes = [v for v in state["videos"].values() if v["sha256"] == digest]
        check(len(same_bytes) == 1, f"{name}: one video on the channel ({len(same_bytes)})")
        opened = state["inserts"] - sessions_before
        check(opened == 1, f"{name}: one upload session opened ({opened})")
        video = state["videos"].get((publication.get("url") or "").rsplit("=", 1)[-1])
        check(bool(video) and video["sha256"] == digest, f"{name}: bytes match the render")
        if video:
            check(video["privacy"] == "private", f"{name}: public request uploaded as private")
            check("[ref:" in video["description"], f"{name}: marker in the description")
            status = video["status"]
            check(
                status.get("selfDeclaredMadeForKids") is False
                and "containsSyntheticMedia" in status,
                f"{name}: kids and synthetic flags sent explicitly",
            )
        reason = publication.get("visibility_reason") or ""
        check("not audited" in reason, f"{name}: the reason names our policy ({reason[:40]!r})")

    def check_global(self, items: list[dict]) -> None:
        print("\n== Across all uploads")
        state = google_state()
        check(state["unauthorised_puts"] == 0, "Authorization sent on every PUT")
        used = self.get("/publishing/status/")["uploads_used_today"]
        check(used == state["inserts"], f"quota used {used} = uploads opened {state['inserts']}")

        secrets = [state["secrets"]["access"], state["secrets"]["refresh"]]
        secrets += state["secrets"]["upload_ids"]
        responses = json.dumps(items) + json.dumps(self.get("/social/accounts/"))
        check(not [s for s in secrets if s in responses], "no secrets in API responses")
        logs = compose("logs", "--no-color", *APP_SERVICES).stdout
        check(len(logs) > 1000, "logs were captured")
        check(not [s for s in secrets if s in logs], "no secrets in container logs")
        for table in ("social_publisher_connectedaccount", "social_publisher_publication"):
            sql = f"select string_agg(t::text, ' ') from (select * from {table}) t;"
            rows = compose(
                "exec", "-T", "postgres", "psql", "-U", "faceless", "-d", "faceless", "-tAc", sql
            )
            check(
                rows.returncode == 0
                and len(rows.stdout) > 50
                and not [s for s in secrets if s in rows.stdout],
                f"no readable secrets in {table}",
            )


def wait_for_api() -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            if client.get(f"{API}/schema/").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(2)
    sys.exit("The API did not start in time.")


def check_no_paid_keys() -> None:
    """Refuse to run if a real key reached a container, because the run could then spend money."""
    for service in APP_SERVICES:
        env = compose("exec", "-T", service, "env").stdout
        leaked = [key for key in PAID_KEYS if f"{key}=" in env and f"{key}=\n" not in env]
        if leaked or "providers.fake.FakeLLM" not in env:
            sys.exit(f"Stopping: {service} is not isolated from real providers ({leaked}).")
    print("  PASS no real provider keys in any container")


def scenarios() -> None:
    check_no_paid_keys()
    run = Run()
    run.check_review_access()
    run.connect()
    results = [
        run.upload_scenario("no crash", None),
        run.upload_scenario("killed before the final chunk", "hang_before_final"),
        run.upload_scenario("killed after the upload finished", "hang_after_commit"),
        run.upload_scenario("killed and the session lost", "gone_after_commit"),
    ]
    run.check_global(results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="Leave the stack running afterwards.")
    args = parser.parse_args()

    started = compose("up", "-d", "--build", capture=False)
    try:
        if started.returncode:
            print("The stack did not start.")
            return 1
        wait_for_api()
        try:
            scenarios()
        except SystemExit as stop:
            failures.append(str(stop.code))
        if failures:
            print(compose("logs", "--no-color", "--tail", "40", *APP_SERVICES).stdout[-6000:])
        print(f"\nRESULT: {'PASSED' if not failures else f'{len(failures)} FAILED'}")
        for failure in failures:
            print(f"  - {failure}")
        return 1 if failures else 0
    finally:
        if not args.keep:
            compose("down", "-v")


if __name__ == "__main__":
    sys.exit(main())
