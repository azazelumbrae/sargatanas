"""Security profiles - the full tier set, honestly enforced.

Tiers, from most closed to most open:

    paranoid     air-gapped + seccomp sandbox
    isolated     air-gapped (no network device)
    stealth      requires Tor SOCKS AND a VPN interface; outbound-only
    tor          routes the guest through Tor via guestfwd + cloud-init
    vpn          requires a VPN interface; NAT egress
    pentesting   internal VM<->VM network + NAT internet for tools
    nat          NAT with port forwards (default)
    restricted   NIC present but isolated from host and internet

The rule for this module: a profile may only claim a protection that is
implemented by code you can run *today* on the host you are on. Anything that
needs root, a real Tor circuit or a real VPN tunnel is either verified at
launch or reported as "not implemented", never silently pretended.

What QEMU's userspace (slirp) backend can and cannot do - stated, not faked:

    * air-gap, NAT, isolated NIC, and VM<->VM multicast all work without root.
    * true "host-only" (guest<->host, no internet) needs a tap/bridge -> root.
    * transparent "route everything through Tor" needs a dedicated gateway
      (Whonix/Tails) or host-side redsocks + iptables (root). We do not claim
      it; the `tor`/`stealth` profiles verify Tor/VPN is up and boot with
      outbound-only networking instead.
"""

from __future__ import annotations

import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass, field

from . import qemu

INTERNAL_MCAST = "230.0.0.1:1234"


@dataclass(frozen=True)
class Profile:
    key: str
    summary: str
    network: str                       # none|nat|restricted|internal|tor|vpn|pentesting
    needs: tuple[str, ...] = ()        # runtime requirements, checked at launch
    sandbox: bool = False              # request `-sandbox on` where QEMU supports it

    @property
    def needs_tor_routing(self) -> bool:
        """Tiers that require in-guest Tor routing via a cloud-init seed."""
        return self.network == "tor"

    def describe_limits(self) -> list[str]:
        """Caveats, so a tier never promises more than it delivers."""
        if self.key == "tor":
            return [
                "Requires a cloud image with cloud-init (see: sargatanas images)",
                "TCP traffic is redirected into Tor inside the guest",
                "Needs Tor running with TransPort enabled on the host",
            ]
        if self.key == "stealth":
            return [
                "Requires a cloud image with cloud-init and a running VPN",
                "Tor routing is applied inside the guest; VPN is a host-level guard",
            ]
        if self.key == "vpn":
            return [
                "Verifies the tunnel interface is up; egress follows the host VPN",
                "Kill switch is Linux-only and needs root",
            ]
        if self.key == "pentesting":
            return [
                "Two NICs: one internal (VM<->VM), one NAT for internet",
            ]
        if self.key == "restricted":
            return ["NIC is present but can reach neither host nor internet"]
        return []

    def firewall_rules(self) -> list[str]:
        """What this profile actually blocks/allows at the QEMU layer."""
        net = self.network
        if net == "none":
            rules = ["No network device: guest is air-gapped",
                     "No inbound or outbound traffic is possible"]
        elif net == "restricted":
            rules = ["NIC present but isolated (slirp restrict=yes)",
                     "No host contact, no internet routing"]
        elif net == "internal":
            rules = ["VM<->VM only over multicast",
                     "No host access, no internet"]
        elif net == "tor":
            rules = ["No inbound ports",
                     "guestfwd exposes Tor TransPort/SOCKS/DNS at 10.0.2.100",
                     "cloud-init redirects all guest TCP into TransPort"]
        elif net == "vpn":
            rules = ["NAT egress follows the host VPN",
                     "Requires the VPN interface up (verified at launch)"]
        elif net == "pentesting":
            rules = ["Internal VM<->VM network (multicast)",
                     "NAT internet for tooling",
                     "Inbound only on forwarded ports"]
        else:  # nat
            rules = ["Outbound NAT to the internet",
                     "Inbound only on forwarded ports (hostfwd)",
                     "No direct access to host services"]
        if self.sandbox:
            rules.append("QEMU seccomp sandbox (-sandbox on) where the build supports it")
        return rules


PROFILES: dict[str, Profile] = {
    "paranoid": Profile(
        key="paranoid",
        summary="Maximum isolation: air-gapped and seccomp-sandboxed.",
        network="none",
        sandbox=True,
    ),
    "isolated": Profile(
        key="isolated",
        summary="Air-gapped: no network device at all.",
        network="none",
    ),
    "stealth": Profile(
        key="stealth",
        summary="Tor routing inside the guest plus a verified VPN tunnel.",
        network="tor",
        needs=("tor-socks", "vpn-interface"),
        sandbox=True,
    ),
    "tor": Profile(
        key="tor",
        summary="Routes all guest traffic through Tor (cloud image required).",
        network="tor",
        needs=("tor-socks",),
    ),
    "vpn": Profile(
        key="vpn",
        summary="Requires the VPN interface up before booting.",
        network="vpn",
        needs=("vpn-interface",),
    ),
    "pentesting": Profile(
        key="pentesting",
        summary="Internal VM network plus NAT internet for tools.",
        network="pentesting",
    ),
    "nat": Profile(
        key="nat",
        summary="QEMU user networking with port forwards (default).",
        network="nat",
    ),
    "restricted": Profile(
        key="restricted",
        summary="NIC present, but no host contact and no internet.",
        network="restricted",
    ),
}

DEFAULT_PROFILE = "isolated"

# Isolation modes, exposed for the CLI/GUI and docs.
ISOLATION_MODES: dict[str, str] = {
    "full": "No network device - completely air-gapped",
    "nat": "Outbound NAT with port forwards",
    "restricted": "NIC isolated from host and internet",
    "internal": "VMs can talk to each other only",
    "tor": "Verify Tor is up, then outbound-only",
    "vpn": "Verify the VPN is up, then NAT egress",
}


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


def _tor_trans_port_reachable(port: int = 9040) -> bool:
    """TransPort is what iptables REDIRECT feeds; SOCKS alone is not enough."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


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
                f"Tor SOCKS listener not reachable on 127.0.0.1:{socks_port}{hint}")
        elif not _tor_trans_port_reachable():
            check.ok = False
            check.reasons.append(
                "Tor TransPort (9040) is not reachable; enable TransPort in torrc "
                "(sargatanas tor config --output torrc)")
        if not shutil.which("nc"):
            check.ok = False
            check.reasons.append(
                "Netcat (nc) is required to splice Tor into the guest (guestfwd)")
    if "vpn-interface" in profile.needs:
        if not vpn_interface_up(vpn_interface):
            check.ok = False
            check.reasons.append(
                f"VPN interface not up (configured: {vpn_interface or 'none'})")
    if profile.key in ("vpn", "stealth"):
        check.warnings.append(
            "Kill switch is Linux-only and needs root; traffic is not blocked on tunnel drop")
    return check


# ---------- QEMU networking ----------

def _user_netdev(net_id: str, forwards: list[str] | None = None,
                 restrict: bool = False) -> list[str]:
    opts = [f"user,id={net_id}"]
    if restrict:
        opts.append("restrict=yes")
    elif forwards:
        fwd = ",".join(f"hostfwd=tcp::{f.replace(':', '-:', 1)}" for f in forwards)
        opts.append(fwd)
    netdev = ",".join(opts)
    return ["-netdev", netdev, "-device", f"virtio-net-pci,netdev={net_id}"]


def _internal_netdev(net_id: str) -> list[str]:
    return ["-netdev", f"socket,id={net_id},mcast={INTERNAL_MCAST}",
            "-device", f"virtio-net-pci,netdev={net_id}"]


TOR_ENDPOINT = "10.0.2.100"


def tor_guestfwd_args(socks_port: int = 9050, trans_port: int = 9040,
                      dns_port: int = 9053) -> list[str]:
    """Expose the host's Tor ports inside the guest, without root.

    QEMU's user networking has no outbound-proxy option, so we push the
    opposite direction: `guestfwd` splices a guest TCP port to a host
    process. With cloud-init applying iptables REDIRECT rules to these ports
    the guest's traffic ends up in Tor.
    """
    forwards = [
        (9050, socks_port),   # SOCKS, for apps that speak it
        (9040, trans_port),   # TransPort, target of the NAT redirect
        (9053, dns_port),     # DNSPort, target of the DNS redirect
    ]
    opts = ",".join(
        f"guestfwd=tcp:{TOR_ENDPOINT}:{guest}-cmd:nc 127.0.0.1 {host}"
        for guest, host in forwards
    )
    return ["-netdev", f"user,id=net0,{opts}",
            "-device", "virtio-net-pci,netdev=net0"]


def qemu_network_args(profile: Profile, forwards: list[str] | None = None,
                      socks_port: int = 9050,
                      trans_port: int = 9040, dns_port: int = 9053) -> list[str]:
    """Translate a profile into QEMU networking arguments.

    For `tor`/`stealth` the guest gets no inbound ports and the Tor endpoints
    are spliced in. Routing is completed inside the guest by the cloud-init
    seed (see cloud.py); QEMU alone cannot redirect traffic to a SOCKS proxy.
    """
    net = profile.network
    if net == "none":
        return []
    if net == "nat":
        return _user_netdev("net0", forwards)
    if net == "restricted":
        return _user_netdev("net0", restrict=True)
    if net == "internal":
        return _internal_netdev("net0")
    if net == "tor":
        return tor_guestfwd_args(socks_port, trans_port, dns_port)
    if net == "vpn":
        return _user_netdev("net0", forwards)
    if net == "pentesting":
        return _internal_netdev("net0") + _user_netdev("net1", forwards)
    raise ValueError(f"Unhandled network type {net}")
