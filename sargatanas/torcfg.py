"""Tor daemon configuration (torrc) generation.

This module does not run or manage the Tor daemon (that needs root or a
service manager); it generates a correct torrc for the operator to use. The
`tor` and `stealth` profiles then *verify* that a Tor SOCKS listener is
actually reachable before booting, which is what keeps the claim honest.

The generated torrc wires up:

    * SOCKS + control ports
    * a DNS port with AutomapHostsOnResolve (the standard anti-DNS-leak setup)
    * a transparent proxy port (TransPort) for gateway-style routing
    * bridges, exit-node selection and strict nodes when configured
"""

from __future__ import annotations

from . import config


def generate_torrc(settings: config.TorSettings) -> str:
    lines = [
        "# Sargatanas - generated torrc",
        f"SocksPort {settings.socks_port}",
        f"ControlPort {settings.control_port}",
        "",
        "# DNS leak protection",
        f"DNSPort {settings.dns_port}",
        "AutomapHostsOnResolve 1",
        "AutomapHostsSuffixes .onion,.exit",
        "VirtualAddrNetworkIPv4 10.192.0.0/10",
        "",
        "# Transparent proxy (for a gateway-style VM)",
        f"TransPort {settings.trans_port}",
    ]

    if settings.bridges:
        lines += ["", "# Bridges (censored networks)", "UseBridges 1"]
        lines += [f"Bridge {b}" for b in settings.bridges]
    if settings.exit_nodes:
        lines += ["", f"ExitNodes {settings.exit_nodes}"]
    if settings.strict_nodes:
        lines.append("StrictNodes 1")

    return "\n".join(lines) + "\n"


def write_torrc(settings: config.TorSettings, path) -> None:
    from .config import write_private
    write_private(path, generate_torrc(settings))
