# Sargatanas

<p align="center">
  <img src="assets/sargatanas-icon.svg" width="120" height="120" alt="Sargatanas">
</p>

<p align="center">
  <strong>Sandboxed virtual machines on QEMU</strong><br>
  <em>Isolation you can verify, on Linux, macOS and Windows</em>
</p>

Sargatanas is a small VM manager: create a QEMU machine, point it at an ISO,
pick an isolation profile, and boot. It is deliberately honest about what each
profile does, and it refuses to start instead of pretending.

## Why

Many VM wrappers ship a "Tor mode" that only emits a proxy flag, a "kill
switch" that never installs a rule, and a config in a different folder than
the one the code reads. Sargatanas was written from that starting point: every
protection in `sargatanas/profiles.py` is either enforced at launch or the
launch fails with a reason.

## Features

- **Create / run / stop / delete** QEMU VMs from one command line and one
  JSON config (0600 on Unix, NTFS ACLs on Windows).
- **Hardware acceleration** detected per host: KVM on Linux, HVF on macOS,
  WHPX on Windows, with the right CPU model for each.
- **ISO discovery** and **quick boot**: run an ISO with no disk at all, so
  every session is disposable.
- **Live mode** boots an ISO with the VM disk mounted as a throwaway snapshot:
  you can boot an installer without wiping the installed system.
- **Snapshots** of a stopped VM's disk (`qemu-img`).
- **Safety by construction**: arguments are built as a list, never a shell
  string, so a VM named `x; rm -rf ~` is just a badly named VM.
- **Stale PID detection** via `psutil`: a crashed VM does not show as running
  after reboot, and a reused PID is never killed by mistake.
- **Cross-platform GUI** (Tkinter, ships with Python) plus an optional GTK
  interface, both reusing the exact same core.
- **QEMU seccomp sandbox** (`-sandbox on`) applied automatically where the
  build supports it (Linux), never claimed elsewhere.
- **One-time migration** from the old `~/n01d-machine` config.

## Security profiles

| Profile | What it does | Enforced? |
|---|---|---|
| `isolated` | No network device in the guest | Yes, always |
| `nat` | QEMU user networking, with `2222:22` forwarded by default | Yes, always |
| `host-only` | User networking with `restrict=yes`: internet out, no host/LAN access | Yes, always |
| `tor` | Verifies a Tor SOCKS listener is up, then boots with no inbound ports | Only if Tor is reachable, else **refuses to start** |
| `vpn` | Verifies the VPN interface is up before booting | Only if the interface is up, else **refuses to start** |

**What the profiles do not do.** These are stated here rather than discovered
later:

- `tor` does **not** transparently route guest traffic through Tor - that
  requires a dedicated gateway (Whonix/Tails). It only verifies Tor is
  reachable and boots with outbound-only networking; you must point the
  guest's own applications at the host Tor proxy.
- `vpn` does not install a **kill switch**. If the tunnel drops, guest traffic
  is not blocked. This is not implemented yet.
- `host-only` blocks the *host and LAN*, not the internet.

## Requirements

- Python 3.9+ (the GUI uses Tkinter, which ships with CPython)
- QEMU (`qemu-system-x86_64` or `qemu-system-aarch64`) and `qemu-img`
- `psutil` (installed automatically as a dependency)

```bash
# macOS
brew install qemu

# Debian / Ubuntu
sudo apt install qemu-system-x86 qemu-utils

# Fedora
sudo dnf install qemu-system-x86 qemu-img

# Windows
choco install qemu        # then add C:\Program Files\qemu to PATH, or
                          # Sargatanas will find it there automatically
```

Optional: Tor (`brew install tor` / `apt install tor`) for the `tor` profile;
PyGObject + GTK3 only if you want the GTK interface instead of Tkinter.

## Install

```bash
git clone https://github.com/alvarosp1/sargatanas
cd sargatanas
pip install .            # or: pipx install .
# GTK interface instead of Tkinter:  pip install '.[gtk]'
```

Run without installing:

```bash
./sargatanas.py list
```

## Usage

```bash
sargatanas status                     # QEMU, acceleration, Tor, free space
sargatanas isos                       # what it can find
sargatanas create lab --iso ~/Downloads/debian.iso --ram 2048 --disk 20
sargatanas run lab                    # boot from disk
sargatanas install lab                # boot the installer
sargatanas live lab                   # boot the ISO, discard changes
sargatanas list
sargatanas stop lab
sargatanas delete lab
sargatanas profiles                   # what each profile really enforces
sargatanas gui                        # Tkinter GUI (works everywhere)
sargatanas gui --gtk                  # GTK interface (Linux/macOS)
```

Overrides:

```bash
sargatanas run lab --profile tor      # temporary profile for one boot
sargatanas run lab --headless         # serial console on stdio
```

## Data layout

Everything lives under one directory: `~/Sargatanas` (change it with
`SARGATANAS_HOME`).

```
~/Sargatanas/
├── config.json      # schema-versioned; 0600 on Unix, NTFS ACLs on Windows
├── vms/<name>/      # disk + pid
├── isos/            # ISOs found first
└── logs/
```

Extra ISO folders: `SARGATANAS_ISOS=~/img:~/Downloads/linux`.

## Development

```bash
python dev.py test                  # or: python -m unittest discover -s tests -t .
python dev.py dist                  # sdist + wheel
python dev.py icons                 # render icons from the SVG
```

The suite needs no QEMU: argument construction is a pure function, and the
process (psutil) and Tor interactions are mocked. CI runs it on Linux, macOS
and Windows.

## Status

Early (0.1.0). The core and the process/argv layer are tested; the GUIs are
smoke-tested only.

## Credits

Derived from [n01d-machine](https://github.com/bad-antics/n01d-machine) by the
NullSec Team (MIT). Original copyright retained in [LICENSE](LICENSE).

## License

MIT - see [LICENSE](LICENSE).
