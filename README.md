# mu300-webui — Lightweight Web Console & Optimization Suite for ZTE U30 Air (`mu300-linux`)

**English** | **[简体中文](./README_CN.md)**

An ultra-lightweight, zero-dependency Web Console and hardware optimization suite built specifically for the **ZTE U30 Air 5G Pocket Router (Unisoc T7510 / V510)** running upstream **`mu300-linux` (Ubuntu 24.04 ARM64)**.

Designed around the strict hardware constraints of the device (**1.4 GB RAM + 3.9 GB eMMC**), `mu300-webui` replaces the memory-heavy Cockpit panel (`110 MB+` resident RAM) with a **pure Python 3 standard-library daemon (`~15 MB` RAM) + offline Tailwind SPA**, while providing out-of-the-box **5G baseband control, adaptive single/multi-subscription Mihomo routing, Cortex-A76 big-core affinity, 80% hardware battery charge protection, and eMMC log guarding**.

---

## 📸 Screenshots

| **Overview Dashboard (5G SA & Telemetry)** | **Adaptive Multi-Subscription Mihomo Routing** |
| :---: | :---: |
| ![Overview Dashboard](./docs/screenshots/overview.png) | ![VPN Multi-Subscription Routing](./docs/screenshots/vpn-routing.png) |
| **Wi-Fi Clients & Per-Device Split Routing** | **80% Hardware Charge Limit & 8-Zone Thermal Matrix** |
| ![Wi-Fi Clients & Policy Binding](./docs/screenshots/wifi-clients.png) | ![Power & 8-Zone Thermal Matrix](./docs/screenshots/power-thermal.png) |

---

## ✨ Key Features

### 1. Ultra-Lightweight Full-Featured Web Console (`:8080`)
- **Zero External Backend Dependencies**: High-concurrency HTTP/API daemon written purely in the Python 3 standard library (`~15 MB` RAM footprint).
- **100% Offline Frontend Rendering**: Bundles `tailwindcss.js` locally so the console renders instantaneously even when the SIM card is out of data, disconnected, or in an underground garage with zero internet access.
- **5G RF & Baseband Control**: Real-time monitoring of `RSRP`, `SINR`, `RSRQ`, active 5G/LTE band, and carrier status; supports **one-click 5G band locking (`n41` / `n78` / `n28`)**, soft RF re-registration, data roaming toggle, SIM slot switching, APN management, and **TTL / MTU spoofing**.
- **8-Zone Thermal & big.LITTLE CPU Telemetry**: Real-time visibility into all 8 Unisoc T7510 thermal zones (5G modem, Cortex-A76 cluster, battery, etc.) and per-cluster load across CPU 0–3 (`Cortex-A55`) and CPU 4–7 (`Cortex-A76`).

### 2. Adaptive Single / Multi-Subscription Mihomo Proxy & Per-Device Split Routing
No manual YAML editing required. Manage any Clash / Mihomo subscriptions directly from the WebUI (**`VPN / 代理` → `订阅管理`**):
- **Single-Subscription Mode (Out-of-the-Box)**: Add a single Clash/Mihomo subscription URL to fetch nodes, run concurrent latency tests, and switch nodes with zero connection drops.
- **Multi-Subscription Fusion Mode (Per-Client Split Routing)**: Add two or more subscriptions (e.g., a high-speed transit subscription + a residential ISP subscription) with custom policy group names:
  - Automatically merges all subscriptions into a unified runtime configuration with isolated policy groups and **live exit IP / country probing** per group;
  - In the **`Wi-Fi 与客户端`** tab, bind any LAN/Wi-Fi client (by MAC/IP) to a static IP and cycle its traffic across any policy group or `DIRECT` with a single click (powered by `SRC-IP-CIDR` rules).
- **Private Unix Domain Socket Controller (`/run/mu300-vpn/mihomo-control.sock`)**: Exposes zero TCP control ports, switches nodes in milliseconds with zero subprocess overhead, and restricts the Mihomo TLS/HTTP sniffer to `80, 443, 8443` (preventing `502 Bad Gateway` on WebUI port `8080`).
- **Optional Tailscale Coexistence**: Automatically detects whether Tailscale is installed on the device. When present, top-priority direct bypass rules (`tailscaled` process & UDP `3478` STUN) are injected automatically so enabling TUN proxy never drops your Tailscale connection.

### 3. Hardware-Level Battery & Performance Optimizations
- **Mihomo Cortex-A76 Big-Core Affinity (`CPUAffinity=4-7`)**: Pins Mihomo TUN encryption/decryption threads to the 4× Cortex-A76 big cores (`CPU 4–7`) while isolating `multi-sipa-0` 5G baseband hardware interrupts on the 4× Cortex-A55 little cores (`CPU 0–3`), doubling single-thread proxy throughput.
- **SQC 80% Hardware Charge Limiter (`mu300-sqc-charge`)**: Directly programs the charger IC `REG01 (CHG_CONFIG)` and feeds the `REG05` watchdog on the stock 5.4 kernel SQC driver to **stop charging at 80% (maintaining external USB power without draining the battery) and resume below 75%**, ideal for 24/7 plugged-in operation.
- **Always-Online 5G & Wi-Fi Power Policy**: Configures `WIFI_IDLE=0` and `RADIO_IDLE=keep` so the 5G cellular uplink stays active even when no Wi-Fi clients are connected.
- **eMMC Log Guardian**: Enforces an 8 MB cap on `systemd-journald` and automatically prunes `/var/log` and `/tmp` every 10 minutes to protect the 3.9 GB root partition from filling up.

---

## 📦 Repository Structure

```text
.
├── u30air-webui/                   # Core WebUI & device-side optimization suite
│   ├── server.py                   # Python 3 stdlib backend API & system guardian (:8080)
│   ├── web/
│   │   ├── index.html              # Responsive single-page Web Console
│   │   └── tailwindcss.js          # Local offline Tailwind CSS engine
│   ├── bin/
│   │   ├── mu300-mihomo            # Unix socket Mihomo controller helper
│   │   └── mu300-sqc-charge        # Unisoc SQC charger IC 80% hardware charge limiter
│   └── service/
│       ├── u30air-webui.service    # systemd unit for u30air-webui
│       ├── mihomo-affinity.conf    # Mihomo Cortex-A76 affinity (CPU 4-7) systemd drop-in
│       └── 10-mu300-journald.conf  # journald 8MB RAM/disk cap configuration
├── install.sh                      # On-device one-line installer & optimizer
├── uninstall.sh                    # On-device uninstaller & backup restorer
├── deploy.py                       # PC-side one-click SFTP deployment script
├── .env.example                    # Environment variable template for deploy.py
├── requirements.txt                # PC-side Python dependency (paramiko)
└── README_CN.md                    # Documentation in Simplified Chinese (简体中文文档)
```

---

## 🚀 Quick Start

**Prerequisites**: ZTE U30 Air flashed with upstream `mu300-linux` (Ubuntu 24.04 ARM64), connected to your PC/phone via Wi-Fi or USB RNDIS (default gateway: `192.168.78.1`).

### Option 1: Install Directly on the Device (`install.sh` — Recommended)

Clone or copy this repository to the device and run `install.sh`:

```bash
git clone https://github.com/Yunsuding/mu300-webui.git /tmp/mu300-webui
cd /tmp/mu300-webui
sudo bash install.sh --all -y
```

Or selectively enable specific optimizations:

```bash
# View all available flags
sudo bash install.sh --help

# Example: install WebUI + Mihomo A76 big-core binding + 80% battery charge limit
sudo bash install.sh --optimize-mihomo --enable-battery-limit
```

### Option 2: One-Click Remote SFTP Deploy from PC (`deploy.py`)

1. Install `paramiko` and configure `.env` on your computer:
   ```bash
   pip install -r requirements.txt
   cp .env.example .env
   # Edit .env and set your SSH_PASSWORD (defaults to SSH_HOST=192.168.78.1, SSH_USER=ubuntu)
   ```
2. Run the deployment script:
   ```bash
   python deploy.py
   ```

Once installed, open **`http://192.168.78.1:8080/`** in your browser!

---

## 🔄 Configuring Clash / Mihomo Subscriptions

1. Open **`http://192.168.78.1:8080/`** and navigate to the **`VPN / 代理`** tab.
2. Click **`订阅管理`** (Subscription Manager) in the top-right corner:
   - **Single Subscription**: Enter a name (e.g. `MainProxy`), paste your Clash/Mihomo HTTPS subscription URL, and click **`保存并立即拉取融合`**.
   - **Multi-Subscription Split Routing**: Add multiple subscriptions and specify a custom **Policy Group Name** (`对应策略组名称`) for each one (e.g. `FastTransit` and `ResidentialISP`).
3. Enable **`启用开机自启与透明代理`** at the bottom and click **`应用配置`**.
4. To route specific LAN/Wi-Fi devices through different subscriptions, open the **`Wi-Fi 与客户端`** tab and click the policy badge next to any online client to cycle between policy groups or `DIRECT`.

---

## 🧹 Uninstallation

To remove `u30air-webui` and restore the original helper scripts on the device:

```bash
sudo bash uninstall.sh
```

---

## 📄 License

MIT License
