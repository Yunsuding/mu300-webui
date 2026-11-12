#!/usr/bin/env bash
# ==============================================================================
# U30 Air (mu300-linux) Lightweight WebUI & Hardware Enhancement Installer
# Target Device: ZTE U30 Air 5G (Unisoc T7510 / V510, Ubuntu 24.04 ARM64)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${SCRIPT_DIR}/u30air-webui"
INSTALL_DIR="/opt/u30air-webui"
MU300_BIN_DIR="/opt/mu300/bin"

OPT_ALL=0
OPT_MIHOMO=0
OPT_BATTERY=0
OPT_KEEP_ONLINE=0
OPT_DISABLE_COCKPIT=0
NON_INTERACTIVE=0

usage() {
  cat <<EOF
Usage: sudo bash install.sh [OPTIONS]

Options:
  --all                   Enable all recommended hardware & performance optimizations
  --optimize-mihomo       Bind Mihomo to Cortex-A76 big cores (CPU 4-7) & install Unix socket helper
  --enable-battery-limit  Install SQC hardware charge limiter (80% stop / 75% resume)
  --keep-online           Keep 5G cellular online when Wi-Fi is idle (WIFI_IDLE=0, RADIO_IDLE=keep)
  --disable-cockpit       Stop and disable cockpit.socket (frees ~110MB RAM)
  -y, --yes               Run non-interactively with selected flags (defaults to --all if none given)
  -h, --help              Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)
      OPT_ALL=1
      shift
      ;;
    --optimize-mihomo)
      OPT_MIHOMO=1
      shift
      ;;
    --enable-battery-limit)
      OPT_BATTERY=1
      shift
      ;;
    --keep-online)
      OPT_KEEP_ONLINE=1
      shift
      ;;
    --disable-cockpit)
      OPT_DISABLE_COCKPIT=1
      shift
      ;;
    -y|--yes)
      NON_INTERACTIVE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[!] Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "[!] Please run this installer as root: sudo bash install.sh"
  exit 1
fi

if [[ ! -d "${SRC_DIR}" ]]; then
  echo "[!] Source directory not found: ${SRC_DIR}"
  exit 1
fi

if [[ "${OPT_ALL}" -eq 1 ]]; then
  OPT_MIHOMO=1
  OPT_BATTERY=1
  OPT_KEEP_ONLINE=1
  OPT_DISABLE_COCKPIT=1
elif [[ "${NON_INTERACTIVE}" -eq 1 && "${OPT_MIHOMO}" -eq 0 && "${OPT_BATTERY}" -eq 0 && "${OPT_KEEP_ONLINE}" -eq 0 && "${OPT_DISABLE_COCKPIT}" -eq 0 ]]; then
  OPT_MIHOMO=1
  OPT_BATTERY=1
  OPT_KEEP_ONLINE=1
  OPT_DISABLE_COCKPIT=1
elif [[ -t 0 && "${OPT_MIHOMO}" -eq 0 && "${OPT_BATTERY}" -eq 0 && "${OPT_KEEP_ONLINE}" -eq 0 && "${OPT_DISABLE_COCKPIT}" -eq 0 ]]; then
  echo "=================================================================="
  echo "  ZTE U30 Air (mu300) Lightweight WebUI & Optimization Suite"
  echo "=================================================================="
  read -r -p "Apply all recommended optimizations (A76 big-core Mihomo, 80% charge limit, 5G keep-online, disable Cockpit)? [Y/n]: " ans
  ans="${ans:-Y}"
  if [[ "${ans}" =~ ^[Yy]$ ]]; then
    OPT_MIHOMO=1
    OPT_BATTERY=1
    OPT_KEEP_ONLINE=1
    OPT_DISABLE_COCKPIT=1
  fi
fi

echo "[1/6] Installing u30air-webui files to ${INSTALL_DIR} ..."
mkdir -p "${INSTALL_DIR}/web" "${INSTALL_DIR}/bin" "${INSTALL_DIR}/service"
cp -f "${SRC_DIR}/server.py" "${INSTALL_DIR}/server.py"
cp -f "${SRC_DIR}/web/index.html" "${INSTALL_DIR}/web/index.html"
cp -f "${SRC_DIR}/web/tailwindcss.js" "${INSTALL_DIR}/web/tailwindcss.js"
cp -f "${SRC_DIR}/bin/mu300-mihomo" "${INSTALL_DIR}/bin/mu300-mihomo"
cp -f "${SRC_DIR}/bin/mu300-sqc-charge" "${INSTALL_DIR}/bin/mu300-sqc-charge"
cp -f "${SRC_DIR}/service/"* "${INSTALL_DIR}/service/"
chmod 755 "${INSTALL_DIR}/server.py" "${INSTALL_DIR}/bin/mu300-mihomo" "${INSTALL_DIR}/bin/mu300-sqc-charge"

echo "[2/6] Configuring systemd journald 8MB cap (eMMC protection) ..."
mkdir -p /etc/systemd/journald.conf.d
cp -f "${SRC_DIR}/service/10-mu300-journald.conf" /etc/systemd/journald.conf.d/10-mu300-journald.conf
systemctl restart systemd-journald || true

if [[ "${OPT_MIHOMO}" -eq 1 ]]; then
  echo "[3/6] Applying Mihomo Cortex-A76 big-core affinity (CPU 4-7) & Unix socket helper ..."
  mkdir -p "${MU300_BIN_DIR}"
  if [[ -f "${MU300_BIN_DIR}/mu300-mihomo" && ! -f "${MU300_BIN_DIR}/mu300-mihomo.orig" ]]; then
    cp -a "${MU300_BIN_DIR}/mu300-mihomo" "${MU300_BIN_DIR}/mu300-mihomo.orig"
  fi
  cp -f "${SRC_DIR}/bin/mu300-mihomo" "${MU300_BIN_DIR}/mu300-mihomo"
  chmod 755 "${MU300_BIN_DIR}/mu300-mihomo"

  mkdir -p /etc/systemd/system/mu300-vpn-mihomo.service.d
  cp -f "${SRC_DIR}/service/mihomo-affinity.conf" /etc/systemd/system/mu300-vpn-mihomo.service.d/affinity.conf
else
  echo "[3/6] Skipping Mihomo optimization (pass --optimize-mihomo to enable)."
fi

if [[ "${OPT_BATTERY}" -eq 1 ]]; then
  echo "[4/6] Installing SQC hardware battery charge limiter (80% stop / 75% resume) ..."
  mkdir -p "${MU300_BIN_DIR}" /etc/mu300
  cp -f "${SRC_DIR}/bin/mu300-sqc-charge" "${MU300_BIN_DIR}/mu300-sqc-charge"
  chmod 755 "${MU300_BIN_DIR}/mu300-sqc-charge"
  if [[ -f /etc/mu300/power.conf ]]; then
    sed -i 's/^CHARGE_LIMIT=.*/CHARGE_LIMIT=1/' /etc/mu300/power.conf || true
    sed -i 's/^CHARGE_STOP=.*/CHARGE_STOP=80/' /etc/mu300/power.conf || true
    sed -i 's/^CHARGE_START=.*/CHARGE_START=75/' /etc/mu300/power.conf || true
  fi
else
  echo "[4/6] Skipping SQC battery charge limit (pass --enable-battery-limit to enable)."
fi

if [[ "${OPT_KEEP_ONLINE}" -eq 1 ]]; then
  echo "[5/6] Configuring 5G cellular & Wi-Fi keep-online in /etc/mu300/power.conf ..."
  if [[ -f /etc/mu300/power.conf ]]; then
    sed -i 's/^WIFI_IDLE=.*/WIFI_IDLE=0/' /etc/mu300/power.conf || true
    sed -i 's/^RADIO_IDLE=.*/RADIO_IDLE=keep/' /etc/mu300/power.conf || true
  fi
else
  echo "[5/6] Skipping keep-online power tuning (pass --keep-online to enable)."
fi

if [[ "${OPT_DISABLE_COCKPIT}" -eq 1 ]]; then
  echo "[*] Disabling Cockpit (9090) to save ~110MB RAM ..."
  systemctl stop cockpit.socket cockpit.service 2>/dev/null || true
  systemctl disable cockpit.socket cockpit.service 2>/dev/null || true
fi

echo "[6/6] Enabling Dual-SIM AT brokers (if standalone runtime is present) and starting u30air-webui.service on port 8080 ..."
RT_DIR="${SCRIPT_DIR}/standalone/device-runtime"
if [[ -d "${RT_DIR}" ]]; then
  install -D -o root -g root -m 644 "${RT_DIR}/opt-mu300/lib/sim-config.sh" /opt/mu300/lib/sim-config.sh
  for b in mu300-api mu300-sim mu300-sim-u30 mu300-sim2-start mu300-sim2-warmup mu300-sim-boot-prep mobile-data sms mu300-ussd; do
    if [[ -f "${RT_DIR}/opt-mu300/bin/${b}" ]]; then
      install -D -o root -g root -m 755 "${RT_DIR}/opt-mu300/bin/${b}" "/opt/mu300/bin/${b}"
    fi
  done
  for u in mu300-atd4.service mu300-atd5.service mu300-sim-prepare.service mu300-sim-warmup.service; do
    if [[ -f "${RT_DIR}/systemd/${u}" ]]; then
      install -D -o root -g root -m 644 "${RT_DIR}/systemd/${u}" "/etc/systemd/system/${u}"
    fi
  done
  install -D -o root -g root -m 644 "${RT_DIR}/systemd/mu300-atd.service.d/u30air-slot0.conf" /etc/systemd/system/mu300-atd.service.d/u30air-slot0.conf
  install -D -o root -g root -m 644 "${RT_DIR}/systemd/mu300-atd2.service.d/u30air-slot0.conf" /etc/systemd/system/mu300-atd2.service.d/u30air-slot0.conf
  install -D -o root -g root -m 644 "${RT_DIR}/systemd/mu300-mobile-data.service.d/fast-redial.conf" /etc/systemd/system/mu300-mobile-data.service.d/fast-redial.conf
fi
cp -f "${SRC_DIR}/service/u30air-webui.service" /etc/systemd/system/u30air-webui.service
systemctl daemon-reload
systemctl enable mu300-atd4.service mu300-atd5.service mu300-sim-prepare.service mu300-sim-warmup.service 2>/dev/null || true
if [[ -x /opt/mu300/bin/mu300-sim-boot-prep ]]; then
  /opt/mu300/bin/mu300-sim-boot-prep enable-live || true
fi
systemctl enable u30air-webui.service
systemctl restart u30air-webui.service

if systemctl is-active --quiet mu300-vpn-mihomo.service 2>/dev/null; then
  systemctl restart mu300-vpn-mihomo.service || true
fi

LAN_IP="$(ip -4 addr show br-lan 2>/dev/null | awk '/inet / {split($2,a,"/"); print a[1]; exit}')"
LAN_IP="${LAN_IP:-192.168.78.1}"

echo ""
echo "=================================================================="
echo "  u30air-webui installed and running!"
echo "  Web Console URL: http://${LAN_IP}:8080/"
echo "=================================================================="
