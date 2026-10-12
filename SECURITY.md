# Security notes

Omniscient is local-first. The service has no telemetry or third-party runtime calls. Review network egress and host firewall rules as part of any deployment.

## Deployment defaults

- Bind to `127.0.0.1`; remote clients cannot connect unless `OMNI_BIND` is explicitly set.
- Do not publish the port through a router, internet-facing proxy, or tunnel.
- For LAN use, reserve the server's IP, restrict the host firewall to trusted clients, install the generated private CA only on those clients, and verify the CA fingerprint printed at server startup.
- Keep `data/` private and enable full-disk encryption. The private CA key can issue server certificates and must be protected like an administrator credential.
- Model/runtime provisioning is the only intended outbound setup step. Once installed, the server sets Transformers/Hugging Face offline flags and loads the pinned Safetensors model from `data/models/`; camera frames are never submitted to a model provider.
- Do not expose port 8443 to the public internet. For LAN access, trust the private CA on each client and restrict the server host firewall to known devices.
- Never copy `data/` into a public backup or source-control repository. It is ignored by Git.

## Data flow

The current prototype captures and decodes frames in the browser because no server-side video decoder is included. It resizes each frame and posts JPEG bytes to the same-origin HTTPS server. The server derives a 880-byte grayscale sample for calibration and scene-change checks, and passes the JPEG through a local process pipe for object inference. Frames and samples are not written to disk. This is local network traffic, not cloud traffic. Direct RTSP ingestion and an encrypted camera-secret store are not implemented yet; do not enter camera credentials into the browser prototype.

## Authentication and transport

First-run provisioning is interactive on the server terminal. The password is hashed with PBKDF2-HMAC-SHA256, a random 256-bit salt, and 600,000 iterations. There is no web signup or password-reset endpoint. Sessions are random, in-memory, idle-expiring tokens; cookies are `__Host-` prefixed, Secure, HttpOnly, and SameSite=Strict. Login forms use a one-time nonce cookie; state-changing APIs require a random session CSRF token. Host headers are allowlisted, request bodies and client threads are bounded, and the service refuses to start if it cannot create TLS certificates.

If the administrator password is lost, stop the service and remove only `data/auth.json`, then restart and provision a new password. Preserve the TLS CA if its trust relationship should remain in place. To rotate the local CA, stop the service, back up any needed settings, remove `data/tls/`, and restart; reinstall the new CA on trusted clients.

## Spatial tracking module

Disabled unless `OMNI_SPATIAL_SITE` is set. Its `/api/spatial/*` endpoints are read-only, require the same session as the rest of the console, and take no file paths or free-form input from clients (unit IDs are matched against the loaded configuration). Site configuration is read from a local directory; paths must stay inside it, schema versions are enforced, and file sizes and point counts are bounded. Track IDs are anonymous and sequential per run; the module performs no identification. Track history is in-memory only (default 10 s, configurable, 0 disables) and nothing is written to disk; the configuration refuses video retention and persisted tracks because they are not implemented. The map and calibration files describe your space and your sensors' placement: keep the site directory private like `data/`. Calibration files are trusted configuration; an incorrect or stale calibration makes overlays wrong, which the UI shows as calibration status but cannot detect automatically.

## Setup wizard and map uploads

The setup API (`/api/setup/*`) uses the same session, CSRF and Host checks as the rest of the console. Map uploads are accepted only for a fixed set of extensions, are limited to 120 MB (a declared oversize body is refused before it is read), are streamed to a private file under `data/spatial/sites/<id>/` and parsed by the project's own PLY/XYZ readers; no archive, script or other format is ever executed or unpacked, and failed uploads leave nothing behind. Site identifiers come from a strict pattern and are looked up under that directory, never joined from request paths. Drafts, maps and calibration files are written with owner-only permissions, so keep `data/` private and encrypted like the rest of it. The map and calibrations describe your premises and sensor placement. Deleting a space deletes its folder, including history. Drafts for simulated setups contain the simulator's hidden truth values; those are never sent to the browser.
