#!/usr/bin/env python3
"""Sargatanas DoH stub.

Listens for plain DNS on 127.0.0.1:53 and forwards every query to a
DNS-over-HTTPS resolver over HTTPS. Since HTTPS is TCP, the traffic goes
through redsocks into Tor: the ISP never sees the query, and applications
keep using ordinary DNS.
"""
import socket
import ssl
import sys
import threading
import urllib.request

# Addressed by IP on purpose: the stub cannot resolve a hostname without
# itself, which is the bootstrap problem that broke the first attempt.
# Cloudflare and Quad9 both serve DoH on their IPs.
DOH_URL = "https://1.1.1.1/dns-query"


def query_doh(payload: bytes) -> bytes | None:
    req = urllib.request.Request(
        DOH_URL, data=payload,
        headers={"Content-Type": "application/dns-message",
                 "Accept": "application/dns-message"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False           # cert is for the name, we dial the IP
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
            return r.read()
    except Exception as exc:            # noqa: BLE001
        print(f"doh: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def handle(data, addr, sock):
    answer = query_doh(data)
    if answer:
        sock.sendto(answer, addr)


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 53))
    print("sargatanas-doh: listening on 127.0.0.1:53", file=sys.stderr)
    while True:
        data, addr = s.recvfrom(4096)
        threading.Thread(target=handle, args=(data, addr, s), daemon=True).start()


if __name__ == "__main__":
    main()
