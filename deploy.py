#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC-side One-Click SFTP Deployment Script for u30air-webui & Standalone Runtime.
Default gateway: 192.168.78.1 (Wi-Fi / USB RNDIS), customizable via `.env` (e.g. Tailscale IP).
Supports:
  python deploy.py                  Deploy :8080 WebUI + SQC 0mA limiter + Mihomo helper
  python deploy.py --with-runtime   Also sync standalone/device-runtime core & patched VPN scripts
  python deploy.py --check          Read-only syntax & device compatibility check
"""

import argparse
import io
import os
import shlex
import sys
from pathlib import Path

import paramiko

ROOT_DIR = Path(__file__).resolve().parent
LOCAL_DIR = ROOT_DIR / "u30air-webui"
RUNTIME_BASE = ROOT_DIR / "standalone" / "device-runtime"
REMOTE_TMP_DIR = "/tmp/u30air-webui-deploy"


def load_env(env_path: Path = None) -> dict:
    env_path = env_path or (ROOT_DIR / ".env")
    env = {}
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


def run_sudo(ssh: paramiko.SSHClient, password: str, cmd: str):
    full_cmd = f"sudo -S -p '' -- bash -lc {shlex.quote(cmd)}"
    stdin, stdout, stderr = ssh.exec_command(full_cmd, timeout=60)
    stdin.write(password + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    rc = stdout.channel.recv_exit_status()
    if out:
        print(out)
    if rc != 0:
        clean_err = "\n".join(l for l in err.splitlines() if "[sudo]" not in l)
        raise RuntimeError(f"Remote command failed (rc={rc}): {clean_err}")
    return out


def sftp_mkdir_p(sftp: paramiko.SFTPClient, remote_dir: str):
    dirs = []
    cur = remote_dir
    while len(cur) > 1:
        dirs.append(cur)
        cur = os.path.dirname(cur)
    for d in reversed(dirs):
        try:
            sftp.stat(d)
        except IOError:
            sftp.mkdir(d)


def upload_text_lf(sftp: paramiko.SFTPClient, local_path: Path, remote_path: str):
    data = local_path.read_bytes().replace(b"\r\n", b"\n")
    with io.BytesIO(data) as bio:
        sftp.putfo(bio, remote_path)


def upload_tree(sftp: paramiko.SFTPClient, local_dir: Path, remote_dir: str):
    sftp_mkdir_p(sftp, remote_dir)
    for root, _, files in os.walk(local_dir):
        rel = os.path.relpath(root, local_dir)
        target_dir = remote_dir if rel == "." else f"{remote_dir}/{rel.replace(os.sep, '/')}"
        sftp_mkdir_p(sftp, target_dir)
        for fname in files:
            if fname.endswith(".pyc") or "__pycache__" in root:
                continue
            l_path = Path(root) / fname
            r_path = f"{target_dir}/{fname}"
            if fname.endswith((".py", ".sh", ".service", ".conf", ".html", ".js")) or "bin" in root:
                upload_text_lf(sftp, l_path, r_path)
            else:
                sftp.put(str(l_path), r_path)


def deploy(target_host: str = None, check_only: bool = False, with_runtime: bool = False):
    for py_file in (LOCAL_DIR / "server.py", LOCAL_DIR / "bin" / "mu300-mihomo"):
        if py_file.exists():
            compile(py_file.read_bytes(), str(py_file), "exec")

    env = load_env()
    host = target_host or os.environ.get("SSH_HOST") or env.get("SSH_HOST", "192.168.78.1")
    port = int(os.environ.get("SSH_PORT") or env.get("SSH_PORT", "22"))
    user = os.environ.get("SSH_USER") or env.get("SSH_USER", "ubuntu")
    password = os.environ.get("SSH_PASSWORD") or env.get("SSH_PASSWORD") or env.get("SSH_KEY_PASS", "")

    if not password:
        print("[!] Missing SSH_PASSWORD. Please copy .env.example to .env and fill in SSH_PASSWORD.")
        sys.exit(1)

    print(f"[*] Connecting to {user}@{host}:{port} ...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, port=port, username=user, password=password, timeout=10)
    except Exception as e:
        if not target_host and host != "192.168.78.1":
            print(f"[!] {host} unreachable ({e}), falling back to LAN/USB gateway 192.168.78.1 ...")
            host = "192.168.78.1"
            ssh.connect(host, port=port, username=user, password=password, timeout=10)
        else:
            raise

    try:
        if check_only:
            run_sudo(ssh, password, "uname -r && /opt/mu300/bin/mu300-update status 2>/dev/null || true")
            print("[CHECK OK] Local syntax & remote read-only check passed.")
            return

        runtime_sync_list = [
            (RUNTIME_BASE / "opt-mu300/bin/mu300-sqc-charge", "/opt/mu300/bin/mu300-sqc-charge", "755"),
            (RUNTIME_BASE / "opt-mu300/bin/mu300-power", "/opt/mu300/bin/mu300-power", "755"),
            (RUNTIME_BASE / "opt-mu300/bin/mu300-api", "/opt/mu300/bin/mu300-api", "755"),
            (RUNTIME_BASE / "opt-mu300/bin/lan-start", "/opt/mu300/bin/lan-start", "755"),
            (RUNTIME_BASE / "opt-mu300/bin/u30air-wait-uplink", "/opt/mu300/bin/u30air-wait-uplink", "755"),
            (RUNTIME_BASE / "opt-mu300/bin/mu300-update", "/opt/mu300/bin/mu300-update", "755"),
            (RUNTIME_BASE / "opt-mu300/bin/mu300-extra", "/opt/mu300/bin/mu300-extra", "755"),
            (RUNTIME_BASE / "extra-vpn/bin/mu300-vpn", "/mnt/mu300-disk/extra/vpn/bin/mu300-vpn", "755"),
            (RUNTIME_BASE / "extra-vpn/lib/vpn/mihomo.sh", "/mnt/mu300-disk/extra/vpn/lib/vpn/mihomo.sh", "755"),
        ]

        run_sudo(ssh, password, f"rm -rf {REMOTE_TMP_DIR} && mkdir -p {REMOTE_TMP_DIR}/runtime && chown -R {shlex.quote(user)}:{shlex.quote(user)} {REMOTE_TMP_DIR}")
        sftp = ssh.open_sftp()
        try:
            print(f"[*] Uploading WebUI package to {REMOTE_TMP_DIR} ...")
            upload_text_lf(sftp, ROOT_DIR / "install.sh", f"{REMOTE_TMP_DIR}/install.sh")
            upload_text_lf(sftp, ROOT_DIR / "uninstall.sh", f"{REMOTE_TMP_DIR}/uninstall.sh")
            upload_tree(sftp, LOCAL_DIR, f"{REMOTE_TMP_DIR}/u30air-webui")
            if with_runtime and RUNTIME_BASE.exists():
                print("[*] Uploading standalone runtime & patched VPN scripts ...")
                for idx, (lpath, _, _) in enumerate(runtime_sync_list):
                    if lpath.exists():
                        upload_text_lf(sftp, lpath, f"{REMOTE_TMP_DIR}/runtime/item_{idx}")
        finally:
            sftp.close()

        rt_install_cmds = ""
        if with_runtime and RUNTIME_BASE.exists():
            lines = []
            for idx, (lpath, rdest, mode) in enumerate(runtime_sync_list):
                if lpath.exists():
                    rdir = os.path.dirname(rdest)
                    lines.append(
                        f"if [ -d {shlex.quote(rdir)} ]; then cp -f {REMOTE_TMP_DIR}/runtime/item_{idx} {shlex.quote(rdest)} "
                        f"&& chown root:root {shlex.quote(rdest)} && chmod {mode} {shlex.quote(rdest)}; fi"
                    )
            lines.append("rm -rf /etc/cockpit /mnt/mu300-disk/extra/panel /usr/local/bin/mu300-panel")
            rt_install_cmds = " && ".join(lines) + " && "

        print("[*] Running installer on device ...")
        run_sudo(ssh, password, f"{rt_install_cmds}bash {REMOTE_TMP_DIR}/install.sh --all -y && rm -rf {REMOTE_TMP_DIR}")
        print(f"\n[OK] Deployment finished! Open http://{host}:8080/ in your browser.")
    finally:
        ssh.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy u30air-webui & optional standalone runtime over SFTP.")
    parser.add_argument("--host", default=None, help="Override target SSH host")
    parser.add_argument("--check", action="store_true", help="Syntax & read-only connectivity check")
    parser.add_argument("--with-runtime", action="store_true", help="Also sync standalone/device-runtime scripts")
    args = parser.parse_args()
    deploy(target_host=args.host, check_only=args.check, with_runtime=args.with_runtime)
