from __future__ import annotations

import ipaddress
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from video_uploader import token_store
from video_uploader.publishers.facebook import complete_oauth

# Meta requires HTTPS for OAuth redirect URIs with no way to disable that
# for this app (unlike Google, which exempts loopback addresses). Rather
# than force the whole local app onto HTTPS -- which would also mean
# re-registering YouTube's already-working http:// redirect URI in Google
# Cloud Console for no reason -- this is a tiny, separate HTTPS-only
# listener used solely to receive Facebook's OAuth callback, using a
# self-signed cert generated on first use. The browser will show a
# one-time "not secure" warning to click through; that's expected for a
# local self-signed dev cert and is isolated to this one connect step.
HTTPS_CATCHER_PORT = 8443
CALLBACK_PATH = "/oauth/facebook/callback"
# Meta's App Domains validator rejects both raw IP addresses (127.0.0.1)
# and bare hostnames without a TLD (localhost) -- confirmed live, both
# were rejected with "Must contain a top level domain". The workaround is
# a fake local domain mapped to 127.0.0.1 via the OS hosts file
# (`127.0.0.1 video-uploader.local`), registered as both the App Domain
# and the OAuth redirect URI's host in Meta's dashboard.
OAUTH_DOMAIN = "video-uploader.local"
CALLBACK_URL = f"https://{OAUTH_DOMAIN}:{HTTPS_CATCHER_PORT}{CALLBACK_PATH}"
MAIN_APP_SETTINGS_URL = "http://127.0.0.1:8000/settings"

CERT_DIR = Path("data")
CERT_PATH = CERT_DIR / "oauth_catcher_cert.pem"
KEY_PATH = CERT_DIR / "oauth_catcher_key.pem"

# CSRF guard shared with the main app's /oauth/facebook/start route (same
# process, different thread -- fine without a lock for this low-frequency,
# effectively-serial single-user flow).
pending_states: set[str] = set()


def _ensure_cert() -> None:
    if CERT_PATH.exists() and KEY_PATH.exists():
        return
    CERT_DIR.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, OAUTH_DOMAIN)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(OAUTH_DOMAIN),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    KEY_PATH.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        state = params.get("state", [None])[0]
        code = params.get("code", [None])[0]
        error_description = params.get("error_description", [None])[0]

        try:
            if state not in pending_states:
                raise ValueError(
                    "Unknown or expired OAuth state -- please try connecting again from /settings."
                )
            pending_states.discard(state)
            if not code:
                raise ValueError(f"Facebook login failed: {error_description or 'no code returned'}")

            token_data = complete_oauth(code, redirect_uri=CALLBACK_URL)
            token_store.save_token("facebook", token_data)
        except Exception as exc:  # noqa: BLE001 - report to the browser, not a crash
            body = f"Facebook connection failed: {exc}".encode()
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(302)
        self.send_header("Location", MAIN_APP_SETTINGS_URL)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # keep console output limited to uvicorn's own logging


_server: HTTPServer | None = None


def start() -> None:
    global _server
    _ensure_cert()
    _server = HTTPServer(("127.0.0.1", HTTPS_CATCHER_PORT), _CallbackHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(CERT_PATH), keyfile=str(KEY_PATH))
    _server.socket = context.wrap_socket(_server.socket, server_side=True)
    thread = threading.Thread(target=_server.serve_forever, daemon=True)
    thread.start()


def shutdown() -> None:
    if _server is not None:
        _server.shutdown()
