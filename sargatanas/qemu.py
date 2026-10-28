"""QEMU process and disk handling.

The command builder is deliberately a pure function: it returns an argv
list and never runs anything. That is what makes the security profiles
testable - the tests assert on the argv, not on a shell string that may be
prefixed with `sudo` and silently half-executed.
"""

from __future__ import annotations

import functools
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import psutil

QEMU_BINARIES = ("qemu-system-x86_64", "qemu-system-aarch64")


class QemuError(RuntimeError):
    pass


def host_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    return machine


def _windows_qemu_binaries() -> dict[str, str]:
    """QEMU on Windows usually installs to Program Files, not PATH."""
    result: dict[str, str] = {}
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if not base:
            continue
        qdir = Path(base) / "qemu"
        if not qdir.is_dir():
            continue
        for name in ("qemu-system-x86_64.exe", "qemu-system-aarch64.exe", "qemu-img.exe"):
            path = qdir / name
            if path.is_file() and name not in result:
                result[name] = str(path)
    return result


def find_qemu(binary: str | None = None) -> str:
    """Locate the system QEMU emulator."""
    if binary:
        found = shutil.which(binary)
        if not found:
            raise QemuError(f"{binary} not found in PATH")
        return found
    arch = host_arch()
    preferred = "qemu-system-x86_64" if arch == "x86_64" else "qemu-system-aarch64"
    for name in (preferred, *QEMU_BINARIES):
        found = shutil.which(name)
        if found:
            return found
    if platform.system() == "Windows":
        win = _windows_qemu_binaries()
        for name in (preferred + ".exe", *(n + ".exe" for n in QEMU_BINARIES)):
            if name in win:
                return win[name]
    raise QemuError("QEMU not found. Install it: brew install qemu | "
                    "apt install qemu-system-x86 qemu-utils | choco install qemu")


def find_qemu_img() -> str:
    found = shutil.which("qemu-img")
    if found:
        return found
    if platform.system() == "Windows":
        win = _windows_qemu_binaries()
        if "qemu-img.exe" in win:
            return win["qemu-img.exe"]
    raise QemuError("qemu-img not found. Install the qemu-utils package.")


def host_cpu_flag() -> str | None:
    """`-cpu host` only exists with KVM/HVF; on Windows WHPX it is `max`."""
    if platform.system() in ("Linux", "Darwin"):
        return "host"
    if platform.system() == "Windows":
        return "max"
    return None


def accelerator() -> list[str]:
    """Hardware acceleration args for the current host."""
    system = platform.system()
    if system == "Linux" and Path("/dev/kvm").exists():
        return ["-accel", "kvm"]
    if system == "Darwin":
        return ["-accel", "hvf"]
    if system == "Windows":
        return ["-accel", "whpx"]
    return []


def acceleration_available() -> bool:
    return bool(accelerator())


@functools.lru_cache(maxsize=1)
def sandbox_supported(qemu_bin: str | None = None) -> bool:
    """Whether this QEMU build supports `-sandbox on` (Linux + libseccomp).

    Probed by launching QEMU with `-sandbox on` and a machine that stays up;
    if QEMU accepts it, the probe times out. If the build lacks seccomp it
    exits immediately with a clear error. Returns False on any doubt - a
    missing sandbox is a safe degradation, a false claim is not.
    """
    if platform.system() != "Linux":
        return False
    try:
        bin_path = qemu_bin or find_qemu()
    except QemuError:
        return False
    try:
        subprocess.run([bin_path, "-sandbox", "on", "-machine", "none",
                        "-display", "none", "-nodefaults"],
                       capture_output=True, timeout=3)
    except subprocess.TimeoutExpired:
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    return False


@dataclass
class QemuSpec:
    """Everything needed to build one QEMU invocation.

    Network arguments are not part of it: profiles.qemu_network_args() is
    the single source of truth for guest networking.
    """

    name: str
    disk: Path | None
    ram_mb: int
    cpus: int
    iso: Path | None = None
    boot: str = "c"              # "c" = disk, "d" = cdrom
    headless: bool = False
    read_only: bool = False      # live mode: changes are discarded
    display: str | None = None
    extra_args: list[str] | None = None


def build_disk_args(spec: QemuSpec) -> list[str]:
    if spec.disk is None:
        return []
    args = ["-drive", f"file={spec.disk},format=qcow2,if=virtio"]
    if not spec.read_only:
        return args
    # Boot the ISO without ever writing to the VM disk: any snapshots the
    # guest creates live in memory and vanish on exit.
    return ["-drive", f"file={spec.disk},format=qcow2,if=virtio,snapshot=on"]


def build_network_args(spec: QemuSpec) -> list[str]:
    """Kept for callers that just want a working NIC; profiles override this."""
    return ["-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0"]


def machine_args() -> list[str]:
    """Pick the emulated machine.

    x86 needs nothing (pc is the default), but on Apple Silicon QEMU has no
    default machine at all and refuses to boot: aarch64 needs `virt`.
    """
    if host_arch() == "aarch64":
        return ["-machine", "virt"]
    return []


def video_args() -> list[str]:
    """aarch64 has no virtio VGA; `ramfb` is the ARM-friendly framebuffer."""
    if host_arch() == "aarch64":
        return ["-device", "ramfb", "-device", "qemu-xhci", "-device", "usb-tablet"]
    return ["-vga", "virtio", "-usb", "-device", "usb-tablet"]


def build_argv(spec: QemuSpec, qemu: str | None = None) -> list[str]:
    """Build the QEMU argv for a spec.

    The result is a list, never a string: a VM name is user input and must
    never reach a shell. `disk=None` builds a diskless invocation (quick
    boot), which is how live ISO sessions stay disposable by construction.
    """
    argv = [qemu or find_qemu(), "-name", f"sargatanas-{spec.name}"]
    argv += machine_args()
    argv += ["-m", str(spec.ram_mb), "-smp", str(spec.cpus)]

    args = accelerator()
    if args:
        argv += args
    cpu = host_cpu_flag()
    if cpu:
        argv += ["-cpu", cpu]

    if spec.disk is not None:
        argv += build_disk_args(spec)
    if spec.iso:
        argv += ["-cdrom", str(spec.iso), "-boot", spec.boot]
    else:
        argv += ["-boot", "c"]

    argv += video_args()

    if spec.headless:
        argv += ["-display", "none", "-serial", "mon:stdio"]
    else:
        argv += ["-display", spec.display or default_display()]

    if spec.extra_args:
        argv += spec.extra_args
    return argv


def default_display() -> str:
    if platform.system() == "Linux" and os.environ.get("WAYLAND_DISPLAY"):
        return "gtk,gl=off"
    return "default" if platform.system() != "Linux" else "gtk"


# ---------- process management ----------

def spawn(argv: list[str]) -> subprocess.Popen:
    """Start QEMU detached from the terminal's session/console."""
    kwargs: dict = dict(stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if platform.system() == "Windows":
        # New process group so Ctrl+C does not reach QEMU, and no console
        # window for a headless VM.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(argv, **kwargs)
    except FileNotFoundError as exc:
        raise QemuError(f"Could not execute {argv[0]}: {exc}") from exc


def is_running(pid: int) -> tuple[bool, str | None]:
    """Return (running, process_name). Checks the name to avoid PID reuse."""
    if pid <= 0:
        return False, None
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False, name
        return True, name
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False, None
    except psutil.Error:
        # AccessDenied or similar: the process exists, we just cannot read it.
        return True, None


def terminate(pid: int, force: bool = False, timeout: float = 15.0) -> bool:
    """Stop a process gracefully, escalating to a hard kill. True if gone."""
    running, name = is_running(pid)
    if not running:
        return True
    if name and "qemu" not in name.lower():
        # Refuse to kill something that merely reused the PID.
        return False
    try:
        proc = psutil.Process(pid)
        if force:
            proc.kill()
        else:
            # On Windows this is an immediate TerminateProcess (no SIGTERM).
            proc.terminate()
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        return False
    try:
        proc.wait(timeout=timeout)
        return True
    except psutil.TimeoutExpired:
        if not force:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except psutil.Error:
                pass
        return not is_running(pid)[0]


# ---------- disks ----------

def create_disk(path: Path, size_gb: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([find_qemu_img(), "create", "-f", "qcow2", str(path), f"{size_gb}G"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise QemuError(f"qemu-img create failed: {result.stderr.strip()}")


def snapshot_create(disk: Path, name: str) -> None:
    result = subprocess.run([find_qemu_img(), "snapshot", "-c", name, str(disk)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise QemuError(f"Snapshot failed: {result.stderr.strip()}")


def snapshot_apply(disk: Path, name: str) -> None:
    result = subprocess.run([find_qemu_img(), "snapshot", "-a", name, str(disk)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise QemuError(f"Snapshot restore failed: {result.stderr.strip()}")


def snapshot_list(disk: Path) -> list[str]:
    result = subprocess.run([find_qemu_img(), "snapshot", "-l", str(disk)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return []
    names = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 1 and parts[0].isdigit():
            names.append(parts[1])
    return names
