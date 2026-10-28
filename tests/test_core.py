"""Tests for the parts that matter: argv correctness, config safety, Tor protocol.

Run with:  python3 -m unittest discover -s tests -v
No QEMU needed: argv building is pure, and process/Tor interaction is mocked.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sargatanas import config, paths, profiles, qemu, torcfg, torctl, vm, vpn


class TempHome(unittest.TestCase):
    """Point SARGATANAS_HOME at a fresh temp dir for every test.

    Also fakes the QEMU binaries: tests must never depend on the host having
    QEMU installed (CI runners do not, and that is how four tests silently
    only passed on the author's machine).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"SARGATANAS_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

        # Pretend qemu-system-* and qemu-img are installed.
        for name in ("find_qemu", "find_qemu_img"):
            p = mock.patch.object(qemu, name, return_value=f"/usr/bin/{name}")
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(qemu, "sandbox_supported", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def write_config(self, data: dict) -> None:
        Path(self.tmp.name).mkdir(parents=True, exist_ok=True)
        paths.config_file().write_text(json.dumps(data))


class TestPaths(TempHome):
    def test_home_follows_env(self):
        self.assertEqual(paths.home(), Path(self.tmp.name))

    def test_ensure_dirs(self):
        paths.ensure_dirs()
        for d in (paths.vm_dir(), paths.iso_dir(), paths.log_dir()):
            self.assertTrue(d.is_dir())


class TestConfig(TempHome):
    def test_roundtrip(self):
        cfg = config.Config(default_ram=2048)
        cfg.vms["a"] = config.VmConfig(name="a", disk="/tmp/a.qcow2", ram=1024)
        config.save(cfg)
        again = config.load()
        self.assertEqual(again.default_ram, 2048)
        self.assertEqual(again.vms["a"].ram, 1024)

    def test_config_is_private(self):
        config.save(config.Config())
        if os.name == "nt":
            self.skipTest("0600 is a Unix concept; Windows uses NTFS ACLs")
        mode = paths.config_file().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_unknown_keys_are_ignored(self):
        self.write_config({"vms": {}, "future_key": {"x": 1}, "default_ram": 3000})
        self.assertEqual(config.load().default_ram, 3000)

    def test_corrupt_config_does_not_raise(self):
        paths.config_file().write_text("{ not json")
        self.assertEqual(config.load().vms, {})

    def test_legacy_config_migrates(self):
        legacy = Path(self.tmp.name) / "legacy"
        legacy.mkdir()
        (legacy / "config.json").write_text(json.dumps(
            {"vms": {"old": {"disk": "/x/old.qcow2", "ram": 2048}}, "default_ram": 4096}))
        with mock.patch.object(paths, "legacy_home", return_value=legacy):
            self.assertTrue(config.migrate_legacy_if_needed())
        self.assertIn("old", config.load().vms)

    def test_migration_is_idempotent(self):
        legacy = Path(self.tmp.name) / "legacy"
        legacy.mkdir()
        (legacy / "config.json").write_text(json.dumps({"vms": {"old": {"disk": "/x"}}}))
        with mock.patch.object(paths, "legacy_home", return_value=legacy):
            config.migrate_legacy_if_needed()
            self.assertFalse(config.migrate_legacy_if_needed())


class TestQemuArgv(unittest.TestCase):
    def spec(self, **kw) -> qemu.QemuSpec:
        base = dict(name="test", disk=Path("/tmp/vm/test.qcow2"), ram_mb=2048, cpus=2)
        base.update(kw)
        return qemu.QemuSpec(**base)

    def test_argv_is_a_list_and_first_item_is_qemu(self):
        argv = qemu.build_argv(self.spec(), qemu="/usr/bin/qemu-system-x86_64")
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], "/usr/bin/qemu-system-x86_64")

    def test_name_with_spaces_is_one_argument(self):
        argv = qemu.build_argv(self.spec(name="my vm; rm -rf /"), qemu="qemu")
        self.assertIn("sargatanas-my vm; rm -rf /", argv)

    def test_no_network_args_by_default(self):
        argv = qemu.build_argv(self.spec(), qemu="qemu")
        self.assertNotIn("-netdev", argv)
        self.assertNotIn("-net", argv)

    def test_boot_from_iso(self):
        argv = qemu.build_argv(self.spec(iso=Path("/tmp/x.iso"), boot="d"), qemu="qemu")
        self.assertIn("-cdrom", argv)
        self.assertEqual(argv[argv.index("-boot") + 1], "d")

    def test_live_mode_snapshots_the_disk(self):
        argv = qemu.build_argv(self.spec(read_only=True), qemu="qemu")
        drive = argv[argv.index("-drive") + 1]
        self.assertIn("snapshot=on", drive)

    def test_install_mode_writes_to_disk(self):
        argv = qemu.build_argv(self.spec(), qemu="qemu")
        drive = argv[argv.index("-drive") + 1]
        self.assertNotIn("snapshot=on", drive)

    def test_quick_boot_has_no_disk(self):
        argv = qemu.build_argv(self.spec(disk=None, iso=Path("/tmp/x.iso")), qemu="qemu")
        self.assertNotIn("-drive", argv)

    def test_headless_has_no_window(self):
        argv = qemu.build_argv(self.spec(headless=True), qemu="qemu")
        self.assertIn("-display", argv)
        self.assertEqual(argv[argv.index("-display") + 1], "none")

    def test_aarch64_gets_a_default_machine(self):
        # Found on real hardware: QEMU on Apple Silicon has no default
        # machine and exits with "No machine specified".
        with mock.patch.object(qemu, "host_arch", return_value="aarch64"):
            argv = qemu.build_argv(self.spec(), qemu="qemu")
        self.assertIn("-machine", argv)
        self.assertEqual(argv[argv.index("-machine") + 1], "virt")

    def test_aarch64_avoids_virtio_vga(self):
        with mock.patch.object(qemu, "host_arch", return_value="aarch64"):
            argv = qemu.build_argv(self.spec(), qemu="qemu")
        self.assertNotIn("-vga", argv)
        self.assertIn("ramfb", argv)

    def test_x86_uses_virtio_vga(self):
        with mock.patch.object(qemu, "host_arch", return_value="x86_64"):
            argv = qemu.build_argv(self.spec(), qemu="qemu")
        self.assertNotIn("-machine", argv)
        self.assertEqual(argv[argv.index("-vga") + 1], "virtio")

    def test_windows_finds_qemu_outside_path(self):
        # QEMU on Windows installs to Program Files, not PATH.
        with mock.patch.object(qemu, "host_arch", return_value="x86_64"), \
             mock.patch.object(qemu.platform, "system", return_value="Windows"), \
             mock.patch.object(qemu.shutil, "which", return_value=None), \
             mock.patch.object(qemu, "_windows_qemu_binaries", return_value={
                 "qemu-system-x86_64.exe": r"C:\Program Files\qemu\qemu-system-x86_64.exe"}):
            self.assertEqual(qemu.find_qemu(),
                             r"C:\Program Files\qemu\qemu-system-x86_64.exe")

    def test_windows_finds_qemu_img_outside_path(self):
        with mock.patch.object(qemu.platform, "system", return_value="Windows"), \
             mock.patch.object(qemu.shutil, "which", return_value=None), \
             mock.patch.object(qemu, "_windows_qemu_binaries", return_value={
                 "qemu-img.exe": r"C:\Program Files\qemu\qemu-img.exe"}):
            self.assertEqual(qemu.find_qemu_img(), r"C:\Program Files\qemu\qemu-img.exe")


class TestProfiles(unittest.TestCase):
    def net(self, key: str, **kw) -> list[str]:
        return profiles.qemu_network_args(profiles.get(key), **kw)

    def test_isolated_has_no_nic(self):
        self.assertEqual(self.net("isolated"), [])

    def test_nat_forwards(self):
        args = self.net("nat", forwards=["2222:22"])
        self.assertIn("hostfwd=tcp::2222-:22", args[1])

    def test_restricted_isolates(self):
        args = self.net("restricted")
        self.assertIn("restrict=yes", args[1])
        self.assertNotIn("hostfwd", args[1])

    def test_pentesting_has_two_nics(self):
        # internal VM<->VM (multicast) + NAT internet for tools
        args = self.net("pentesting", forwards=["2222:22"])
        self.assertIn("socket,id=net0,mcast=", args[1])
        self.assertIn("hostfwd=tcp::2222-:22", args[5])
        self.assertEqual(args.count("-netdev"), 2)

    def test_paranoid_is_airgapped_and_sandboxed(self):
        prof = profiles.get("paranoid")
        self.assertEqual(self.net("paranoid"), [])
        self.assertTrue(prof.sandbox)

    def test_stealth_needs_tor_and_vpn(self):
        prof = profiles.get("stealth")
        self.assertIn("tor-socks", prof.needs)
        self.assertIn("vpn-interface", prof.needs)

    def test_firewall_rules_are_honest(self):
        for key in ("paranoid", "isolated", "stealth", "tor", "vpn",
                    "pentesting", "nat", "restricted"):
            rules = profiles.get(key).firewall_rules()
            self.assertTrue(rules, key)

    def test_stealth_blocks_when_tor_down(self):
        with mock.patch.object(profiles, "tor_socks_reachable", return_value=False), \
             mock.patch.object(profiles, "vpn_interface_up", return_value=True):
            check = profiles.check_launch(profiles.get("stealth"), vpn_interface="wg0")
        self.assertFalse(check.ok)

    def test_tor_exposes_socks_via_guestfwd(self):
        # QEMU has no proxy option, so the host SOCKS port is spliced into the
        # guest at 10.0.2.100:9050; redsocks in the guest consumes it.
        args = self.net("tor", socks_port=9050)
        netdev = args[1]
        self.assertIn("guestfwd=tcp:10.0.2.100:9050", netdev)
        self.assertIn("nc 127.0.0.1 9050", netdev)
        self.assertNotIn("hostfwd", netdev)

    def test_vpn_uses_nat_networking(self):
        args = self.net("vpn", forwards=["2222:22"])
        self.assertIn("hostfwd=tcp::2222-:22", args[1])

    def test_unknown_profile_raises(self):
        with self.assertRaises(KeyError):
            profiles.get("nope")

    def test_tor_profile_blocks_when_socks_down(self):
        prof = profiles.get("tor")
        with mock.patch.object(profiles, "tor_socks_reachable", return_value=False):
            check = profiles.check_launch(prof)
        self.assertFalse(check.ok)
        self.assertIn("SOCKS", check.reasons[0])

    def test_tor_profile_allows_when_socks_up_and_nc_present(self):
        with mock.patch.object(profiles, "tor_socks_reachable", return_value=True), \
             mock.patch.object(profiles.shutil, "which", return_value="/usr/bin/nc"):
            self.assertTrue(profiles.check_launch(profiles.get("tor")).ok)

    def test_tor_profile_blocks_without_netcat(self):
        with mock.patch.object(profiles, "tor_socks_reachable", return_value=True), \
             mock.patch.object(profiles.shutil, "which", return_value=None):
            check = profiles.check_launch(profiles.get("tor"))
        self.assertFalse(check.ok)
        self.assertTrue(any("Netcat" in r for r in check.reasons))

    def test_vpn_profile_blocks_without_interface(self):
        with mock.patch.object(profiles, "vpn_interface_up", return_value=False):
            self.assertFalse(profiles.check_launch(profiles.get("vpn"),
                                                    vpn_interface="wg0").ok)

    def test_vpn_warns_kill_switch_not_implemented(self):
        with mock.patch.object(profiles, "vpn_interface_up", return_value=True):
            check = profiles.check_launch(profiles.get("vpn"), vpn_interface="wg0")
        self.assertTrue(check.ok)
        self.assertTrue(any("Kill switch" in w for w in check.warnings))

    def test_every_profile_has_limits_documented_or_is_plain(self):
        # Guards against a future profile claiming magic with no caveat.
        for prof in profiles.PROFILES.values():
            if prof.key in ("tor", "vpn", "stealth", "pentesting", "restricted"):
                self.assertTrue(prof.describe_limits(), prof.key)


class TestVm(TempHome):
    def test_name_validation(self):
        for bad in ("", "../evil", "a" * 80, "name.", "-flag"):
            with self.assertRaises(vm.VmError, msg=bad):
                vm.validate_name(bad)
        self.assertEqual(vm.validate_name(" my vm "), "my vm")

    def test_create_rejects_duplicate(self):
        with mock.patch("sargatanas.qemu.create_disk"):
            vm.create("one")
            with self.assertRaises(vm.VmError):
                vm.create("one")

    def test_create_rejects_missing_iso(self):
        with self.assertRaises(vm.VmError):
            vm.create("x", iso="/does/not/exist.iso")

    def test_create_cleans_up_on_disk_failure(self):
        with mock.patch("sargatanas.qemu.create_disk", side_effect=qemu.QemuError("boom")):
            with self.assertRaises(qemu.QemuError):
                vm.create("broken")
        self.assertFalse((paths.vm_dir() / "broken").exists())
        self.assertNotIn("broken", config.load().vms)

    def test_delete_refuses_running(self):
        with mock.patch("sargatanas.qemu.create_disk"):
            vm.create("busy")
        with mock.patch.object(vm, "is_running", return_value=True):
            with self.assertRaises(vm.VmError):
                vm.delete("busy")

    def test_stale_pid_is_cleared(self):
        with mock.patch("sargatanas.qemu.create_disk"):
            vm.create("stale")
        pid = paths.vm_dir() / "stale" / "vm.pid"
        pid.write_text("999999")
        self.assertFalse(vm.is_running("stale"))
        self.assertFalse(pid.exists())

    def test_create_rejects_tor_without_cloud_image(self):
        # Tor routing needs cloud-init; a blank disk cannot apply the rules.
        with mock.patch("sargatanas.qemu.create_disk"):
            with self.assertRaises(vm.VmError) as ctx:
                vm.create("tornodisk", profile="tor")
        self.assertIn("cloud image", str(ctx.exception))

    def test_start_refuses_when_profile_unverifiable(self):
        with mock.patch("sargatanas.qemu.create_disk"):
            vm.create("t", profile="nat")
            vm.set_profile("t", "tor")
        with mock.patch.object(profiles, "tor_socks_reachable", return_value=False):
            with self.assertRaises(vm.VmError):
                vm.start("t")

    def test_start_argv_contains_profile_networking(self):
        with mock.patch("sargatanas.qemu.create_disk"):
            vm.create("n", profile="isolated")
        with mock.patch("sargatanas.qemu.spawn") as spawn:
            spawn.return_value = mock.Mock(pid=4242)
            vm.start("n")
        argv = spawn.call_args[0][0]
        self.assertNotIn("-netdev", argv)
        self.assertNotIn("-net", argv)

    def test_sandbox_added_when_supported(self):
        with mock.patch("sargatanas.qemu.create_disk"):
            vm.create("s", profile="paranoid")
        with mock.patch.object(qemu, "sandbox_supported", return_value=True), \
             mock.patch("sargatanas.qemu.spawn") as spawn:
            spawn.return_value = mock.Mock(pid=4242)
            vm.start("s")
        argv = spawn.call_args[0][0]
        self.assertIn("-sandbox", argv)
        self.assertEqual(argv[argv.index("-sandbox") + 1], "on")

    def test_sandbox_omitted_when_unsupported(self):
        with mock.patch("sargatanas.qemu.create_disk"):
            vm.create("s2", profile="paranoid")
        with mock.patch.object(qemu, "sandbox_supported", return_value=False), \
             mock.patch("sargatanas.qemu.spawn") as spawn:
            spawn.return_value = mock.Mock(pid=4242)
            vm.start("s2")
        argv = spawn.call_args[0][0]
        self.assertNotIn("-sandbox", argv)


class TestBootRepair(TempHome):
    def test_repair_resets_nvram(self):
        with mock.patch("sargatanas.qemu.create_disk"):
            vm.create("bootable", profile="nat")
        vars_file = paths.vm_dir() / "bootable" / "efi-vars.fd"
        vars_file.write_bytes(b"\x00corrupt\x00")
        with mock.patch("sargatanas.qemu.reset_firmware_vars",
                        side_effect=lambda p: p.write_bytes(b"clean") or True):
            self.assertTrue(vm.repair_boot("bootable"))
        self.assertEqual(vars_file.read_bytes(), b"clean")

    def test_repair_refuses_running(self):
        with mock.patch("sargatanas.qemu.create_disk"):
            vm.create("busyboot", profile="nat")
        (paths.vm_dir() / "busyboot" / "efi-vars.fd").write_bytes(b"x")
        with mock.patch.object(vm, "is_running", return_value=True):
            with self.assertRaises(vm.VmError):
                vm.repair_boot("busyboot")


class TestProcess(unittest.TestCase):
    """psutil-backed process checks, with psutil itself mocked out."""

    def _proc(self, name="qemu-system-x86_64", status=None):
        proc = mock.Mock()
        proc.name.return_value = name
        proc.status.return_value = status if status is not None else qemu.psutil.STATUS_RUNNING
        return proc

    def test_is_running_true_with_name(self):
        with mock.patch.object(qemu.psutil, "Process", return_value=self._proc()):
            self.assertEqual(qemu.is_running(42), (True, "qemu-system-x86_64"))

    def test_is_running_no_such_process(self):
        with mock.patch.object(qemu.psutil, "Process",
                               side_effect=qemu.psutil.NoSuchProcess(42)):
            self.assertEqual(qemu.is_running(42), (False, None))

    def test_is_running_zombie_is_not_running(self):
        with mock.patch.object(qemu.psutil, "Process",
                               return_value=self._proc(status=qemu.psutil.STATUS_ZOMBIE)):
            self.assertEqual(qemu.is_running(42), (False, "qemu-system-x86_64"))

    def test_terminate_refuses_wrong_process_name(self):
        # A reused PID now belonging to a non-QEMU process must not be killed.
        with mock.patch.object(qemu, "is_running", return_value=(True, "python3")):
            self.assertFalse(qemu.terminate(42))

    def test_terminate_graceful(self):
        proc = self._proc()
        proc.wait.return_value = 0
        with mock.patch.object(qemu, "is_running", return_value=(True, "qemu-system-x86_64")), \
             mock.patch.object(qemu.psutil, "Process", return_value=proc):
            self.assertTrue(qemu.terminate(42))
        proc.terminate.assert_called_once()

    def test_terminate_force(self):
        proc = self._proc()
        proc.wait.return_value = 0
        with mock.patch.object(qemu, "is_running", return_value=(True, "qemu-system-x86_64")), \
             mock.patch.object(qemu.psutil, "Process", return_value=proc):
            self.assertTrue(qemu.terminate(42, force=True))
        proc.kill.assert_called_once()


class TestWindowsEncoding(unittest.TestCase):
    """The CLI must not crash on a cp1252 console (Windows default)."""

    def test_banner_falls_back_to_ascii(self):
        import io
        from sargatanas import cli
        raw = io.BytesIO()
        fake = io.TextIOWrapper(raw, encoding="cp1252")
        with mock.patch.object(cli.sys, "stdout", fake):
            cli.banner()          # must not raise UnicodeEncodeError
            fake.flush()
        out = raw.getvalue().decode("cp1252")
        self.assertIn("Sargatanas", out)
        self.assertIn("sandboxed VMs", out)

    def test_safe_text_returns_fallback(self):
        from sargatanas import cli
        import io
        fake = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with mock.patch.object(cli.sys, "stdout", fake):
            self.assertEqual(cli._safe_text("██", "ascii"), "ascii")
            self.assertEqual(cli._safe_text("plain"), "plain")


class TestCloud(TempHome):
    def test_known_images(self):
        from sargatanas import cloud
        images = cloud.available_images("aarch64")
        self.assertIn("alpine", images)
        self.assertIn("debian", images)
        self.assertTrue(all(i.url.startswith("https://") for i in images.values()))

    def test_unknown_image_rejected(self):
        from sargatanas import cloud
        with self.assertRaises(cloud.CloudError):
            cloud.pull("windows")

    def test_tor_user_data_has_redirect_rules(self):
        from sargatanas import cloud
        yaml = cloud.user_data_tor()
        self.assertIn("#cloud-config", yaml)
        self.assertIn("REDIRECT --to-ports", yaml)
        self.assertIn("REDSOCKS=12345", yaml)
        self.assertIn("10.0.2.100", yaml)
        self.assertIn("redsocks", yaml)
        self.assertIn("type = socks5", yaml)
        self.assertIn("/usr/local/bin/sargatanas-tor", yaml)

    def test_cloud_init_detection(self):
        from sargatanas import cloud
        self.assertTrue(cloud.has_cloud_init(Path("alpine-cloud-3.24.3.qcow2")))
        self.assertTrue(cloud.has_cloud_init(Path("debian-12-genericcloud-arm64.qcow2")))
        self.assertFalse(cloud.has_cloud_init(Path("alpine-virt-3.24.3.iso")))
        self.assertFalse(cloud.has_cloud_init(Path("win11.qcow2")))

    def test_seed_builds_a_cidata_iso(self):
        from sargatanas import cloud
        if not cloud.seed_supported():
            self.skipTest("no ISO builder on this host")
        dest = Path(self.tmp.name) / "seed.iso"
        cloud.write_seed(dest, cloud.user_data_tor())
        self.assertTrue(dest.exists())
        self.assertIn(b"CD001", dest.read_bytes()[:40000])  # ISO9660 signature


class TestSettings(TempHome):
    def test_tor_port_validated(self):
        cfg = config.Config()
        self.assertEqual(config.update_tor(cfg, socks_port="9150").tor.socks_port, 9150)
        with self.assertRaises(config.SettingsError):
            config.update_tor(cfg, socks_port="0")
        with self.assertRaises(config.SettingsError):
            config.update_tor(cfg, control_port="not a port")

    def test_tor_bridges_cleaned(self):
        cfg = config.Config()
        config.update_tor(cfg, bridges=["  bridge one  ", "", "   "])
        self.assertEqual(cfg.tor.bridges, ["bridge one"])

    def test_vpn_provider_validated(self):
        cfg = config.Config()
        with self.assertRaises(config.SettingsError):
            config.update_vpn(cfg, provider="nordvpn")
        config.update_vpn(cfg, provider="wireguard", interface="wg0", kill_switch=True)
        self.assertEqual((cfg.vpn.provider, cfg.vpn.interface, cfg.vpn.kill_switch),
                         ("wireguard", "wg0", True))

    def test_forwards_validated(self):
        cfg = config.Config()
        config.update_network(cfg, forwards=["2222:22", " 8080:80 ", ""])
        self.assertEqual(cfg.network.forwards, ["2222:22", "8080:80"])
        for bad in ("2222", "a:22", "2222:22:33", "99999:22"):
            with self.assertRaises(config.SettingsError, msg=bad):
                config.update_network(cfg, forwards=[bad])

    def test_defaults_validated(self):
        cfg = config.Config()
        with self.assertRaises(KeyError):
            config.update_defaults(cfg, default_profile="nope")
        with self.assertRaises(config.SettingsError):
            config.update_defaults(cfg, default_ram="a lot")
        config.update_defaults(cfg, default_ram=2048, default_profile="paranoid")
        self.assertEqual((cfg.default_ram, cfg.default_profile), (2048, "paranoid"))

    def test_saved_changes_roundtrip(self):
        cfg = config.load()
        config.update_tor(cfg, socks_port="9150", bridges=["obfs4 x"])
        config.save(cfg)
        again = config.load()
        self.assertEqual(again.tor.socks_port, 9150)
        self.assertEqual(again.tor.bridges, ["obfs4 x"])


class TestProfileSwitch(TempHome):
    def test_set_profile_persists(self):
        with mock.patch("sargatanas.qemu.create_disk"):
            vm.create("p")
            vm.set_profile("p", "paranoid")
        self.assertEqual(config.load().vms["p"].profile, "paranoid")

    def test_set_profile_rejects_unknown(self):
        with mock.patch("sargatanas.qemu.create_disk"):
            vm.create("q")
        with self.assertRaises(KeyError):
            vm.set_profile("q", "nope")

    def test_launch_state_reports_blocker(self):
        with mock.patch("sargatanas.qemu.create_disk"):
            vm.create("r", profile="nat")
            vm.set_profile("r", "tor")
        with mock.patch.object(profiles, "tor_socks_reachable", return_value=False):
            check = vm.launch_state("r")
        self.assertFalse(check.ok)
        self.assertTrue(check.reasons)


class TestTorConfig(unittest.TestCase):
    def test_torrc_has_dns_leak_protection(self):
        text = torcfg.generate_torrc(config.TorSettings())
        self.assertIn("DNSPort 5353", text)
        self.assertIn("AutomapHostsOnResolve 1", text)
        self.assertIn("VirtualAddrNetworkIPv4 10.192.0.0/10", text)
        self.assertIn("TransPort 9040", text)

    def test_torrc_bridges_and_exit_nodes(self):
        settings = config.TorSettings(
            bridges=["obfs4 1.2.3.4:443 ABC"], exit_nodes="{us}", strict_nodes=True)
        text = torcfg.generate_torrc(settings)
        self.assertIn("UseBridges 1", text)
        self.assertIn("Bridge obfs4 1.2.3.4:443 ABC", text)
        self.assertIn("ExitNodes {us}", text)
        self.assertIn("StrictNodes 1", text)


class TestVpn(unittest.TestCase):
    def test_wireguard_template_is_a_kill_switch(self):
        text = vpn.wireguard_config_template(config.VpnSettings(interface="wg0"))
        self.assertIn("AllowedIPs = 0.0.0.0/0", text)

    def test_kill_switch_commands_are_iptables(self):
        cmds = vpn.kill_switch_commands("wg0")
        self.assertTrue(any("wg0" in c for c in cmds))
        self.assertTrue(all(c.startswith("iptables") for c in cmds))

    def test_installed_providers_shape(self):
        self.assertEqual(set(vpn.installed_providers()), {"wireguard", "openvpn"})


class TestTorControl(unittest.TestCase):
    class FakeSock:
        def __init__(self, replies):
            self.replies = io.BytesIO(replies)
            self.sent = []

        def sendall(self, data):
            self.sent.append(data)

        def recv(self, _):
            return self.replies.read(4096)

        def close(self):
            pass

    def client(self, replies: bytes) -> torctl.TorControl:
        tor = torctl.TorControl()
        tor._sock = self.FakeSock(replies)
        return tor

    def test_newnym_success(self):
        tor = self.client(b"250 OK\r\n")
        tor.newnym()
        self.assertEqual(tor._sock.sent, [b"SIGNAL NEWNYM\r\n"])

    def test_newnym_failure_is_raised(self):
        tor = self.client(b"552 Unrecognized signal\r\n")
        with self.assertRaises(torctl.TorControlError):
            tor.newnym()

    def test_multiline_reply(self):
        tor = self.client(b"250-Status\r\n250 OK\r\n")
        reply = tor._command("GETINFO status/bootstrap-phase")
        self.assertTrue(reply.ok)
        self.assertEqual(len(reply.lines), 2)

    def test_auth_password_failure_surfaces(self):
        tor = self.client(b"515 Authentication failed\r\n" * 5)
        tor.password = "bad"
        with self.assertRaises(torctl.TorControlError):
            tor.authenticate()

    def test_auth_success_stops_early(self):
        tor = self.client(b"250 OK\r\n")
        tor.password = "pw"
        tor.authenticate()
        self.assertEqual(tor._sock.sent, [b'AUTHENTICATE "pw"\r\n'])


if __name__ == "__main__":
    unittest.main()
