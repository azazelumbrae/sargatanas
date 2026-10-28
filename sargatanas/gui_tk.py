"""Tkinter GUI - the primary frontend.

Tkinter ships with CPython on Linux, macOS and Windows, so this is the one
GUI that works everywhere with zero extra dependencies. It reuses the exact
same sargatanas.vm/config/profiles core as the CLI, so the two interfaces
cannot drift apart.

The GUI is not read-only: it creates VMs, starts them with any tier, edits
Tor/VPN/network settings, writes the torrc, and manages the kill switch.

Threading: Tkinter is not thread-safe. Anything slow (creating a disk,
starting QEMU) runs in a worker and reports back through a queue polled by
`after()`, so the window never freezes and errors always reach the user.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__, config, paths, profiles, qemu, torcfg, vm, vpn

ACCENT = "#c9a227"
BG = "#14161f"
CARD = "#1c1f2e"
FIELD = "#2a2f45"
TEXT = "#e6e6e6"
MUTED = "#8a8f9c"
OK = "#4ade80"
ERR = "#f87171"


def _ensure_tcl_library() -> None:
    """Point Tcl at its own script library when the interpreter cannot find it.

    Some relocatable/uv-built Pythons and Homebrew builds fail with
    "Tcl wasn't installed properly" because TCL_LIBRARY is unset and the
    compiled-in prefix is wrong. We only set the variable when the standard
    location is missing, so a healthy interpreter is never overridden.
    """
    if os.environ.get("TCL_LIBRARY"):
        return
    try:
        tcl = tk.Tcl()
        if tcl.eval("info library"):
            return
    except tk.TclError:
        pass
    base = Path(sys.base_prefix)
    for lib in (base / "lib" / "tcl8.6", base / "lib" / "tcl9.0", base / "lib" / "tcl9.1"):
        if (lib / "init.tcl").exists():
            os.environ["TCL_LIBRARY"] = str(lib)
            tk_lib = base / "lib" / ("tk" + lib.name[len("tcl"):])
            if (tk_lib / "tk.tcl").exists():
                os.environ["TK_LIBRARY"] = str(tk_lib)
            return


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("1040x700")
        self.minsize(820, 560)
        self.cfg = config.load()
        self._queue: queue.Queue = queue.Queue()
        self._style()
        self._build()
        self.after(120, self._drain_queue)
        self.refresh_vms()

    # ---------- styling ----------

    def _style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.configure(bg=BG)
        style.configure(".", background=BG, foreground=TEXT,
                        fieldbackground=FIELD, borderwidth=0)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=CARD, foreground=TEXT, padding=[14, 8])
        style.map("TNotebook.Tab", background=[("selected", ACCENT)],
                  foreground=[("selected", BG)])
        style.configure("Treeview", background=CARD, foreground=TEXT,
                        fieldbackground=CARD, rowheight=30)
        style.configure("Treeview.Heading", background=FIELD, foreground=TEXT,
                        font=("", 10, "bold"))
        style.configure("TButton", background=FIELD, foreground=TEXT, padding=6)
        style.map("TButton", background=[("active", ACCENT)], foreground=[("active", BG)])
        style.configure("Accent.TButton", background=ACCENT, foreground=BG,
                        font=("", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#e8c65a")])
        style.configure("Danger.TButton", background="#5b1a24", foreground=TEXT)
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT)
        style.configure("TCombobox", fieldbackground=FIELD, foreground=TEXT, background=FIELD)
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.configure("TLabelframe", background=BG, foreground=ACCENT)
        style.configure("TLabelframe.Label", background=BG, foreground=ACCENT)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Status.TLabel", background=CARD, foreground=MUTED, padding=6)

    # ---------- layout ----------

    def _build(self) -> None:
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.nb.add(self._machines_tab(), text="  Machines  ")
        self.nb.add(self._create_tab(), text="  Create  ")
        self.nb.add(self._isos_tab(), text="  ISOs  ")
        self.nb.add(self._profiles_tab(), text="  Profiles  ")
        self.nb.add(self._security_tab(), text="  Security  ")
        self.nb.add(self._network_tab(), text="  Network  ")
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self._on_tab())

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status_var, style="Status.TLabel",
                  anchor="w").pack(fill="x", side="bottom")

    def _section(self, parent, title: str) -> tk.Frame:
        box = tk.Frame(parent, bg=BG)
        tk.Label(box, text=title, bg=BG, fg=ACCENT,
                 font=("", 15, "bold")).pack(anchor="w", padx=16, pady=(14, 6))
        inner = tk.Frame(box, bg=BG)
        inner.pack(fill="both", expand=True, padx=16)
        return box, inner

    # ---------- Machines ----------

    def _machines_tab(self) -> ttk.Frame:
        frame = ttk.Frame()
        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=10, pady=8)
        self._button(bar, "▶ Run", self._run_dialog, "Accent.TButton")
        self._button(bar, "🚀 Launch…", self._launch_dialog)
        self._button(bar, "■ Stop", self._stop)
        self._button(bar, "🛡 Profile…", self._change_profile)
        self._button(bar, "🗑 Delete", self._delete, "Danger.TButton")
        self._button(bar, "↻ Refresh", self.refresh_vms)

        cols = ("name", "state", "ram", "cpus", "profile")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=14)
        for col, label, width in (("name", "Name", 220), ("state", "State", 100),
                                  ("ram", "RAM", 80), ("cpus", "CPU", 60),
                                  ("profile", "Profile", 120)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_detail())
        self.tree.bind("<Double-1>", lambda _e: self._launch_dialog())

        self.detail = tk.Label(frame, text="", bg=CARD, fg=MUTED, justify="left",
                               anchor="w", padx=12, pady=8, font=("Menlo", 11))
        self.detail.pack(fill="x", padx=10, pady=10)
        return frame

    def refresh_vms(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        try:
            rows = vm.status(self.cfg)
        except Exception as exc:  # noqa: BLE001 - never crash the UI
            self._fail(f"Error reading VMs: {exc}")
            return
        for row in rows:
            self.tree.insert("", "end", values=(row.name, "running" if row.running else "stopped",
                                                f"{row.ram}M", row.cpus, row.profile),
                             tags=("running" if row.running else "stopped",))
        self.tree.tag_configure("running", foreground=OK)
        self.tree.tag_configure("stopped", foreground=MUTED)
        self._update_detail()
        self.set_status(f"{len(rows)} machine(s)")

    def _selected_vm(self) -> str | None:
        sel = self.tree.selection()
        return self.tree.item(sel[0])["values"][0] if sel else None

    def _update_detail(self) -> None:
        name = self._selected_vm()
        if not name:
            self.detail.configure(text="Select a machine. Double-click to launch it.")
            return
        try:
            v = self.cfg.vms[name]
            check = vm.launch_state(name, cfg=self.cfg)
        except Exception as exc:  # noqa: BLE001
            self.detail.configure(text=str(exc), fg=ERR)
            return
        lines = [f"Profile: {v.profile or self.cfg.default_profile}"
                 f"   RAM: {v.ram} MB   CPUs: {v.cpus}",
                 f"ISO: {v.iso or '(none)'}",
                 f"Disk: {v.disk}"]
        if check.ok:
            lines.append("Can launch: yes")
        else:
            lines.append("Cannot launch: " + "; ".join(check.reasons))
        for w in check.warnings:
            lines.append(f"Warning: {w}")
        self.detail.configure(text="\n".join(lines), fg=ERR if not check.ok else MUTED)

    # ---------- Create ----------

    def _create_tab(self) -> ttk.Frame:
        frame = ttk.Frame()
        form = tk.Frame(frame, bg=BG)
        form.pack(fill="x", padx=16, pady=12)

        self.name_var = tk.StringVar()
        self.iso_path_var = tk.StringVar()
        self.ram_var = tk.IntVar(value=self.cfg.default_ram)
        self.cpu_var = tk.IntVar(value=self.cfg.default_cpus)
        self.disk_var = tk.IntVar(value=self.cfg.default_disk_gb)
        self.profile_var = tk.StringVar(value=self.cfg.default_profile)

        def row(r, label, widget):
            tk.Label(form, text=label, bg=BG, fg=TEXT).grid(row=r, column=0, sticky="w",
                                                            pady=6, padx=(0, 12))
            widget.grid(row=r, column=1, sticky="ew", pady=6)

        row(0, "Name", ttk.Entry(form, textvariable=self.name_var))

        self._iso_names = ["No ISO"]
        self._iso_paths = [""]
        for p in _find_isos():
            self._iso_names.append(p.name)
            self._iso_paths.append(str(p))
        self.iso_combo = ttk.Combobox(form, values=self._iso_names, state="readonly")
        self.iso_combo.current(0)
        self.iso_combo.bind("<<ComboboxSelected>>", self._on_iso_selected)
        row(1, "ISO", self.iso_combo)
        row(2, "RAM (MB)", ttk.Spinbox(form, from_=256, to=65536, increment=256,
                                       textvariable=self.ram_var))
        row(3, "CPUs", ttk.Spinbox(form, from_=1, to=32, increment=1, textvariable=self.cpu_var))
        row(4, "Disk (GB)", ttk.Spinbox(form, from_=1, to=1024, increment=1,
                                        textvariable=self.disk_var))
        row(5, "Profile", ttk.Combobox(form, textvariable=self.profile_var,
                                       values=list(profiles.PROFILES), state="readonly"))
        form.columnconfigure(1, weight=1)

        ttk.Button(form, text="Create machine", style="Accent.TButton",
                   command=self._create).grid(row=6, column=0, columnspan=2, sticky="ew",
                                              pady=14)
        return frame

    def _on_iso_selected(self, _event=None) -> None:
        idx = self.iso_combo.current()
        if 0 <= idx < len(self._iso_paths):
            self.iso_path_var.set(self._iso_paths[idx])

    def _set_iso_path(self, path: str) -> None:
        if path not in self._iso_paths:
            self._iso_paths.append(path)
            self._iso_names.append(Path(path).name)
            self.iso_combo.configure(values=self._iso_names)
        self.iso_path_var.set(path)
        self.iso_combo.current(self._iso_paths.index(path))

    def _create(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            self._fail("Enter a name")
            return
        kwargs = dict(iso=self.iso_path_var.get() or None, ram=self.ram_var.get(),
                      cpus=self.cpu_var.get(), disk_gb=self.disk_var.get(),
                      profile=self.profile_var.get() or self.cfg.default_profile)
        self.set_status(f"Creating '{name}'…")

        def worker():
            try:
                vm.create(name, **kwargs)
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("error", str(exc), None))
            else:
                self._queue.put(("created", name, None))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- ISOs ----------

    def _isos_tab(self) -> ttk.Frame:
        frame = ttk.Frame()
        box, inner = self._section(frame, "ISO images")
        box.pack(fill="both", expand=True)
        ttk.Label(inner, text=f"Searching {paths.iso_dir()}, ~/Downloads, ~/ISOs, ~/Desktop",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        self.iso_listbox = tk.Listbox(inner, bg=CARD, fg=TEXT, selectbackground=ACCENT,
                                      selectforeground=BG, relief="flat", activestyle="none")
        self.iso_listbox.pack(fill="both", expand=True)
        bar = ttk.Frame(inner)
        bar.pack(fill="x", pady=8)
        self._button(bar, "⚡ Quick boot", self._quick_boot_selected, "Accent.TButton")
        self._button(bar, "→ New VM", self._use_iso_selected)
        self._button(bar, "Browse…", self._browse_iso)
        self._isos: list[Path] = []
        self._refresh_isos()
        return frame

    def _refresh_isos(self) -> None:
        self.iso_listbox.delete(0, "end")
        self._isos = _find_isos()
        for iso in self._isos:
            self.iso_listbox.insert("end", f"{iso.name}   ({iso.parent})")

    # ---------- Profiles ----------

    def _profiles_tab(self) -> ttk.Frame:
        frame = ttk.Frame()
        box, inner = self._section(frame, "Security tiers — most closed to most open")
        box.pack(fill="both", expand=True)
        text = tk.Text(inner, bg=CARD, fg=TEXT, relief="flat", wrap="word",
                       font=("Menlo", 12), padx=12, pady=12)
        text.pack(fill="both", expand=True)
        for key, prof in profiles.PROFILES.items():
            text.insert("end", f"{key}\n  {prof.summary}\n")
            for rule in prof.firewall_rules():
                text.insert("end", f"    - {rule}\n")
            for limit in prof.describe_limits():
                text.insert("end", f"    ! {limit}\n")
            text.insert("end", "\n")
        text.configure(state="disabled")
        return frame

    # ---------- Security ----------

    def _security_tab(self) -> ttk.Frame:
        frame = ttk.Frame()
        box, inner = self._section(frame, "Security center")
        box.pack(fill="both", expand=True)

        self.sec_status = tk.Label(inner, text="", bg=CARD, fg=TEXT, justify="left",
                                   anchor="w", padx=12, pady=10, font=("Menlo", 11))
        self.sec_status.pack(fill="x", pady=(0, 10))

        bar = ttk.Frame(inner)
        bar.pack(fill="x", pady=(0, 10))
        self._button(bar, "↻ Refresh status", self._refresh_security)
        self._button(bar, "🔓 New Tor identity", self._new_tor_identity)
        self._button(bar, "📄 Write torrc…", self._write_torrc)
        self._button(bar, "⛔ Kill switch on", self._kill_switch_on, "Danger.TButton")
        self._button(bar, "✅ Kill switch off", self._kill_switch_off)

        ttk.Label(inner, text="Firewall rules per tier", style="Muted.TLabel").pack(anchor="w")
        self.fw_combo = ttk.Combobox(inner, values=list(profiles.PROFILES), state="readonly")
        self.fw_combo.set(self.cfg.default_profile)
        self.fw_combo.pack(anchor="w", pady=4)
        self.fw_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_firewall())
        self.fw_text = tk.Text(inner, bg=CARD, fg=TEXT, relief="flat", wrap="word",
                               height=10, padx=12, pady=10, font=("Menlo", 11))
        self.fw_text.pack(fill="both", expand=True, pady=(0, 8))
        self._refresh_security()
        self._refresh_firewall()
        return frame

    def _refresh_security(self) -> None:
        tor_up = profiles.tor_socks_reachable(port=self.cfg.tor.socks_port)
        prov = vpn.installed_providers()
        self.sec_status.configure(text=(
            f"Tor SOCKS : {'up' if tor_up else 'down'}"
            f" ({'installed' if profiles.tor_installed() else 'missing'})\n"
            f"WireGuard : {'installed' if prov['wireguard'] else 'missing'}\n"
            f"OpenVPN   : {'installed' if prov['openvpn'] else 'missing'}\n"
            f"Seccomp   : {'available' if qemu.sandbox_supported() else 'unavailable on this QEMU'}\n"
            f"Kill switch: {'available (Linux+iptables)' if vpn.kill_switch_supported() else 'unavailable on this OS'}"))

    def _refresh_firewall(self) -> None:
        prof = profiles.get(self.fw_combo.get())
        lines = [prof.summary, ""] + [f"- {r}" for r in prof.firewall_rules()]
        self.fw_text.configure(state="normal")
        self.fw_text.delete("1.0", "end")
        self.fw_text.insert("end", "\n".join(lines))
        self.fw_text.configure(state="disabled")

    def _new_tor_identity(self) -> None:
        def work():
            from . import torctl
            try:
                with torctl.TorControl(port=self.cfg.tor.control_port,
                                       password=self.cfg.tor.control_password) as tor:
                    tor.authenticate()
                    tor.newnym()
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("error", str(exc), None))
            else:
                self._queue.put(("info", "New Tor identity requested", None))
        threading.Thread(target=work, daemon=True).start()

    def _write_torrc(self) -> None:
        path = filedialog.asksaveasfilename(title="Write torrc",
                                            initialfile="torrc",
                                            defaultextension="")
        if not path:
            return
        try:
            torcfg.write_torrc(self.cfg.tor, Path(path))
        except Exception as exc:  # noqa: BLE001
            self._fail(str(exc))
            return
        self.set_status(f"torrc written to {path}")

    def _kill_switch_on(self) -> None:
        if not messagebox.askyesno(APP_NAME,
                                   "Apply a host-wide kill switch?\n\n"
                                   "Everything outside the VPN interface will be rejected."):
            return
        self._run_kill_switch(True)

    def _kill_switch_off(self) -> None:
        self._run_kill_switch(False)

    def _run_kill_switch(self, enable: bool) -> None:
        def work():
            try:
                if enable:
                    vpn.install_kill_switch(self.cfg.vpn.interface)
                else:
                    vpn.remove_kill_switch(self.cfg.vpn.interface)
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("error", str(exc), None))
            else:
                self._queue.put(("info", "Kill switch updated", None))
        threading.Thread(target=work, daemon=True).start()

    # ---------- Network ----------

    def _network_tab(self) -> ttk.Frame:
        frame = ttk.Frame()
        box, inner = self._section(frame, "Network configuration")
        box.pack(fill="both", expand=True)

        form = tk.Frame(inner, bg=BG)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        t = self.cfg.tor
        self.tor_socks = tk.StringVar(value=str(t.socks_port))
        self.tor_control = tk.StringVar(value=str(t.control_port))
        self.tor_dns = tk.StringVar(value=str(t.dns_port))
        self.tor_bridges = tk.StringVar(value="\n".join(t.bridges))
        self.tor_exit = tk.StringVar(value=t.exit_nodes or "")
        self.vpn_provider = tk.StringVar(value=self.cfg.vpn.provider or "")
        self.vpn_iface = tk.StringVar(value=self.cfg.vpn.interface or "")
        self.forwards = tk.StringVar(value="\n".join(self.cfg.network.forwards))
        self.killswitch = tk.BooleanVar(value=self.cfg.vpn.kill_switch)
        self.def_ram = tk.StringVar(value=str(self.cfg.default_ram))
        self.def_cpus = tk.StringVar(value=str(self.cfg.default_cpus))
        self.def_disk = tk.StringVar(value=str(self.cfg.default_disk_gb))
        self.def_profile = tk.StringVar(value=self.cfg.default_profile)

        def cell(r, c, label, widget):
            tk.Label(form, text=label, bg=BG, fg=TEXT).grid(row=r, column=c * 2,
                                                            sticky="w", pady=5)
            widget.grid(row=r, column=c * 2 + 1, sticky="ew", padx=(6, 18), pady=5)

        grp_tor = tk.Label(form, text="Tor", bg=BG, fg=ACCENT, font=("", 12, "bold"))
        grp_tor.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
        cell(1, 0, "SOCKS port", ttk.Entry(form, textvariable=self.tor_socks, width=10))
        cell(1, 1, "Control port", ttk.Entry(form, textvariable=self.tor_control, width=10))
        cell(2, 0, "DNS port", ttk.Entry(form, textvariable=self.tor_dns, width=10))
        cell(2, 1, "Exit nodes", ttk.Entry(form, textvariable=self.tor_exit))
        tk.Label(form, text="Bridges (one per line)", bg=BG, fg=TEXT).grid(
            row=3, column=0, sticky="nw", pady=5)
        self.bridges_text = tk.Text(form, bg=FIELD, fg=TEXT, height=3, relief="flat",
                                    padx=8, pady=6)
        self.bridges_text.insert("1.0", self.tor_bridges.get())
        self.bridges_text.grid(row=3, column=1, columnspan=3, sticky="ew", pady=5)

        tk.Label(form, text="VPN", bg=BG, fg=ACCENT, font=("", 12, "bold")).grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(10, 4))
        cell(5, 0, "Provider", ttk.Combobox(form, textvariable=self.vpn_provider,
                                            values=["", "wireguard", "openvpn"], state="readonly"))
        cell(5, 1, "Interface", ttk.Entry(form, textvariable=self.vpn_iface))
        ttk.Checkbutton(form, text="Kill switch enabled", variable=self.killswitch).grid(
            row=6, column=0, columnspan=4, sticky="w", pady=5)

        tk.Label(form, text="Network", bg=BG, fg=ACCENT, font=("", 12, "bold")).grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(10, 4))
        tk.Label(form, text="Port forwards\n(host:guest)", bg=BG, fg=TEXT).grid(
            row=8, column=0, sticky="nw", pady=5)
        self.fwd_text = tk.Text(form, bg=FIELD, fg=TEXT, height=3, relief="flat",
                                padx=8, pady=6)
        self.fwd_text.insert("1.0", self.forwards.get())
        self.fwd_text.grid(row=8, column=1, columnspan=3, sticky="ew", pady=5)

        tk.Label(form, text="Defaults", bg=BG, fg=ACCENT, font=("", 12, "bold")).grid(
            row=9, column=0, columnspan=4, sticky="w", pady=(10, 4))
        cell(10, 0, "RAM (MB)", ttk.Entry(form, textvariable=self.def_ram, width=10))
        cell(10, 1, "CPUs", ttk.Entry(form, textvariable=self.def_cpus, width=10))
        cell(11, 0, "Disk (GB)", ttk.Entry(form, textvariable=self.def_disk, width=10))
        cell(11, 1, "Default tier", ttk.Combobox(form, textvariable=self.def_profile,
                                                 values=list(profiles.PROFILES),
                                                 state="readonly"))

        bar = ttk.Frame(inner)
        bar.pack(fill="x", pady=12)
        self._button(bar, "💾 Save configuration", self._save_network, "Accent.TButton")
        self._button(bar, "↻ Reload", self._reload_network)
        return frame

    def _save_network(self) -> None:
        bridges = [l.strip() for l in self.bridges_text.get("1.0", "end").splitlines()
                   if l.strip()]
        forwards = [l.strip() for l in self.fwd_text.get("1.0", "end").splitlines()
                    if l.strip()]
        try:
            config.update_tor(self.cfg, socks_port=self.tor_socks.get(),
                              control_port=self.tor_control.get(),
                              dns_port=self.tor_dns.get(),
                              exit_nodes=self.tor_exit.get(), bridges=bridges)
            config.update_vpn(self.cfg, provider=self.vpn_provider.get(),
                              interface=self.vpn_iface.get(),
                              kill_switch=self.killswitch.get())
            config.update_network(self.cfg, forwards=forwards)
            config.update_defaults(self.cfg, default_ram=self.def_ram.get(),
                                   default_cpus=self.def_cpus.get(),
                                   default_disk_gb=self.def_disk.get(),
                                   default_profile=self.def_profile.get())
            config.save(self.cfg)
        except (config.SettingsError, KeyError) as exc:
            self._fail(str(exc).strip("'\""))
            return
        self._refresh_security()
        self._update_detail()
        self.set_status("Configuration saved")

    def _reload_network(self) -> None:
        self.cfg = config.load()
        self.tor_socks.set(str(self.cfg.tor.socks_port))
        self.tor_control.set(str(self.cfg.tor.control_port))
        self.tor_dns.set(str(self.cfg.tor.dns_port))
        self.tor_exit.set(self.cfg.tor.exit_nodes or "")
        self.bridges_text.delete("1.0", "end")
        self.bridges_text.insert("1.0", "\n".join(self.cfg.tor.bridges))
        self.vpn_provider.set(self.cfg.vpn.provider or "")
        self.vpn_iface.set(self.cfg.vpn.interface or "")
        self.killswitch.set(self.cfg.vpn.kill_switch)
        self.fwd_text.delete("1.0", "end")
        self.fwd_text.insert("1.0", "\n".join(self.cfg.network.forwards))
        self.def_ram.set(str(self.cfg.default_ram))
        self.def_cpus.set(str(self.cfg.default_cpus))
        self.def_disk.set(str(self.cfg.default_disk_gb))
        self.def_profile.set(self.cfg.default_profile)
        self._refresh_security()
        self.set_status("Configuration reloaded")

    # ---------- launching ----------

    def _run_dialog(self) -> None:
        """The primary flow: pick how to boot this machine."""
        name = self._selected_vm()
        if not name:
            self._fail("Select a machine first")
            return
        v = self.cfg.vms[name]
        top = self._dialog(f"Launch '{name}'")
        state = tk.StringVar(value=v.profile or self.cfg.default_profile)
        live = tk.BooleanVar(value=False)
        install = tk.BooleanVar(value=False)

        ttk.Label(top, text="Boot mode").pack(anchor="w", pady=(8, 2))
        for text, var in (("From disk", None),
                          ("From ISO — live, changes discarded", live),
                          ("From ISO — installer", install)):
            if var is None:
                ttk.Radiobutton(top, text=text, value=True, variable=live,
                                command=lambda: install.set(False)).pack(anchor="w", padx=12)
            else:
                ttk.Radiobutton(top, text=text, variable=var, value=True).pack(anchor="w", padx=12)

        ttk.Label(top, text="Security tier").pack(anchor="w", pady=(12, 2))
        combo = ttk.Combobox(top, textvariable=state, values=list(profiles.PROFILES),
                             state="readonly")
        combo.pack(fill="x", padx=12)

        info = tk.Label(top, text="", bg=BG, fg=MUTED, justify="left", anchor="w",
                        wraplength=420, font=("Menlo", 11))
        info.pack(fill="x", padx=12, pady=10)

        def update_info(*_a):
            try:
                check = vm.launch_state(name, state.get(), cfg=self.cfg)
            except Exception as exc:  # noqa: BLE001
                info.configure(text=str(exc), fg=ERR)
                return
            prof = profiles.get(state.get())
            lines = [prof.summary]
            lines += check.reasons + check.warnings
            lines += prof.describe_limits()
            info.configure(text="\n".join(lines),
                           fg=ERR if not check.ok else MUTED)
        combo.bind("<<ComboboxSelected>>", update_info)
        update_info()

        def go():
            top.destroy()
            self._launch(name, state.get(), live.get(), install.get())
        ttk.Button(top, text="Launch", style="Accent.TButton", command=go).pack(
            fill="x", padx=12, pady=(0, 12))

    def _launch_dialog(self) -> None:
        self._run_dialog()

    def _launch(self, name: str, profile: str, live: bool, install: bool) -> None:
        self.set_status(f"Starting '{name}' with {profile}…")

        def work():
            try:
                vm.start(name, live=live, install=install, profile=profile)
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("error", str(exc), None))
            else:
                self._queue.put(("launched", f"'{name}' started with {profile}", None))
        threading.Thread(target=work, daemon=True).start()

    def _change_profile(self) -> None:
        name = self._selected_vm()
        if not name:
            return
        top = self._dialog(f"Default tier for '{name}'")
        var = tk.StringVar(value=self.cfg.vms[name].profile or self.cfg.default_profile)
        ttk.Label(top, text="Tier used when you press Run").pack(anchor="w", padx=12, pady=8)
        ttk.Combobox(top, textvariable=var, values=list(profiles.PROFILES),
                     state="readonly").pack(fill="x", padx=12)

        def save():
            try:
                vm.set_profile(name, var.get(), cfg=self.cfg)
            except Exception as exc:  # noqa: BLE001
                self._fail(str(exc))
                return
            top.destroy()
            self._update_detail()
            self.set_status(f"'{name}' now uses {var.get()}")
        ttk.Button(top, text="Save", style="Accent.TButton", command=save).pack(
            fill="x", padx=12, pady=12)

    def _stop(self) -> None:
        name = self._selected_vm()
        if not name:
            return
        if vm.stop(name):
            self.set_status(f"'{name}' stopped")
        else:
            self._fail(f"Could not stop '{name}'")
        self.refresh_vms()

    def _delete(self) -> None:
        name = self._selected_vm()
        if not name:
            return
        if not messagebox.askyesno(APP_NAME, f"Delete '{name}' and its disk permanently?"):
            return
        try:
            vm.delete(name, cfg=self.cfg)
        except Exception as exc:  # noqa: BLE001
            self._fail(str(exc))
            return
        self.refresh_vms()
        self.set_status(f"'{name}' deleted")

    # ---------- machinery ----------

    def _dialog(self, title: str) -> tk.Toplevel:
        top = tk.Toplevel(self)
        top.title(title)
        top.configure(bg=BG)
        top.geometry("460x380")
        top.transient(self)
        top.grab_set()
        return top

    @staticmethod
    def _button(parent, text, command, style=None):
        return ttk.Button(parent, text=text, command=command, style=style).pack(
            side="left", padx=3)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _fail(self, message: str) -> None:
        messagebox.showerror(APP_NAME, message)

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload, _ = self._queue.get_nowait()
                if kind == "error":
                    self._fail(payload)
                    self.set_status("Error")
                elif kind == "created":
                    self.cfg = config.load()
                    self.name_var.set("")
                    self.refresh_vms()
                    self.set_status(f"'{payload}' created")
                    self.nb.select(0)
                elif kind == "launched":
                    self.set_status(payload)
                    self.after(800, self.refresh_vms)
                else:
                    self.set_status(payload)
                    self._refresh_security()
        except queue.Empty:
            pass
        self.after(120, self._drain_queue)

    def _on_tab(self) -> None:
        idx = self.nb.index(self.nb.select())
        if idx == 0:
            self.refresh_vms()
        elif idx == 2:
            self._refresh_isos()
        elif idx == 4:
            self._refresh_security()

    def _quick_boot_selected(self) -> None:
        sel = self.iso_listbox.curselection()
        if sel:
            self._quick_boot(self._isos[sel[0]])

    def _quick_boot(self, iso: Path) -> None:
        try:
            import argparse
            from .cli import cmd_boot
            cmd_boot(argparse.Namespace(iso=str(iso), ram=None, cpus=None,
                                        profile=None, headless=False))
        except Exception as exc:  # noqa: BLE001
            self._fail(str(exc))
            return
        self.set_status(f"Booting {iso.name}")

    def _use_iso_selected(self) -> None:
        sel = self.iso_listbox.curselection()
        if sel:
            self._set_iso_path(str(self._isos[sel[0]]))
            self.nb.select(1)

    def _browse_iso(self) -> None:
        path = filedialog.askopenfilename(
            title="Select an ISO", filetypes=[("ISO images", "*.iso"), ("All files", "*")])
        if path:
            self._set_iso_path(path)


def _find_isos() -> list[Path]:
    # Avoid a hard import cycle; the CLI owns ISO discovery.
    from .cli import find_isos
    return find_isos(limit=100)


def main() -> int:
    paths.ensure_dirs()
    _ensure_tcl_library()
    App().mainloop()
    return 0


__all__ = ["main", "App", "_ensure_tcl_library"]
