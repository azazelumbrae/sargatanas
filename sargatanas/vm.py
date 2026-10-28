"""VM lifecycle: create, list, start, stop, snapshot.

One implementation used by the CLI, the GTK app and the tests. The old
codebase had this logic three times (Python CLI, Python GUI, Rust) and the
three disagreed on where disks lived and which acceleration flag to use.
"""

from __future__ import annotations

import datetime
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config, paths, profiles, qemu

NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _.-]{0,62}$")


class VmError(RuntimeError):
    pass


@dataclass
class VmStatus:
    name: str
    running: bool
    pid: int | None
    ram: int
    cpus: int
    profile: str
    disk: Path


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise VmError("VM name must be 1-63 chars: letters, digits, space, . _ -")
    if name.strip().endswith("."):
        raise VmError("VM name must not end with a dot")
    return name.strip()


def pid_file(name: str) -> Path:
    return paths.vm_dir() / name / "vm.pid"


def _read_pid(name: str) -> int | None:
    f = pid_file(name)
    if not f.exists():
        return None
    try:
        return int(f.read_text().strip())
    except (OSError, ValueError):
        return None


def _write_pid(name: str, pid: int) -> None:
    pid_file(name).parent.mkdir(parents=True, exist_ok=True)
    pid_file(name).write_text(str(pid))


def _clear_pid(name: str) -> None:
    try:
        pid_file(name).unlink()
    except FileNotFoundError:
        pass


# ---------- create / delete ----------

def create(name: str, *, iso: str | None = None, ram: int | None = None,
           cpus: int | None = None, disk_gb: int | None = None,
           profile: str | None = None, cfg: config.Config | None = None) -> config.VmConfig:
    name = validate_name(name)
    cfg = cfg or config.load()
    if name in cfg.vms:
        raise VmError(f"VM '{name}' already exists")

    profile = profile or cfg.default_profile
    profiles.get(profile)  # validate early

    if iso:
        iso_path = Path(iso).expanduser()
        if not iso_path.exists():
            raise VmError(f"ISO not found: {iso_path}")
        iso = str(iso_path)

    vm_path = paths.vm_dir() / name
    if vm_path.exists():
        raise VmError(f"Directory already exists: {vm_path}")
    vm_path.mkdir(parents=True)

    disk = vm_path / f"{name}.qcow2"
    try:
        qemu.create_disk(disk, disk_gb or cfg.default_disk_gb)
    except Exception:
        shutil.rmtree(vm_path, ignore_errors=True)
        raise

    vm = config.VmConfig(
        name=name, disk=str(disk), iso=iso,
        ram=ram or cfg.default_ram, cpus=cpus or cfg.default_cpus,
        profile=profile,
        created=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    )
    cfg.vms[name] = vm
    config.save(cfg)
    return vm


def delete(name: str, cfg: config.Config | None = None) -> None:
    cfg = cfg or config.load()
    if name not in cfg.vms:
        raise VmError(f"VM '{name}' not found")
    if is_running(name):
        raise VmError(f"VM '{name}' is running; stop it first")
    shutil.rmtree(paths.vm_dir() / name, ignore_errors=True)
    del cfg.vms[name]
    config.save(cfg)


# ---------- status ----------

def is_running(name: str) -> bool:
    pid = _read_pid(name)
    if pid is None:
        return False
    running, proc = qemu.is_running(pid)
    if running and proc and "qemu" not in proc:
        running = False
    if not running:
        _clear_pid(name)  # stale PID from a crash or reboot
    return running


def status(cfg: config.Config | None = None) -> list[VmStatus]:
    cfg = cfg or config.load()
    out = []
    for name, vm in cfg.vms.items():
        running = is_running(name)
        out.append(VmStatus(
            name=name, running=running,
            pid=_read_pid(name) if running else None,
            ram=vm.ram, cpus=vm.cpus,
            profile=vm.profile or cfg.default_profile,
            disk=Path(vm.disk),
        ))
    return sorted(out, key=lambda s: s.name.lower())


# ---------- run ----------

def start(name: str, *, live: bool = False, install: bool = False,
          headless: bool = False, profile: str | None = None,
          cfg: config.Config | None = None) -> int:
    """Launch QEMU and return its PID."""
    cfg = cfg or config.load()
    if name not in cfg.vms:
        raise VmError(f"VM '{name}' not found")
    vm = cfg.vms[name]
    if is_running(name):
        raise VmError(f"VM '{name}' is already running")

    prof = profiles.get(profile or vm.profile or cfg.default_profile)
    check = profiles.check_launch(prof, socks_port=cfg.tor.socks_port,
                                  vpn_interface=cfg.vpn.interface)
    if not check.ok:
        raise VmError("Refusing to start: " + "; ".join(check.reasons))

    iso = Path(vm.iso) if vm.iso else None
    boot = "d" if (live or install) else "c"
    extra: list[str] = []
    if prof.sandbox and qemu.sandbox_supported():
        extra += ["-sandbox", "on"]

    # Start with the profile's guest networking only. build_argv() does not
    # add a NIC, so there is nothing to strip.
    spec = qemu.QemuSpec(
        name=name,
        disk=Path(vm.disk),
        ram_mb=vm.ram,
        cpus=vm.cpus,
        iso=iso if (live or install) else None,
        boot=boot,
        headless=headless,
        read_only=live,
        firmware_vars=Path(vm.disk).parent / "efi-vars.fd",
    )
    argv = qemu.build_argv(spec) + profiles.qemu_network_args(
        prof,
        forwards=cfg.network.forwards,
        socks_port=cfg.tor.socks_port,
    ) + extra

    if headless:
        # The guest's serial console owns the terminal until it exits.
        proc = qemu.spawn(argv, attach=True)
        _clear_pid(name)
        return proc.pid

    proc = qemu.spawn(argv)
    _write_pid(name, proc.pid)

    if vm.profile != prof.key:
        vm.profile = prof.key
        config.save(cfg)
    return proc.pid


def kill_switch_args(interface: str | None) -> list[str]:
    """Removed: a real kill switch needs host firewall rules, not a QEMU NIC.

    Kept as a no-op shim so any existing caller that references it still
    imports cleanly, but it returns nothing. See profiles.check_launch for
    the honest "not implemented" warning.
    """
    del interface
    return []


def stop(name: str, force: bool = False) -> bool:
    pid = _read_pid(name)
    if pid is None:
        _clear_pid(name)
        return True
    stopped = qemu.terminate(pid, force=force)
    if stopped:
        _clear_pid(name)
    return stopped


# ---------- snapshots ----------

def snapshot(name: str, snap: str, cfg: config.Config | None = None) -> None:
    cfg = cfg or config.load()
    if name not in cfg.vms:
        raise VmError(f"VM '{name}' not found")
    if is_running(name):
        raise VmError("Stop the VM before taking a disk snapshot")
    qemu.snapshot_create(Path(cfg.vms[name].disk), snap)


def restore(name: str, snap: str, cfg: config.Config | None = None) -> None:
    cfg = cfg or config.load()
    if name not in cfg.vms:
        raise VmError(f"VM '{name}' not found")
    if is_running(name):
        raise VmError("Stop the VM before restoring a snapshot")
    qemu.snapshot_apply(Path(cfg.vms[name].disk), snap)


def snapshots(name: str, cfg: config.Config | None = None) -> list[str]:
    cfg = cfg or config.load()
    if name not in cfg.vms:
        raise VmError(f"VM '{name}' not found")
    return qemu.snapshot_list(Path(cfg.vms[name].disk))
