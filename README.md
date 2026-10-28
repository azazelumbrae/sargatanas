<div align="center">

<img src="assets/sargatanas-icon.svg" width="140" height="140" alt="Sargatanas">

# Sargatanas

**Gestor de máquinas virtuales con aislamiento de red, sobre QEMU**

Crea una VM, asígnale un nivel de seguridad, arráncala y sabe en todo momento
qué protecciones están activas de verdad.

<br>

[![CI](https://img.shields.io/github/actions/workflow/status/mildredhoe/sargantanas/ci.yml?branch=main&style=flat-square&label=CI&logo=github)](https://github.com/mildredhoe/sargantanas/actions)
[![Versión](https://img.shields.io/badge/versión-0.1.0-c9a227?style=flat-square)](https://github.com/mildredhoe/sargantanas/releases)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![QEMU](https://img.shields.io/badge/qemu-6.0%2B-ff6600?style=flat-square)](https://www.qemu.org/)
[![Plataformas](https://img.shields.io/badge/plataformas-linux%20%7C%20macos%20%7C%20windows-2ea043?style=flat-square)](#-plataformas)
[![Licencia](https://img.shields.io/badge/licencia-MIT-3da639?style=flat-square)](LICENSE)

<br>

[Funcionalidades](#-funcionalidades) •
[Niveles](#-niveles-de-seguridad) •
[Modos de red](#-modos-de-aislamiento-de-red) •
[Tor y VPN](#-tor-y-vpn) •
[Instalación](#-instalación) •
[Uso](#-uso) •
[GUI](#-interfaz-gráfica)

</div>

---

## 📌 Qué es

**Sargatanas** es una capa sencilla sobre **QEMU** para gestionar máquinas
virtuales desde una sola terminal o una interfaz gráfica.

Su idea central: en lugar de arrancar una VM "y ya", eliges un **nivel de
seguridad** y Sargatanas configura la red en consecuencia. Los niveles que
dependen de servicios externos (Tor, VPN) **se comprueban antes de arrancar**:
si el servicio no está disponible, la VM no arranca y te dice por qué, en vez
de arrancar sin la protección que creías tener.

Todo el estado vive en un único directorio (`~/Sargatanas`), con un JSON
versionado y legible.

---

## ✨ Funcionalidades

<table>
<tr>
<td width="50%" valign="top">

### Gestión de máquinas
- Crear, arrancar, parar y borrar VMs
- Disco `qcow2` del tamaño que quieras
- RAM y CPUs por VM
- Estado en vivo (¿está corriendo de verdad?)

### Arranque flexible
- **Disco** — arranque normal del sistema instalado
- **Instalador** — arranca el ISO para instalar
- **Live** — arranca el ISO **sin tocar el disco** (los cambios se descartan)
- **Quick boot** — ISO sin disco, totalmente desechable

</td>
<td width="50%" valign="top">

### Seguridad
- **8 niveles** de aislamiento, de más cerrado a más abierto
- **6 modos de red** configurables
- Comprobación de **Tor** y **VPN** antes de arrancar
- Reglas de **cortafuegos** por nivel, consultables
- **Sandbox de QEMU** (seccomp) donde el binario lo soporte

### Utilidades
- **Snapshots** del disco (`qemu-img`)
- **Detección de ISOs** en tus carpetas habituales
- **Rotación de identidad de Tor**
- **Plantillas** de WireGuard y OpenVPN
- **Kill switch** de VPN

</td>
</tr>
</table>

---

## 🛡️ Niveles de seguridad

De **más cerrado** a **más abierto**. Cada nivel declara qué comprueba y qué
bloquea; los que dependen de un servicio externo se **niegan a arrancar** si no
pueden cumplirse.

| # | Nivel | Red del guest | Comprueba antes de arrancar | Sandbox |
|:-:|-------|---------------|-----------------------------|:-------:|
| 1 | 🔴 **`paranoid`** | Sin dispositivo de red (air-gap) | — | ✅ |
| 2 | 🟠 **`isolated`** | Sin dispositivo de red (air-gap) | — | — |
| 3 | 🟣 **`stealth`** | Solo salida, sin puertos de entrada | **Tor** + **VPN** | ✅ |
| 4 | 🟣 **`tor`** | Solo salida, sin puertos de entrada | **Tor** | — |
| 5 | 🔵 **`vpn`** | NAT siguiendo el túnel | **VPN** | — |
| 6 | 🟢 **`pentesting`** | Red interna VM↔VM + NAT | — | — |
| 7 | ⚪ **`nat`** | NAT con reenvío de puertos | — | — |
| 8 | ⚫ **`restricted`** | NIC aislada (sin host, sin internet) | — | — |

> **Ejemplo:** `sargatanas create lab --profile stealth` solo arranca si hay un
> SOCKS de Tor **y** una interfaz VPN activas. Si falta cualquiera, la VM no se
> lanza.

---

## 🌐 Modos de aislamiento de red

| Modo | Qué hace | Requiere |
|------|----------|:--------:|
| `full` | Sin dispositivo de red — **air-gap** total | — |
| `nat` | Salida a internet con **reenvío de puertos** | — |
| `restricted` | NIC presente pero **aislada** del host y de internet | — |
| `internal` | Solo **comunicación entre VMs** (multicast) | — |
| `tor` | Comprueba Tor → modo **solo salida** | Tor |
| `vpn` | Comprueba VPN → **egress por el túnel** | VPN |

> **Host-only real:** un modo donde la VM vea sólo al *host* y no a internet
> requiere un `tap`/bridge con permisos de root; QEMU en modo usuario (slirp) no
> puede hacerlo. Por eso existe `restricted`, que da una NIC aislada.

---

## 🧅 Tor y VPN

<table>
<tr>
<td width="50%" valign="top">

### Tor
- **Comprobación de SOCKS** antes de arrancar
- **Rotación de identidad** real (protocolo de control)
- **Generación de `torrc`** con:
  - Puentes (bridges)
  - Selección de nodos de salida
  - Nodos estrictos
  - Protección anti-fuga de DNS

```bash
sargatanas tor config --output torrc
sargatanas tor identity
sargatanas tor check
```

</td>
<td width="50%" valign="top">

### VPN
- Detección de **WireGuard** y **OpenVPN**
- **Plantillas** de configuración listas
- **Kill switch**:
  - WireGuard: `AllowedIPs = 0.0.0.0/0`
  - OpenVPN: reglas `iptables` (Linux)

```bash
sargatanas vpn status
sargatanas vpn wireguard-config
sargatanas vpn kill-switch on --interface wg0
```

</td>
</tr>
</table>

---

## 🔍 Cortafuegos por nivel

Cada nivel tiene sus reglas explícitas, consultables en cualquier momento:

```bash
sargatanas firewall paranoid
```

```
paranoid - Máximo aislamiento: air-gap y sandbox seccomp.

  · Sin dispositivo de red: el guest está aislado
  · No es posible tráfico de entrada ni de salida
  · Sandbox seccomp de QEMU (-sandbox on) donde el binario lo soporte
```

---

## 💻 Plataformas

| Sistema | Aceleración | Detección automática |
|---------|-------------|----------------------|
| 🐧 **Linux** | KVM | `/dev/kvm` |
| 🍎 **macOS** (Intel y Apple Silicon) | HVF | Binario `aarch64` + máquina `virt` + `ramfb` |
| 🪟 **Windows** | WHPX | `C:\Program Files\qemu` aunque no esté en `PATH` |

Los tres sistemas usan la **misma interfaz de línea de comandos** y la **misma
GUI** (Tkinter, incluida con Python).

---

## 📦 Instalación

| Paso | Comando |
|------|---------|
| 1. Requisitos | Ver tabla de abajo |
| 2. Clonar | `git clone https://github.com/mildredhoe/sargantanas && cd sargantanas` |
| 3. Instalar | `pip install .` |
| 4. Comprobar | `sargatanas status` |

**Requisitos:** Python 3.9+, QEMU (`qemu-system-*`), `qemu-img`.
`psutil` se instala solo como dependencia.

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

Opcional: **Tor** (`brew install tor` / `apt install tor`) y **WireGuard**
(`brew install wireguard-tools` / `apt install wireguard`).

---

## 🚀 Uso

```bash
# Diagnóstico
sargatanas status                # QEMU, aceleración, Tor, espacio libre
sargatanas security              # Centro de seguridad: protecciones en vivo
sargatanas isos                  # ISOs encontrados

# Crear y arrancar
sargatanas create lab --iso ~/Downloads/debian.iso --ram 2048 --disk 20
sargatanas install lab           # arrancar el instalador
sargatanas run lab               # arrancar desde disco
sargatanas live lab              # arrancar el ISO sin tocar el disco

# Gestión
sargatanas list
sargatanas stop lab
sargatanas delete lab

# Seguridad
sargatanas profiles              # los niveles, de cerrado a abierto
sargatanas firewall <nivel>      # reglas de un nivel
```

**Sobrescribir un parámetro puntual:**

```bash
sargatanas run lab --profile tor   # otro nivel sólo para este arranque
sargatanas run lab --headless      # consola serie, sin ventana
```

---

## 🖥️ Interfaz gráfica

```bash
sargatanas gui          # Tkinter (Linux · macOS · Windows)
sargatanas gui --gtk    # GTK (Linux · macOS)
```

Pestañas: **Machines · Create · ISOs · Profiles · Security · Network**

---

## 📁 Dónde se guarda todo

```
~/Sargatanas/
├── config.json      # versionado; 0600 en Unix, ACLs NTFS en Windows
├── vms/<nombre>/    # disco qcow2 + pid
├── isos/            # aquí se buscan los ISO primero
└── logs/
```

| Variable | Para qué |
|----------|----------|
| `SARGATANAS_HOME` | Cambiar el directorio de datos |
| `SARGATANAS_ISOS` | Carpetas ISO adicionales (`:` separado) |
| `NO_COLOR` | Desactivar color en la terminal |

---

## 🧑‍💻 Desarrollo

```bash
python dev.py test     # suite de tests (no necesita QEMU)
python dev.py dist     # paquete sdist + wheel
python dev.py icons    # regenerar iconos desde el SVG
```

Los tests cubren la construcción de argumentos de QEMU, los niveles, la
configuración, el protocolo de Tor y la gestión de procesos. Corren en Linux,
macOS y Windows.

---

<div align="center">

## ⚖️ Límites conocidos

Lo que **no** hace, dicho por delante:

| Lo que no hace | Por qué |
|----------------|---------|
| Routing **transparente** por Tor | Necesita una gateway dedicada (Whonix/Tails) |
| **Host-only** real (VM↔host) | Necesita `tap`/bridge con root |
| **Kill switch** en macOS/Windows | Sólo implementado en Linux (iptables) |

<br>

Hecho con ❤️ por [Álvaro Souza](https://github.com/mildredhoe)

<sub>MIT · Basado en <a href="https://github.com/bad-antics/n01d-machine">n01d-machine</a></sub>

</div>
