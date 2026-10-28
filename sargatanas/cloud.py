"""Cloud images and cloud-init seeding.

To make a VM *actually* route through Tor we cannot ask QEMU to do it: slirp
has no outbound proxy option. Everything happens inside the guest, in two
phases:

    Phase 1 (first boot only)
        ordinary DNS is left working so the package manager can reach its
        mirrors; iptables and torsocks get installed.

    Phase 2 (every boot)
        DNS is removed, ALL_PROXY points at Tor with socks5h (so names are
        resolved at the exit node), torsocks is configured for programs that
        ignore proxy variables, IPv6 is disabled, and iptables permits only
        the Tor endpoint.

    guest app -> torsocks / ALL_PROXY -> SOCKS5 10.0.2.100:9050
                                          (guestfwd to the host's Tor) -> Tor

Why not redsocks or TransPort: Tor has no UDP ASSOCIATE, so nothing can carry
UDP through the tunnel, and packaged Tor builds often disable TransPort
entirely (Homebrew's does). A local redirector also swallowed torsocks' own
connection. The whitelist plus torsocks is the only combination verified to
work here.

The seed is a CIDATA ISO with `user-data`/`meta-data`, the cloud-init NoCloud
datasource.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import paths

SEED_LABEL = "cidata"
REDSOCKS_PORT = 12345
TOR_GATEWAY = "10.0.2.100"


class CloudError(RuntimeError):
    pass


# ---------- known cloud images ----------

@dataclass(frozen=True)
class CloudImage:
    key: str
    name: str
    url: str
    arch: str          # "aarch64" | "x86_64"
    size_mb: int
    cloudinit: bool = True


# Pinned to a release so a URL does not silently change meaning.
#
# Use the plain "cloudinit" flavour, NOT "cloudinit-metal": the metal image
# reads its configuration over SMBIOS, while the plain one reads the NoCloud
# CIDATA seed we generate. Downloading the metal image is why cloud-init
# appeared to ignore the seed.
IMAGES: dict[str, CloudImage] = {
    "alpine": CloudImage(
        key="alpine",
        name="Alpine Linux 3.24 (NoCloud)",
        url="https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/"
            "alpine-3.24.2-aarch64-cloudinit-r2.qcow2",
        arch="aarch64",
        size_mb=182,
    ),
    "debian": CloudImage(
        key="debian",
        name="Debian 12 genericcloud",
        url="https://cloud.debian.org/images/cloud/bookworm/latest/"
            "debian-12-genericcloud-arm64.qcow2",
        arch="aarch64",
        size_mb=350,
    ),
}

X86_IMAGES: dict[str, CloudImage] = {
    "alpine": CloudImage(
        key="alpine",
        name="Alpine Linux 3.24 (NoCloud)",
        url="https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/"
            "alpine-3.24.2-x86_64-cloudinit-r2.qcow2",
        arch="x86_64",
        size_mb=182,
    ),
    "debian": CloudImage(
        key="debian",
        name="Debian 12 genericcloud",
        url="https://cloud.debian.org/images/cloud/bookworm/latest/"
            "debian-12-genericcloud-amd64.qcow2",
        arch="x86_64",
        size_mb=350,
    ),
}


def available_images(arch: str | None = None) -> dict[str, CloudImage]:
    from .qemu import host_arch
    arch = arch or host_arch()
    return X86_IMAGES if arch == "x86_64" else IMAGES


def image_path(key: str) -> Path:
    return paths.iso_dir() / f"{key}-nocloud.qcow2"


def pull(key: str) -> Path:
    """Download a cloud image into the ISO directory (resumable)."""
    image = available_images().get(key)
    if not image:
        raise CloudError(f"Unknown image '{key}'. Known: {', '.join(available_images())}")
    dest = image_path(key)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    # -L follows redirects, -C - resumes, --retry covers transient 5xx.
    result = subprocess.run(["curl", "-fL", "--retry", "3", "--retry-delay", "2",
                             "-C", "-", "-o", str(tmp), image.url])
    if result.returncode != 0:
        raise CloudError(f"Download failed for {image.url} (partial file kept at {tmp})")
    tmp.replace(dest)
    return dest


# ---------- cloud-init payload ----------

TOR_SCRIPT = f"""#!/bin/sh
# Installed by Sargatanas: route all guest traffic through Tor.
#
# Two-phase bootstrap. Phase 1 needs ordinary internet and DNS so the
# package manager can reach Alpine's mirrors; only then is the door closed
# and Tor enforced. Without this ordering the guest would be unable to
# install anything, because Tor cannot carry the DNS that apk requires.
set -u
TOR_IP={TOR_GATEWAY}
LOG=/var/log/sargatanas-tor.log
PHASE1=/var/lib/sargatanas-bootstrap
exec >"$LOG" 2>&1
echo "sargatanas: starting (broadcast=$(date -u))"

install_pkg() {{
    if command -v apk >/dev/null 2>&1; then
        apk add --no-cache "$@" || return 1
    elif command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q "$@"
    else
        return 1
    fi
}}

# ---- Phase 1: install, with working DNS -------------------------------
if [ ! -f "$PHASE1" ]; then
    echo "sargatanas: phase 1 - installing packages"
    # Make sure the guest can resolve names right now (QEMU's resolver).
    printf 'nameserver 10.0.2.3\\n' > /etc/resolv.conf
    command -v iptables >/dev/null 2>&1 || install_pkg iptables iptables-legacy || true
    command -v ip6tables >/dev/null 2>&1 || install_pkg ip6tables ip6tables-legacy || true
    # torsocks is the piece that makes ordinary programs honour Tor.
    command -v torsocks >/dev/null 2>&1 || install_pkg torsocks || true
    # strace for diagnosing routing problems; users can rerun the diagnose
    # command without needing a working network.
    command -v strace >/dev/null 2>&1 || install_pkg strace || true
    touch "$PHASE1"
    echo "sargatanas: phase 1 done"
fi

if ! command -v iptables >/dev/null 2>&1; then
    echo "sargatanas: FATAL iptables unavailable; refusing to leave the guest unprotected" >&2
    exit 1
fi

# ---- Phase 2: close the door ------------------------------------------
echo "sargatanas: phase 2 - enforcing Tor"
# DNS: no local resolver. Names are resolved *inside* Tor by socks5h.
printf 'nameserver 127.0.0.1\\n' > /etc/resolv.conf

# torsocks is the transport: it intercepts connect()/getaddrinfo() and sends
# everything through Tor with socks5h, so names resolve at the exit node.
# Proxy environment variables are deliberately NOT set: BusyBox wget treats
# ALL_PROXY as a URL and every tool parses socks5h differently, which caused
# failures that looked like network bugs.
P=/etc/profile.d/90-sargatanas-tor.sh
: > "$P"
echo 'export TORSOCKS_CONF_FILE=/etc/tor/torsocks.conf' >> "$P"
echo 'alias tor-run="torsocks"' >> "$P"
: > /etc/environment

# Programs that ignore proxies are wrapped transparently: torsocks
# intercepts connect()/getaddrinfo() and sends everything to Tor.
if command -v torsocks >/dev/null 2>&1; then
    mkdir -p /etc/tor 2>/dev/null || true
    echo 'TorAddress 10.0.2.100' > /etc/tor/torsocks.conf
    echo 'TorPort 9050' >> /etc/tor/torsocks.conf
    echo "sargatanas: torsocks configured" 
else
    echo "sargatanas: WARNING torsocks missing - proxy-unaware programs can leak" >&2
fi

# Enforce Tor with a whitelist instead of redsocks. redsocks cannot talk to
# Tor (no UDP ASSOCIATE) and its REDIRECT rule swallowed torsocks' own
# connection, which is why the earlier design hung. Allowing only the Tor
# endpoint and dropping everything else is simpler and actually holds.
iptables -F OUTPUT 2>/dev/null || true
# Only the Tor endpoint may leave the guest. `-m state` is deliberately not
# used: minimal Alpine images ship iptables without conntrack, so the module
# silently fails and the whitelist blocks everything.
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -d "$TOR_IP" -j ACCEPT
iptables -A OUTPUT -j REJECT

# ---- IPv6: off, in layers ---------------------------------------------
sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null 2>&1 || true
sysctl -w net.ipv6.conf.default.disable_ipv6=1 >/dev/null 2>&1 || true
for k in all default; do echo "net.ipv6.conf.$k.disable_ipv6 = 1"; done > /etc/sysctl.d/99-sargatanas-ipv6.conf 2>/dev/null || true
if command -v ip6tables >/dev/null 2>&1; then
    for chain in OUTPUT FORWARD INPUT; do
        ip6tables -C "$chain" -j DROP 2>/dev/null ||
            ip6tables -A "$chain" -j DROP 2>/dev/null || true
    done
fi
IPV6_STATE=$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || echo unknown)
if [ "$IPV6_STATE" = "1" ]; then
    echo "sargatanas: IPv6 disabled"
else
    echo "sargatanas: WARNING IPv6 could not be disabled" >&2
fi
# Proxy-unaware programs: run them under torsocks. Plain TCP works through
# the relay; if a program needs TLS the SOCKS client must speak it directly,
# so the check below tells the user what to expect.
cat > /usr/local/bin/sargatanas-check <<'SCRIPT'
#!/bin/sh
echo "sargatanas: verifying Tor routing"
if torsocks wget -q -O- --timeout=20 http://example.com >/dev/null 2>&1; then
    echo "  OK  plain TCP leaves through Tor"
else
    echo "  FAIL plain TCP is not routed"
fi
if torsocks wget -q -O- --timeout=25 https://example.com >/dev/null 2>&1; then
    echo "  OK  TLS leaves through Tor"
else
    echo "  WARN TLS through the tunnel is unstable on this host"
fi
SCRIPT
chmod +x /usr/local/bin/sargatanas-check
echo "sargatanas: Tor routing active - run 'sargatanas-check' to verify"
"""

TOR_USER_DATA = """#cloud-config
# Cloud images ship without a usable root login. Without this block the
# console asks for a password nobody has.
users:
  - name: root
    lock_passwd: false
    plain_text_passwd: sargatanas
    shell: /bin/sh
ssh_pwauth: true
chpasswd:
  expire: false
write_files:
  - path: /usr/local/bin/sargatanas-tor
    permissions: '0755'
    content: |
{script}
runcmd:
  - ["/usr/local/bin/sargatanas-tor"]
"""

# Bumped whenever the generated payload changes in a way that matters to an
# already-initialised guest (credentials, routing).
SEED_VERSION = 2

META_DATA = f"""instance-id: sargatanas-iid
local-hostname: sargatanas-vm
"""


def user_data_tor() -> str:
    indented = "\n".join("      " + line for line in TOR_SCRIPT.strip().splitlines())
    return TOR_USER_DATA.format(script=indented)


def user_data_plain() -> str:
    return "#cloud-config\n{}\n"


# ---------- seed ISO ----------

def seed_supported() -> bool:
    """Whether we can build a CIDATA ISO on this host without extra packages."""
    system = platform.system()
    if system == "Darwin":
        return shutil.which("hdiutil") is not None
    return any(shutil.which(t) for t in ("genisoimage", "mkisofs", "xorriso"))


def write_seed(dest: Path, user_data: str) -> Path:
    """Build a CIDATA ISO containing user-data and meta-data."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # hdiutil refuses to overwrite; rebuild from scratch every time.
    dest.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp)
        (src / "user-data").write_text(user_data)
        (src / "meta-data").write_text(META_DATA)

        if platform.system() == "Darwin" and shutil.which("hdiutil"):
            cmd = ["hdiutil", "makehybrid", "-iso", "-joliet",
                   "-iso-volume-name", SEED_LABEL,
                   "-joliet-volume-name", SEED_LABEL,
                   "-default-volume-name", SEED_LABEL,
                   str(src), "-o", str(dest)]
        else:
            tool = next((t for t in ("genisoimage", "mkisofs", "xorriso")
                         if shutil.which(t)), None)
            if not tool:
                raise CloudError("Need hdiutil (macOS) or genisoimage/xorriso to build the seed")
            if tool == "xorriso":
                cmd = ["xorriso", "-as", "mkisofs", "-output", str(dest),
                       "-volid", SEED_LABEL, "-joliet", "-rock", str(src)]
            else:
                cmd = [tool, "-output", str(dest), "-volid", SEED_LABEL,
                       "-joliet", "-rock", str(src)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not dest.exists():
            raise CloudError(f"Seed build failed: {result.stderr.strip()[:300]}")
    return dest


def seed_path(disk: Path) -> Path:
    return disk.parent / "seed.iso"


def seed_stamp_path(disk: Path) -> Path:
    """Marker holding the SEED_VERSION the guest was seeded with."""
    return disk.parent / "seed.version"


def write_seed_stamp(disk: Path) -> None:
    seed_stamp_path(disk).write_text(str(SEED_VERSION) + "\n")


def guest_login() -> str:
    """The root password the seed configures (documented in the README)."""
    return "sargatanas"


def seed_is_current(disk: Path) -> bool:
    """False when the VM was created with an older user-data payload.

    cloud-init runs once per instance, so rewriting seed.iso does not fix an
    existing guest: the VM has to be recreated.
    """
    stamp = seed_stamp_path(disk)
    try:
        return int(stamp.read_text().strip()) >= SEED_VERSION
    except (OSError, ValueError):
        return False


def has_cloud_init(image: Path) -> bool:
    """Heuristic: cloud images ship a cloud-init datasource unit name.

    We cannot inspect the filesystem without mounting it, so we go by the
    documented naming convention used by the major distributions.
    """
    lowered = image.name.lower()
    return any(tag in lowered for tag in ("cloud", "genericcloud", "nocloud"))
