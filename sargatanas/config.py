"""Configuration storage.

A single JSON file, written atomically, with a schema version so a future
change can migrate instead of silently wiping a user's VM list.

The file is created with 0600 permissions: it can contain VPN interface
names and Tor bridge lines, which are identifying.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths

DEFAULT_DATA_DIR_ENV = "SARGATANAS_HOME"


@dataclass
class VmConfig:
    name: str
    disk: str
    iso: str | None = None
    ram: int = 4096
    cpus: int = 4
    profile: str | None = None
    created: str | None = None

    @property
    def disk_path(self) -> Path:
        return Path(self.disk)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NetworkDefaults:
    # "nat" | "isolated" | "none"
    mode: str = "nat"
    # A fresh MAC per boot defeats cross-session fingerprinting. Turn off if
    # you rely on a stable MAC (DHCP reservations, licence binding).
    random_mac: bool = True
    # Used only when random_mac is False.
    mac: str | None = None
    # Host forwards for user-mode NAT, as "host:guest"
    forwards: list[str] = field(default_factory=lambda: ["2222:22"])


@dataclass
class TorSettings:
    socks_port: int = 9050
    control_port: int = 9051
    dns_port: int = 5353
    trans_port: int = 9040
    control_password: str | None = None
    bridges: list[str] = field(default_factory=list)
    exit_nodes: str | None = None
    strict_nodes: bool = False


@dataclass
class VpnSettings:
    provider: str | None = None  # "wireguard" | "openvpn"
    interface: str | None = None
    config_file: str | None = None
    kill_switch: bool = False


@dataclass
class Config:
    schema: int = paths.CONFIG_SCHEMA
    default_ram: int = 4096
    default_cpus: int = 4
    default_disk_gb: int = 40
    default_profile: str = "isolated"
    vms: dict[str, VmConfig] = field(default_factory=dict)
    network: NetworkDefaults = field(default_factory=NetworkDefaults)
    tor: TorSettings = field(default_factory=TorSettings)
    vpn: VpnSettings = field(default_factory=VpnSettings)

    # ---------- (de)serialisation ----------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["vms"] = {k: v.to_dict() for k, v in self.vms.items()}
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        """Build a Config, ignoring unknown keys.

        Tolerant on purpose: a config written by a newer build (or by the
        old n01d tool) must not crash the app, it just loses the keys this
        version does not know about.
        """
        known = {f: raw[f] for f in ("default_ram", "default_cpus", "default_disk_gb",
                                     "default_profile", "schema") if f in raw}

        vms: dict[str, VmConfig] = {}
        for name, vm in (raw.get("vms") or {}).items():
            if not isinstance(vm, dict) or "disk" not in vm:
                continue
            vms[name] = VmConfig(
                name=name,
                disk=vm["disk"],
                iso=vm.get("iso"),
                ram=int(vm.get("ram", 4096)),
                cpus=int(vm.get("cpus", 4)),
                profile=vm.get("profile") or vm.get("security_profile"),
                created=vm.get("created"),
            )

        def section(cls_, key):
            data = raw.get(key) or {}
            if not isinstance(data, dict):
                data = {}
            allowed = {f.name for f in cls_.__dataclass_fields__.values()}
            return cls_(**{k: v for k, v in data.items() if k in allowed})

        return cls(vms=vms, network=section(NetworkDefaults, "network"),
                   tor=section(TorSettings, "tor"), vpn=section(VpnSettings, "vpn"),
                   **known)


# ---------- load / save ----------

def load(path: Path | None = None) -> Config:
    path = path or paths.config_file()
    if not path.exists():
        return Config()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        # A corrupt config must not destroy the VM list on disk. Start fresh
        # in memory; the caller can re-save deliberately.
        return Config()
    if not isinstance(raw, dict):
        return Config()
    return Config.from_dict(raw)


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg.to_dict(), indent=2) + "\n"
    write_private(path, payload)


def write_private(path: Path, payload: str) -> None:
    """Atomic write with 0600 permissions."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def migrate_legacy_if_needed() -> bool:
    """Copy the old n01d-machine config once, so nobody loses their VM list.

    Returns True when a migration happened.
    """
    target = paths.config_file()
    if target.exists():
        return False
    legacy = paths.legacy_home() / "config.json"
    if not legacy.exists():
        return False
    try:
        raw = json.loads(legacy.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict) or not raw.get("vms"):
        return False
    cfg = Config.from_dict(raw)
    save(cfg)
    return True


# ---------- editing (shared by the CLI and the GUI) ----------

class SettingsError(ValueError):
    """A user-supplied value is invalid; the message is shown verbatim."""


def _port(value: object, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be a number") from None
    if not 1 <= port <= 65535:
        raise SettingsError(f"{name} must be between 1 and 65535")
    return port


def _forwards(values: list[str]) -> list[str]:
    """Validate 'host:guest' port forwards, which become QEMU hostfwd."""
    clean = []
    for item in values:
        item = (item or "").strip()
        if not item:
            continue
        if item.count(":") != 1:
            raise SettingsError(f"Port forward '{item}' must look like 2222:22")
        host, guest = item.split(":")
        _port(host, f"Host port in '{item}'")
        _port(guest, f"Guest port in '{item}'")
        clean.append(f"{host}:{guest}")
    return clean


def update_tor(cfg: Config, **changes: Any) -> Config:
    for key in ("socks_port", "control_port", "dns_port", "trans_port"):
        if key in changes:
            setattr(cfg.tor, key, _port(changes[key], key.replace("_", " ")))
    if "bridges" in changes:
        bridges = changes["bridges"]
        cfg.tor.bridges = [b.strip() for b in bridges if b and b.strip()]
    for key in ("exit_nodes", "control_password"):
        if key in changes:
            value = changes[key]
            setattr(cfg.tor, key, (value or "").strip() or None)
    if "strict_nodes" in changes:
        cfg.tor.strict_nodes = bool(changes["strict_nodes"])
    return cfg


def update_vpn(cfg: Config, **changes: Any) -> Config:
    if "provider" in changes:
        provider = (changes["provider"] or "").strip() or None
        if provider not in (None, "wireguard", "openvpn"):
            raise SettingsError("VPN provider must be wireguard or openvpn")
        cfg.vpn.provider = provider
    for key in ("interface", "config_file"):
        if key in changes:
            setattr(cfg.vpn, key, (changes[key] or "").strip() or None)
    if "kill_switch" in changes:
        cfg.vpn.kill_switch = bool(changes["kill_switch"])
    return cfg


def update_network(cfg: Config, **changes: Any) -> Config:
    if "mode" in changes:
        mode = (changes["mode"] or "").strip()
        if mode not in ("nat", "isolated", "none", "bridge"):
            raise SettingsError("Network mode must be nat, isolated, none or bridge")
        cfg.network.mode = mode
    if "forwards" in changes:
        cfg.network.forwards = _forwards(list(changes["forwards"]))
    if "random_mac" in changes:
        cfg.network.random_mac = bool(changes["random_mac"])
    if "mac" in changes:
        value = (changes["mac"] or "").strip() or None
        if value and not _valid_mac(value):
            raise SettingsError(f"'{value}' is not a valid MAC address (aa:bb:cc:dd:ee:ff)")
        cfg.network.mac = value
    return cfg


def _valid_mac(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}", value))


def update_defaults(cfg: Config, **changes: Any) -> Config:
    from . import profiles
    for key in ("default_ram", "default_cpus", "default_disk_gb"):
        if key in changes:
            try:
                value = int(changes[key])
            except (TypeError, ValueError):
                raise SettingsError(f"{key.replace('_', ' ')} must be a number") from None
            if value <= 0:
                raise SettingsError(f"{key.replace('_', ' ')} must be positive")
            setattr(cfg, key, value)
    if "default_profile" in changes:
        key = (changes["default_profile"] or "").strip()
        profiles.get(key)  # raises KeyError for unknown tiers
        cfg.default_profile = key
    return cfg
