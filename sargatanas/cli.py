"""Command line interface.

Run `sargatanas --help` for the full surface. With no arguments it opens an
interactive menu; with `--gui` it launches the GTK app on Linux/macOS.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
from pathlib import Path

from . import APP_NAME, __version__, config, paths, profiles, qemu, vm

CYAN, GREEN, YELLOW, RED, DIM, BOLD, NC = (
    "\033[0;36m", "\033[0;32m", "\033[1;33m", "\033[0;31m",
    "\033[2m", "\033[1m", "\033[0m")


def _enable_windows_vt() -> None:
    """Turn on ANSI escape processing in old cmd/PowerShell consoles."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 0x0004)
    except Exception:  # noqa: BLE001 - colours are cosmetic
        pass


_enable_windows_vt()


def color(text: str, code: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"{code}{text}{NC}"


BANNER = r"""
  ██▀▀ ▄▀█ █▀█ █▀▀ ▄▀█ ▀█▀ ▄▀█ █▄░█ ▄▀█ █▀
  █▄▄ █▀█ █▀▄ █▄█ █▀█ ░█░ █▀█ █░▀█ █▀█ ▄█
"""

BANNER_ASCII = r"""
  sssss  aaa  rrrr   ggg  aaa  ttt  aaa  n   a
  s     a   a r   r g     a   a  t  a   a nn  a
  sssss aaaaa rrrr  g  gg aaaaa  t  aaaaa n n a
      s a   a r  r  g   g a   a  t  a   a n  na
  sssss a   a r   r  ggg  a   a  t  a   a n   a
"""


def _safe_text(text: str, fallback: str = "") -> str:
    """Return `text`, or `fallback`, depending on what the terminal can encode.

    Windows consoles still default to cp1252, where the block characters in
    BANNER raise UnicodeEncodeError and kill the CLI outright.
    """
    try:
        text.encode(sys.stdout.encoding or "utf-8")
        return text
    except (UnicodeEncodeError, LookupError):
        return fallback


def banner() -> None:
    art = _safe_text(BANNER) or BANNER_ASCII
    print(color(art, CYAN))
    print(f"  {color(f'{APP_NAME} {__version__} - sandboxed VMs on QEMU', DIM)}")
    print(f"  {color(f'data: {paths.home()}', DIM)}\n")


def fmt_size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "?"
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f} GB"
    return f"{max(size / (1024 * 1024), 0.1):.0f} MB"


def find_isos(limit: int = 20) -> list[Path]:
    """ISOs from the data dir first, then the usual download folders."""
    seen: set[Path] = set()
    found: list[Path] = []

    def add(p: Path) -> None:
        if p not in seen:
            seen.add(p)
            found.append(p)

    if paths.iso_dir().exists():
        for iso in sorted(paths.iso_dir().glob("*.iso")):
            add(iso)
    search = [Path.home() / "Downloads", Path.home() / "ISOs",
              Path.home() / "Desktop", *paths.extra_iso_search_paths()]
    for base in search:
        if base.exists():
            for iso in sorted(base.glob("*.iso")):
                add(iso)
    return found[:limit]


def require_qemu() -> bool:
    try:
        qemu.find_qemu()
        return True
    except qemu.QemuError as exc:
        print(color(f"[-] {exc}", RED))
        return False


# ---------- commands ----------

def cmd_list(_: argparse.Namespace) -> int:
    cfg = config.load()
    rows = vm.status(cfg)
    banner()
    if not rows:
        print(color("  No VMs yet. Create one: sargatanas create <name>", YELLOW))
        return 0
    print(f"  {'NAME':<20} {'STATE':<9} {'RAM':>6} {'CPU':>4}  {'PROFILE':<10} DISK")
    print("  " + _safe_text("─" * 70, "-" * 70))
    for row in rows:
        state = color("running", GREEN) if row.running else color("stopped", DIM)
        print(f"  {row.name:<20} {state:<9} {row.ram:>5}M {row.cpus:>4}  "
              f"{row.profile:<10} {fmt_size(row.disk)}")
    return 0


def cmd_images(args: argparse.Namespace) -> int:
    from . import cloud
    banner()
    if args.action == "pull":
        try:
            path = cloud.pull(args.key)
        except cloud.CloudError as exc:
            print(color(f"[-] {exc}", RED))
            return 1
        print(color(f"[+] Image ready: {path}", GREEN))
        return 0

    images = cloud.available_images()
    print(f"  {'KEY':<10} {'SIZE':>7}  STATUS       NAME")
    print("  " + _safe_text("─" * 66, "-" * 66))
    for key, img in images.items():
        present = cloud.image_path(key).exists()
        status = color("downloaded", GREEN) if present else color("missing", YELLOW)
        print(f"  {key:<10} {img.size_mb:>5} MB  {status:<20} {img.name}")
    print()
    print(color("  Pull with: sargatanas images pull <key>", DIM))
    print(color("  Cloud images are required for the tor/stealth tiers.", DIM))
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    iso = args.iso
    if args.cloud:
        from . import cloud
        if args.cloud not in cloud.available_images():
            print(color(f"[-] Unknown cloud image '{args.cloud}'. "
                        f"Known: {', '.join(cloud.available_images())}", RED))
            return 1
        dest = cloud.image_path(args.cloud)
        if dest.exists():
            print(color(f"[*] Using cached image: {dest.name}", DIM))
        else:
            print(color(f"[*] Downloading {args.cloud} cloud image (~"
                        f"{cloud.available_images()[args.cloud].size_mb} MB)…", YELLOW))
            try:
                cloud.pull(args.cloud)
            except cloud.CloudError as exc:
                print(color(f"[-] {exc}", RED))
                return 1
            print(color(f"[+] Image ready: {dest}", GREEN))
    if iso and iso.lower() in ("pick", "-"):
        isos = find_isos()
        if not isos:
            print(color("[-] No ISO found in the usual folders", RED))
            return 1
        for i, p in enumerate(isos, 1):
            print(f"  [{i}] {p.name}")
        choice = input("\n ISO: ").strip()
        iso = str(isos[int(choice) - 1])
    created = vm.create(args.name, iso=iso, ram=args.ram, cpus=args.cpus,
                        disk_gb=args.disk, profile=args.profile,
                        image=args.image, cloud=args.cloud)
    print(color(f"[+] VM '{created.name}' created ({args.disk or 'default'} GB disk)", GREEN))
    if vm.stale_seed(created.name) is False and args.cloud:
        from . import cloud
        print(color(f"    Guest login: root / {cloud.guest_login()}", CYAN))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if not require_qemu():
        return 1
    if vm.stale_seed(args.name):
        from . import cloud
        print(color(f"[!] '{args.name}' was seeded by an older version of Sargatanas.", YELLOW))
        print(color(f"    cloud-init only runs once, so the guest login may not work.", DIM))
        print(color(f"    Recreate it: sargatanas delete {args.name} -y && "
                    f"sargatanas create {args.name} --cloud alpine", DIM))
        print(color(f"    Login it sets: root / {cloud.guest_login()}", DIM))
        print()

    pid = vm.start(args.name, live=args.live, install=args.install,
                     headless=args.headless, profile=args.profile)
    mode = "live" if args.live else ("install" if args.install else "disk")
    print(color(f"[+] VM '{args.name}' started ({mode} boot, pid {pid})", GREEN))
    if not args.headless:
        print(color("    Close the QEMU window to stop it, or: sargatanas stop " + args.name, DIM))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    if vm.stop(args.name, force=args.force):
        print(color(f"[+] VM '{args.name}' stopped", GREEN))
        return 0
    print(color(f"[-] Could not stop '{args.name}' (try --force)", RED))
    return 1


def cmd_delete(args: argparse.Namespace) -> int:
    if not args.yes:
        answer = input(f" Delete '{args.name}' and its disk? [y/N]: ").strip().lower()
        if answer != "y":
            print(" Aborted")
            return 1
    vm.delete(args.name)
    print(color(f"[+] VM '{args.name}' deleted", GREEN))
    return 0


def cmd_isos(_: argparse.Namespace) -> int:
    isos = find_isos(limit=100)
    banner()
    if not isos:
        print(color(f"  No ISOs. Drop one in {paths.iso_dir()}", YELLOW))
        return 0
    for iso in isos:
        print(f"  {_safe_text('•', '*')} {iso.name:<45} {fmt_size(iso):>8}  {color(str(iso.parent), DIM)}")
    return 0


def cmd_boot(args: argparse.Namespace) -> int:
    if not require_qemu():
        return 1
    iso = Path(args.iso).expanduser()
    if not iso.exists():
        print(color(f"[-] ISO not found: {iso}", RED))
        return 1
    cfg = config.load()
    prof = profiles.get(args.profile or cfg.default_profile)
    spec = qemu.QemuSpec(name="quickboot", disk=None, ram_mb=args.ram or cfg.default_ram,
                         cpus=args.cpus or cfg.default_cpus, iso=iso, boot="d",
                         headless=args.headless)
    argv = qemu.build_argv(spec)
    argv += profiles.qemu_network_args(prof, forwards=[], socks_port=cfg.tor.socks_port)
    qemu.spawn(argv)
    print(color(f"[+] Booting {iso.name} ({prof.key} profile)", GREEN))
    return 0


def cmd_profiles(_: argparse.Namespace) -> int:
    banner()
    for key, prof in profiles.PROFILES.items():
        print(f"  {color(key, CYAN):<22} {prof.summary}")
        for limit in prof.describe_limits():
            print(f"  {'':<22} {color('· ' + limit, DIM)}")
    print()
    print(color("  Profiles are enforced at launch; unavailable ones refuse to start.", DIM))
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    banner()
    print(f"  QEMU binary : {_safe(qemu.find_qemu)}")
    print(f"  Acceleration: {_accel_name()}")
    print(f"  Tor socks   : {color('up', GREEN) if profiles.tor_socks_reachable() else color('down', YELLOW)}")
    free = shutil.disk_usage(paths.home().parent).free / (1024 ** 3)
    print(f"  Free disk   : {free:.1f} GB")
    return 0


def _accel_name() -> str:
    accel = qemu.accelerator()
    return accel[-1] if accel else color("none (software emulation)", YELLOW)


def _safe(fn) -> str:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - status output must not crash
        return color(str(exc), RED)


def cmd_repair(args: argparse.Namespace) -> int:
    if not vm.repair_boot(args.name):
        print(color(f"[-] Nothing to repair for '{args.name}' "
                    f"(no efi-vars.fd; x86 hosts do not use one)", YELLOW))
        return 1
    print(color(f"[+] UEFI NVRAM reset for '{args.name}'", GREEN))
    print(color("    Boot it again: sargatanas run " + args.name, DIM))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    vm.snapshot(args.name, args.snapshot)
    print(color(f"[+] Snapshot '{args.snapshot}' created", GREEN))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    vm.restore(args.name, args.snapshot)
    print(color(f"[+] Restored '{args.name}' to '{args.snapshot}'", GREEN))
    return 0


def cmd_tor(args: argparse.Namespace) -> int:
    from . import torctl
    cfg = config.load()
    if args.command == "identity":
        try:
            with torctl.TorControl(port=cfg.tor.control_port,
                                   password=cfg.tor.control_password) as tor:
                tor.authenticate()
                tor.newnym()
        except torctl.TorControlError as exc:
            print(color(f"[-] {exc}", RED))
            return 1
        print(color("[+] New Tor identity requested", GREEN))
        return 0
    if args.command == "check":
        up = profiles.tor_socks_reachable(port=cfg.tor.socks_port)
        print(f"  SOCKS 127.0.0.1:{cfg.tor.socks_port}: "
              f"{color('up', GREEN) if up else color('down', RED)}")
        return 0 if up else 1
    if args.command == "config":
        from . import torcfg
        text = torcfg.generate_torrc(cfg.tor)
        out = getattr(args, "output", None)
        if out:
            torcfg.write_torrc(cfg.tor, Path(out).expanduser())
            print(color(f"[+] torrc written to {out}", GREEN))
            print(color(f"    Review it, then start Tor with: tor -f {out}", DIM))
        else:
            print(text)
        return 0
    return 0


def cmd_vpn(args: argparse.Namespace) -> int:
    from . import vpn
    cfg = config.load()
    if args.vpn_command == "status":
        prov = vpn.installed_providers()
        print(f"  WireGuard : {color('installed', GREEN) if prov['wireguard'] else color('missing', YELLOW)}")
        print(f"  OpenVPN   : {color('installed', GREEN) if prov['openvpn'] else color('missing', YELLOW)}")
        iface = cfg.vpn.interface
        print(f"  Interface : {iface or color('(not configured)', DIM)}")
        if iface:
            up = profiles.vpn_interface_up(iface)
            print(f"  State     : {color('up', GREEN) if up else color('down', RED)}")
        print(f"  Kill switch: {color('available (Linux+iptables)', GREEN) if vpn.kill_switch_supported() else color('not available on this OS', DIM)}")
        return 0
    if args.vpn_command == "wireguard-config":
        print(vpn.wireguard_config_template(cfg.vpn))
        return 0
    if args.vpn_command == "openvpn-config":
        print(vpn.openvpn_config_template(cfg.vpn))
        return 0
    if args.vpn_command == "kill-switch":
        iface = args.interface or cfg.vpn.interface
        try:
            if args.action == "on":
                print(color("[!] Host-wide policy: it cuts internet if the tunnel drops.", YELLOW))
                cmds = vpn.install_kill_switch(iface)
            else:
                cmds = vpn.remove_kill_switch(iface)
        except vpn.VpnError as exc:
            print(color(f"[-] {exc}", RED))
            return 1
        for c in cmds:
            print(f"    {c}")
        print(color(f"[+] Kill switch {'enabled' if args.action == 'on' else 'removed'} for {iface}", GREEN))
        return 0
    return 0


def cmd_firewall(args: argparse.Namespace) -> int:
    prof = profiles.get(args.profile)
    banner()
    print(f"  {color(prof.key, CYAN)} - {prof.summary}")
    print()
    for rule in prof.firewall_rules():
        print(f"    · {rule}")
    print()
    print(color("  These rules are enforced by QEMU's network backend at launch.", DIM))
    return 0


def cmd_security(_: argparse.Namespace) -> int:
    from . import vpn
    cfg = config.load()
    banner()
    print(f"  {color('Protections', BOLD)}")
    tor_up = profiles.tor_socks_reachable(port=cfg.tor.socks_port)
    print(f"    Tor SOCKS  : {color('up', GREEN) if tor_up else color('down', YELLOW)} "
          f"({'installed' if profiles.tor_installed() else 'not installed'})")
    prov = vpn.installed_providers()
    print(f"    WireGuard  : {'installed' if prov['wireguard'] else 'missing'}")
    print(f"    OpenVPN    : {'installed' if prov['openvpn'] else 'missing'}")
    print(f"    Seccomp    : {color('available', GREEN) if qemu.sandbox_supported() else color('unavailable on this QEMU', DIM)}")
    if cfg.network.random_mac:
        print(f"    MAC        : {color('random per boot', GREEN)}")
    else:
        print(f"    MAC        : {cfg.network.mac or 'QEMU default (52:54:00:12:34:56)'}")
    print()
    print(f"  {color('Isolation modes', BOLD)}")
    for mode, desc in profiles.ISOLATION_MODES.items():
        print(f"    {color(mode, CYAN):<12} {desc}")
    print()
    print(f"  {color('Tiers', BOLD)}  (see 'sargatanas profiles' and 'sargatanas firewall <profile>')")
    print(f"    {color('paranoid > isolated > stealth > tor/vpn > pentesting > nat > restricted', DIM)}")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    if getattr(args, "gtk", False):
        return _gui_gtk()
    # Tkinter is stdlib and works on Linux, macOS and Windows.
    try:
        from .gui_tk import main as tk_main
    except ImportError as exc:
        print(color(f"[-] Tkinter GUI unavailable: {exc}", RED))
        print("    Install a Python with Tk, or use: sargatanas gui --gtk")
        return 1
    try:
        return tk_main()
    except Exception as exc:  # noqa: BLE001 - top-level UI boundary
        print(color(f"[-] Could not start the GUI: {exc}", RED))
        print("    If there is no display server, use the CLI instead.")
        return 1


def _gui_gtk() -> int:
    try:
        from .gui import main as gui_main
    except ImportError as exc:
        print(color(f"[-] GTK GUI unavailable: {exc}", RED))
        print("    Linux: sudo apt install python3-gi gir1.2-gtk-3.0")
        print("    macOS: brew install pygobject3 gtk+3")
        return 1
    return gui_main()


def cmd_menu(_: argparse.Namespace) -> int:
    while True:
        banner()
        print("  [1] List VMs        [4] Delete VM")
        print("  [2] Create VM       [5] Quick boot ISO")
        print("  [3] Run VM          [6] Status")
        print("  [q] Quit\n")
        choice = input("  sargatanas> ").strip().lower()
        if choice == "q":
            return 0
        if choice == "1":
            cmd_list(argparse.Namespace())
        elif choice == "5":
            isos = find_isos()
            for i, p in enumerate(isos, 1):
                print(f"  [{i}] {p.name}")
            path = input("  ISO path or number: ").strip()
            try:
                path = str(isos[int(path) - 1])
            except (ValueError, IndexError):
                pass
            cmd_boot(argparse.Namespace(iso=path, ram=None, cpus=None,
                                        profile=None, headless=False))
        elif choice == "6":
            cmd_status(argparse.Namespace())
        elif choice in ("2", "3", "4"):
            print(color("  Use the CLI form for now: sargatanas "
                        f"{ {'2': 'create', '3': 'run', '4': 'delete'}[choice]} ...", DIM))
        else:
            continue
        input("\n  Enter to continue...")


# ---------- argument parsing ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sargatanas",
        description=f"{APP_NAME} - sandboxed virtual machines on QEMU.",
        epilog="Profiles: " + ", ".join(profiles.PROFILES),
    )
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("list", help="List VMs and their state")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("create", help="Create a VM")
    s.add_argument("name")
    s.add_argument("--iso", help="ISO path, or 'pick' to choose from a list")
    s.add_argument("--image", help="Cloud image (.qcow2) instead of a blank disk")
    s.add_argument("--cloud", help="Cloud image key (alpine, debian); downloads if missing")
    s.add_argument("--ram", type=int, help="RAM in MB")
    s.add_argument("--cpus", type=int)
    s.add_argument("--disk", type=int, help="Disk size in GB")
    s.add_argument("--profile", choices=list(profiles.PROFILES))
    s.set_defaults(func=cmd_create)

    for name, help_text in (("run", "Boot from disk"), ("live", "Boot the ISO without saving changes"),
                            ("install", "Boot the ISO installer")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("name")
        s.add_argument("--profile", choices=list(profiles.PROFILES))
        s.add_argument("--headless", action="store_true")
        s.set_defaults(func=cmd_run, live=name == "live", install=name == "install")

    s = sub.add_parser("stop", help="Stop a running VM")
    s.add_argument("name")
    s.add_argument("--force", action="store_true", help="SIGKILL instead of SIGTERM")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("delete", help="Delete a VM and its disk")
    s.add_argument("name")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("isos", help="List discovered ISOs")
    s.set_defaults(func=cmd_isos)

    s = sub.add_parser("boot", help="Quick boot an ISO with no disk")
    s.add_argument("iso")
    s.add_argument("--ram", type=int)
    s.add_argument("--cpus", type=int)
    s.add_argument("--profile", choices=list(profiles.PROFILES))
    s.add_argument("--headless", action="store_true")
    s.set_defaults(func=cmd_boot)

    s = sub.add_parser("images", help="List or download cloud images")
    img_sub = s.add_subparsers(dest="action")
    img_sub.add_parser("list", help="List known cloud images")
    img_pull = img_sub.add_parser("pull", help="Download a cloud image")
    img_pull.add_argument("key", choices=["alpine", "debian"])
    s.set_defaults(func=cmd_images, action="list")

    s = sub.add_parser("profiles", help="Explain the security profiles")
    s.set_defaults(func=cmd_profiles)

    s = sub.add_parser("security", help="Security center: protections, modes, status")
    s.set_defaults(func=cmd_security)

    s = sub.add_parser("firewall", help="Show the firewall rules a profile enforces")
    s.add_argument("profile", choices=list(profiles.PROFILES))
    s.set_defaults(func=cmd_firewall)

    s = sub.add_parser("status", help="Host and tooling status")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("repair", help="Fix a VM stuck at the UEFI shell")
    s.add_argument("name")
    s.set_defaults(func=cmd_repair)

    s = sub.add_parser("snapshot", help="Snapshot a stopped VM's disk")
    s.add_argument("name")
    s.add_argument("snapshot")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("restore", help="Restore a stopped VM from a snapshot")
    s.add_argument("name")
    s.add_argument("snapshot")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("tor", help="Tor control and config")
    tor_sub = s.add_subparsers(dest="command")
    tor_sub.add_parser("identity", help="Ask Tor for a new circuit")
    tor_sub.add_parser("check", help="Check the SOCKS port")
    tor_cfg = tor_sub.add_parser("config", help="Generate a torrc (bridges, exit nodes, DNS)")
    tor_cfg.add_argument("--output", help="Write to a file instead of stdout")
    s.set_defaults(func=cmd_tor)

    s = sub.add_parser("vpn", help="VPN status, configs and kill switch")
    vpn_sub = s.add_subparsers(dest="vpn_command")
    vpn_sub.add_parser("status", help="VPN tools and tunnel status")
    vpn_sub.add_parser("wireguard-config", help="Print a WireGuard config template")
    vpn_sub.add_parser("openvpn-config", help="Print an OpenVPN config template")
    ks = vpn_sub.add_parser("kill-switch", help="Enable/remove the OpenVPN kill switch (Linux)")
    ks.add_argument("action", choices=["on", "off"])
    ks.add_argument("--interface", help="Tunnel interface (default: config or tun0)")
    s.set_defaults(func=cmd_vpn)

    s = sub.add_parser("gui", help="Launch the graphical interface (Tkinter)")
    s.add_argument("--gtk", action="store_true", help="Use the GTK interface instead")
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("menu", help="Interactive menu")
    s.set_defaults(func=cmd_menu)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        return cmd_menu(args)

    paths.ensure_dirs()
    if config.migrate_legacy_if_needed():
        print(color(f"[*] Migrated settings from {paths.legacy_home()}", YELLOW))
    try:
        return args.func(args)
    except (vm.VmError, qemu.QemuError, KeyError) as exc:
        print(color(f"[-] {exc}", RED))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
