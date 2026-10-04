# Omniscient

Omniscient is a private, local-first foundation for a home security camera hub. The app is served by its own HTTPS server, requires an administrator password, and has no cloud API, analytics, remote font, CDN, or telemetry integration.

## Start the local server

From this directory, run:

```sh
python3 server.py
```

On first run, create an administrator password of at least 16 characters. The server stores only a PBKDF2-SHA256 password hash. It also creates a private local certificate authority and a server certificate under the ignored `data/` directory. Open `https://localhost:8443` and sign in. The browser will warn about the private CA until you install `data/tls/local-ca.crt` into the trusted certificate store on that computer; verify its fingerprint with `openssl x509 -in data/tls/local-ca.crt -noout -fingerprint -sha256` and compare it with the value printed by the server.

The server binds to `127.0.0.1` by default. This keeps the console accessible only from the server computer. To allow selected devices on the home LAN, bind to the server's LAN IP explicitly:

```sh
OMNI_BIND=192.168.1.20 python3 server.py
```

Replace the example address with the server's reserved LAN address. Set `OMNI_ALLOWED_HOSTS` before the first run if you want a local DNS name, for example `OMNI_ALLOWED_HOSTS=omniscient.local OMNI_BIND=192.168.1.20 python3 server.py`; this name is included in the generated certificate. Install and trust the local CA on each allowed client, use a matching server hostname or IP in the URL, and restrict port 8443 at the host firewall to trusted devices. Do not forward the port from the internet or expose it through a public reverse proxy.

## Security boundaries

- TLS 1.2 or later protects browser-to-server traffic. There is no plaintext HTTP listener or HTTP fallback.
- The console and APIs require a session cookie marked Secure, HttpOnly, and SameSite=Strict. Sessions expire after eight hours; state-changing APIs require same-origin and CSRF checks. Five failed password attempts lock that client address for ten minutes.
- Static files are allowlisted. Responses use a restrictive Content Security Policy and browser security headers. External assets have been removed.
- Video is not saved. For the current prototype, the browser decodes a selected local video or camera source and sends only a tiny grayscale frame sample to the authenticated server over HTTPS. Motion detection, baseline comparison, track association, danger scoring, and restricted-space checks run on the server; frame samples are discarded after in-memory analysis.
- The server stores the password hash, the local TLS CA/server keys, and restricted-space polygons under `data/`, with private filesystem permissions. It does not store recordings or camera passwords. Use full-disk encryption on the host to protect the CA private key and configuration at rest.

## Camera hub status

The current source picker uses a camera available to the browser or a local video file. It does not yet ingest IP cameras directly over RTSP/ONVIF. This host has no local video decoder installed, so connecting an IP camera requires a separately reviewed local ingest service. The intended design is to run that service on this same host, keep camera credentials in an encrypted local secret store, and expose authenticated camera controls only through this HTTPS server. No camera feed should be routed through a cloud service.

Object labels and danger ratings are heuristic motion analysis, not reliable person or object identification. Do not use the prototype as a safety-rated alarm system.
