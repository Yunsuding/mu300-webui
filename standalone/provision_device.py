#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage-2 All-Offline Full-Stack Provisioner for ZTE U30 Air (Ubuntu 24.04 LTS + Vendor Linux 5.4).

Connects over SSH (default 192.168.78.1 over USB/Wi-Fi) to ANY freshly-flashed U30 Air and installs:
  1. Patched U30 Air VPN + Mihomo engine + GeoIP/GeoSite (offline from u30air-patched-vpn-mihomo.tar.gz + extra-vpn)
  2. Hardware 0mA bypass charge limiter (mu300-sqc-charge + mu300-power, 60%/50% anti-swelling + always-online)
  3. Patched native API (mu300-api), static DHCP MAC-IP support (lan-start), uplink gate (u30air-wait-uplink)
  4. CPU 4-7 Cortex-A76 big-core affinity for Mihomo + 6h dual-subscription auto-merge drop-ins
  5. eMMC wear-reduction sysctl, 8MB journald cap, standalone mu300-update safety lock & timer lockdowns
  6. Self-developed lightweight WebUI (:8080) with health check verification

Modes:
  --check            Read-only audit of target device (no writes, no service restarts)
  --mode clean       Default for a brand-new / second U30 Air (keeps its own Wi-Fi SSID/password & SSH keys, no personal secrets)
  --mode clone-personal --backup <tar.gz>
                     Also imports personal VPN subscriptions, profiles & client routing rules from local private backup
                     (never copies Tailscale node identity or SSH host keys, preventing multi-device collisions)
"""

import argparse
import getpass
import hashlib
import io
import json
import os
import shlex
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import paramiko  # noqa: E402
import manage  # noqa: E402

RUNTIME_DIR = HERE / "device-runtime"
BUNDLES_DIR = ROOT / "device-backups" / "offline-bundles"
PATCHED_VPN_TAR = BUNDLES_DIR / "u30air-patched-vpn-mihomo.tar.gz"

# Core customized runtime files to synchronize onto every U30 Air
CORE_RUNTIME_FILES = [
    ("opt-mu300/bin/mu300-sqc-charge", "/opt/mu300/bin/mu300-sqc-charge", 0o755),
    ("opt-mu300/bin/mu300-power", "/opt/mu300/bin/mu300-power", 0o755),
    ("opt-mu300/bin/mu300-api", "/opt/mu300/bin/mu300-api", 0o755),
    ("opt-mu300/bin/lan-start", "/opt/mu300/bin/lan-start", 0o755),
    ("opt-mu300/bin/thermal-guard", "/opt/mu300/bin/thermal-guard", 0o755),
    ("opt-mu300/bin/u30air-wait-uplink", "/opt/mu300/bin/u30air-wait-uplink", 0o755),
    ("opt-mu300/bin/mu300-update", "/opt/mu300/bin/mu300-update", 0o755),
    ("opt-mu300/bin/mu300-extra", "/opt/mu300/bin/mu300-extra", 0o755),
    ("systemd/u30air-uplink-ready.service", "/etc/systemd/system/u30air-uplink-ready.service", 0o644),
    ("systemd/mu300-vpn.service.d/affinity.conf", "/etc/systemd/system/mu300-vpn.service.d/affinity.conf", 0o644),
    ("systemd/mu300-vpn-subscriptions.service.d/u30air-merge.conf", "/etc/systemd/system/mu300-vpn-subscriptions.service.d/u30air-merge.conf", 0o644),
    ("systemd/mu300-vpn-subscriptions.timer.d/interval.conf", "/etc/systemd/system/mu300-vpn-subscriptions.timer.d/interval.conf", 0o644),
    ("system-tuning/sysctl.d/90-u30air-emmc.conf", "/etc/sysctl.d/90-u30air-emmc.conf", 0o644),
    ("system-tuning/sysctl.d/10-bufferbloat.conf", "/etc/sysctl.d/10-bufferbloat.conf", 0o644),
    ("system-tuning/systemd/journald.conf.d/10-mu300.conf", "/etc/systemd/journald.conf.d/10-mu300.conf", 0o644),
]

WEBUI_FILES = [
    (ROOT / "u30air-webui/server.py", "/opt/u30air-webui/server.py", 0o755),
    (ROOT / "u30air-webui/web/index.html", "/opt/u30air-webui/web/index.html", 0o644),
    (ROOT / "u30air-webui/web/tailwindcss.js", "/opt/u30air-webui/web/tailwindcss.js", 0o644),
    (ROOT / "u30air-webui/service/u30air-webui.service", "/etc/systemd/system/u30air-webui.service", 0o644),
    (ROOT / "u30air-webui/bin/mu300-mihomo", "/usr/local/bin/mu300-mihomo", 0o755),
    (ROOT / "u30air-webui/bin/mu300-mihomo", "/opt/mu300/bin/mu300-mihomo", 0o755),
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_stage_tar(extra_vpn_dir: Path, power_conf_override: bool, personal_backup: Path = None) -> tuple[bytes, list[dict]]:
    """Pack all stage-2 files into an in-memory tar.gz with normalized LF for text scripts."""
    buf = io.BytesIO()
    manifest = []

    def add_item(data: bytes, remote_path: str, mode: int):
        rel = remote_path.lstrip("/")
        ti = tarfile.TarInfo(name=rel)
        ti.size = len(data)
        ti.mode = mode
        ti.uid = 0
        ti.gid = 0
        tf.addfile(ti, io.BytesIO(data))
        manifest.append({"path": remote_path, "mode": oct(mode), "sha256": sha256_bytes(data), "size": len(data)})

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        # 1. Core runtime files
        for repo_rel, remote_dest, mode in CORE_RUNTIME_FILES:
            src = RUNTIME_DIR / repo_rel
            raw = src.read_bytes().replace(b"\r\n", b"\n")
            add_item(raw, remote_dest, mode)

        # 2. Patched VPN scripts overlay (/mnt/mu300-disk/extra/vpn/*)
        for p in sorted(extra_vpn_dir.rglob("*")):
            if not p.is_file():
                continue
            rel_in_vpn = p.relative_to(extra_vpn_dir).as_posix()
            remote_dest = f"/mnt/mu300-disk/extra/vpn/{rel_in_vpn}"
            raw = p.read_bytes().replace(b"\r\n", b"\n")
            is_exec = rel_in_vpn.startswith(("bin/", "hooks/")) or rel_in_vpn in ("install.sh", "lib/vpn/mihomo.sh", "lib/vpn/openvpn-up", "service/procd/mu300-vpn")
            add_item(raw, remote_dest, 0o755 if is_exec else 0o644)

        # 3. Default power.conf template (applied when missing or --apply-power-defaults)
        if power_conf_override:
            pconf = (RUNTIME_DIR / "etc-mu300-templates/power.conf").read_bytes().replace(b"\r\n", b"\n")
            add_item(pconf, "/etc/mu300/power.conf", 0o644)

        # 4. WebUI files
        for src, remote_dest, mode in WEBUI_FILES:
            raw = src.read_bytes()
            if src.suffix in (".py", ".html", ".js", ".service") or src.name == "mu300-mihomo":
                raw = raw.replace(b"\r\n", b"\n")
            add_item(raw, remote_dest, mode)

        # 5. Optional personal VPN/routing clone (strictly excludes Tailscale identity & SSH host keys)
        if personal_backup:
            with tarfile.open(personal_backup, mode="r:gz") as btf:
                for m in btf.getmembers():
                    if not m.isfile():
                        continue
                    n = m.name.lstrip("/")
                    if (
                        n.startswith("etc/mu300/vpn/")
                        or n in ("etc/mu300/vpn.conf", "etc/mu300/dhcp-hosts.conf", "etc/mu300/client_policies.json", "opt/u30air-webui/client_policies.json")
                    ):
                        raw = btf.extractfile(m).read()
                        add_item(raw, "/" + n, m.mode & 0o777)

    return buf.getvalue(), manifest


def connect_target(host: str, port: int, user: str, password: str) -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, port=port, username=user, password=password, timeout=12)
    return ssh


def run_sudo(ssh: paramiko.SSHClient, cmd: str, password: str, timeout: int = 180) -> str:
    remote_rc_cmd = f"sudo -S -p '' -- sh -c {shlex.quote(cmd)}"
    stdin, stdout, stderr = ssh.exec_command(remote_rc_cmd, timeout=timeout)
    stdin.write((password or "") + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()
    out_b = stdout.read().decode(errors="replace")
    err_b = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    clean_err = "\n".join(l for l in err_b.splitlines() if "[sudo]" not in l).strip()
    if rc != 0:
        raise RuntimeError(f"远端执行失败 (exit={rc}):\n{out_b}\n{clean_err}")
    return out_b


def main():
    parser = argparse.ArgumentParser(description="ZTE U30 Air 二阶段全离线满血系统一键注入工具")
    parser.add_argument("--host", default="192.168.78.1", help="目标 U30 Air IP 地址（USB/Wi-Fi 默认 192.168.78.1）")
    parser.add_argument("--port", type=int, default=22, help="SSH 端口（默认 22）")
    parser.add_argument("--user", default="ubuntu", help="SSH 用户名（默认 ubuntu）")
    parser.add_argument("--use-env-password", action="store_true", help="优先读取本地 .env 中的 SSH_PASSWORD / SSH_KEY_PASS")
    parser.add_argument("--check", action="store_true", help="只读预检目标设备状态，不写入任何文件")
    parser.add_argument("--mode", choices=["clean", "restore-backup", "clone-personal"], default="clean", help="clean=异机干净初始化（默认）；restore-backup=从本地备份导入 VPN 订阅与分流配置")
    parser.add_argument("--backup", default="", help="--mode clone-personal 时指定的本地 full-device-baseline.tar.gz 路径")
    parser.add_argument("--vpn-bundle", default=str(PATCHED_VPN_TAR), help="改版 VPN+Mihomo 离线包路径")
    parser.add_argument("--force-vpn-bundle", action="store_true", help="即使目标机已有 mihomo 二进制也强制重新解压离线包")
    parser.add_argument("--apply-power-defaults", action="store_true", help="强制覆盖写入 60%%/50%% 防鼓包与常在线 power.conf（新机无配置时会自动写入）")
    args = parser.parse_args()

    password = ""
    if args.use_env_password:
        cfg = manage.load_env()
        password = cfg.get("SSH_KEY_PASS", "")
    if not password:
        password = getpass.getpass(f"请输入目标设备 {args.user}@{args.host} 的登录密码: ")

    personal_backup = None
    if args.mode in ("restore-backup", "clone-personal"):
        if args.backup:
            personal_backup = Path(args.backup).resolve()
        else:
            candidates = sorted((ROOT / "device-backups").glob("standalone-baseline-*/full-device-baseline.tar.gz"))
            if candidates:
                personal_backup = candidates[-1]
        if not personal_backup or not personal_backup.exists():
            raise SystemExit("错误：恢复备份模式需要指定本地备份归档（请通过 --backup <backup.tar.gz> 指定）。")

    print(f"[*] 正在连接 {args.user}@{args.host}:{args.port} ...")
    ssh = connect_target(args.host, args.port, args.user, password)
    try:
        probe_script = r"""
set -e
echo "KERNEL=$(uname -r)"
echo "ARCH=$(uname -m)"
echo "OS=$(. /etc/os-release && echo "$ID:$VERSION_ID")"
echo "DEVICE=$(/opt/mu300/bin/mu300-device 2>/dev/null || echo unknown)"
echo "IMAGE_VER=$(cat /etc/mu300/image-version 2>/dev/null || echo unknown)"
echo "HAS_MIHOMO=$([ -x /mnt/mu300-disk/extra/vpn-mihomo/bin/mihomo ] && echo 1 || echo 0)"
echo "HAS_POWER_CONF=$([ -f /etc/mu300/power.conf ] && echo 1 || echo 0)"
echo "WEBUI_ACTIVE=$(systemctl is-active u30air-webui.service 2>/dev/null || echo inactive)"
df -h /
"""
        info_out = run_sudo(ssh, probe_script, password, timeout=30)
        info = {}
        for line in info_out.splitlines():
            if "=" in line and not line.startswith("/"):
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()

        print("=== 目标设备预检结果 ===")
        print(info_out.strip())

        if info.get("ARCH") != "aarch64" or not info.get("OS", "").startswith("ubuntu:24.04"):
            raise SystemExit(f"拒绝执行：目标系统不是 Ubuntu 24.04 ARM64（实测: {info.get('OS')} / {info.get('ARCH')}）")
        if not info.get("KERNEL", "").startswith("5.4."):
            raise SystemExit(f"拒绝执行：目标内核不是厂商 5.4 内核（实测: {info.get('KERNEL')}），SQC 0mA 硬件旁路仅支持 5.4 驱动栈！")

        need_vpn_tar = (info.get("HAS_MIHOMO") != "1") or args.force_vpn_bundle
        vpn_bundle_path = Path(args.vpn_bundle).resolve()
        if need_vpn_tar and not vpn_bundle_path.exists():
            raise SystemExit(
                f"缺少改版 VPN+Mihomo 离线包：{vpn_bundle_path}\n"
                "请先运行 `python standalone/fetch_offline_bundles.py` 下载离线底包后再试。"
            )

        if args.check:
            print(f"\n[CHECK OK] 目标设备兼容检查通过！需解压 VPN+Mihomo 二进制包: {'是' if need_vpn_tar else '否（已存在，仅同步最新脚本）'}")
            return

        sftp = ssh.open_sftp()
        try:
            # Step A: Install patched VPN + Mihomo binary bundle if needed
            if need_vpn_tar:
                print(f"[*] 正在上传改版 VPN+Mihomo 离线包 ({vpn_bundle_path.stat().st_size / 1048576:.2f} MB) ...")
                vpn_sha = hashlib.sha256(vpn_bundle_path.read_bytes()).hexdigest()
                sftp.put(str(vpn_bundle_path), "/tmp/u30air-patched-vpn.tar.gz")
                install_vpn_cmd = f"""
set -e
actual=$(sha256sum /tmp/u30air-patched-vpn.tar.gz | awk '{{print $1}}')
if [ "$actual" != "{vpn_sha}" ]; then
    rm -f /tmp/u30air-patched-vpn.tar.gz
    echo "VPN bundle SHA256 mismatch!" >&2
    exit 1
fi
mkdir -p /mnt/mu300-disk/extra
tar -xzf /tmp/u30air-patched-vpn.tar.gz -C /mnt/mu300-disk/extra
chown -R root:root /mnt/mu300-disk/extra/vpn /mnt/mu300-disk/extra/vpn-mihomo
rm -f /tmp/u30air-patched-vpn.tar.gz
"""
                run_sudo(ssh, install_vpn_cmd, password, timeout=180)
                print("[OK] 改版 VPN + Mihomo 引擎解压就绪。")

            # Step B: Build & upload stage-2 customization archive
            write_power_conf = args.apply_power_defaults or (info.get("HAS_POWER_CONF") != "1")
            stage_bytes, stage_manifest = build_stage_tar(
                RUNTIME_DIR / "extra-vpn",
                power_conf_override=write_power_conf,
                personal_backup=personal_backup,
            )
            stage_sha = sha256_bytes(stage_bytes)
            print(f"[*] 正在上传定制运行时与 WebUI 组件 ({len(stage_manifest)} 个文件, {len(stage_bytes)/1024:.1f} KB) ...")
            with sftp.file("/tmp/u30air-stage2.tar.gz", "wb") as rf:
                rf.write(stage_bytes)
        finally:
            sftp.close()

        # Step C: Atomically apply, link VPN, harden services, and verify WebUI :8080
        apply_cmd = f"""
set -e
actual=$(sha256sum /tmp/u30air-stage2.tar.gz | awk '{{print $1}}')
if [ "$actual" != "{stage_sha}" ]; then
    rm -f /tmp/u30air-stage2.tar.gz
    echo "Stage2 archive SHA256 mismatch!" >&2
    exit 1
fi
mkdir -p /opt/u30air-webui /etc/mu300/vpn/profiles /etc/mu300/vpn/subscriptions
chmod 0700 /etc/mu300/vpn
tar -xzf /tmp/u30air-stage2.tar.gz -C / --no-same-owner
rm -f /tmp/u30air-stage2.tar.gz

chown -R root:root /opt/u30air-webui /mnt/mu300-disk/extra/vpn
[ -f /etc/mu300/dhcp-hosts.conf ] || touch /etc/mu300/dhcp-hosts.conf

# Link patched VPN & extra commands into system PATH and systemd
MU300_DISK=/mnt/mu300-disk MU300_EXTRA_DIR=/mnt/mu300-disk/extra/vpn MU300_VPN_LIVE=0 sh /mnt/mu300-disk/extra/vpn/hooks/link ""
/opt/mu300/bin/mu300-extra link || true
ln -sfn /mnt/mu300-disk/extra/vpn-mihomo/bin/mihomo /usr/local/bin/mihomo
ln -sfn /mnt/mu300-disk/extra/vpn/bin/mu300-vpn /usr/local/bin/mu300-vpn

# Timezone, package holds & sysctl
timedatectl set-timezone Asia/Shanghai 2>/dev/null || ln -sfn /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
apt-mark hold hostapd kmod >/dev/null 2>&1 || true
sysctl --system >/dev/null 2>&1 || true

# Disable bloated / insecure / upstream polling units & purge any leftover legacy Cockpit 9090 panel
for u in cockpit.socket cockpit.service bluetooth.service mu300-bluetooth.service mu300-telnetd.service mu300-update-notice.timer apt-daily.timer apt-daily-upgrade.timer motd-news.timer; do
    systemctl disable --now "$u" 2>/dev/null || true
done
if dpkg -l 2>/dev/null | grep -qi '^ii.*cockpit'; then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y 'cockpit*' >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get autoremove -y >/dev/null 2>&1 || true
    apt-get clean >/dev/null 2>&1 || true
fi
rm -rf /mnt/mu300-disk/extra/panel /mnt/mu300-disk/extra/.panel.sha256 /etc/cockpit /usr/share/cockpit /usr/local/bin/mu300-panel
rm -f /etc/systemd/system/timers.target.wants/mu300-update-notice.timer
/opt/mu300/bin/mu300-update notice || true

systemctl daemon-reload
systemctl restart systemd-journald 2>/dev/null || true
systemctl enable u30air-webui.service mu300-power.service >/dev/null
systemctl restart mu300-power.service
systemctl restart u30air-webui.service

if [ "{args.mode}" != "clean" ] && [ -f /etc/mu300/vpn.conf ]; then
    python3 -c "import sys; sys.path.insert(0, '/opt/u30air-webui'); import server; server.build_merged_vpn_config()" 2>/dev/null || true
    systemctl enable --now mu300-vpn.service 2>/dev/null || true
    systemctl restart mu300-vpn.service 2>/dev/null || true
fi

# Verify WebUI HTTP 200 on 127.0.0.1:8080
python3 - <<'PY'
import time, urllib.request
ok = False
for _ in range(15):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/api/ping", timeout=2) as r:
            if r.status == 200:
                ok = True
                break
    except Exception:
        time.sleep(0.5)
if not ok:
    raise SystemExit("WebUI health check failed on :8080")
print("WEBUI_HEALTH=200_OK")
PY
"""
        res = run_sudo(ssh, apply_cmd, password, timeout=180)
        print(res.strip())
        print(f"\n[SUCCESS] U30 Air ({args.host}) 满血独立维护版注入完成！")
        print(f"  -> 管理面板地址: http://{args.host}:8080/")
        if args.mode == "clean":
            print("  -> 当前为干净新机模式：登录面板后可在「代理控制」中添加订阅，如需远程组网可执行 `tailscale up` 绑定独立新节点。")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
