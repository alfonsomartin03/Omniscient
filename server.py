#!/usr/bin/env python3
"""Local-only HTTPS and RT-DETR object tracking service for Omniscient.

Frames are analyzed in memory and discarded. Inference is offline and local.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookies
import http.server
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from vision import MODEL_WORKER, ModelBusy, ModelFailure, bbox_iou

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TLS_DIR = DATA / "tls"
AUTH_FILE = DATA / "auth.json"
ZONES_FILE = DATA / "restricted_spaces.json"
STATIC = {"/": "index.html", "/index.html": "index.html", "/styles.css": "styles.css", "/app.js": "app.js"}
FRAME_W, FRAME_H = 40, 22
FRAME_SIZE = FRAME_W * FRAME_H
MAX_FRAME_W, MAX_FRAME_H = 960, 540
MAX_BODY = 1_800_000
SESSION_TTL = 8 * 60 * 60
PBKDF2_ROUNDS = 600_000

SESSIONS: dict[str, dict] = {}
LOGIN_NONCES: dict[str, dict] = {}
LOGIN_FAILURES: dict[str, tuple[int, float]] = {}
ANALYSIS: dict[str, dict] = {}
ZONES: list[dict] = []
ALLOWED_HOSTS: set[str] = {"localhost", "127.0.0.1", "::1"}
STATE_LOCK = threading.RLock()


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json_private(path: Path, value) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    temp.write_text(json.dumps(value, separators=(",", ":")))
    os.chmod(temp, 0o600)
    temp.replace(path)


def password_record(password: str, salt: bytes | None = None) -> dict:
    salt = salt or secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(digest).decode(), "rounds": PBKDF2_ROUNDS}


def extra_hostnames() -> list[str]:
    hosts = [value.strip().lower() for value in os.environ.get("OMNI_ALLOWED_HOSTS", "").split(",") if value.strip()]
    for host in hosts:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*", host):
                raise SystemExit("OMNI_ALLOWED_HOSTS must contain only valid DNS names or IP addresses.")
    return hosts


def password_matches(password: str, record: dict) -> bool:
    try:
        if len(password) > 1024 or int(record["rounds"]) != PBKDF2_ROUNDS:
            return False
        salt = base64.b64decode(record["salt"], validate=True)
        expected = base64.b64decode(record["hash"], validate=True)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(record["rounds"]))
        return hmac.compare_digest(actual, expected)
    except (KeyError, ValueError, TypeError):
        return False


def ensure_password() -> None:
    if AUTH_FILE.exists():
        os.chmod(AUTH_FILE, 0o600)
        record = read_json(AUTH_FILE, None)
        if record and record.get("salt") and record.get("hash"):
            return
        raise SystemExit("Invalid data/auth.json. Move it aside to provision a new local password.")
    print("First run: create the local Omniscient administrator password.")
    import getpass
    while True:
        password = getpass.getpass("New password (minimum 16 characters): ")
        confirm = getpass.getpass("Confirm password: ")
        if len(password) < 16:
            print("Use at least 16 characters.")
        elif not hmac.compare_digest(password, confirm):
            print("Passwords did not match.")
        else:
            DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_json_private(AUTH_FILE, password_record(password))
            print("Password hash saved with PBKDF2-SHA256. The password itself is not stored.")
            return


def ensure_tls() -> tuple[Path, Path, Path]:
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    TLS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    os.chmod(TLS_DIR, 0o700)
    ca_key, ca_cert = TLS_DIR / "local-ca.key", TLS_DIR / "local-ca.crt"
    cert, key = TLS_DIR / "server.crt", TLS_DIR / "server.key"
    openssl = __import__("shutil").which("openssl")
    if not openssl:
        raise SystemExit("OpenSSL is required to create the local TLS certificate. No HTTP fallback is allowed.")
    ca_available = ca_key.exists() and ca_cert.exists()
    cert_fresh = False
    if ca_available and cert.exists() and key.exists():
        cert_fresh = subprocess.run([openssl, "x509", "-in", str(cert), "-checkend", str(30 * 24 * 60 * 60), "-noout"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not cert_fresh:
        host = socket.gethostname()
        extra_hosts = extra_hostnames()
        san_parts = ["DNS:localhost", "IP:127.0.0.1", "IP:::1", f"DNS:{host}"]
        for extra in extra_hosts:
            try:
                ipaddress.ip_address(extra)
                san_parts.append(f"IP:{extra}")
            except ValueError:
                san_parts.append(f"DNS:{extra}")
        try:
            for info in socket.getaddrinfo(host, None):
                value = info[4][0].split("%", 1)[0]
                ipaddress.ip_address(value)
                san_parts.append(f"IP:{value}")
        except (OSError, ValueError):
            pass
        san = ",".join(dict.fromkeys(san_parts))
        ext = TLS_DIR / "server-ext.cnf"
        ext.write_text(f"basicConstraints=critical,CA:FALSE\nsubjectAltName={san}\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\n")
        commands = []
        if not ca_available:
            commands.append([openssl, "req", "-x509", "-newkey", "rsa:4096", "-sha256", "-nodes", "-days", "3650", "-subj", "/CN=Omniscient Local Root CA", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", str(ca_key), "-out", str(ca_cert)])
        commands.extend([
            [openssl, "req", "-new", "-newkey", "rsa:3072", "-sha256", "-nodes", "-subj", f"/CN={host}", "-keyout", str(key), "-out", str(TLS_DIR / "server.csr")],
            [openssl, "x509", "-req", "-sha256", "-days", "825", "-in", str(TLS_DIR / "server.csr"), "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial", "-extfile", str(ext), "-out", str(cert)],
        ])
        for command in commands:
            try:
                subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=40)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                message = getattr(exc, "stderr", b"")
                raise SystemExit(f"Could not create TLS certificate: {message.decode(errors='replace') if isinstance(message, bytes) else exc}")
        for private_key in (ca_key, key):
            os.chmod(private_key, 0o600)
        for public_file in (ca_cert, cert):
            os.chmod(public_file, 0o644)
        ext.unlink(missing_ok=True)
        (TLS_DIR / "server.csr").unlink(missing_ok=True)
        (TLS_DIR / "local-ca.srl").unlink(missing_ok=True)
    for private_key in (ca_key, key):
        os.chmod(private_key, 0o600)
    for public_file in (ca_cert, cert):
        os.chmod(public_file, 0o644)
    return cert, key, ca_cert


def point_in_polygon(point: tuple[float, float], polygon: list[dict]) -> bool:
    x, y = point
    inside = False
    for index, a in enumerate(polygon):
        b = polygon[index - 1]
        if (a["y"] > y) != (b["y"] > y) and x < (b["x"] - a["x"]) * (y - a["y"]) / (b["y"] - a["y"]) + a["x"]:
            inside = not inside
    return inside


def decode_gray_frame(frame_data: bytes) -> bytes:
    if not frame_data or len(frame_data) > MAX_BODY:
        raise ValueError("Camera frame exceeds the permitted size")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(frame_data)) as image:
            if image.format != "JPEG":
                raise ValueError("Expected a JPEG camera frame")
            if not (16 <= image.width <= MAX_FRAME_W and 16 <= image.height <= MAX_FRAME_H):
                raise ValueError("Camera frame dimensions are outside the permitted range")
            return image.convert("L").resize((FRAME_W, FRAME_H)).tobytes()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Invalid JPEG camera frame") from exc


def frame_result(calibration: int, objects=None, events=None) -> dict:
    return {
        "calibration": calibration,
        "objects": objects or [],
        "events": events or [],
        "zoneEvents": [{"id": zone["id"], "events": zone["events"]} for zone in ZONES],
    }


def analyze_frame(session_id: str, frame_data: bytes) -> dict:
    gray = decode_gray_frame(frame_data)
    now = time.monotonic()
    with STATE_LOCK:
        state = ANALYSIS.setdefault(session_id, {
            "baseline_sum": [0] * FRAME_SIZE,
            "baseline_frames": 0,
            "baseline": None,
            "previous_gray": None,
            "last_inference": 0.0,
            "last_detections": [],
            "tracks": {},
            "next_id": 1,
        })
        if state["baseline"] is None:
            state["baseline_frames"] += 1
            for index, value in enumerate(gray):
                state["baseline_sum"][index] += value
            if state["baseline_frames"] >= 30:
                count = state["baseline_frames"]
                state["baseline"] = bytes(round(value / count) for value in state["baseline_sum"])
                state["baseline_sum"] = None
                state["previous_gray"] = gray
            return frame_result(round(state["baseline_frames"] / 30 * 100))

        previous = state["previous_gray"] or gray
        delta = [abs(a - b) for a, b in zip(gray, previous)]
        state["previous_gray"] = gray
        age = now - state["last_inference"]
        changed_cells = sum(value > 18 for value in delta)
        # A quiet fixed camera only needs a periodic detector refresh. Substantial
        # scene changes bypass that idle cadence so entries are seen promptly.
        needs_detect = (not state["last_inference"] or age >= 2.5
                        or (age >= 0.4 and (sum(delta) / FRAME_SIZE >= 3 or changed_cells >= 12)))
        detections = state["last_detections"]

    if needs_detect:
        detections = MODEL_WORKER.detect(frame_data)

    with STATE_LOCK:
        if ANALYSIS.get(session_id) is not state:
            return frame_result(0)
        if needs_detect:
            state["last_inference"] = time.monotonic()
            state["last_detections"] = detections
        anomaly = bytearray(abs(value - state["baseline"][i]) > 44 for i, value in enumerate(gray))
        active, matched, output, events = state["tracks"], set(), [], []
        for detection in detections[:40]:
            box = detection["box"]
            candidate, best_iou = None, 0.12
            for track in active.values():
                if track["id"] in matched or track["label"] != detection["label"]:
                    continue
                overlap = bbox_iou(track["box"], box)
                if overlap > best_iou:
                    candidate, best_iou = track, overlap
            x1, y1, x2, y2 = box
            center = ((x1 + x2) / 2, (y1 + y2) / 2)
            zone = next((item for item in ZONES if point_in_polygon(center, item["points"])), None)
            gx1, gy1 = max(0, min(FRAME_W - 1, int(x1 * FRAME_W))), max(0, min(FRAME_H - 1, int(y1 * FRAME_H)))
            gx2, gy2 = max(gx1 + 1, min(FRAME_W, int(x2 * FRAME_W))), max(gy1 + 1, min(FRAME_H, int(y2 * FRAME_H)))
            changed = sum(anomaly[y * FRAME_W + x] for y in range(gy1, gy2) for x in range(gx1, gx2))
            changed_ratio = changed / ((gx2 - gx1) * (gy2 - gy1))
            danger = "high" if zone else "medium" if changed_ratio > .16 else "low"
            if candidate is None:
                track_id = state["next_id"]
                state["next_id"] += 1
                candidate = {"id": track_id, "label": detection["label"], "box": box,
                             "last_seen": now, "zone_id": None, "danger": danger,
                             "confidence": detection["confidence"]}
                active[track_id] = candidate
            else:
                candidate["box"] = [old * .30 + new * .70 for old, new in zip(candidate["box"], box)]
                candidate.update(last_seen=now, danger=danger, confidence=detection["confidence"])
            if zone and candidate["zone_id"] != zone["id"]:
                zone["events"] += 1
                events.append({"level": "high", "title": f"{zone['name']} entry",
                               "detail": f"{candidate['label']} entered a user-defined restricted space.",
                               "trackId": candidate["id"]})
            candidate["zone_id"] = zone["id"] if zone else None
            matched.add(candidate["id"])
            bx1, by1, bx2, by2 = candidate["box"]
            output.append({"id": candidate["id"], "label": candidate["label"],
                           "confidence": round(candidate["confidence"], 2),
                           "x": bx1, "y": by1, "w": bx2 - bx1, "h": by2 - by1, "danger": danger})
            if not zone and danger == "medium" and not candidate.get("alerted"):
                candidate["alerted"] = True
                events.append({"level": "medium", "title": f"Unfamiliar {candidate['label']} detected",
                               "detail": "This object differs from the camera's calibrated normal view. Review the feed for context.",
                               "trackId": candidate["id"]})
        for track_id in list(active):
            track = active[track_id]
            if track_id not in matched and now - track["last_seen"] <= 1.0:
                bx1, by1, bx2, by2 = track["box"]
                output.append({"id": track_id, "label": track["label"],
                               "confidence": round(track["confidence"], 2),
                               "x": bx1, "y": by1, "w": bx2 - bx1, "h": by2 - by1,
                               "danger": track["danger"], "stale": True})
            elif now - track["last_seen"] > 1.0:
                del active[track_id]
        return frame_result(100, output, events)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "OmniscientLocal"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, _format, *_args):
        return

    def security_headers(self):
        self.send_header("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(self), microphone=(), geolocation=(), usb=(), serial=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; upgrade-insecure-requests")

    def respond(self, code: int, body: bytes = b"", content_type: str = "application/json", extra: dict | None = None):
        self.send_response(code)
        self.security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def json(self, code: int, value, extra: dict | None = None):
        self.respond(code, json.dumps(value, separators=(",", ":")).encode(), extra=extra)

    def body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid content length")
        if length < 0 or length > MAX_BODY:
            raise ValueError("Request body is too large")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("Incomplete request body")
        return body

    def valid_host(self) -> bool:
        try:
            parsed = urllib.parse.urlsplit(f"//{self.headers.get('Host', '')}")
            return parsed.hostname is not None and parsed.hostname.lower() in ALLOWED_HOSTS and parsed.port == self.server.server_address[1]
        except ValueError:
            return False

    def session(self):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            token = jar.get("__Host-omni_session").value if jar.get("__Host-omni_session") else ""
        except http.cookies.CookieError:
            token = ""
        with STATE_LOCK:
            session = SESSIONS.get(token)
            if not session:
                return None, None
            now = time.monotonic()
            if now - session["last"] > SESSION_TTL:
                SESSIONS.pop(token, None)
                ANALYSIS.pop(token, None)
                return None, None
            session["last"] = now
            return token, session

    def authorized(self, csrf=False):
        token, session = self.session()
        if not session:
            self.respond(401, b"Unauthorized", "text/plain; charset=utf-8")
            return None
        if csrf and not hmac.compare_digest(self.headers.get("X-Omni-CSRF", ""), session["csrf"]):
            self.respond(403, b"Forbidden", "text/plain; charset=utf-8")
            return None
        return token, session

    def login_page(self, status=200, error=""):
        now = time.monotonic()
        with STATE_LOCK:
            for token, item in list(LOGIN_NONCES.items()):
                if item["expires"] <= now:
                    LOGIN_NONCES.pop(token, None)
            while len(LOGIN_NONCES) >= 128:
                LOGIN_NONCES.pop(next(iter(LOGIN_NONCES)))
            nonce = secrets.token_urlsafe(32)
            LOGIN_NONCES[nonce] = {"expires": now + 300, "host": self.headers.get("Host", "").lower()}
        cookie = f"__Host-omni_login={nonce}; Path=/; Max-Age=300; HttpOnly; Secure; SameSite=Strict"
        body = LOGIN_PAGE.replace("__ERROR__", error).replace("__NONCE__", nonce).encode()
        self.respond(status, body, "text/html; charset=utf-8", {"Set-Cookie": cookie})

    def do_GET(self):
        if not self.valid_host():
            self.respond(400, b"Invalid host", "text/plain; charset=utf-8"); return
        path = urllib.parse.urlsplit(self.path).path
        token, session = self.session()
        if path == "/login":
            if token:
                self.send_response(303); self.security_headers(); self.send_header("Location", "/"); self.send_header("Cache-Control", "no-store"); self.end_headers(); return
            self.login_page()
            return
        if path in STATIC:
            if not token:
                self.send_response(303); self.security_headers(); self.send_header("Location", "/login"); self.send_header("Cache-Control", "no-store"); self.end_headers(); return
            file_path = ROOT / STATIC[path]
            content_type = "text/html; charset=utf-8" if path.endswith("html") or path == "/" else "text/css; charset=utf-8" if path.endswith("css") else "text/javascript; charset=utf-8"
            self.respond(200, file_path.read_bytes(), content_type, {"X-Omni-CSRF": session["csrf"]})
            return
        if path == "/api/session":
            authorized = self.authorized()
            if authorized:
                self.json(200, {"csrf": authorized[1]["csrf"], "modelReady": MODEL_WORKER.available, "modelStatus": MODEL_WORKER.status})
            return
        if path == "/api/zones":
            if self.authorized():
                with STATE_LOCK: self.json(200, {"zones": ZONES})
            return
        self.respond(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self.valid_host():
            self.respond(400, b"Invalid host", "text/plain; charset=utf-8"); return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/login":
            self.login()
            return
        authorized = self.authorized(csrf=True)
        if not authorized:
            return
        token, _ = authorized
        if path == "/api/frame":
            if self.headers.get("Content-Type", "").split(";", 1)[0].lower() != "image/jpeg":
                self.json(415, {"error": "Expected a JPEG camera frame"}); return
            try:
                result = analyze_frame(token, self.body())
            except ValueError as exc:
                self.json(400, {"error": str(exc)}); return
            except ModelBusy:
                self.json(429, {"busy": True}, {"Retry-After": "1"}); return
            except ModelFailure:
                self.json(503, {"error": "Local model inference failed"}); return
            self.json(200, result); return
        try:
            payload = json.loads(self.body() or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.json(400, {"error": "Invalid request"}); return
        if not isinstance(payload, dict):
            self.json(400, {"error": "Invalid request"}); return
        if path == "/api/calibrate":
            with STATE_LOCK: ANALYSIS.pop(token, None)
            self.json(200, {"ok": True}); return
        if path == "/api/logout":
            with STATE_LOCK:
                SESSIONS.pop(token, None)
                ANALYSIS.pop(token, None)
            self.respond(200, b'{"ok":true}', extra={"Set-Cookie": "__Host-omni_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"}); return
        if path == "/api/zones":
            name = str(payload.get("name", "Restricted space"))[:48]
            points = payload.get("points")
            if not isinstance(points, list) or not 3 <= len(points) <= 32:
                self.json(400, {"error": "A restricted space needs 3 to 32 points"}); return
            try:
                clean_points = [{"x": float(point["x"]), "y": float(point["y"])} for point in points]
            except (TypeError, ValueError, KeyError):
                self.json(400, {"error": "Invalid polygon points"}); return
            if any(not 0 <= p["x"] <= 1 or not 0 <= p["y"] <= 1 for p in clean_points):
                self.json(400, {"error": "Polygon points must be inside the video frame"}); return
            with STATE_LOCK:
                if len(ZONES) >= 64:
                    self.json(400, {"error": "Maximum of 64 restricted spaces reached"}); return
                item = {"id": secrets.randbits(52), "name": name, "points": clean_points, "events": 0}
                ZONES.append(item); write_json_private(ZONES_FILE, ZONES)
                self.json(201, {"zone": item, "zones": ZONES}); return
        self.respond(404, b"Not found", "text/plain; charset=utf-8")

    def do_DELETE(self):
        if not self.valid_host():
            self.respond(400, b"Invalid host", "text/plain; charset=utf-8"); return
        if not self.authorized(csrf=True): return
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/zones/"):
            try: zone_id = int(path.rsplit("/", 1)[1])
            except ValueError: self.json(400, {"error": "Invalid zone"}); return
            with STATE_LOCK:
                ZONES[:] = [zone for zone in ZONES if zone["id"] != zone_id]
                write_json_private(ZONES_FILE, ZONES)
                self.json(200, {"zones": ZONES}); return
        self.respond(404, b"Not found", "text/plain; charset=utf-8")

    def login(self):
        ip = self.client_address[0]
        now = time.monotonic()
        if len(LOGIN_FAILURES) >= 4096:
            for address, (_attempts, lock_expiry) in list(LOGIN_FAILURES.items()):
                if lock_expiry <= now:
                    LOGIN_FAILURES.pop(address, None)
                if len(LOGIN_FAILURES) < 2048:
                    break
        if len(LOGIN_FAILURES) >= 4096 and ip not in LOGIN_FAILURES:
            self.respond(429, b"Authentication temporarily unavailable", "text/plain; charset=utf-8")
            return
        failures, locked_until = LOGIN_FAILURES.get(ip, (0, 0.0))
        if locked_until > now:
            self.login_page(429, "Too many attempts. Try again later.")
            return
        try:
            fields = urllib.parse.parse_qs(self.body().decode("utf-8"), strict_parsing=True)
        except (ValueError, UnicodeDecodeError):
            self.respond(400, b"Invalid request", "text/plain; charset=utf-8"); return
        password = fields.get("password", [""])[0]
        submitted_nonce = fields.get("login_nonce", [""])[0]
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            cookie_nonce = cookie.get("__Host-omni_login").value if cookie.get("__Host-omni_login") else ""
        except http.cookies.CookieError:
            cookie_nonce = ""
        with STATE_LOCK:
            nonce_record = LOGIN_NONCES.pop(cookie_nonce, None)
        if (not nonce_record or nonce_record["expires"] <= now
                or nonce_record["host"] != self.headers.get("Host", "").lower()
                or not hmac.compare_digest(cookie_nonce, submitted_nonce)):
            self.login_page(403, "The sign-in form expired or the browser blocked its local cookie. Refresh this page and try again.")
            return
        if len(password) > 1024:
            password = ""
        if password_matches(password, read_json(AUTH_FILE, {})):
            with STATE_LOCK:
                for old_token, session in list(SESSIONS.items()):
                    if now - session["last"] > SESSION_TTL:
                        SESSIONS.pop(old_token, None)
                        ANALYSIS.pop(old_token, None)
                if len(SESSIONS) >= 8:
                    full = True
                else:
                    full = False
                    LOGIN_FAILURES.pop(ip, None)
                    token = secrets.token_urlsafe(32)
                    SESSIONS[token] = {"csrf": secrets.token_urlsafe(32), "last": now}
            if full:
                self.login_page(429, "Maximum active sessions reached. Sign out elsewhere first.")
                return
            cookie = f"__Host-omni_session={token}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; Secure; SameSite=Strict"
            self.send_response(303); self.security_headers(); self.send_header("Location", "/"); self.send_header("Set-Cookie", cookie); self.send_header("Set-Cookie", "__Host-omni_login=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"); self.send_header("Cache-Control", "no-store"); self.end_headers(); return
        failures += 1
        locked_until = now + 10 * 60 if failures >= 5 else 0.0
        LOGIN_FAILURES[ip] = (0 if locked_until else failures, locked_until)
        error = "Incorrect password." if not locked_until else "Too many attempts. Try again in 10 minutes."
        self.login_page(401, error)


LOGIN_PAGE = """<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta name=\"color-scheme\" content=\"dark\"><title>Omniscient · Local sign in</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1119;color:#e7edf0;font:15px system-ui,sans-serif}.card{width:min(360px,calc(100% - 48px));padding:28px;background:#111a24;border:1px solid #1e2b37;border-radius:8px}.mark{color:#48d6c5;font-size:24px}.sub{color:#8797a0;font-size:13px;line-height:1.6}.local{font-size:10px;color:#64c69b;letter-spacing:1px;margin:20px 0}.error{color:#ef8a80;min-height:20px;font-size:12px}label{font-size:12px;color:#a6b5bb}input{display:block;width:100%;box-sizing:border-box;margin:7px 0 14px;padding:12px;background:#0b1119;border:1px solid #31434f;border-radius:4px;color:#fff}button{width:100%;padding:11px;background:#43c5b3;border:0;border-radius:4px;color:#08201e;font-weight:700;cursor:pointer}</style><main class=\"card\"><div class=\"mark\">◉ omniscient</div><p class=\"sub\">Sign in to your private, locally hosted security console.</p><div class=\"local\">◇ &nbsp; LOCAL SERVER · ENCRYPTED CONNECTION</div><form method=\"post\" action=\"/login\"><input type=\"hidden\" name=\"login_nonce\" value=\"__NONCE__\"><label for=\"password\">Administrator password</label><input id=\"password\" name=\"password\" type=\"password\" autocomplete=\"current-password\" minlength=\"16\" required autofocus><div class=\"error\">__ERROR__</div><button type=\"submit\">Unlock console</button></form></main></html>"""


class BoundedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, handler):
        self._request_slots = threading.BoundedSemaphore(12)
        self._last_housekeeping = time.monotonic()
        super().__init__(address, handler)

    def service_actions(self):
        now = time.monotonic()
        if now - self._last_housekeeping < 30:
            return
        self._last_housekeeping = now
        MODEL_WORKER.unload_if_idle()
        with STATE_LOCK:
            for token, session in list(SESSIONS.items()):
                if now - session["last"] > SESSION_TTL:
                    SESSIONS.pop(token, None)
                    ANALYSIS.pop(token, None)
            for token, item in list(LOGIN_NONCES.items()):
                if item["expires"] <= now:
                    LOGIN_NONCES.pop(token, None)

    def server_close(self):
        MODEL_WORKER.close()
        super().server_close()

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            request.shutdown(socket.SHUT_RDWR)
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def main():
    global ZONES, ALLOWED_HOSTS
    ensure_password()
    loaded_zones = read_json(ZONES_FILE, [])
    if ZONES_FILE.exists():
        os.chmod(ZONES_FILE, 0o600)
    ZONES = []
    if isinstance(loaded_zones, list):
        for zone in loaded_zones[:64]:
            try:
                points = [{"x": float(point["x"]), "y": float(point["y"])} for point in zone["points"]]
                if (isinstance(zone.get("name"), str) and len(points) >= 3 and len(points) <= 32
                        and all(0 <= point["x"] <= 1 and 0 <= point["y"] <= 1 for point in points)):
                    ZONES.append({"id": int(zone["id"]), "name": zone["name"][:48], "points": points, "events": max(0, int(zone.get("events", 0)))})
            except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
                continue
    cert, key, ca_cert = ensure_tls()
    bind = os.environ.get("OMNI_BIND", "127.0.0.1")
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        raise SystemExit("OMNI_BIND must be an IP address. Default is loopback-only (127.0.0.1).")
    port = int(os.environ.get("OMNI_PORT", "8443"))
    ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", bind.lower(), socket.gethostname().lower()}
    ALLOWED_HOSTS.update(extra_hostnames())
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ALLOWED_HOSTS.add(info[4][0].split("%", 1)[0])
    except OSError:
        pass
    server = BoundedHTTPServer((bind, port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_NO_COMPRESSION
    if hasattr(ssl, "OP_NO_RENEGOTIATION"):
        context.options |= ssl.OP_NO_RENEGOTIATION
    context.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    fingerprint = subprocess.check_output([__import__("shutil").which("openssl"), "x509", "-in", str(ca_cert), "-noout", "-fingerprint", "-sha256"], text=True).strip()
    print(f"Omniscient listening at https://{bind}:{port}")
    print(f"Install this private CA on trusted LAN clients to remove TLS warnings: {ca_cert}")
    print(f"Verify its fingerprint out-of-band: {fingerprint}")
    print("Loopback-only by default. No telemetry, external requests, cloud APIs, or HTTP fallback.")
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        print("\nShutting down Omniscient.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
