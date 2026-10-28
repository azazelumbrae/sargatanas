"""Tor control-port client (the only part of Tor we can talk to reliably).

The original code wrote "AUTHENTICATE\\r\\nSIGNAL NEWNYM\\r\\nQUIT\\r\\n" in one
shot, never read the replies, and reported success unconditionally. This
client speaks the protocol properly: one command, read the reply, map the
status code to a real result.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

PROTOCOL_COOKIE = b"Tor\x00\x00\x00\x00\x00\x00\x00\x00"


class TorControlError(RuntimeError):
    pass


@dataclass
class ControlReply:
    code: int
    lines: list[str]

    @property
    def ok(self) -> bool:
        return self.code == 250


class TorControl:
    """Minimal Tor control port client.

    Usable as a context manager:

        with TorControl(port=9051, password=pw) as tor:
            tor.newnym()
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9051,
                 password: str | None = None, cookie_path: str | None = None,
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.password = password
        self.cookie_path = cookie_path
        self.timeout = timeout
        self._sock = None
        self._buf = b""

    # ---------- plumbing ----------

    def __enter__(self) -> "TorControl":
        import socket
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise TorControlError(
                f"Cannot reach Tor control port {self.host}:{self.port}: {exc}. "
                "Is Tor running and IsControlPort enabled?") from exc
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _readline(self) -> str:
        while b"\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise TorControlError("Tor closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line.decode("utf-8", "replace")

    def _read_reply(self) -> ControlReply:
        first = self._readline()
        try:
            code = int(first[:3])
        except ValueError as exc:
            raise TorControlError(f"Malformed reply from Tor: {first!r}") from exc
        lines = [first[4:]] if len(first) > 4 else []
        separator = first[3:4]
        while separator == "-":
            line = self._readline()
            lines.append(line[4:] if len(line) > 4 else "")
            separator = line[3:4]
        return ControlReply(code, lines)

    def _command(self, cmd: str) -> ControlReply:
        self._sock.sendall(cmd.encode() + b"\r\n")
        return self._read_reply()

    # ---------- protocol ----------

    def authenticate(self) -> None:
        """Try password, then cookie, then safe-cookie, then null."""
        attempts: list[str] = []
        if self.password:
            attempts.append(f'AUTHENTICATE "{self.password}"')
        if self.cookie_path:
            with open(self.cookie_path, "rb") as fh:
                cookie = fh.read().lstrip(PROTOCOL_COOKIE)
            attempts.append('AUTHENTICATE ' + cookie.hex())
        attempts += ["AUTHENTICATE", "AUTHCHALLENGE SAFECOOKIE"]
        last_error = "no authentication method succeeded"
        for attempt in attempts:
            reply = self._command(attempt)
            if reply.ok:
                if attempt.startswith("AUTHCHALLENGE"):
                    self._finish_safecookie(reply)
                return
            last_error = "; ".join(reply.lines[-1:]) or f"code {reply.code}"
        raise TorControlError(f"Tor authentication failed: {last_error}")

    def _finish_safecookie(self, reply: ControlReply) -> None:
        client_nonce = b"00000000000000000000000000000001"
        data = dict(
            (part.split("=")[0], part.split("=")[1])
            for part in reply.lines[-1].split()
            if "=" in part
        )
        server_hash = bytes.fromhex(data["SERVERHASH"])
        server_nonce = bytes.fromhex(data["SERVERNONCE"])
        cookie = self._read_cookie()
        msg = cookie + client_nonce + server_nonce
        client_hash = hmac.new(
            b"Tor safe cookie authentication controller-to-server hash",
            msg, hashlib.sha256).digest()
        expected = hmac.new(
            b"Tor safe cookie authentication server-to-controller hash",
            msg, hashlib.sha256).digest()
        if not hmac.compare_digest(server_hash, expected):
            raise TorControlError("Tor safe-cookie hash mismatch (possible MITM)")
        r = self._command(f"AUTHENTICATE {client_hash.hex()}")
        if not r.ok:
            raise TorControlError("Tor rejected the safe-cookie authentication")

    def _read_cookie(self) -> bytes:
        path = self.cookie_path
        if not path:
            raise TorControlError("A cookie file is required for SAFECOOKIE")
        with open(path, "rb") as fh:
            return fh.read().replace(PROTOCOL_COOKIE, b"")

    def newnym(self) -> None:
        reply = self._command("SIGNAL NEWNYM")
        if not reply.ok:
            raise TorControlError(f"NEWNYM rejected: {reply.lines}")

    def signal(self, name: str) -> None:
        reply = self._command(f"SIGNAL {name}")
        if not reply.ok:
            raise TorControlError(f"SIGNAL {name} rejected: {reply.lines}")

    def is_bootstrapped(self) -> bool:
        reply = self._command("GETINFO status/bootstrap-phase")
        return reply.ok and "PROGRESS=100" in " ".join(reply.lines)
