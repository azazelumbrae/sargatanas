"""GTK3 interface.

Deliberately thin: it only calls sargatanas.vm/config/qemu, so the CLI and
the GUI can never drift apart again. GTK stays optional - importing this
module fails cleanly when PyGObject is not installed.

Threading note: GTK is not thread safe. Disk creation runs in a worker and
returns to the main loop with GLib.idle_add, and it reports failures there
instead of raising inside the worker (which used to kill the operation
silently and leave the UI stuck on "Creating...").
"""

from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import APP_NAME, __version__, config, paths, profiles, qemu, vm  # noqa: E402

CSS = b"""
window { background-color: #14161f; }
.header { background-color: #1c1f2e; padding: 14px; }
.sidebar { background-color: #1c1f2e; padding: 8px; }
.sidebar-button { background: transparent; border: none; color: #e6e6e6;
                  padding: 12px 18px; border-radius: 8px; margin: 2px 4px; }
.sidebar-button:hover { background-color: #2a2f45; }
.sidebar-button:checked { background-color: #c9a227; color: #14161f; }
.title { color: #c9a227; font-size: 20px; font-weight: bold; }
.subtitle { color: #8a8f9c; font-size: 12px; }
.muted { color: #8a8f9c; font-size: 12px; }
.card { background-color: #1c1f2e; border-radius: 10px; padding: 14px; margin: 6px 10px; }
.card-name { color: #c9a227; font-size: 16px; font-weight: bold; }
.state-running { color: #4ade80; font-weight: bold; }
.state-stopped { color: #8a8f9c; }
.action-button { background-color: #2a2f45; color: #e6e6e6; border: none;
                 padding: 8px 14px; border-radius: 6px; margin: 2px; }
.action-button:hover { background-color: #c9a227; color: #14161f; }
.primary { background-color: #c9a227; color: #14161f; font-weight: bold;
           padding: 12px 28px; border-radius: 8px; border: none; }
.statusbar { background-color: #1c1f2e; color: #8a8f9c; padding: 8px 14px; }
entry, spinbutton, combobox { background-color: #2a2f45; color: #e6e6e6;
                              border-radius: 6px; padding: 6px; }
"""


class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=f"{APP_NAME} {__version__}")
        self.set_default_size(960, 640)
        self.cfg = config.load()
        self.selected_profile = self.cfg.default_profile
        self._apply_css()
        self._build()

    # ---------- layout ----------

    def _apply_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _build(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header.get_style_context().add_class("header")
        title = Gtk.Label(label=APP_NAME)
        title.get_style_context().add_class("title")
        title.set_xalign(0)
        sub = Gtk.Label(label="sandboxed VMs on QEMU")
        sub.get_style_context().add_class("subtitle")
        sub.set_xalign(0)
        header.pack_start(title, False, False, 0)
        header.pack_start(sub, False, False, 0)
        root.pack_start(header, False, False, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        root.pack_start(content, True, True, 0)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sidebar.get_style_context().add_class("sidebar")
        sidebar.set_size_request(190, -1)
        self.tabs: dict[str, Gtk.ToggleButton] = {}
        for label, key in (("🖥  Machines", "vms"), ("💿  ISOs", "isos"),
                           ("＋  Nuevo", "create"), ("🛡  Perfiles", "profiles")):
            btn = Gtk.ToggleButton(label=label)
            btn.get_style_context().add_class("sidebar-button")
            btn.connect("toggled", self._on_tab, key)
            sidebar.pack_start(btn, False, False, 0)
            self.tabs[key] = btn
        content.pack_start(sidebar, False, False, 0)

        self.stack = Gtk.Stack()
        content.pack_start(self.stack, True, True, 0)
        self.stack.add_named(self._build_vm_view(), "vms")
        self.stack.add_named(self._build_iso_view(), "isos")
        self.stack.add_named(self._build_create_view(), "create")
        self.stack.add_named(self._build_profiles_view(), "profiles")

        self.status = Gtk.Label(label="Listo")
        self.status.set_xalign(0)
        self.status.get_style_context().add_class("statusbar")
        root.pack_end(self.status, False, False, 0)

        self.tabs["vms"].set_active(True)
        self.refresh_vms()

    def _on_tab(self, button: Gtk.ToggleButton, key: str) -> None:
        if not button.get_active():
            return
        for other_key, other in self.tabs.items():
            if other_key != key:
                other.set_active(False)
        self.stack.set_visible_child_name(key)
        if key == "vms":
            self.refresh_vms()
        elif key == "isos":
            self.refresh_isos()

    # ---------- views ----------

    def _section(self, title: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.set_margin_top(20)
        label = Gtk.Label(label=title)
        label.get_style_context().add_class("title")
        label.set_xalign(0)
        box.pack_start(label, False, False, 0)
        return box

    def _build_vm_view(self) -> Gtk.Widget:
        box = self._section("Máquinas")
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box.pack_start(scroll, True, True, 0)
        self.vm_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroll.add(self.vm_list)
        return box

    def _build_iso_view(self) -> Gtk.Widget:
        box = self._section("Imágenes ISO")
        hint = Gtk.Label(label=f"Buscando en {paths.iso_dir()} y ~/Downloads")
        hint.get_style_context().add_class("muted")
        hint.set_xalign(0)
        box.pack_start(hint, False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box.pack_start(scroll, True, True, 0)
        self.iso_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroll.add(self.iso_list)
        return box

    def _build_create_view(self) -> Gtk.Widget:
        from .cli import find_isos
        box = self._section("Nueva máquina")
        grid = Gtk.Grid(row_spacing=12, column_spacing=12)
        box.pack_start(grid, False, False, 0)

        self.name_entry = Gtk.Entry(placeholder_text="p. ej. lab-malware")
        self.name_entry.set_hexpand(True)
        grid.attach(Gtk.Label(label="Nombre", xalign=0), 0, 0, 1, 1)
        grid.attach(self.name_entry, 1, 0, 2, 1)

        self.iso_combo = Gtk.ComboBoxText()
        self.iso_combo.append("", "Sin ISO")
        for iso in find_isos():
            self.iso_combo.append(str(iso), iso.name)
        self.iso_combo.set_active(0)
        self.iso_combo.set_hexpand(True)
        grid.attach(Gtk.Label(label="ISO", xalign=0), 0, 1, 1, 1)
        grid.attach(self.iso_combo, 1, 1, 2, 1)

        self.ram_spin = Gtk.SpinButton.new_with_range(256, 65536, 256)
        self.ram_spin.set_value(self.cfg.default_ram)
        self.cpu_spin = Gtk.SpinButton.new_with_range(1, 32, 1)
        self.cpu_spin.set_value(self.cfg.default_cpus)
        self.disk_spin = Gtk.SpinButton.new_with_range(1, 1024, 1)
        self.disk_spin.set_value(self.cfg.default_disk_gb)
        grid.attach(Gtk.Label(label="RAM (MB)", xalign=0), 0, 2, 1, 1)
        grid.attach(self.ram_spin, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label="CPUs", xalign=0), 2, 2, 1, 1)
        grid.attach(self.cpu_spin, 3, 2, 1, 1)
        grid.attach(Gtk.Label(label="Disco (GB)", xalign=0), 0, 3, 1, 1)
        grid.attach(self.disk_spin, 1, 3, 1, 1)

        self.profile_combo = Gtk.ComboBoxText()
        for key, prof in profiles.PROFILES.items():
            self.profile_combo.append(key, f"{key} — {prof.summary}")
        self.profile_combo.set_active_id(self.cfg.default_profile)
        grid.attach(Gtk.Label(label="Perfil", xalign=0), 0, 4, 1, 1)
        grid.attach(self.profile_combo, 1, 4, 3, 1)

        create_btn = Gtk.Button(label="Crear máquina")
        create_btn.get_style_context().add_class("primary")
        create_btn.connect("clicked", self._on_create)
        box.pack_start(create_btn, False, False, 16)
        return box

    def _build_profiles_view(self) -> Gtk.Widget:
        box = self._section("Perfiles de seguridad")
        note = Gtk.Label(label="Un perfil solo se aplica si puede verificarse; "
                               "si no, la máquina se niega a arrancar.")
        note.get_style_context().add_class("muted")
        note.set_xalign(0)
        box.pack_start(note, False, False, 0)
        for key, prof in profiles.PROFILES.items():
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            card.get_style_context().add_class("card")
            name = Gtk.Label(label=key, xalign=0)
            name.get_style_context().add_class("card-name")
            card.pack_start(name, False, False, 0)
            card.pack_start(Gtk.Label(label=prof.summary, xalign=0), False, False, 0)
            for limit in prof.describe_limits():
                muted = Gtk.Label(label=f"· {limit}", xalign=0)
                muted.get_style_context().add_class("muted")
                card.pack_start(muted, False, False, 0)
            box.pack_start(card, False, False, 0)
        return box

    # ---------- data ----------

    def refresh_vms(self) -> None:
        for child in self.vm_list.get_children():
            self.vm_list.remove(child)
        try:
            rows = vm.status(self.cfg)
        except Exception as exc:  # noqa: BLE001 - never crash the UI on a bad disk
            self.set_status(f"Error leyendo VMs: {exc}")
            return
        if not rows:
            self.vm_list.pack_start(
                Gtk.Label(label="No hay máquinas todavía.", margin=40), False, False, 0)
        for row in rows:
            self.vm_list.pack_start(self._vm_card(row), False, False, 0)
        self.vm_list.show_all()

    def _vm_card(self, row: vm.VmStatus) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        card.get_style_context().add_class("card")

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        card.pack_start(info, True, True, 0)
        name = Gtk.Label(label=row.name, xalign=0)
        name.get_style_context().add_class("card-name")
        info.pack_start(name, False, False, 0)
        state = Gtk.Label(label="● running" if row.running else "○ stopped", xalign=0)
        state.get_style_context().add_class("state-running" if row.running else "state-stopped")
        info.pack_start(state, False, False, 0)
        meta = Gtk.Label(label=f"{row.ram} MB · {row.cpus} vCPU · perfil {row.profile}", xalign=0)
        meta.get_style_context().add_class("muted")
        info.pack_start(meta, False, False, 0)

        buttons = Gtk.Box(spacing=4)
        card.pack_end(buttons, False, False, 0)
        if row.running:
            self._button(buttons, "■ Parar", lambda: self._stop(row.name))
        else:
            self._button(buttons, "▶ Arrancar", lambda: self._run(row.name))
            self._button(buttons, "💿 Live", lambda: self._run(row.name, live=True))
            self._button(buttons, "📥 Instalar", lambda: self._run(row.name, install=True))
            self._button(buttons, "🗑", lambda: self._delete(row.name))
        return card

    @staticmethod
    def _button(box: Gtk.Box, label: str, callback) -> Gtk.Button:
        btn = Gtk.Button(label=label)
        btn.get_style_context().add_class("action-button")
        btn.connect("clicked", lambda _b: callback())
        box.pack_start(btn, False, False, 0)
        return btn

    def refresh_isos(self) -> None:
        from .cli import find_isos
        for child in self.iso_list.get_children():
            self.iso_list.remove(child)
        isos = find_isos(limit=100)
        if not isos:
            self.iso_list.pack_start(Gtk.Label(label="No se encontraron ISOs.", margin=40),
                                     False, False, 0)
        for iso in isos:
            card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            card.get_style_context().add_class("card")
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            card.pack_start(info, True, True, 0)
            name = Gtk.Label(label=iso.name, xalign=0)
            name.get_style_context().add_class("card-name")
            info.pack_start(name, False, False, 0)
            muted = Gtk.Label(label=str(iso.parent), xalign=0)
            muted.get_style_context().add_class("muted")
            info.pack_start(muted, False, False, 0)
            buttons = Gtk.Box(spacing=4)
            card.pack_end(buttons, False, False, 0)
            self._button(buttons, "⚡ Quick boot", lambda p=iso: self._quick_boot(p))
            self._button(buttons, "→ Usar en nueva VM", lambda p=iso: self._use_iso(p))
            self.iso_list.pack_start(card, False, False, 0)
        self.iso_list.show_all()

    # ---------- actions ----------

    def _on_create(self, _button) -> None:
        name = self.name_entry.get_text().strip()
        if not name:
            self._error("Escribe un nombre")
            return
        iso = self.iso_combo.get_active_id() or None
        ram = int(self.ram_spin.get_value())
        cpus = int(self.cpu_spin.get_value())
        disk = int(self.disk_spin.get_value())
        profile = self.profile_combo.get_active_id() or self.cfg.default_profile
        self.set_status(f"Creando '{name}'…")

        def worker() -> None:
            try:
                vm.create(name, iso=iso, ram=ram, cpus=cpus, disk_gb=disk, profile=profile)
            except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
                GLib.idle_add(self._create_done, name, str(exc))
            else:
                GLib.idle_add(self._create_done, name, None)

        threading.Thread(target=worker, daemon=True).start()

    def _create_done(self, name: str, error: str | None) -> bool:
        if error:
            self._error(error)
            self.set_status("Error al crear")
        else:
            self.cfg = config.load()
            self.name_entry.set_text("")
            self.set_status(f"'{name}' creada")
            self.tabs["vms"].set_active(True)
        return False

    def _run(self, name: str, live: bool = False, install: bool = False) -> None:
        try:
            vm.start(name, live=live, install=install)
        except Exception as exc:  # noqa: BLE001
            self._error(str(exc))
            return
        self.set_status(f"'{name}' arrancada")
        self.refresh_vms()
        # Poll so the card flips back to "stopped" when the window closes.
        GLib.timeout_add_seconds(3, self._poll)

    def _poll(self) -> bool:
        self.refresh_vms()
        return False

    def _stop(self, name: str) -> None:
        vm.stop(name)
        self.set_status(f"'{name}' parada")
        self.refresh_vms()

    def _delete(self, name: str) -> None:
        dialog = Gtk.MessageDialog(transient_for=self, flags=0,
                                   message_type=Gtk.MessageType.WARNING,
                                   buttons=Gtk.ButtonsType.YES_NO,
                                   text=f"¿Borrar '{name}'?")
        dialog.format_secondary_text("Se elimina el disco de forma permanente.")
        if dialog.run() == Gtk.ResponseType.YES:
            try:
                vm.delete(name)
            except Exception as exc:  # noqa: BLE001
                self._error(str(exc))
            else:
                self.cfg = config.load()
                self.set_status(f"'{name}' borrada")
                self.refresh_vms()
        dialog.destroy()

    def _quick_boot(self, iso: Path) -> None:
        try:
            from .cli import cmd_boot
            import argparse
            cmd_boot(argparse.Namespace(iso=str(iso), ram=None, cpus=None,
                                        profile=None, headless=False))
        except Exception as exc:  # noqa: BLE001
            self._error(str(exc))

    def _use_iso(self, iso: Path) -> None:
        self.iso_combo.append(str(iso), iso.name)
        self.iso_combo.set_active_id(str(iso))
        self.tabs["create"].set_active(True)

    # ---------- helpers ----------

    def set_status(self, text: str) -> None:
        self.status.set_text(text)

    def _error(self, message: str) -> None:
        dialog = Gtk.MessageDialog(transient_for=self, flags=0,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.OK, text="Error")
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()


def main() -> int:
    paths.ensure_dirs()
    win = MainWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
    return 0


__all__ = ["main", "MainWindow", "qemu"]
