# Omniscient

Omniscient is a local-first camera-monitoring prototype. Its first object-recognition path uses Peking University's RT-DETRv2 R50 model, packaged as Safetensors and loaded from disk only. It recognizes COCO classes, draws persistent class-labeled boxes, assigns local risk levels from baseline changes and restricted-space entries, and keeps short-lived tracks through brief missed detections.

## One-time vision setup

Model/runtime setup needs an internet connection to download Python packages and the pinned 172 MB model from Hugging Face. Camera frames are not part of that setup. Install these components during trusted provisioning:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-vision.txt
python setup_model.py
```

The model files go under ignored `data/models/`. The setup script pins an immutable model repository revision and downloads only its JSON configuration, image processor configuration, and Safetensors weights. It verifies the downloaded 172 MB weights against the publisher's SHA-256 value before use. Review the model license before redistribution.

## Start the local server

```sh
. .venv/bin/activate
python server.py
```

On first run, create an administrator password of at least 16 characters. The server stores only a PBKDF2-SHA256 password hash. It also creates a private local certificate authority and a server certificate under ignored `data/`. Open `https://localhost:8443` and sign in. The browser will warn about the private CA until you install `data/tls/local-ca.crt` into the trusted certificate store on that computer; verify its fingerprint with `openssl x509 -in data/tls/local-ca.crt -noout -fingerprint -sha256` and compare it with the value printed by the server.

If the local model is missing or fails to load, the dashboard reports that state and does not substitute motion blobs for labeled detections.

The server binds to `127.0.0.1` by default. To allow selected devices on the home LAN, bind to the server's reserved LAN IP explicitly:

```sh
OMNI_BIND=192.168.1.20 python server.py
```

For a local DNS name, set `OMNI_ALLOWED_HOSTS` before first run, for example `OMNI_ALLOWED_HOSTS=omniscient.local OMNI_BIND=192.168.1.20 python server.py`; the name is included in the generated certificate. Install and trust the local CA on each allowed client, use a matching name or IP in the URL, and restrict port 8443 at the host firewall to trusted devices. Do not forward this port to the internet or put it behind a public reverse proxy.

## Local data and security boundaries

- Browser-to-server traffic uses TLS 1.2 or later. There is no plaintext HTTP listener or cloud inference fallback.
- Sessions use Secure, HttpOnly, SameSite=Strict cookies, eight-hour expiry, CSRF checks, and per-address login throttling. Static assets are allowlisted and protected by a restrictive CSP and browser security headers.
- The browser decodes the selected local video or camera feed and sends resized JPEG frames through the authenticated HTTPS endpoint. RT-DETRv2 inference, class-aware track association, normal-scene comparison, danger scoring, and restricted-space checks run on the Omniscient host. Frames and events are currently held in memory and discarded at logout/server shutdown; video is not recorded.
- At runtime, Transformers is set to offline mode and model loading uses `local_files_only=True`. There are no analytics, cloud APIs, remote fonts, or runtime model downloads. The one-time package/model setup above does make outbound requests for software and model files.
- Password/configuration and TLS private-key files under `data/` have private filesystem permissions. Use full-disk encryption and protected backups on the host. TLS protects data in transit; it does not encrypt server memory or the model and configuration files at rest.

## Prototype limits

The UI currently analyzes one browser-provided live camera or uploaded clip at a time. It does not yet manage multiple feeds or ingest RTSP/ONVIF cameras directly. The model recognizes its fixed COCO class list; it will not name arbitrary tools, unknown objects, or identify people. The normal-view comparison is a small grayscale scene baseline and its danger rating is a review aid, not a guarantee of threat. Validate camera coverage, lighting, and thresholds before relying on alerts. This prototype is not a safety-rated alarm system.
