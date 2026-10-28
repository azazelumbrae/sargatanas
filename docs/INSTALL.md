# Install

## From source

```bash
git clone https://github.com/alvarosp1/sargatanas
cd sargatanas
python3 -m venv .venv && . .venv/bin/activate
pip install .
sargatanas --version
```

## QEMU per platform

| OS | Command |
|---|---|
| macOS | `brew install qemu` |
| Debian/Ubuntu | `sudo apt install qemu-system-x86 qemu-utils` |
| Fedora | `sudo dnf install qemu-system-x86 qemu-img` |
| Arch | `sudo pacman -S qemu-full` |
| Windows | `choco install qemu`, then add `C:\Program Files\qemu` to `PATH` |

Verify with `sargatanas status`; it reports the binary and the accelerator.

## Acceleration

| Host | Accelerator | Notes |
|---|---|---|
| Linux x86_64 | KVM | needs `/dev/kvm` and membership in the `kvm` group |
| macOS (Intel and Apple Silicon) | HVF | Apple Silicon uses `qemu-system-aarch64` and needs an ARM ISO |
| Windows | WHPX | enable "Windows Hypervisor Platform" in Windows Features |

Without acceleration QEMU still runs, just much slower.

## GUI

```bash
# Debian/Ubuntu
sudo apt install python3-gi gir1.2-gtk-3.0
# macOS
brew install pygobject3 gtk+3
```

Then `sargatanas gui`. The desktop entry in `assets/sargatanas.desktop` can be
installed to `~/.local/share/applications/`.

## Troubleshooting

**`No machine specified`** - QEMU was invoked on aarch64 without `-machine`.
Sargatanas sets `-machine virt` automatically; make sure you are running the
current version.

**`-accel kvm: No such file or directory`** - `/dev/kvm` is missing. In a VM
inside a VM, enable nested virtualization.

**`Refusing to start: Tor profile needs a SOCKS listener`** - start Tor first
(`brew services start tor` / `sudo systemctl start tor`) or check
`SARGATANAS`'s `tor.socks_port` in `~/Sargatanas/config.json`.
