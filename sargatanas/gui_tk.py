"""Tkinter GUI - the cross-platform frontend.

Tkinter ships with CPython on Linux, macOS and Windows, so this is the one
GUI that works everywhere with zero extra dependencies. It reuses the exact
same sargatanas.vm/config/profiles core as the CLI, so the two interfaces
cannot drift apart.

Disk creation runs on a worker thread and reports back through a queue
polled by `after()` - Tkinter is not thread-safe, so the UI thread never
touches the worker directly.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__, config, paths, profiles, qemu, vm

ACCENT = "#c9a227"
BG = "#14161f"
CARD = "#1c1f2e"
FIELD = "#2a2f45"
TEXT = "#e6e6e6"
MUTED = "#8a8f9c"
OK = "#4ade80"
ERR = "#f87171"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("920x620")
        self.minsize(720, 480)
        self.cfg = config.load()
        self._queue: queue.Queue = queue.Queue()
        self._style()
        self._build()
        self.after(100, self._drain_queue)
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
        style.configure("TNotebook.Tab", background=CARD, foreground=TEXT,
                        padding=[14, 8])
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", BG)])
        style.configure("Treeview", background=CARD, foreground=TEXT,
                        fieldbackground=CARD, rowheight=30)
        style.configure("Treeview.Heading", background=FIELD, foreground=TEXT,
                        font=("", 10, "bold"))
        style.configure("TButton", background=FIELD, foreground=TEXT, padding=6)
        style.map("TButton", background=[("active", ACCENT)],
                  foreground=[("active", BG)])
        style.configure("Accent.TButton", background=ACCENT, foreground=BG,
                        font=("", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#e8c65a")])
        style.configure("Danger.TButton", background="#5b1a24", foreground=TEXT)
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT,
                        insertcolor=TEXT)
        style.configure("TCombobox", fieldbackground=FIELD, foreground=TEXT,
                        background=FIELD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Status.TLabel", background=CARD, foreground=MUTED, padding=6)

    # ---------- layout ----------

    def _build(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        nb.add(self._machines_tab(), text="  Machines  ")
        nb.add(self._create_tab(), text="  Create  ")
        nb.add(self._isos_tab(), text="  ISOs  ")
        nb.add(self._profiles_tab(), text="  Profiles  ")
        nb.add(self._security_tab(), text="  Security  ")
        nb.add(self._network_tab(), text="  Network  ")

        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Label(self, textvariable=self.status_var, style="Status.TLabel", anchor="w")
        status.pack(fill="x", side="bottom")

    def _machines_tab(self) -> ttk.Frame:
        frame = ttk.Frame()
        frame.pack(fill="both", expand=True)

        cols = ("name", "state", "ram", "cpus", "profile")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col, label, width in (("name", "Name", 180), ("state", "State", 90),
                                  ("ram", "RAM", 70), ("cpus", "CPU", 50),
                                  ("profile", "Profile", 90)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=10)

        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        self._button(bar, "▶ Run", lambda: self._run(False, False), "Accent.TButton")
        self._button(bar, "💿 Live", lambda: self._run(True, False))
        self._button(bar, "📥 Install", lambda: self._run(False, True))
        self._button(bar, "■ Stop", self._stop)
        self._button(bar, "🗑 Delete", self._delete, "Danger.TButton")
        self._button(bar, "↻ Refresh", self.refresh_vms)
        return frame

    def _create_tab(self) -> ttk.Frame:
        frame = ttk.Frame()
        form = ttk.Frame(frame, padding=16)
        form.pack(fill="x")

        self.name_var = tk.StringVar()
        self.iso_path_var = tk.StringVar()
        self.ram_var = tk.IntVar(value=self.cfg.default_ram)
        self.cpu_var = tk.IntVar(value=self.cfg.default_cpus)
        self.disk_var = tk.IntVar(value=self.cfg.default_disk_gb)
        self.profile_var = tk.StringVar(value=self.cfg.default_profile)

        def row(r, label, widget):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=6, padx=(0, 12))
            widget.grid(row=r, column=1, sticky="ew", pady=6)

        row(0, "Name", ttk.Entry(form, textvariable=self.name_var))

        # ISO: the combo shows file names; the path lives in iso_path_var.
        self._iso_names = ["No ISO"]
        self._iso_paths: list[str] = [""]
        for p in _find_isos():
            self._iso_names.append(p.name)
            self._iso_paths.append(str(p))
        self.iso_combo = ttk.Combobox(form, values=self._iso_names, state="readonly")
        self.iso_combo.current(0)
        self.iso_combo.bind("<<ComboboxSelected>>", self._on_iso_selected)
        row(1, "ISO", self.iso_combo)

        row(2, "RAM (MB)", ttk.Spinbox(form, from_=256, to=65536, increment=256, textvariable=self.ram_var))
        row(3, "CPUs", ttk.Spinbox(form, from_=1, to=32, increment=1, textvariable=self.cpu_var))
        row(4, "Disk (GB)", ttk.Spinbox(form, from_=1, to=1024, increment=1, textvariable=self.disk_var))
        prof_combo = ttk.Combobox(form, textvariable=self.profile_var,
                                  values=list(profiles.PROFILES), state="readonly")
        row(5, "Profile", prof_combo)
        form.columnconfigure(1, weight=1)

        ttk.Button(form, text="Create machine", style="Accent.TButton",
                   command=self._create).grid(row=6, column=0, columnspan=2,
                                              sticky="ew", pady=16)
        ttk.Label(form, text="Disk creation runs in the background; watch the status bar.",
                  style="Muted.TLabel").grid(row=7, column=0, columnspan=2, sticky="w")
        return frame

    def _isos_tab(self) -> ttk.Frame:
        frame = ttk.Frame()
        hint = ttk.Label(frame, text=f"Looked in {paths.iso_dir()} and ~/Downloads, ~/ISOs, ~/Desktop",
                         style="Muted.TLabel")
        hint.pack(anchor="w", padx=12, pady=(10, 0))
        self.iso_listbox = tk.Listbox(frame, bg=CARD, fg=TEXT, selectbackground=ACCENT,
                                      selectforeground=BG, relief="flat", activestyle="none")
        self.iso_listbox.pack(fill="both", expand=True, padx=10, pady=10)
        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        self._button(bar, "⚡ Quick boot", self._quick_boot_selected, "Accent.TButton")
        self._button(bar, "→ Use in new VM", self._use_iso_selected)
        self._button(bar, "Browse…", self._browse_iso)
        self._isos: list[Path] = []
        self._refresh_isos()
        return frame

    def _profiles_tab(self) -> ttk.Frame:
        frame = ttk.Frame()
        box = tk.Text(frame, bg=BG, fg=TEXT, relief="flat", wrap="word",
                      font=("Menlo", 12), padx=14, pady=14)
        box.pack(fill="both", expand=True)
        for key, prof in profiles.PROFILES.items():
            box.insert("end", f"{key}\n  {prof.summary}\n")
            for limit in prof.describe_limits():
                box.insert("end", f"    · {limit}\n")
            box.insert("end", "\n")
        box.insert("end", "A profile that cannot be verified refuses to start.\n")
        box.configure(state="disabled")
        return frame

    def _security_tab(self) -> ttk.Frame:
        """Security Center: protections status + tiers with their firewall rules."""
        from . import vpn as vpn_mod
        frame = ttk.Frame()
        box = tk.Text(frame, bg=BG, fg=TEXT, relief="flat", wrap="word",
                      font=("Menlo", 12), padx=14, pady=14)
        box.pack(fill="both", expand=True)

        cfg = self.cfg
        tor_up = profiles.tor_socks_reachable(port=cfg.tor.socks_port)
        prov = vpn_mod.installed_providers()
        box.insert("end", "Protections\n")
        box.insert("end", f"  Tor SOCKS   : {'up' if tor_up else 'down'} "
                          f"({'installed' if profiles.tor_installed() else 'missing'})\n")
        box.insert("end", f"  WireGuard   : {'installed' if prov['wireguard'] else 'missing'}\n")
        box.insert("end", f"  OpenVPN     : {'installed' if prov['openvpn'] else 'missing'}\n")
        box.insert("end", f"  Seccomp     : {'available' if qemu.sandbox_supported() else 'unavailable'}\n\n")

        box.insert("end", "Isolation modes\n")
        for mode, desc in profiles.ISOLATION_MODES.items():
            box.insert("end", f"  {mode:<12} {desc}\n")
        box.insert("end", "\nTiers\n")
        for key, prof in profiles.PROFILES.items():
            box.insert("end", f"  {key} - {prof.summary}\n")
            for rule in prof.firewall_rules():
                box.insert("end", f"      · {rule}\n")
        box.configure(state="disabled")
        return frame

    def _network_tab(self) -> ttk.Frame:
        """Network configuration: isolation modes + Tor/VPN settings."""
        frame = ttk.Frame()
        box = tk.Text(frame, bg=BG, fg=TEXT, relief="flat", wrap="word",
                      font=("Menlo", 12), padx=14, pady=14)
        box.pack(fill="both", expand=True)
        cfg = self.cfg
        box.insert("end", "Tor\n")
        box.insert("end", f"  SOCKS : {cfg.tor.socks_port}   Control: {cfg.tor.control_port}   "
                          f"DNS: {cfg.tor.dns_port}\n")
        if cfg.tor.bridges:
            box.insert("end", f"  Bridges: {len(cfg.tor.bridges)} configured\n")
        if cfg.tor.exit_nodes:
            box.insert("end", f"  Exit nodes: {cfg.tor.exit_nodes}\n")
        box.insert("end", "\nVPN\n")
        box.insert("end", f"  Provider : {cfg.vpn.provider or '(not set)'}\n")
        box.insert("end", f"  Interface: {cfg.vpn.interface or '(not set)'}\n")
        if cfg.vpn.interface:
            up = profiles.vpn_interface_up(cfg.vpn.interface)
            box.insert("end", f"  State    : {'up' if up else 'down'}\n")
        box.insert("end", "\nPort forwards (nat / pentesting):\n")
        for fwd in cfg.network.forwards:
            box.insert("end", f"  host:{fwd}\n")
        box.insert("end",
                   "\nThese are view-only; edit ~/Sargatanas/config.json to change them.\n")
        box.configure(state="disabled")
        return frame

    # ---------- helpers ----------

    @staticmethod
    def _button(parent, text, command, style=None):
        return ttk.Button(parent, text=text, command=command, style=style).pack(
            side="left", padx=3)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _selected_vm(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0])["values"][0]

    # ---------- data ----------

    def refresh_vms(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        try:
            rows = vm.status(self.cfg)
        except Exception as exc:  # noqa: BLE001 - never crash the UI
            self.set_status(f"Error reading VMs: {exc}")
            return
        for row in rows:
            state = "running" if row.running else "stopped"
            self.tree.insert("", "end", values=(row.name, state, f"{row.ram}M",
                                                row.cpus, row.profile),
                             tags=(state,))
        self.tree.tag_configure("running", foreground=OK)
        self.tree.tag_configure("stopped", foreground=MUTED)
        self.set_status(f"{len(rows)} machine(s)")

    def _refresh_isos(self) -> None:
        self.iso_listbox.delete(0, "end")
        self._isos = _find_isos()
        for iso in self._isos:
            self.iso_listbox.insert("end", f"{iso.name}   ({iso.parent})")

    def _on_iso_selected(self, _event) -> None:
        idx = self.iso_combo.current()
        self.iso_path_var.set(self._iso_paths[idx] if 0 <= idx < len(self._iso_paths) else "")

    def _set_iso_path(self, path: str) -> None:
        if path not in self._iso_paths:
            self._iso_paths.append(path)
            self._iso_names.append(Path(path).name)
            self.iso_combo.configure(values=self._iso_names)
        self.iso_path_var.set(path)
        self.iso_combo.current(self._iso_paths.index(path))

    # ---------- actions ----------

    def _run(self, live: bool, install: bool) -> None:
        name = self._selected_vm()
        if not name:
            messagebox.showwarning(APP_NAME, "Select a machine first")
            return
        try:
            vm.start(name, live=live, install=install)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.set_status(f"'{name}' started")
        self.refresh_vms()

    def _stop(self) -> None:
        name = self._selected_vm()
        if not name:
            return
        vm.stop(name)
        self.set_status(f"'{name}' stopped")
        self.refresh_vms()

    def _delete(self) -> None:
        name = self._selected_vm()
        if not name:
            return
        if not messagebox.askyesno(APP_NAME, f"Delete '{name}' and its disk permanently?"):
            return
        try:
            vm.delete(name)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.cfg = config.load()
        self.set_status(f"'{name}' deleted")
        self.refresh_vms()

    def _create(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning(APP_NAME, "Enter a name")
            return
        iso = self.iso_path_var.get() or None
        profile = self.profile_var.get() or self.cfg.default_profile
        kwargs = dict(iso=iso, ram=self.ram_var.get(), cpus=self.cpu_var.get(),
                      disk_gb=self.disk_var.get(), profile=profile)
        self.set_status(f"Creating '{name}'…")

        def worker():
            try:
                vm.create(name, **kwargs)
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("error", f"{name}", str(exc)))
            else:
                self._queue.put(("ok", f"{name}", None))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, name, msg = self._queue.get_nowait()
                if kind == "ok":
                    self.cfg = config.load()
                    self.name_var.set("")
                    self.set_status(f"'{name}' created")
                    self.refresh_vms()
                else:
                    messagebox.showerror(APP_NAME, msg)
                    self.set_status(f"Error creating '{name}'")
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _quick_boot_selected(self) -> None:
        sel = self.iso_listbox.curselection()
        if not sel:
            return
        self._quick_boot(self._isos[sel[0]])

    def _quick_boot(self, iso: Path) -> None:
        try:
            import argparse
            from .cli import cmd_boot
            cmd_boot(argparse.Namespace(iso=str(iso), ram=None, cpus=None,
                                        profile=None, headless=False))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.set_status(f"Booting {iso.name}")

    def _use_iso_selected(self) -> None:
        sel = self.iso_listbox.curselection()
        if sel:
            self._set_iso_path(str(self._isos[sel[0]]))

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
    App().mainloop()
    return 0


__all__ = ["main", "App"]
