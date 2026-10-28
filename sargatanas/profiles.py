"""Security profiles - honest about what each one actually enforces.

The rule for this module: a profile may only claim a protection that is
implemented by code you can run *today* on the host you are on. Anything
that needs root, a real Tor circuit or a real VPN tunnel is reported as
"unavailable" with the reason, never silently pretended.

What each profile does today:

    isolated    no network device at all -> air-gapped. Works everywhere.
    nat         plain QEMU user networking, host forwards. Default.
    host-only   user networking with restrict=yes: guest cannot reach the
                host or the LAN, outbound internet still works.
    tor         user networking proxied through a *running* Tor SOCKS port.
                Verified before launch; refuses to start if Tor is down.
    vpn         launches only if the VPN interface is up; iptables kill
                switch is Linux-only and requires root.

Terminology note: `tor` routes TCP through Tor at the hypervisor's user
networking layer. It does not send UDP through Tor, and it is not the
Whonix/transparent-proxy model. The CLI says so.
"""

from __future__ import annotations

import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass, field

from . import qemu


@dataclass(frozen=True)
class Profile:
    key: str
    summary: str
    network: str                       # none | nat | isolated | tor | vpn
    needs: tuple[str, ...] = ()        # runtime requirements, checked at launch
    sandbox: bool = True               # QEMU self-sandbox (seccomp)
    extra_qemu_args: tuple[str, ...] = ()

    def describe_limits(self) -> list[str]:
        if self.key == "tor":
            return ["TCP only - UDP/DNS over Tor is not handled",
                    "Not equivalent to a Whonix/Tails workstation"]
        if self.key == "vpn":
            return ["Kill switch requires root + iptables (Linux only)",
                    "Non-Linux: VPN must be configured at the OS level"]
        if self.key == "host-only":
            return ["Guest still reaches the internet via QEMU user NAT"]
        return []


PROFILES: dict[str, Profile] = {
    "isolated": Profile(
        key="isolated",
        summary="No network device. Guest cannot reach anything.",
        network="none",
    ),
    "nat": Profile(
        key="nat",
        summary="QEMU user networking with port forwards (default).",
        network="nat",
    ),
    "host-only": Profile(
        key="host-only",
        summary="Internet out, but no access to the host or the LAN.",
        network="isolated",
    ),
    "tor": Profile(
        key="tor",
        summary="All TCP routed through a Tor SOCKS proxy on the host.",
        network="tor",
        needs=("tor-socks",),
    ),
    "vpn": Profile(
        key="vpn",
        summary="Refuses to boot unless the VPN interface is up.",
        network="vpn",
        needs=("vpn-interface",),
    ),
}

DEFAULT_PROFILE = "isolated"


def get(key: str) -> Profile:
    if key not in PROFILES:
        raise KeyError(f"Unknown profile '{key}'. Known: {', '.join(PROFILES)}")
    return PROFILES[key]


# ---------- runtime checks ----------

def tor_socks_reachable(host: str = "127.0.0.1", port: int = 9050,
                        timeout: float = 1.5) -> bool:
    """Is something actually listening on the Tor SOCKS port?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def tor_installed() -> bool:
    return shutil.which("tor") is not None


def vpn_interface_up(interface: str | None) -> bool:
    """Check for an up tunnel interface, per platform."""
    if not interface:
        return False
    if platform.system() == "Linux":
        return subprocess.run(["ip", "link", "show", interface],
                              capture_output=True).returncode == 0
    if platform.system() == "Darwin":
        out = subprocess.run(["ifconfig", interface], capture_output=True, text=True)
        return out.returncode == 0 and "UP" in out.stdout
    if platform.system() == "Windows":
        out = subprocess.run(["netsh", "interface", "show", "interface"],
                             capture_output=True, text=True)
        return interface.lower() in out.stdout.lower()
    return False


@dataclass
class LaunchCheck:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_launch(profile: Profile, *, socks_port: int = 9050,
                 vpn_interface: str | None = None) -> LaunchCheck:
    """Pre-flight check. Blocks launch when the profile cannot be honoured."""
    check = LaunchCheck(ok=True)
    if "tor-socks" in profile.needs:
        if not tor_socks_reachable(port=socks_port):
            hint = "" if tor_installed() else " (tor is not installed: brew install tor | apt install tor)"
            check.ok = False
            check.reasons.append(
                f"Tor profile needs a SOCKS listener on 127.0.0.1:{socks_port}{hint}")
    if "vpn-interface" in profile.needs:
        if not vpn_interface_up(vpn_interface):
            check.ok = False
            check.reasons.append(
                f"VPN profile needs an up interface (configured: {vpn_interface or 'none'})")
    if profile.key == "vpn" and platform.system() != "Linux":
        check.warnings.append("Kill switch unavailable on this OS; traffic is not blocked on tunnel drop")
    if profile.sandbox:
        check.warnings.append("QEMU seccomp sandbox is enabled where the QEMU build supports it")
    return check


def qemu_network_args(profile: Profile, forwards: list[str] | None = None,
                      socks_port: int = 9050) -> list[str]:
    """Translate a profile into QEMU networking arguments."""
    if profile.network == "none":
        return []
    if profile.network == "isolated":
        return ["-netdev", "user,id=net0,restrict=yes",
                "-device", "virtio-net-pci,netdev=net0"]
    if profile.network == "tor":
        # hostfwd-less user networking whose outbound TCP is proxied by Tor.
        # (A "socket,connect=" netdev would splice TCP streams, not IP -
        # the guest would get no DHCP and no working stack.)
        return ["-netdev", f"user,id=net0,socks=127.0.0.1:{socks_port}",
                "-device", "virtio-net-pci,netdev=net0"]
    if profile.network in ("nat", "vpn"):
        fwd = ",".join(f"hostfwd=tcp::{f.replace(':', '-:', 1)}" for f in (forwards or []))
        netdev = "user,id=net0" + (f",{fwd}" if fwd else "")
        return ["-netdev", netdev, "-device", "virtio-net-pci,netdev=net0"]
    raise ValueError(f"Unhandled network type {profile.network}")
