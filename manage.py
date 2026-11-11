#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
U30 Air (mu300-linux) PC-side Remote Operations CLI.
Supports health status inspection, remote command execution, WebUI restart, and safe reboot.
Reads connection settings from local `.env` (defaults to 192.168.78.1 over Wi-Fi / USB RNDIS).
"""

import argparse
import os
import shlex
import sys
import time
from pathlib import Path

import paramiko

BASE_DIR = Path(__file__).resolve().parent


def load_env() -> dict:
    env_file = BASE_DIR / ".env"
    config = {
        "SSH_HOST": os.environ.get("SSH_HOST", "192.168.78.1"),
        "SSH_PORT": os.environ.get("SSH_PORT", "22"),
        "SSH_USER": os.environ.get("SSH_USER", "ubuntu"),
        "SSH_KEY_PASS": os.environ.get("SSH_PASSWORD", ""),
        "NAME": "U30 Air",
    }
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                config[k.strip()] = v.strip().strip('"').strip("'")
    if "SSH_PASSWORD" in config and not config.get("SSH_KEY_PASS"):
        config["SSH_KEY_PASS"] = config["SSH_PASSWORD"]
    return config


def get_ssh_client(config: dict) -> paramiko.SSHClient:
    host = config.get("SSH_HOST", "192.168.78.1")
    port = int(config.get("SSH_PORT", 22))
    user = config.get("SSH_USER", "ubuntu")
    password = config.get("SSH_KEY_PASS", "")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, port=port, username=user, password=password, timeout=8)
    except Exception:
        if host != "192.168.78.1":
            print(f"[!] 连接 {host} 超时，自动回退尝试局域网/USB 网关 192.168.78.1 ...")
            ssh.connect("192.168.78.1", port=port, username=user, password=password, timeout=8)
        else:
            raise
    return ssh


def exec_remote(ssh: paramiko.SSHClient, cmd: str, password: str = "", use_sudo: bool = False, timeout: int = 45):
    remote_cmd = f"sudo -S -p '' -- sh -c {shlex.quote(cmd)}" if use_sudo else cmd
    stdin, stdout, stderr = ssh.exec_command(remote_cmd, timeout=timeout)
    if use_sudo:
        stdin.write((password or "") + "\n")
        stdin.flush()
    stdin.channel.shutdown_write()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    return out, err


def cmd_status(config: dict):
    print(f"[*] 正在连接设备: {config.get('NAME', 'U30 Air')} ({config.get('SSH_HOST')})...\n")
    try:
        ssh = get_ssh_client(config)
        password = config.get("SSH_KEY_PASS", "")
        commands = [
            ("主机与系统", "uname -n && . /etc/os-release && echo \"$PRETTY_NAME\"", False),
            ("内核版本与时区", "uname -r && date '+%Y-%m-%d %H:%M:%S %Z (%z)'", False),
            ("运行时间与负载", "uptime", False),
            ("内存使用情况 (RAM & Swap)", "free -h", False),
            ("存储空间使用情况", "df -h /", False),
            ("电池与电源旁路状态", "/opt/mu300/bin/mu300-power status 2>/dev/null | head -n 10 || true", False),
            ("蜂窝网络信号与频段", "/opt/mu300/bin/mu300-at AT+CESQ 2>/dev/null || echo 'mu300-at unavailable'", True),
            ("WebUI 服务状态 (8080)", "systemctl is-active u30air-webui.service 2>/dev/null || echo 'inactive'", False),
            ("Mihomo 代理服务状态", "systemctl is-active mu300-vpn.service 2>/dev/null || echo 'inactive'", False),
        ]
        for title, cmd, need_sudo in commands:
            out, _ = exec_remote(ssh, cmd, password=password, use_sudo=need_sudo)
            print(f"=== {title} ===")
            print(out.strip() or "(无输出)")
            print()
        ssh.close()
    except Exception as e:
        print(f"[-] 连接失败: {e}")


def cmd_run(config: dict, command: str, use_sudo: bool = False):
    try:
        ssh = get_ssh_client(config)
        print(f"[$] {command}")
        password = config.get("SSH_KEY_PASS", "")
        out, err = exec_remote(ssh, command, password=password, use_sudo=(use_sudo or command.lstrip().startswith("sudo ")))
        if out:
            print(out, end="")
        if err:
            clean_err = "\n".join(line for line in err.splitlines() if "[sudo]" not in line)
            if clean_err.strip():
                print(f"[STDERR]\n{clean_err}", end="")
        ssh.close()
    except Exception as e:
        print(f"[-] 执行失败: {e}")


def cmd_reboot(config: dict, assume_yes: bool = False):
    if not assume_yes:
        confirm = input(f"确定要重启设备 ({config.get('SSH_HOST')}) 吗？[y/N]: ")
        if confirm.lower() != "y":
            print("已取消重启操作。")
            return
    try:
        ssh = get_ssh_client(config)
        password = config.get("SSH_KEY_PASS", "")
        exec_remote(ssh, "reboot", password=password, use_sudo=True, timeout=10)
        time.sleep(1)
        ssh.close()
        print("[+] 重启指令已发送。")
    except Exception as e:
        print(f"[-] 发送重启指令失败: {e}")


def cmd_webui_restart(config: dict):
    print("[*] 正在重启 u30air-webui.service ...")
    try:
        ssh = get_ssh_client(config)
        password = config.get("SSH_KEY_PASS", "")
        exec_remote(ssh, "systemctl restart u30air-webui.service", password=password, use_sudo=True)
        time.sleep(1)
        out, _ = exec_remote(ssh, "systemctl status u30air-webui.service --no-pager")
        print(out)
        ssh.close()
    except Exception as e:
        print(f"[-] 重启服务失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="U30 Air 随身服务器远程运维 CLI")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    subparsers.add_parser("status", help="巡检设备负载、内存、电池旁路、5G 信号与服务状态")

    exec_p = subparsers.add_parser("exec", help="在远程设备上执行命令")
    exec_p.add_argument("cmd", nargs="+", help="执行的 shell 指令")
    exec_p.add_argument("--sudo", action="store_true", help="使用 sudo 提权执行")

    rb_p = subparsers.add_parser("reboot", help="安全重启设备")
    rb_p.add_argument("--yes", action="store_true", help="跳过交互确认")

    subparsers.add_parser("webui-restart", help="平滑重启 :8080 WebUI 服务")

    args = parser.parse_args()
    cfg = load_env()

    if args.command == "status":
        cmd_status(cfg)
    elif args.command == "exec":
        cmd_run(cfg, " ".join(args.cmd), use_sudo=args.sudo)
    elif args.command == "reboot":
        cmd_reboot(cfg, assume_yes=args.yes)
    elif args.command == "webui-restart":
        cmd_webui_restart(cfg)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
