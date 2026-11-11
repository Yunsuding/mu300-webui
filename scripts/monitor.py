#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Observe SSH (22) and WebUI (8080) TCP reachability from a PC; read-only."""

import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import manage  # noqa: E402


def check_tcp_port(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    cfg = manage.load_env()
    host = cfg.get("SSH_HOST", "192.168.78.1")
    port = int(cfg.get("SSH_PORT", 22))
    previous = None
    print(f"[*] Monitoring {host} (SSH:{port}, WebUI:8080) ... Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            current = (check_tcp_port(host, port), check_tcp_port(host, 8080))
            if current != previous:
                print(
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    "SSH:",
                    current[0],
                    "WebUI:",
                    current[1],
                    flush=True,
                )
                previous = current
            time.sleep(3)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
