#!/usr/bin/env python3
"""
PC-side One-Click SFTP Deployment Script for u30air-webui (mu300-linux)
Default gateway: 192.168.78.1 (Wi-Fi / USB RNDIS)
"""
import os
import sys
import shlex
import paramiko

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(ROOT_DIR, "u30air-webui")
REMOTE_TMP_DIR = "/tmp/u30air-webui-deploy"


def load_env(env_path=None):
    if env_path is None:
        env_path = os.path.join(ROOT_DIR, ".env")
    env = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


def run_sudo(ssh, password, cmd):
    q_pw = shlex.quote(password)
    full_cmd = f"printf '%s\\n' {q_pw} | sudo -S -p '' bash -lc {shlex.quote(cmd)}"
    stdin, stdout, stderr = ssh.exec_command(full_cmd, timeout=45)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    rc = stdout.channel.recv_exit_status()
    if out:
        print(out)
    if rc != 0 and err:
        print(f"[WARN] (rc={rc}) {err}")
    return rc, out, err


def sftp_mkdir_p(sftp, remote_dir):
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


def upload_text_lf(sftp, local_path, remote_path):
    with open(local_path, "rb") as f:
        data = f.read().replace(b"\r\n", b"\n")
    with sftp.open(remote_path, "wb") as rf:
        rf.write(data)


def upload_tree(sftp, local_dir, remote_dir):
    sftp_mkdir_p(sftp, remote_dir)
    for root, dirs, files in os.walk(local_dir):
        rel = os.path.relpath(root, local_dir)
        target_dir = remote_dir if rel == "." else f"{remote_dir}/{rel.replace(os.sep, '/')}"
        sftp_mkdir_p(sftp, target_dir)
        for fname in files:
            l_path = os.path.join(root, fname)
            r_path = f"{target_dir}/{fname}"
            print(f"  -> Uploading {r_path}")
            if fname.endswith((".py", ".sh", ".service", ".conf", ".html", ".js")) or "bin" in root:
                upload_text_lf(sftp, l_path, r_path)
            else:
                sftp.put(l_path, r_path)


def deploy():
    env = load_env()
    host = os.environ.get("SSH_HOST") or env.get("SSH_HOST", "192.168.78.1")
    port = int(os.environ.get("SSH_PORT") or env.get("SSH_PORT", "22"))
    user = os.environ.get("SSH_USER") or env.get("SSH_USER", "ubuntu")
    password = os.environ.get("SSH_PASSWORD") or env.get("SSH_PASSWORD", "")

    if not password:
        print("[!] Missing SSH_PASSWORD. Please copy .env.example to .env and fill in SSH_PASSWORD.")
        sys.exit(1)

    print(f"[*] Connecting to {user}@{host}:{port} ...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, port=port, username=user, password=password, timeout=10)

    try:
        sftp = ssh.open_sftp()
        print(f"[*] Uploading package to {REMOTE_TMP_DIR} ...")
        sftp_mkdir_p(sftp, REMOTE_TMP_DIR)
        upload_text_lf(sftp, os.path.join(ROOT_DIR, "install.sh"), f"{REMOTE_TMP_DIR}/install.sh")
        upload_text_lf(sftp, os.path.join(ROOT_DIR, "uninstall.sh"), f"{REMOTE_TMP_DIR}/uninstall.sh")
        upload_tree(sftp, LOCAL_DIR, f"{REMOTE_TMP_DIR}/u30air-webui")
        sftp.close()

        print("[*] Running installer on device ...")
        run_sudo(ssh, password, f"bash {REMOTE_TMP_DIR}/install.sh --all -y && rm -rf {REMOTE_TMP_DIR}")
        print(f"\n[✓] Deployment finished! Open http://{host}:8080/ in your browser.")
    finally:
        ssh.close()


if __name__ == "__main__":
    deploy()
