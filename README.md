<div align="center">

<img src="assets/sargatanas-icon.svg" width="140" height="140" alt="Sargatanas">

# Sargatanas

**Virtual machine manager with network isolation, built on QEMU**

Create a VM, pick a security tier, boot it — and always know which protections
are actually active.

<br>

[![CI](https://img.shields.io/github/actions/workflow/status/mildredhoe/sargantanas/ci.yml?branch=main&style=flat-square&label=CI&logo=github)](https://github.com/mildredhoe/sargantanas/actions)
[![Version](https://img.shields.io/badge/version-0.1.0-c9a227?style=flat-square)](https://github.com/mildredhoe/sargantanas/releases)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![QEMU](https://img.shields.io/badge/qemu-6.0%2B-ff6600?style=flat-square)](https://www.qemu.org/)
[![Platforms](https://img.shields.io/badge/platforms-linux%20%7C%20macos%20%7C%20windows-2ea043?style=flat-square)](#-platforms)
[![License](https://img.shields.io/badge/license-MIT-3da639?style=flat-square)](LICENSE)

<br>

[Features](#-features) •
[Security tiers](#️-security-tiers) •
[Network modes](#-network-isolation-modes) •
[Tor & VPN](#-tor--vpn) •
[Install](#-install) •
[Usage](#-usage) •
[GUI](#️-graphical-interface)

</div>

---

## 📌 What it is

**Sargatanas** is a thin layer over **QEMU** for managing virtual machines from
a single terminal or a graphical interface.

The core idea: instead of "just booting a VM", you pick a **security tier** and
Sargatanas configures networking accordingly. Tiers that depend on external
services (Tor, VPN) are **checked before launch**: if the service is not
available, the VM does not start and tells you why — rather than starting
without the protection you thought you had.

All state lives in one directory (`~/Sargatanas`), in a versioned, readable
JSON file.

---

## ✨ Features

<table>
<tr>
<td width="50%" valign="top">

### Machine management
- Create, start, stop and delete VMs
- `qcow2` disk of any size
- Per-VM RAM and CPU count
- Live status (is it really running?)

### Flexible booting
- **Disk** — normal boot of the installed system
- **Install** — boot the ISO installer
- **Live** — boot the ISO **without touching the disk** (changes discarded)
- **Quick boot** — diskless ISO, fully disposable

</td>
<td width="50%" valign="top">

### Security
- **8 tiers** of isolation, most closed to most open
- **6 network modes** to choose from
- **Tor** and **VPN** checks before launch
- Per-tier **firewall rules**, inspectable at any time
- **QEMU sandbox** (seccomp) where the binary supports it

### Utilities
- Disk **snapshots** (`qemu-img`)
- **ISO discovery** in your usual folders
- **Tor identity rotation**
- **WireGuard** and **OpenVPN** templates
- VPN **kill switch**

</td>
</tr>
</table>

---

## 🛡️ Security tiers

From **most closed** to **most open**. Every tier declares what it checks and
what it blocks; ones that depend on an external service **refuse to start** if
they cannot be honoured.

| # | Tier | Guest network | Checks before launch | Sandbox |
|:-:|------|---------------|----------------------|:-------:|
| 1 | 🔴 **`paranoid`** | No network device (air-gap) | — | ✅ |
| 2 | 🟠 **`isolated`** | No network device (air-gap) | — | — |
| 3 | 🟣 **`stealth`** | Outbound only, no inbound ports | **Tor** + **VPN** | ✅ |
| 4 | 🟣 **`tor`** | Outbound only, no inbound ports | **Tor** | — |
| 5 | 🔵 **`vpn`** | NAT through the tunnel | **VPN** | — |
| 6 | 🟢 **`pentesting`** | Internal VM↔VM network + NAT | — | — |
| 7 | ⚪ **`nat`** | NAT with port forwards | — | — |
| 8 | ⚫ **`restricted`** | NIC present but isolated | — | — |

> **Example:** `sargatanas create lab --profile stealth` only starts if a Tor
> SOCKS listener **and** an active VPN interface are present. If either is
> missing, the VM is not launched.

---

## 🌐 Network isolation modes

| Mode | What it does | Requires |
|------|--------------|:--------:|
| `full` | No network device — **complete air-gap** | — |
| `nat` | Internet out with **port forwards** | — |
| `restricted` | NIC present but **isolated** from host and internet | — |
| `internal` | **VM↔VM traffic only** (multicast) | — |
| `tor` | Checks Tor → **outbound only** | Tor |
| `vpn` | Checks VPN → **egress through the tunnel** | VPN |

> **True host-only:** a mode where the VM sees only the *host* and not the
> internet requires a `tap`/bridge with root privileges; QEMU user-mode
> networking (slirp) cannot do it. That is why `restricted` exists — it gives
> you an isolated NIC instead.

---

## 🧅 Tor & VPN

<table>
<tr>
<td width="50%" valign="top">

### Tor
- **SOCKS check** before launch
- Real **identity rotation** (control protocol)
- **`torrc` generation** with:
  - Bridges
  - Exit node selection
  - Strict nodes
  - DNS leak protection

```bash
sargatanas tor config --output torrc
sargatanas tor identity
sargatanas tor check
```

</td>
<td width="50%" valign="top">

### VPN
- **WireGuard** and **OpenVPN** detection
- Ready-made **config templates**
- **Kill switch**:
  - WireGuard: `AllowedIPs = 0.0.0.0/0`
  - OpenVPN: `iptables` rules (Linux)

```bash
sargatanas vpn status
sargatanas vpn wireguard-config
sargatanas vpn kill-switch on --interface wg0
```

</td>
</tr>
</table>

---

## 🔍 Per-tier firewall

Every tier has explicit rules you can inspect at any time:

```bash
sargatanas firewall paranoid
```

```
paranoid - Maximum isolation: air-gapped and seccomp-sandboxed.

  · No network device: guest is air-gapped
  · No inbound or outbound traffic is possible
  · QEMU seccomp sandbox (-sandbox on) where the build supports it
```

---

## 💻 Platforms

| System | Acceleration | Auto-detection |
|--------|--------------|----------------|
| 🐧 **Linux** | KVM | `/dev/kvm` |
| 🍎 **macOS** (Intel and Apple Silicon) | HVF | `aarch64` binary + `virt` machine + `ramfb` |
| 🪟 **Windows** | WHPX | `C:\Program Files\qemu` even when not on `PATH` |

All three systems share the **same command line** and the **same GUI**
(Tkinter, bundled with Python).

---

## 📦 Install

| Step | Command |
|------|---------|
| 1. Requirements | See the table below |
| 2. Clone | `git clone https://github.com/mildredhoe/sargantanas && cd sargantanas` |
| 3. Install | `pip install .` |
| 4. Verify | `sargatanas status` |

**Requirements:** Python 3.9+, QEMU (`qemu-system-*`), `qemu-img`.
`psutil` is installed automatically as a dependency.

```bash
# macOS
brew install qemu

# Debian / Ubuntu
sudo apt install qemu-system-x86 qemu-utils

# Fedora
sudo dnf install qemu-system-x86 qemu-img

# Windows
choco install qemu
```

Optional: **Tor** (`brew install tor` / `apt install tor`) and **WireGuard**
(`brew install wireguard-tools` / `apt install wireguard`).

---

## 🚀 Usage

```bash
# Diagnostics
sargatanas status                # QEMU, acceleration, Tor, free space
sargatanas security              # Security center: live protections
sargatanas isos                  # discovered ISOs

# Create and boot
sargatanas create lab --iso ~/Downloads/debian.iso --ram 2048 --disk 20
sargatanas install lab           # boot the installer
sargatanas run lab               # boot from disk
sargatanas live lab              # boot the ISO without touching the disk

# Management
sargatanas list
sargatanas stop lab
sargatanas delete lab

# Security
sargatanas profiles              # the tiers, closed to open
sargatanas firewall <tier>       # rules for one tier
```

**Override a single parameter:**

```bash
sargatanas run lab --profile tor   # a different tier for this boot only
sargatanas run lab --headless      # serial console, no window
```

---

## 🖥️ Graphical interface

```bash
sargatanas gui          # Tkinter (Linux · macOS · Windows)
sargatanas gui --gtk    # GTK (Linux · macOS)
```

Tabs: **Machines · Create · ISOs · Profiles · Security · Network**

---

## 📁 Where everything lives

```
~/Sargatanas/
├── config.json      # versioned; 0600 on Unix, NTFS ACLs on Windows
├── vms/<name>/      # qcow2 disk + pid
├── isos/            # ISOs are searched here first
└── logs/
```

| Variable | Purpose |
|----------|---------|
| `SARGATANAS_HOME` | Change the data directory |
| `SARGATANAS_ISOS` | Extra ISO folders (`:`-separated) |
| `NO_COLOR` | Disable terminal colour |

---

## 🧑‍💻 Development

```bash
python dev.py test     # test suite (no QEMU needed)
python dev.py dist     # sdist + wheel
python dev.py icons    # regenerate icons from the SVG
```

The tests cover QEMU argument construction, the tiers, configuration, the Tor
control protocol and process management. They run on Linux, macOS and Windows.

---

<div align="center">

## ⚖️ Known limits

What it explicitly does **not** do:

| Limitation | Why |
|------------|-----|
| Transparent **Tor routing** | Requires a dedicated gateway (Whonix/Tails) |
| True **host-only** (VM↔host) | Requires `tap`/bridge with root |
| **Kill switch** on macOS/Windows | Linux only (iptables) |

</div>
