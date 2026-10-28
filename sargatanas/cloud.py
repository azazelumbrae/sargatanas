"""Cloud images and cloud-init seeding.

To make a VM *actually* route through Tor we cannot ask QEMU to do it: slirp
has no outbound proxy option. The redirect has to happen inside the guest.

Chain:

    guest app
      -> iptables REDIRECT (all TCP, and DNS)
      -> redsocks listening locally in the guest
      -> SOCKS5 at 10.0.2.100:9050   (guestfwd spliced to the host's Tor)
      -> Tor

Why `redsocks` instead of Tor's TransPort: many packaged Tor builds (Homebrew
on macOS among them) are compiled with transparent proxying disabled, so
TransPort simply does not exist. redsocks speaks SOCKS, which every Tor build
supports, so this path works everywhere. It also means the rules survive a
guest reboot.

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
IMAGES: dict[str, CloudImage] = {
    "alpine": CloudImage(
        key="alpine",
        name="Alpine Linux 3.24 (cloud)",
        url="https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/"
            "alpine-3.24.2-aarch64-cloudinit-metal-r2.qcow2",
        arch="aarch64",
        size_mb=704,
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
        name="Alpine Linux 3.24 (cloud)",
        url="https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/"
            "alpine-3.24.2-x86_64-cloudinit-metal-r2.qcow2",
        arch="x86_64",
        size_mb=704,
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
    return paths.iso_dir() / f"{key}-cloud.qcow2"


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
# Installed by Sargatanas: send all guest traffic through Tor.
# {TOR_GATEWAY}:9050 is a guestfwd endpoint spliced to the host's Tor SOCKS
# port, so no host firewall, no root on the host, no TransPort build needed.
set -e
TOR_IP={TOR_GATEWAY}
REDSOCKS={REDSOCKS_PORT}

# redsocks is not in every base image; install it if missing.
if ! command -v redsocks >/dev/null 2>&1; then
    if command -v apk >/dev/null 2>&1; then
        apk add --no-cache redsocks >/dev/null 2>&1 || true
    elif command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq >/dev/null 2>&1 || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq redsocks >/dev/null 2>&1 || true
    fi
fi

cat > /etc/redsocks.conf <<EOF
base {{
    log_debug = off;
    log_info = off;
    daemon = on;
    user = nobody;
    group = nobody;
}}
redsocks {{
    local_ip = 127.0.0.1;
    local_port = $REDSOCKS;
    ip = $TOR_IP;
    port = 9050;
    type = socks5;
}}
EOF

# DNS first, so name resolution cannot leak before the redirect is in place.
for ns in /etc/resolv.conf; do
    printf 'nameserver %s\\n' "$TOR_IP" > "$ns" 2>/dev/null || true
done

redsocks -c /etc/redsocks.conf 2>/dev/null || redsocks -p /run/redsocks.pid -c /etc/redsocks.conf 2>/dev/null || true
sleep 1

# Everything outbound goes to redsocks, except the path to Tor itself.
iptables -t nat -C OUTPUT -d "$TOR_IP" -j RETURN 2>/dev/null ||
    iptables -t nat -A OUTPUT -d "$TOR_IP" -j RETURN
iptables -t nat -C OUTPUT -p tcp -j REDIRECT --to-ports "$REDSOCKS" 2>/dev/null ||
    iptables -t nat -A OUTPUT -p tcp -j REDIRECT --to-ports "$REDSOCKS"
# IPv6 has no route to Tor; drop it rather than leak.
ip6tables -C OUTPUT -j REJECT 2>/dev/null || ip6tables -A OUTPUT -j REJECT 2>/dev/null || true
echo "sargatanas: all TCP now routed through Tor" >&2
"""

TOR_USER_DATA = """#cloud-config
write_files:
  - path: /usr/local/bin/sargatanas-tor
    permissions: '0755'
    content: |
{script}
runcmd:
  - ["/usr/local/bin/sargatanas-tor"]
"""

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


def has_cloud_init(image: Path) -> bool:
    """Heuristic: cloud images ship a cloud-init datasource unit name.

    We cannot inspect the filesystem without mounting it, so we go by the
    documented naming convention used by the major distributions.
    """
    lowered = image.name.lower()
    return any(tag in lowered for tag in ("cloud", "genericcloud", "nocloud"))
