"""A local stand-in for Google's OAuth, YouTube Data and resumable upload endpoints."""

import hashlib
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ACCESS = "FAKEACCESSTOKEN-" + secrets.token_hex(8)
REFRESH = "FAKEREFRESHTOKEN-" + secrets.token_hex(8)
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)
lock = threading.Lock()
release = threading.Event()
S = {
    "mode": "normal",
    "hung": 0,
    "sessions": {},
    "videos": {},
    "inserts": 0,
    "unauthorised_puts": 0,
    "requests": [],
    "secrets": {"access": ACCESS, "refresh": REFRESH, "upload_ids": []},
}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _empty(self, code, headers=None):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _authed(self):
        return self.headers.get("Authorization") == f"Bearer {ACCESS}"

    def do_GET(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/_state":
            with lock:
                sessions = {
                    k: {a: b for a, b in v.items() if a != "data"} for k, v in S["sessions"].items()
                }
                return self._json(
                    200, {k: v for k, v in S.items() if k != "sessions"} | {"sessions": sessions}
                )
        S["requests"].append(f"GET {u.path}")
        if not self._authed():
            return self._json(401, {"error": {"code": 401, "errors": [{"reason": "authError"}]}})
        if u.path.endswith("/channels"):
            return self._json(
                200,
                {
                    "items": [
                        {
                            "id": "UCfake",
                            "snippet": {"title": "Fake channel"},
                            "contentDetails": {"relatedPlaylists": {"uploads": "UUfake"}},
                        }
                    ]
                },
            )
        if u.path.endswith("/playlistItems"):
            items = [
                {"snippet": {"description": v["description"], "resourceId": {"videoId": i}}}
                for i, v in reversed(list(S["videos"].items()))
            ]
            return self._json(200, {"items": items})
        if u.path.endswith("/videos"):
            v = S["videos"].get(q.get("id"))
            if not v:
                return self._json(200, {"items": []})
            return self._json(
                200,
                {
                    "items": [
                        {
                            "id": q["id"],
                            "status": {"uploadStatus": "processed", "privacyStatus": v["privacy"]},
                            "processingDetails": {"processingStatus": "succeeded"},
                        }
                    ]
                },
            )
        return self._json(404, {"error": {"code": 404}})

    def do_POST(self):
        u = urlsplit(self.path)
        body = self._body()
        if u.path == "/_mode":
            d = json.loads(body)
            release.clear()
            S["mode"], S["hung"] = d["mode"], 0
            return self._json(200, {})
        if u.path == "/_release":
            release.set()
            return self._json(200, {})
        S["requests"].append(f"POST {u.path}")
        if u.path == "/token":
            return self._json(
                200,
                {
                    "access_token": ACCESS,
                    "refresh_token": REFRESH,
                    "expires_in": 3600,
                    "scope": SCOPES,
                },
            )
        if u.path == "/upload":
            if not self._authed():
                return self._json(401, {"error": {"code": 401}})
            meta = json.loads(body)
            sid, up = secrets.token_hex(6), "UPLOADSECRET-" + secrets.token_hex(8)
            with lock:
                S["inserts"] += 1
                S["secrets"]["upload_ids"].append(up)
                S["sessions"][sid] = {
                    "meta": meta,
                    "total": int(self.headers["X-Upload-Content-Length"]),
                    "data": b"",
                    "video": None,
                    "gone": False,
                    "chunks": 0,
                }
            host = self.headers["Host"]
            return self._empty(200, {"Location": f"http://{host}/session/{sid}?upload_id={up}"})
        return self._json(404, {})

    def _commit(self, s):
        vid = "vid" + secrets.token_hex(4)
        S["videos"][vid] = {
            "description": s["meta"]["snippet"]["description"],
            "title": s["meta"]["snippet"]["title"],
            "privacy": s["meta"]["status"]["privacyStatus"],
            "status": s["meta"]["status"],
            "sha256": hashlib.sha256(s["data"]).hexdigest(),
            "size": len(s["data"]),
        }
        s["video"] = vid
        return vid

    def _done(self, s):
        v = S["videos"][s["video"]]
        return self._json(
            200,
            {
                "id": s["video"],
                "status": {"privacyStatus": v["privacy"], "uploadStatus": "uploaded"},
            },
        )

    def _hang(self):
        S["hung"] += 1
        S["mode"] = "normal"
        release.wait(110)
        self.close_connection = True
        self.connection.close()

    def do_PUT(self):
        u = urlsplit(self.path)
        body = self._body()
        S["requests"].append(f"PUT range={self.headers.get('Content-Range')}")
        if not self._authed():
            S["unauthorised_puts"] += 1
        s = S["sessions"].get(u.path.rsplit("/", 1)[-1])
        if not s or s["gone"]:
            return self._json(404, {"error": {"code": 404}})
        cr = self.headers.get("Content-Range", "")
        if cr.startswith("bytes */"):
            if s["video"]:
                return self._done(s)
            n = len(s["data"])
            return self._empty(308, {"Range": f"bytes=0-{n - 1}"} if n else {})
        first, last, total = map(int, re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr).groups())
        assert first == len(s["data"]), (first, len(s["data"]))
        is_last = last + 1 == total
        mode = S["mode"]
        if is_last and mode == "hang_before_final":
            return self._hang()
        s["data"] += body
        s["chunks"] += 1
        if is_last:
            self._commit(s)
            if mode == "hang_after_commit":
                return self._hang()
            if mode == "gone_after_commit":
                s["gone"] = True
                return self._hang()
            return self._done(s)
        return self._empty(308, {"Range": f"bytes=0-{len(s['data']) - 1}"})


ThreadingHTTPServer(("0.0.0.0", 9999), H).serve_forever()
