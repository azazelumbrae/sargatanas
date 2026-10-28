"""VPN support: WireGuard/OpenVPN detection, config templates, kill switch.

Honesty rules the same way as the rest of the project:

    * WireGuard's `AllowedIPs = 0.0.0.0/0` *is* its kill switch: when the
      peer is down there is no route for the traffic, so nothing leaks.
    * OpenVPN has no such primitive, so the kill switch is a set of Linux
      iptables rules. They are generated here, printed for review, and only
      applied on Linux when run with root (via sudo). On macOS/Windows the
      kill switch is reported as not implemented.
    * A *per-VM* kill switch needs tap/bridge networking (root); slirp VMs
      have no host interface to attach rules to. That is documented, not
      faked.
"""

from __future__ import annotations

import platform
import shutil
import subprocess

from . import config


class VpnError(RuntimeError):
    pass


# ---------- detection ----------

def detect_wireguard() -> bool:
    return shutil.which("wg") is not None and shutil.which("wg-quick") is not None


def detect_openvpn() -> bool:
    return shutil.which("openvpn") is not None


def installed_providers() -> dict[str, bool]:
    return {"wireguard": detect_wireguard(), "openvpn": detect_openvpn()}


# ---------- config templates ----------

def wireguard_config_template(settings: config.VpnSettings) -> str:
    iface = settings.interface or "wg0"
    server = settings.config_file or "vpn.example.com:51820"
    return f"""# Sargatanas - WireGuard template for interface {iface}
[Interface]
PrivateKey = <YOUR_PRIVATE_KEY>
Address = 10.0.0.2/24
DNS = 1.1.1.1

[Peer]
PublicKey = <SERVER_PUBLIC_KEY>
Endpoint = {server}
# Routing everything through the tunnel is the WireGuard kill switch:
# if the peer drops, 0.0.0.0/0 has no route and traffic stops instead of
# leaking out the physical interface.
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""


def openvpn_config_template(settings: config.VpnSettings) -> str:
    return f"""# Sargatanas - OpenVPN client template
client
dev tun
proto udp
remote <SERVER> 1194
resolv-retry infinite
nobind
persist-key
persist-tun
ca ca.crt
cert client.crt
key client.key
remote-cert-tls server
cipher AES-256-GCM
verb 3
"""


# ---------- kill switch (OpenVPN-style, Linux only) ----------

def kill_switch_supported() -> bool:
    return platform.system() == "Linux" and shutil.which("iptables") is not None


def kill_switch_commands(interface: str | None = None) -> list[str]:
    """The exact iptables rules for a host-level OpenVPN kill switch.

    Everything must leave through the tunnel interface; anything else is
    rejected. This is a *host-wide* policy, so review before applying - it
    will cut your internet if the tunnel is down.
    """
    iface = interface or "tun0"
    return [
        f"iptables -I OUTPUT 1 -o lo -j ACCEPT",
        f"iptables -I OUTPUT 2 -o {iface} -j ACCEPT",
        f"iptables -I OUTPUT 3 -j REJECT",
    ]


def kill_switch_undo_commands(interface: str | None = None) -> list[str]:
    iface = interface or "tun0"
    return [
        f"iptables -D OUTPUT -o lo -j ACCEPT",
        f"iptables -D OUTPUT -o {iface} -j ACCEPT",
        f"iptables -D OUTPUT -j REJECT",
    ]


def _run_sudo(commands: list[str]) -> None:
    for cmd in commands:
        result = subprocess.run(["sudo", *cmd.split()], capture_output=True, text=True)
        if result.returncode != 0:
            raise VpnError(f"'{cmd}' failed: {result.stderr.strip()}")


def install_kill_switch(interface: str | None = None) -> list[str]:
    if not kill_switch_supported():
        raise VpnError("Kill switch requires Linux + iptables (and root)")
    commands = kill_switch_commands(interface)
    _run_sudo(commands)
    return commands


def remove_kill_switch(interface: str | None = None) -> list[str]:
    if not kill_switch_supported():
        raise VpnError("Kill switch requires Linux + iptables (and root)")
    commands = kill_switch_undo_commands(interface)
    _run_sudo(commands)
    return commands
