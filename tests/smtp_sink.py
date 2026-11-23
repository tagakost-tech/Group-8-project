"""A real SMTP server for tests, so the send path talks to a socket.

Every existing test fakes sending by replacing `_deliver` or `dispatch_to_contact`
with something that returns a tuple. That exercises the bookkeeping but never
the code that MATTERS most: the part deciding whether a server's answer means
"this address is dead, suppress it forever" or "try again in ten minutes". That
decision is made from real smtplib exceptions and real response codes, and it
had never seen either.

Getting it wrong is expensive in both directions. Treating a transient 4xx as
permanent suppresses a live prospect with no undo - this codebase once retired
235 good contacts that way. Treating a permanent 5xx as transient means asking
the same server the same question until the domain's reputation suffers.

The certificate is generated in-process, per run, into a temp directory. No key
material is written to the repo. EmailSender always issues STARTTLS on any port
but 465, so a plaintext sink cannot be used.
"""

from __future__ import annotations

import datetime
import socket
import ssl
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path


def _self_signed_cert() -> tuple[str, str]:
    """A throwaway localhost cert. Returns (certfile, keyfile)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    d = Path(tempfile.mkdtemp(prefix="smtp-sink-"))
    certfile, keyfile = d / "cert.pem", d / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(certfile), str(keyfile)


@dataclass
class SmtpSink:
    """A scriptable SMTP server on 127.0.0.1.

    Every reply the sender cares about can be scripted:
        auth_response   what AUTH gets ("235 ok" / "535 auth failed")
        rcpt_responses  per-recipient reply, keyed by address, e.g.
                        {"dead@x.de": "550 5.1.1 unknown user"}
        data_response   what the message body gets after the final dot
        drop_on_data    close the socket mid-DATA instead of replying
    """

    auth_response: str = "235 2.7.0 Authentication successful"
    rcpt_responses: dict = field(default_factory=dict)
    default_rcpt: str = "250 2.1.5 Ok"
    data_response: str = "250 2.0.0 Ok: queued as TEST"
    drop_on_data: bool = False

    port: int = field(init=False, default=0)
    received: list = field(init=False, default_factory=list)
    _stop: threading.Event = field(init=False, default_factory=threading.Event)

    def __enter__(self):
        self._certfile, self._keyfile = _self_signed_cert()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        try:
            self._sock.close()
        except OSError:
            pass

    # -- the conversation -------------------------------------------------

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:  # socket.timeout is an alias since 3.10
                continue
            except OSError:
                break
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        try:
            self._converse(conn)
        except (OSError, ssl.SSLError):
            pass  # a dropped client is a normal end to a session here
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _converse(self, conn):
        conn.sendall(b"220 sink.test ESMTP ready\r\n")
        rdr = conn.makefile("rb")
        recipients: list[str] = []

        while True:
            line = rdr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            upper = text.upper()

            if upper.startswith(("EHLO", "HELO")):
                # STARTTLS only before the upgrade; afterwards advertise AUTH.
                if isinstance(conn, ssl.SSLSocket):
                    conn.sendall(b"250-sink.test\r\n250 AUTH PLAIN LOGIN\r\n")
                else:
                    conn.sendall(b"250-sink.test\r\n250 STARTTLS\r\n")
            elif upper.startswith("STARTTLS"):
                conn.sendall(b"220 2.0.0 Ready to start TLS\r\n")
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(self._certfile, self._keyfile)
                conn = ctx.wrap_socket(conn, server_side=True)
                rdr = conn.makefile("rb")
            elif upper.startswith("AUTH"):
                # Reply and KEEP THE CONNECTION OPEN. Closing straight after a
                # 5xx made smtplib report a disconnect instead of an
                # authentication failure, so the client could not tell a wrong
                # password from a flaky network - which is the exact
                # distinction these tests exist to check. Real servers leave
                # the session up so the client can try another mechanism.
                conn.sendall(self.auth_response.encode() + b"\r\n")
            elif upper.startswith("MAIL FROM"):
                recipients = []
                conn.sendall(b"250 2.1.0 Ok\r\n")
            elif upper.startswith("RCPT TO"):
                addr = text.split("<", 1)[-1].split(">", 1)[0].strip().lower()
                reply = self.rcpt_responses.get(addr, self.default_rcpt)
                if not reply.startswith(("4", "5")):
                    recipients.append(addr)
                conn.sendall(reply.encode() + b"\r\n")
            elif upper == "DATA":
                conn.sendall(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                body = []
                while True:
                    chunk = rdr.readline()
                    if not chunk or chunk in (b".\r\n", b".\n"):
                        break
                    body.append(chunk)
                if self.drop_on_data:
                    # Mid-transaction disconnect: the message may or may not
                    # have been queued, which is exactly why it is not a bounce.
                    return
                self.received.append(
                    {
                        "recipients": list(recipients),
                        "body": b"".join(body).decode("utf-8", "replace"),
                    }
                )
                conn.sendall(self.data_response.encode() + b"\r\n")
            elif upper.startswith(("NOOP", "RSET")):
                conn.sendall(b"250 2.0.0 Ok\r\n")
            elif upper.startswith("QUIT"):
                conn.sendall(b"221 2.0.0 Bye\r\n")
                return
            else:
                conn.sendall(b"250 2.0.0 Ok\r\n")
