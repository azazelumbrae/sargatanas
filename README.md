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
- **Eight security tiers** (`paranoid` → `restricted`) with network isolation,
  Tor/VPN verification, and per-tier firewall rules - all enforced at launch.
- **Tor integration**: SOCKS check, identity rotation, and torrc generation
  (bridges, exit nodes, DNS leak protection).
- **VPN support**: WireGuard/OpenVPN detection, config templates, and a kill
  switch (WireGuard `AllowedIPs`; OpenVPN via Linux `iptables`).
- **Security Center** in the GUI and CLI (`sargatanas security`).
- **One-time migration** from the old `~/n01d-machine` config.

## Security profiles

Eight tiers, from most closed to most open. Each one only claims what it
actually enforces, and a profile that cannot be verified **refuses to start**
instead of degrading silently.

| Tier | Network | Verifies at launch |
|---|---|---|
| `paranoid` | Air-gapped + seccomp sandbox | - |
| `isolated` | Air-gapped (no NIC) | - |
| `stealth` | Outbound-only | Tor SOCKS **and** VPN interface |
| `tor` | Outbound-only | Tor SOCKS |
| `vpn` | NAT egress | VPN interface |
| `pentesting` | Internal VM↔VM + NAT | - |
| `nat` | NAT + port forwards | - |
| `restricted` | NIC, no host, no internet | - |

## Network isolation modes

| Mode | What it does |
|---|---|
| `full` | No network device - completely air-gapped |
| `nat` | Outbound NAT with port forwards |
| `restricted` | NIC present, isolated from host and internet |
| `internal` | VMs talk to each other only (multicast) |
| `tor` | Verify Tor is up, then outbound-only |
| `vpn` | Verify the VPN is up, then NAT egress |

## Tor integration

- **SOCKS check** - verifies a listener is actually reachable before booting
- **One-click identity rotation** - `sargatanas tor identity` (real control-port
  protocol, not a fire-and-forget string)
- **torrc generation** - `sargatanas tor config` emits bridges, exit-node
  selection, strict nodes, and DNS leak protection
  (`DNSPort` + `AutomapHostsOnResolve` + `VirtualAddrNetwork`)

## VPN features

- **WireGuard & OpenVPN** detection, tunnel-interface checks, and config
  templates (`sargatanas vpn wireguard-config` / `openvpn-config`)
- **Kill switch** - WireGuard's `AllowedIPs = 0.0.0.0/0` (no route on drop),
  and Linux `iptables` rules for OpenVPN (`sargatanas vpn kill-switch on|off`)

## Firewall rules

`sargatanas firewall <profile>` prints exactly what a tier blocks and allows at
the QEMU networking layer - no fake `iptables` strings that nobody runs. The
same rules appear in the GUI's **Security** tab.

## Security Center

`sargatanas security` (and the GUI's Security tab) shows live Tor/VPN/seccomp
status, the isolation modes, and the tier ordering.

## Honest limits

Stated here rather than discovered later:

- `tor`/`stealth` verify Tor/VPN is up but do **not** transparently route guest
  traffic - that needs a dedicated gateway (Whonix/Tails). The guest must be
  pointed at the host Tor proxy.
- The OpenVPN **kill switch** is Linux-only and needs root; on macOS/Windows it
  is reported as unavailable.
- True **host-only** (guest↔host, no internet) needs a root `tap`/bridge; slirp
  cannot do it, so `restricted` gives you an isolated NIC instead.

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
sargatanas profiles                   # the tiers, most closed -> most open
sargatanas security                   # Security Center: protections + modes
sargatanas firewall paranoid          # what a tier blocks/allows
sargatanas tor config                 # generate a torrc (bridges, exit nodes, DNS)
sargatanas tor identity               # ask Tor for a new circuit
sargatanas vpn status                 # WireGuard/OpenVPN + tunnel status
sargatanas vpn wireguard-config       # WireGuard config template
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
