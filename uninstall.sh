#!/usr/bin/env bash
# ==============================================================================
# U30 Air (mu300-linux) Lightweight WebUI Uninstaller
# ==============================================================================
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "[!] Please run as root: sudo bash uninstall.sh"
  exit 1
fi

echo "[1/3] Stopping and disabling u30air-webui.service ..."
systemctl stop u30air-webui.service 2>/dev/null || true
systemctl disable u30air-webui.service 2>/dev/null || true
rm -f /etc/systemd/system/u30air-webui.service
systemctl daemon-reload

echo "[2/3] Restoring original mu300-mihomo helper if backup exists ..."
if [[ -f /opt/mu300/bin/mu300-mihomo.orig ]]; then
  mv -f /opt/mu300/bin/mu300-mihomo.orig /opt/mu300/bin/mu300-mihomo
fi
rm -f /etc/systemd/system/mu300-vpn-mihomo.service.d/affinity.conf
systemctl daemon-reload

echo "[3/3] Removing /opt/u30air-webui ..."
rm -rf /opt/u30air-webui

echo "[✓] Uninstall complete. To re-enable Cockpit (9090): sudo systemctl enable --now cockpit.socket"
