"""Two sites on one IP address never share an encrypted connection.

The httpx fallback fetcher sends each request to the IP address that was
checked, with the site's own name as the TLS server name. Its connection
pool therefore knew a connection by address only. Before, a second site on
the same address (shared hosting, a CDN) fetched within the keep-alive
window rode the connection opened for the first: its certificate was never
checked for the second site's name, and whatever the server answered was
parsed as the second company's page.

A real TLS server on this machine stands in for the shared address; the
resolver and the address check are replaced so that loopback counts as
public here.
"""

import datetime
import functools
import http.server
import ipaddress
import ssl
import threading

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import modules.search as search_mod
from modules.search import _HttpxFetcher


@pytest.fixture
def shared_address(tmp_path, monkeypatch):
    """A TLS server whose certificate names first.test only, reached under
    any name. Yields its port and the server names it was asked for."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "first.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("first.test")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    pem = tmp_path / "first.pem"
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        + cert.public_bytes(serialization.Encoding.PEM)
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            body = b"the first company's page"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    handshakes = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pem)
    context.sni_callback = lambda sock, server_name, ctx: handshakes.append(server_name)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    monkeypatch.setattr(
        search_mod.httpx,
        "HTTPTransport",
        functools.partial(
            httpx.HTTPTransport, verify=ssl.create_default_context(cafile=str(pem))
        ),
    )
    monkeypatch.setattr(search_mod, "_special_kind", lambda address: "")
    monkeypatch.setattr(
        search_mod, "resolve_host", lambda host: [ipaddress.ip_address("127.0.0.1")]
    )
    yield server.server_address[1], handshakes
    server.shutdown()
    server.server_close()


def test_a_second_site_on_the_same_address_gets_its_own_certificate_check(
    shared_address,
):
    """first.test is fetched, then second.test at the same address straight
    after. The certificate names first.test only, so the second fetch must
    fail its certificate check; it must not be answered over the first
    site's connection."""
    port, handshakes = shared_address
    fetcher = _HttpxFetcher({}, timeout=10.0)
    try:
        assert fetcher.get(f"https://first.test:{port}/").status_code == 200
        with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
            fetcher.get(f"https://second.test:{port}/")
    finally:
        fetcher.close()
    assert handshakes == ["first.test", "second.test"]


def test_the_same_site_is_still_fetched_twice(shared_address):
    port, handshakes = shared_address
    fetcher = _HttpxFetcher({}, timeout=10.0)
    try:
        for path in ("/", "/contact"):
            assert fetcher.get(f"https://first.test:{port}{path}").status_code == 200
    finally:
        fetcher.close()
    assert handshakes == ["first.test", "first.test"]
