#!/usr/bin/env python3
"""Local-only HTTPS and motion-analysis service for Omniscient.

Frames are analyzed in memory and discarded. The service has no cloud endpoints,
telemetry, third-party dependencies, or public account-registration route.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookies
import http.server
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

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TLS_DIR = DATA / "tls"
AUTH_FILE = DATA / "auth.json"
ZONES_FILE = DATA / "restricted_spaces.json"
STATIC = {"/": "index.html", "/index.html": "index.html", "/styles.css": "styles.css", "/app.js": "app.js"}
FRAME_W, FRAME_H = 40, 22
FRAME_SIZE = FRAME_W * FRAME_H
MAX_BODY = 24_000
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


def _analyze_frame(session_id: str, encoded: str) -> dict:
    try:
        pixels = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ValueError("Invalid frame encoding")
    if len(pixels) != FRAME_SIZE:
        raise ValueError("Unexpected frame size")
    state = ANALYSIS.setdefault(session_id, {"previous": None, "baseline_sum": [0] * FRAME_SIZE, "baseline_frames": 0, "baseline": None, "tracks": {}, "next_id": 1, "anomaly_since": 0.0, "anomaly_alerted": False})
    now = time.time()
    if state["baseline"] is None:
        state["baseline_frames"] += 1
        for i, value in enumerate(pixels):
            state["baseline_sum"][i] += value
        if state["baseline_frames"] >= 30:
            n = state["baseline_frames"]
            state["baseline"] = bytes(round(value / n) for value in state["baseline_sum"])
            state["previous"] = pixels
            return {"calibration": 100, "objects": [], "events": [{"level": "info", "title": "Normal view calibrated", "detail": "Omniscient learned the current scene. Keep the camera fixed for reliable change detection."}], "zoneEvents": [{"id": zone["id"], "events": zone["events"]} for zone in ZONES]}
        return {"calibration": round(state["baseline_frames"] / 30 * 100), "objects": [], "events": [], "zoneEvents": [{"id": zone["id"], "events": zone["events"]} for zone in ZONES]}

    baseline = state["baseline"]
    anomaly = bytearray(FRAME_SIZE)
    changed = 0
    for i, value in enumerate(pixels):
        if abs(value - baseline[i]) > 44:
            anomaly[i] = 1
            changed += 1
    events = []
    if changed > FRAME_SIZE * .045:
        if not state["anomaly_since"]:
            state["anomaly_since"] = now
        if not state["anomaly_alerted"] and now - state["anomaly_since"] > 2.5:
            level = "high" if changed > FRAME_SIZE * .16 else "medium"
            events.append({"level": level, "title": "Scene changed from normal view", "detail": f"{round(changed / FRAME_SIZE * 100)}% of the image differs from calibration. Review for a moved or newly present object."})
            state["anomaly_alerted"] = True
    else:
        state["anomaly_since"] = 0.0
        state["anomaly_alerted"] = False

    previous = state["previous"] or pixels
    moving = bytearray(FRAME_SIZE)
    moving_count = 0
    for i, value in enumerate(pixels):
        if abs(value - previous[i]) > 25:
            moving[i] = 1
            moving_count += 1
    state["previous"] = pixels
    boxes = []
    if moving_count <= FRAME_SIZE * .46:
        visited = bytearray(FRAME_SIZE)
        for start in range(FRAME_SIZE):
            if not moving[start] or visited[start]:
                continue
            queue = [start]
            visited[start] = 1
            min_x = min_y = FRAME_W
            max_x = max_y = count = sum_x = sum_y = 0
            cursor = 0
            while cursor < len(queue):
                index = queue[cursor]
                cursor += 1
                x, y = index % FRAME_W, index // FRAME_W
                count += 1
                min_x, min_y = min(min_x, x), min(min_y, y)
                max_x, max_y = max(max_x, x), max(max_y, y)
                sum_x += x
                sum_y += y
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        nx, ny = x + dx, y + dy
                        if (dx or dy) and 0 <= nx < FRAME_W and 0 <= ny < FRAME_H:
                            ni = ny * FRAME_W + nx
                            if moving[ni] and not visited[ni]:
                                visited[ni] = 1
                                queue.append(ni)
            if count >= 5 and max_x - min_x >= 1 and max_y - min_y >= 1:
                boxes.append({"x": min_x, "y": min_y, "w": max_x - min_x + 1, "h": max_y - min_y + 1, "cx": sum_x / count, "cy": sum_y / count})
    boxes.sort(key=lambda box: (box["w"] * box["h"]), reverse=True)
    active, matched, output = state["tracks"], set(), []
    for box in boxes[:8]:
        candidate, distance = None, 14.0
        for track_id, track in active.items():
            if track_id in matched:
                continue
            d = ((track["cx"] - box["cx"]) ** 2 + (track["cy"] - box["cy"]) ** 2) ** .5
            if d < distance:
                candidate, distance = track, d
        center = (box["cx"] / FRAME_W, box["cy"] / FRAME_H)
        zone = next((item for item in ZONES if point_in_polygon(center, item["points"])), None)
        area_cells = [(x, y) for y in range(box["y"], min(FRAME_H, box["y"] + box["h"])) for x in range(box["x"], min(FRAME_W, box["x"] + box["w"]))]
        overlap = sum(anomaly[y * FRAME_W + x] for x, y in area_cells) / max(1, len(area_cells))
        danger = "high" if zone else "medium" if overlap > .3 else "low"
        if candidate is None:
            track_id = state["next_id"]
            state["next_id"] += 1
            candidate = {"id": track_id, "cx": box["cx"], "cy": box["cy"], "born": now, "last_seen": now, "dwell_alerted": False, "zone_id": None}
            active[track_id] = candidate
            if zone:
                zone["events"] += 1
                events.append({"level": "high", "title": f"{zone['name']} entry", "detail": "Object entered a user-defined restricted space. Review the feed for context.", "trackId": track_id})
            elif danger == "medium":
                events.append({"level": "medium", "title": "Unusual object movement", "detail": "This track overlaps a region that differs from the calibrated normal view.", "trackId": track_id})
        else:
            if zone and candidate["zone_id"] != zone["id"]:
                zone["events"] += 1
                events.append({"level": "high", "title": f"{zone['name']} entry", "detail": "Object entered a user-defined restricted space. Review the feed for context.", "trackId": candidate["id"]})
            if not zone and not candidate["dwell_alerted"] and now - candidate["born"] > 8:
                candidate["dwell_alerted"] = True
                if danger == "low":
                    danger = "medium"
                events.append({"level": "medium", "title": "Extended presence", "detail": "Object remained visible for more than 8 seconds. Review whether the activity is expected.", "trackId": candidate["id"]})
            candidate.update(cx=box["cx"], cy=box["cy"], last_seen=now)
        candidate["zone_id"] = zone["id"] if zone else None
        matched.add(candidate["id"])
        output.append({"id": candidate["id"], "x": box["x"] / FRAME_W, "y": box["y"] / FRAME_H, "w": box["w"] / FRAME_W, "h": box["h"] / FRAME_H, "danger": danger})
    for track_id in list(active):
        if now - active[track_id]["last_seen"] > 1.2:
            del active[track_id]
    return {"calibration": 100, "objects": output, "events": events, "zoneEvents": [{"id": zone["id"], "events": zone["events"]} for zone in ZONES]}


def analyze_frame(session_id: str, encoded: str) -> dict:
    with STATE_LOCK:
        return _analyze_frame(session_id, encoded)


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
        return self.rfile.read(length)

    def same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return self.headers.get("Sec-Fetch-Site", "same-origin") == "same-origin"
        try:
            origin_url = urllib.parse.urlsplit(origin)
            host_url = urllib.parse.urlsplit(f"//{self.headers.get('Host', '')}")
            origin_port = origin_url.port or (443 if origin_url.scheme.lower() == "https" else 80)
            return (
                origin_url.scheme.lower() == "https"
                and origin_url.hostname is not None
                and host_url.hostname is not None
                and hmac.compare_digest(origin_url.hostname.lower(), host_url.hostname.lower())
                and origin_port == self.server.server_address[1]
                and not origin_url.path
                and not origin_url.query
                and not origin_url.fragment
            )
        except ValueError:
            return False

    def valid_host(self) -> bool:
        try:
            parsed = urllib.parse.urlsplit(f"//{self.headers.get('Host', '')}")
            return parsed.hostname is not None and parsed.hostname.lower() in {host.lower() for host in ALLOWED_HOSTS} and parsed.port == self.server.server_address[1]
        except ValueError:
            return False

    def session(self):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            token = jar.get("__Host-omni_session").value if jar.get("__Host-omni_session") else ""
        except http.cookies.CookieError:
            token = ""
        session = SESSIONS.get(token)
        if not session:
            return None, None
        if time.monotonic() - session["last"] > SESSION_TTL:
            SESSIONS.pop(token, None)
            ANALYSIS.pop(token, None)
            return None, None
        session["last"] = time.monotonic()
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
        token, _ = self.session()
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
            self.respond(200, file_path.read_bytes(), content_type, {"X-Omni-CSRF": SESSIONS[token]["csrf"]})
            return
        if path == "/api/session":
            authorized = self.authorized()
            if authorized:
                self.json(200, {"csrf": authorized[1]["csrf"], "serverAnalysis": True, "frameWidth": FRAME_W, "frameHeight": FRAME_H})
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
        try:
            payload = json.loads(self.body() or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.json(400, {"error": "Invalid request"}); return
        if not isinstance(payload, dict):
            self.json(400, {"error": "Invalid request"}); return
        if path == "/api/frame":
            try:
                result = analyze_frame(token, payload.get("pixels", ""))
            except ValueError as exc:
                self.json(400, {"error": str(exc)}); return
            self.json(200, result); return
        if path == "/api/calibrate":
            ANALYSIS.pop(token, None)
            self.json(200, {"ok": True}); return
        if path == "/api/logout":
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
        nonce_record = LOGIN_NONCES.pop(cookie_nonce, None)
        if (not nonce_record or nonce_record["expires"] <= now
                or nonce_record["host"] != self.headers.get("Host", "").lower()
                or not hmac.compare_digest(cookie_nonce, submitted_nonce)):
            self.login_page(403, "The sign-in form expired or the browser blocked its local cookie. Refresh this page and try again.")
            return
        if len(password) > 1024:
            password = ""
        if password_matches(password, read_json(AUTH_FILE, {})):
            for old_token, session in list(SESSIONS.items()):
                if now - session["last"] > SESSION_TTL:
                    SESSIONS.pop(old_token, None)
                    ANALYSIS.pop(old_token, None)
            if len(SESSIONS) >= 8:
                self.login_page(429, "Maximum active sessions reached. Sign out elsewhere first.")
                return
            LOGIN_FAILURES.pop(ip, None)
            token = secrets.token_urlsafe(32)
            SESSIONS[token] = {"csrf": secrets.token_urlsafe(32), "last": now}
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
        self._request_slots = threading.BoundedSemaphore(24)
        super().__init__(address, handler)

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
    ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", socket.gethostname()}
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
