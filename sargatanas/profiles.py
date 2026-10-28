"""Security profiles - honest about what each one actually enforces.

The rule for this module: a profile may only claim a protection that is
implemented by code you can run *today* on the host you are on. Anything
that needs root, a real Tor circuit or a real VPN tunnel is reported as
"unavailable" or "not implemented", never silently pretended.

What each profile does today:

    isolated    no network device at all -> air-gapped. Works everywhere.
    nat         QEMU user networking with port forwards. Default.
    host-only   user networking with restrict=yes: guest cannot reach the
                host or the LAN, outbound internet still works.
    tor         a guard: verifies a Tor SOCKS listener is up, then boots
                with outbound-only user networking (no inbound ports). It
                does NOT transparently redirect guest traffic - the guest
                must point its own applications at the host Tor proxy.
    vpn         a guard: verifies the tunnel interface is up, then boots
                with normal user networking. Guest egress follows the
                host's VPN. No kill switch is implemented yet.

Seccomp: QEMU's `-sandbox on` is only available in QEMU builds that enable
it (Linux with libseccomp). It is probed at runtime and applied where
supported; on other platforms it is neither applied nor claimed.
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
    extra_qemu_args: tuple[str, ...] = ()

    def describe_limits(self) -> list[str]:
        if self.key == "tor":
            return [
                "Verifies Tor is reachable; does not transparently redirect traffic",
                "Guest apps must be pointed at the host Tor SOCKS manually",
                "TCP only - UDP/DNS over Tor is not handled",
            ]
        if self.key == "vpn":
            return [
                "Verifies the tunnel interface is up; egress follows the host VPN",
                "Kill switch (block traffic on tunnel drop) is not implemented",
            ]
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
        summary="Verifies Tor is up, then boots with no inbound ports.",
        network="tor",
        needs=("tor-socks",),
    ),
    "vpn": Profile(
        key="vpn",
        summary="Verifies the VPN interface is up before booting.",
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
    if profile.key == "vpn":
        check.warnings.append(
            "Kill switch is not implemented; traffic is not blocked if the tunnel drops")
    return check


def qemu_network_args(profile: Profile, forwards: list[str] | None = None,
                      socks_port: int = 9050) -> list[str]:
    """Translate a profile into QEMU networking arguments.

    `socks_port` is accepted for API stability but the guard profile does not
    use it: QEMU user networking has no `socks=` option, so pretending to
    proxy the guest through Tor at the hypervisor layer would be a lie.
    """
    del socks_port
    if profile.network == "none":
        return []
    if profile.network == "isolated":
        return ["-netdev", "user,id=net0,restrict=yes",
                "-device", "virtio-net-pci,netdev=net0"]
    if profile.network == "tor":
        # Outbound-only user networking: no hostfwd, so nothing from the host
        # can reach the guest. The guest still has normal outbound internet;
        # the Tor guard only *verifies* Tor is up and documents that routing
        # must be configured inside the guest.
        return ["-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0"]
    if profile.network in ("nat", "vpn"):
        fwd = ",".join(f"hostfwd=tcp::{f.replace(':', '-:', 1)}" for f in (forwards or []))
        netdev = "user,id=net0" + (f",{fwd}" if fwd else "")
        return ["-netdev", netdev, "-device", "virtio-net-pci,netdev=net0"]
    raise ValueError(f"Unhandled network type {profile.network}")
