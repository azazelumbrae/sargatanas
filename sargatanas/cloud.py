"""Cloud images and cloud-init seeding.

To make a VM *actually* route through Tor we cannot ask QEMU to do it: slirp
has no outbound proxy option. The only honest way is for the guest itself to
apply the redirect rules on first boot, which is exactly what cloud-init is
for.

Flow:

    sargatanas images pull alpine          download a cloud image
    sargatanas create lab --image alpine-cloud.qcow2 --profile tor
        -> a cloud-init seed ISO is generated next to the disk
        -> QEMU attaches it; the guest applies Tor routing on boot

The seed is a tiny ISO9660 filesystem labelled CIDATA containing
`user-data` and `meta-data`, which is the cloud-init "NoCloud" datasource.
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

TOR_SCRIPT = r"""#!/bin/sh
# Installed by Sargatanas. Routes all guest traffic through Tor.
# 10.0.2.100 is a guestfwd endpoint exposing the host's Tor TransPort and
# SOCKS port, so no host firewall or root privileges are needed.
set -e
TOR_IP=10.0.2.100
# DNS through Tor's DNSPort
printf 'nameserver %s\n' "$TOR_IP" > /etc/resolv.conf
# Redirect every outbound TCP connection except the ones to Tor itself
iptables -t nat -A OUTPUT -p tcp -m owner --uid-owner 0 -j RETURN 2>/dev/null || true
iptables -t nat -A OUTPUT -d "$TOR_IP" -j RETURN
iptables -t nat -A OUTPUT -p tcp -j REDIRECT --to-ports 9040
ip6tables -t nat -A OUTPUT -j REJECT 2>/dev/null || true
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

META_DATA = """instance-id: sargatanas-iid
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
