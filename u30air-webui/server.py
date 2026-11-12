#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
U30 Air 独立极轻量 Web 控制台后端服务 (Ultra-lightweight WebUI Daemon with Auth)
- 面向 mu300-linux 开源固件社区的极轻量替代面板 (常驻内存 ~15MB，替代 110MB+ Cockpit)
- 支持 Linux 系统账号密码原生认证 (PAM / shadow: ubuntu)
- 支持 Session Cookie 与持久化会话（记住登录）
- 端口监听 8080 (支持局域网 192.168.78.1:8080 及可选 Tailscale 组网访问)
- 支持单订阅/多订阅自适应代理分流、局域网静态 DHCP 绑定、80% 硬件限充与日志防撑爆守护
"""

import os
import sys
import json
import time
import gzip
import hashlib
import tempfile
import secrets
import subprocess
import mimetypes
import re
import socket
import ssl
import struct
import ipaddress
import threading
import http.client
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from http.cookies import SimpleCookie

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import spwd
    import crypt
except ImportError:
    spwd = None
    crypt = None

PORT = 8080
API_BIN = "/usr/local/bin/mu300-api"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
SESSIONS_FILE = os.path.join(BASE_DIR, ".sessions.json")

# 会话存储 (Token -> {"user": user, "expire": timestamp})
SESSIONS = {}
STATE_LOCK = threading.RLock()
AT_LOCK = threading.Lock()
STATIC_ASSET_CACHE = {}
NET_PREV = {"time": time.time(), "rx": 0, "tx": 0, "rx_rate": 0, "tx_rate": 0, "iface": "sipa_eth0"}

def get_active_sim_slot_idx():
    """零子进程读取当前生效的物理 SIM 卡槽索引 (0=SIM1/sipa_eth0, 1=SIM2/sipa_eth8)"""
    for p in ("/run/mu300/sim-slot", "/etc/mu300-sim-slot"):
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    v = f.read().strip()
                if v in ("0", "1"):
                    return int(v)
            except OSError:
                pass
    return 0

def get_slot_at_dir(slot_idx=None):
    """返回指定卡槽对应的 AT 守护管道目录"""
    idx = get_active_sim_slot_idx() if slot_idx is None else int(slot_idx)
    if idx == 1 and (os.path.exists("/run/mu300-at4/cmd") or os.path.exists("/run/mu300-radio-freeze")):
        return "/run/mu300-at4"
    return "/run/mu300-at"

def get_active_cellular_iface():
    """根据当前活动卡槽返回对应的数据承载网卡 (SIM1 -> sipa_eth0, SIM2 -> sipa_eth8)"""
    return "sipa_eth8" if get_active_sim_slot_idx() == 1 else "sipa_eth0"

def _atomic_write_text(filepath, content, mode=None):
    """原子写入配置文件，防止断电或多线程并发导致 JSON/YAML 半写损坏"""
    parent = os.path.dirname(os.path.abspath(filepath)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".u30tmp-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
        if mode is not None:
            try:
                os.chmod(tmp_path, mode)
            except OSError:
                pass
        os.replace(tmp_path, filepath)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

def get_realtime_traffic():
    """实时读取 /proc/net/dev 获取当前活动 SIM 承载网卡微秒级精确上下行网速"""
    global NET_PREV
    with STATE_LOCK:
        now = time.time()
        slot_idx = get_active_sim_slot_idx()
        active_iface = "sipa_eth8" if slot_idx == 1 else "sipa_eth0"
        if NET_PREV.get("iface") != active_iface:
            NET_PREV = {"time": now, "rx": 0, "tx": 0, "rx_rate": 0.0, "tx_rate": 0.0, "iface": active_iface}
        dt = max(0.2, now - NET_PREV["time"])
        try:
            with open("/proc/net/dev", "r") as f:
                lines = f.readlines()
            dev_map = {}
            for line in lines[2:]:
                parts = line.split(":")
                if len(parts) == 2:
                    dev_map[parts[0].strip()] = parts[1].split()
            chosen = active_iface if active_iface in dev_map else ("sipa_eth0" if "sipa_eth0" in dev_map else ("sipa_eth8" if "sipa_eth8" in dev_map else active_iface))
            fields = dev_map.get(chosen)
            if fields and len(fields) >= 9:
                rx = int(fields[0])
                tx = int(fields[8])
                if NET_PREV.get("iface") != chosen:
                    NET_PREV["iface"] = chosen
                    NET_PREV["rx"] = 0
                    NET_PREV["tx"] = 0
                if NET_PREV["rx"] > 0 and rx >= NET_PREV["rx"] and tx >= NET_PREV["tx"]:
                    rx_rate = max(0.0, (rx - NET_PREV["rx"]) / dt)
                    tx_rate = max(0.0, (tx - NET_PREV["tx"]) / dt)
                    NET_PREV["rx_rate"] = rx_rate
                    NET_PREV["tx_rate"] = tx_rate
                else:
                    NET_PREV["rx_rate"] = 0.0
                    NET_PREV["tx_rate"] = 0.0
                NET_PREV["rx"] = rx
                NET_PREV["tx"] = tx
                NET_PREV["time"] = now
        except Exception:
            pass
        return {
            "ok": 1,
            "iface": NET_PREV.get("iface", active_iface),
            "sim_slot": slot_idx + 1,
            "rx_rate": NET_PREV["rx_rate"],
            "tx_rate": NET_PREV["tx_rate"],
            "rx_bytes": NET_PREV["rx"],
            "tx_bytes": NET_PREV["tx"]
        }

CELLULAR_CACHE = {
    "time": 0,
    "static_time": 0,
    "data": {
        "qci": 8,
        "dl_rate_str": "512 Mbps",
        "ul_rate_str": "60 Mbps",
        "dl_kbps": 512000,
        "ul_kbps": 60000,
        "rsrp": -92,
        "rsrq": -9.5,
        "sinr": 8.0,
        "band": "n41",
        "rat": "5G SA",
        "operator": "46000",
        "operator_name": "中国移动",
        "connected": True
    }
}

def get_cellular_qos(force_static=False):
    """实时读取蜂窝射频与 QoS 签约状态 (信号 12s 缓存，运营商/签约速率 300s 长缓存，AT 串口互斥)"""
    global CELLULAR_CACHE
    now = time.time()
    slot_idx = get_active_sim_slot_idx()
    slot_changed = (CELLULAR_CACHE.get("slot") != slot_idx)
    if not force_static and not slot_changed and now - CELLULAR_CACHE["time"] < 12 and CELLULAR_CACHE["data"]:
        return CELLULAR_CACHE["data"]

    if os.path.exists("/run/mu300/sim-switching"):
        return dict(CELLULAR_CACHE["data"])

    if not AT_LOCK.acquire(timeout=1.5):
        return dict(CELLULAR_CACHE["data"])

    try:
        now = time.time()
        slot_idx = get_active_sim_slot_idx()
        slot_changed = (CELLULAR_CACHE.get("slot") != slot_idx)
        if not force_static and not slot_changed and now - CELLULAR_CACHE["time"] < 12 and CELLULAR_CACHE["data"]:
            return CELLULAR_CACHE["data"]

        data = dict(CELLULAR_CACHE["data"])
        data["sim_slot"] = slot_idx + 1
        data["iface"] = "sipa_eth8" if slot_idx == 1 else "sipa_eth0"
        at_bin = "/opt/mu300/bin/mu300-at"
        at_env = dict(os.environ, MU300_AT_DIR=get_slot_at_dir(slot_idx), MU300_SIM_SLOT=str(slot_idx))
        need_static = force_static or slot_changed or (now - CELLULAR_CACHE.get("static_time", 0) >= 300)
        if os.path.exists(at_bin):
            # 1. 信号质量 (CESQ: ss_rsrq, ss_rsrp, ss_sinr) - 每次缓存到期刷新
            proc = subprocess.run([at_bin, "AT+CESQ"], capture_output=True, text=True, timeout=3, env=at_env)
            out = proc.stdout
            m = re.search(r'\+CESQ:\s*(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)(?:,(\d+),(\d+),(\d+))?', out)
            if m:
                ss_rsrq, ss_rsrp, ss_sinr = m.group(7), m.group(8), m.group(9)
                if ss_rsrp and ss_rsrp != '255':
                    data["rsrp"] = -156 + int(ss_rsrp)
                elif m.group(6) and m.group(6) != '255':
                    data["rsrp"] = int(m.group(6)) - 140

                if ss_rsrq and ss_rsrq != '255':
                    data["rsrq"] = round(-43.0 + int(ss_rsrq) * 0.5, 1)
                elif m.group(5) and m.group(5) != '255':
                    data["rsrq"] = round(-19.5 + int(m.group(5)) * 0.5, 1)

                if ss_sinr and ss_sinr != '255':
                    data["sinr"] = round(-23.0 + int(ss_sinr) * 0.5, 1)

            if need_static:
                # 2. 签约 QoS 状态 (C5GQOSRDP / CGEQOSRDP: QCI 与 AMBR 签约速率，5 分钟刷新一次)
                proc = subprocess.run([at_bin, "AT+C5GQOSRDP=1"], capture_output=True, text=True, timeout=3, env=at_env)
                out = proc.stdout
                if "+C5GQOSRDP:" not in out:
                    proc = subprocess.run([at_bin, "AT+CGEQOSRDP=1"], capture_output=True, text=True, timeout=3, env=at_env)
                    out = proc.stdout

                m = re.search(r'\+(?:C5GQOSRDP|CGEQOSRDP):\s*(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)', out)
                if m:
                    qci = int(m.group(2))
                    dl = int(m.group(7))
                    ul = int(m.group(8))
                    data["qci"] = qci
                    data["dl_kbps"] = dl
                    data["ul_kbps"] = ul

                    def fmt(kbps):
                        if kbps >= 1000000:
                            val = kbps / 1000000.0
                            return f"{val:.1f} Gbps" if val % 1 != 0 else f"{int(val)} Gbps"
                        elif kbps >= 1000:
                            return f"{int(round(kbps / 1000.0))} Mbps"
                        else:
                            return f"{kbps} kbps"

                    data["dl_rate_str"] = fmt(dl)
                    data["ul_rate_str"] = fmt(ul)

                # 3. 运营商与制式 (COPS，5 分钟刷新一次)
                proc = subprocess.run([at_bin, "AT+COPS?"], capture_output=True, text=True, timeout=3, env=at_env)
                out = proc.stdout
                m = re.search(r'\+COPS:\s*(\d+),(\d+),"(\d+)"(?:,(\d+))?', out)
                if m:
                    oper = m.group(3)
                    act = m.group(4) or "11"
                    data["operator"] = oper
                    oper_map = {
                        "46000": "中国移动", "46002": "中国移动", "46007": "中国移动", "46008": "中国移动",
                        "46001": "中国联通", "46006": "中国联通", "46009": "中国联通",
                        "46011": "中国电信", "46003": "中国电信", "46005": "中国电信",
                        "46015": "中国广电"
                    }
                    data["operator_name"] = oper_map.get(oper, f"运营商 ({oper})")
                    rat_map = {"7": "4G LTE", "13": "5G NSA", "11": "5G SA", "12": "5G SA"}
                    data["rat"] = rat_map.get(act, "5G")
                CELLULAR_CACHE["static_time"] = now

            oper = data.get("operator", "46000")
            lock_file = "/var/run/unisoc-modem/lock.json"
            locked_nr = None
            locked_lte = None
            serving_nr_band = None
            serving_lte_band = None
            if os.path.exists(lock_file):
                try:
                    with open(lock_file, "r") as lf:
                        lj = json.load(lf)
                        nr_obj = lj.get("nr", {}) if isinstance(lj.get("nr"), dict) else {}
                        lte_obj = lj.get("lte", {}) if isinstance(lj.get("lte"), dict) else {}
                        srv_obj = lj.get("serving", {}) if isinstance(lj.get("serving"), dict) else {}
                        locked_nr = str(nr_obj.get("locked", "") or "")
                        locked_lte = str(lte_obj.get("locked", "") or "")
                        serving_nr_band = str(nr_obj.get("band", "") or srv_obj.get("nr_band", "") or srv_obj.get("band", "") or "")
                        serving_lte_band = str(lte_obj.get("band", "") or srv_obj.get("lte_band", "") or "")
                except Exception:
                    pass

            if "5G" in data.get("rat", "5G"):
                if serving_nr_band:
                    b_clean = serving_nr_band.lstrip("nN")
                    data["band"] = f"n{b_clean}"
                elif locked_nr and "," not in locked_nr:
                    data["band"] = f"n{locked_nr.lstrip('nN')}"
                elif oper in ["46001", "46006", "46009", "46011", "46003", "46005"]:
                    data["band"] = "n78"
                elif oper == "46015":
                    data["band"] = "n28"
                else:
                    data["band"] = "n41"
            else:
                if serving_lte_band:
                    b_clean = serving_lte_band.lstrip("bB")
                    data["band"] = f"B{b_clean}"
                elif locked_lte and "," not in locked_lte:
                    data["band"] = f"B{locked_lte.lstrip('bB')}"
                elif oper in ["46001", "46006"]:
                    data["band"] = "B3"
                elif oper in ["46011", "46003"]:
                    data["band"] = "B1"
                else:
                    data["band"] = "B39"

        CELLULAR_CACHE["time"] = now
        CELLULAR_CACHE["slot"] = slot_idx
        CELLULAR_CACHE["data"] = data
    except Exception as e:
        print(f"[Cellular QoS Error] {e}")
    finally:
        AT_LOCK.release()

    return CELLULAR_CACHE["data"]

CELLULAR_IP_CACHE = {
    "time": 0,
    "slot": 0,
    "data": {
        "ipv4": "",
        "public_ipv4": "",
        "ipv6": "",
        "iface": "sipa_eth0"
    }
}

PUBLIC_IP_CACHE = {
    "time": 0,
    "ip": "",
    "updating": False
}

def fetch_public_ipv4():
    """获取移动蜂窝网络真实公网出口 IPv4 (优先绑定当前活动 sipa_eth* 网卡，防止被本地代理干扰)"""
    primary_iface = get_active_cellular_iface()
    candidate_ifaces = [primary_iface] + [x for x in ("sipa_eth0", "sipa_eth8") if x != primary_iface]
    for c_iface in candidate_ifaces:
        if not os.path.exists(f"/sys/class/net/{c_iface}"):
            continue
        try:
            proc = subprocess.run(
                ["curl", "--interface", c_iface, "-4", "-s", "--connect-timeout", "2", "http://myip.ipip.net"],
                capture_output=True, text=True, timeout=2.5
            )
            m = re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', proc.stdout)
            if m:
                ip = m.group(0)
                if not ip.startswith(("10.", "192.168.", "172.16.", "127.", "100.")):
                    return ip
        except Exception:
            pass

    # 方式 2: 国内外高可用接口直连
    endpoints = [
        "http://myip.ipip.net",
        "http://cip.cc",
        "https://api.ipify.org"
    ]
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.88.1"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
                m = re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text)
                if m:
                    candidate = m.group(0)
                    if not candidate.startswith(("10.", "192.168.", "172.16.", "127.", "100.")):
                        return candidate
        except Exception:
            continue
    return ""

def update_public_ip_async():
    """异步后台刷新公网 IPv4，避免阻塞前端请求"""
    global PUBLIC_IP_CACHE
    now = time.time()
    if (now - PUBLIC_IP_CACHE["time"] < 60 and PUBLIC_IP_CACHE["ip"]) or PUBLIC_IP_CACHE["updating"]:
        return
    PUBLIC_IP_CACHE["updating"] = True

    def _worker():
        try:
            ip = fetch_public_ipv4()
            if ip:
                PUBLIC_IP_CACHE["ip"] = ip
                PUBLIC_IP_CACHE["time"] = time.time()
        except Exception:
            pass
        finally:
            PUBLIC_IP_CACHE["updating"] = False

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

def get_cellular_ip_info():
    """获取移动蜂窝网络的内网 IPv4、公网出口 IPv4 与 IPv6 地址 (毫秒级响应，支持 SIM1 sipa_eth0 / SIM2 sipa_eth8)"""
    global CELLULAR_IP_CACHE
    now = time.time()
    slot_idx = get_active_sim_slot_idx()
    primary_iface = "sipa_eth8" if slot_idx == 1 else "sipa_eth0"
    if CELLULAR_IP_CACHE.get("slot") == slot_idx and now - CELLULAR_IP_CACHE["time"] < 3 and CELLULAR_IP_CACHE["data"].get("ipv4"):
        update_public_ip_async()
        ret = dict(CELLULAR_IP_CACHE["data"])
        if PUBLIC_IP_CACHE["ip"]:
            ret["public_ipv4"] = PUBLIC_IP_CACHE["ip"]
        return ret

    info = {
        "ipv4": "",
        "public_ipv4": PUBLIC_IP_CACHE["ip"],
        "ipv6": "",
        "iface": primary_iface,
        "sim_slot": slot_idx + 1,
    }

    # 1. 获取蜂窝接口内网 IPv4 (优先通过 socket ioctl 获取活动 sipa_eth* 地址，零子进程)
    target_ifaces = [primary_iface] + [x for x in ("sipa_eth0", "sipa_eth8", "sipa_eth1") if x != primary_iface]
    for iface in target_ifaces:
        if fcntl:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                ip = socket.inet_ntoa(fcntl.ioctl(
                    s.fileno(),
                    0x8915,  # SIOCGIFADDR
                    struct.pack('256s', iface[:15].encode('utf-8'))
                )[20:24])
                s.close()
                if ip and not ip.startswith("127."):
                    info["ipv4"] = ip
                    info["iface"] = iface
                    break
            except Exception:
                pass

    # 2. 如果 ioctl 未获取到，解析 ip -4 addr show <iface>
    if not info["ipv4"]:
        for iface in target_ifaces[:2]:
            try:
                proc = subprocess.run(["ip", "-4", "addr", "show", iface], capture_output=True, text=True, timeout=1.5)
                m = re.search(r'inet\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)', proc.stdout)
                if m:
                    info["ipv4"] = m.group(1)
                    info["iface"] = iface
                    break
            except Exception:
                pass

    # 3. 触发公网 IPv4 探测与获取
    update_public_ip_async()
    if not info["public_ipv4"]:
        # 首次调用如果为空，做一次极速探测
        try:
            fast_ip = fetch_public_ipv4()
            if fast_ip:
                PUBLIC_IP_CACHE["ip"] = fast_ip
                PUBLIC_IP_CACHE["time"] = now
                info["public_ipv4"] = fast_ip
        except Exception:
            pass

    # 4. 获取移动蜂窝网络 IPv6 地址 (优先 sipa_eth*，其次为蜂窝前缀桥接的 br-lan)
    # 在 MU300 Linux 中，基带分配的公共 IPv6 /64 地址由 sipa_eth0 获取并委派给 br-lan (供随身Wi-Fi客户端)
    candidates_sipa = []
    candidates_lan = []
    if os.path.exists("/proc/net/if_inet6"):
        try:
            with open("/proc/net/if_inet6", "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 6:
                        hex_ip, _, _, scope, _, dev = parts[:6]
                        if scope == "00":  # 全局单播地址 (Global Scope)
                            try:
                                addr_obj = ipaddress.IPv6Address(int(hex_ip, 16))
                                ip_str = addr_obj.compressed
                                # 排除私有 ULA (fd/fc, 如 tailscale / clash) 及 link-local
                                if addr_obj.is_global and not ip_str.lower().startswith(("fe8", "fd", "fc")):
                                    if dev.startswith("sipa_"):
                                        candidates_sipa.append(ip_str)
                                    elif dev in ["br-lan", "wlan0", "usb0"]:
                                        candidates_lan.append(ip_str)
                            except Exception:
                                pass
        except Exception:
            pass

    if candidates_sipa:
        info["ipv6"] = candidates_sipa[0]
    elif candidates_lan:
        info["ipv6"] = candidates_lan[0]

    # 5. 兜底获取 IPv6: 执行 ip -6 addr show scope global
    if not info["ipv6"]:
        try:
            proc = subprocess.run(["ip", "-6", "addr", "show", "scope", "global"], capture_output=True, text=True, timeout=1.5)
            for line in proc.stdout.splitlines():
                line = line.strip()
                if line.startswith("inet6 "):
                    addr_str = line.split()[1].split("/")[0]
                    if not addr_str.lower().startswith(("fd", "fc", "fe8")):
                        info["ipv6"] = addr_str
                        break
        except Exception:
            pass

    CELLULAR_IP_CACHE["time"] = now
    CELLULAR_IP_CACHE["slot"] = slot_idx
    CELLULAR_IP_CACHE["data"] = info
    return info

BATTERY_INFO_CACHE = {"time": 0, "data": None}

def get_battery_info(force=False):
    """Read policy and hardware independently; an attached cable is not charging."""
    try:
        import time as _time
        now = _time.time()
        cache = globals().get("BATTERY_INFO_CACHE")
        if not force and cache and cache.get("data") is not None and (now - cache.get("time", 0) < 8.0):
            return cache["data"]
    except Exception:
        now = 0
        cache = None

    def read(path):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return None

    def number(value, scale=1):
        try:
            return int(value) if scale == 1 else round(int(value) / scale, 2)
        except (TypeError, ValueError):
            return None

    def int_div(value, div=1000):
        try:
            return int(int(value) / div)
        except (TypeError, ValueError):
            return None

    supply = "/sys/class/power_supply/"
    inputs = [read(supply + p + "/online") for p in ("ac", "usb", "bq2560x_charger")]
    plugged = True if "1" in inputs else (False if "0" in inputs else None)
    cap = number(read(supply + "battery/capacity"))
    volts = number(read(supply + "battery/voltage_now"), 1000000)
    raw = read(supply + "bq2560x_charger/status") or "Unknown"
    sysfs_ma = int_div(read(supply + "battery/current_now"), 1000)
    sysfs_temp = number(read(supply + "battery/temp"))
    full_mah = int_div(read(supply + "battery/charge_full"), 1000)
    design_mah = int_div(read(supply + "battery/charge_full_design"), 1000)
    soh_pct = round(min(105.0, (full_mah / design_mah) * 100.0), 1) if (full_mah and design_mah and design_mah > 0) else None

    policy = {}
    raw_pconf = read("/etc/mu300/power.conf")
    if raw_pconf is not None:
        parsed_conf = {"CHARGE_TO": 100, "SAVER_BELOW": 20, "RESUME_AT": 0, "FULL_ONCE": 0, "CHARGE_PAUSE": 0}
        for line in raw_pconf.splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                parsed_conf[k.strip()] = v.strip()
        policy = {
            "ok": 1,
            "supply": "plugged" if plugged else ("battery" if plugged is False else None),
            "charge_off": read("/run/mu300/power/charge-off") or None,
            "conf": parsed_conf,
            "battery": {"capacity": cap, "ma": sysfs_ma, "temp": sysfs_temp}
        }
    else:
        try:
            result = subprocess.run([API_BIN, "power.get"], input="{}", text=True,
                                    capture_output=True, timeout=3)
            if result.returncode == 0:
                candidate = json.loads(result.stdout)
                if isinstance(candidate, dict) and candidate.get("ok"):
                    policy = candidate
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass

    conf = policy.get("conf") if isinstance(policy.get("conf"), dict) else {}
    battery = policy.get("battery") if isinstance(policy.get("battery"), dict) else {}
    if plugged is None and policy.get("supply") in ("plugged", "battery"):
        plugged = policy["supply"] == "plugged"
    if cap is None:
        cap = number(battery.get("capacity"))
    if volts is None:
        volts = number(battery.get("mv"), 1000)

    enabled = None
    verified = False
    input_limit_ma = None
    charge_current_limit_ma = None
    regs = read(supply + "bq2560x_charger/device/registers") or ""
    primary = regs.split("SLAVE Reg:")[0]
    match = re.search(r"^Reg\[01\] = 0x([0-9a-fA-F]{2})$", primary, re.M)
    m_r00 = re.search(r"^Reg\[00\] = 0x([0-9a-fA-F]{2})$", primary, re.M)
    m_r02 = re.search(r"^Reg\[02\] = 0x([0-9a-fA-F]{2})$", primary, re.M)
    if m_r00:
        r00_val = int(m_r00[1], 16)
        input_limit_ma = 0 if (r00_val & 0x80) else (100 + (r00_val & 0x1f) * 100)
    if m_r02:
        charge_current_limit_ma = (int(m_r02[1], 16) & 0x3f) * 60

    hal_val = None
    if raw_pconf is not None:
        try:
            import os as _os, struct as _struct
            if _os.uname().release == "5.4.254-gb50db5b6224c":
                fd = _os.open("/dev/sqc_chg_node", _os.O_RDONLY | _os.O_CLOEXEC)
                try:
                    _os.lseek(fd, 273, _os.SEEK_SET)
                    buf = _os.read(fd, 4)
                    if len(buf) == 4:
                        v = _struct.unpack("<I", buf)[0]
                        if v in (0, 1):
                            hal_val = (v == 1)
                finally:
                    _os.close(fd)
        except Exception:
            hal_val = None

    if hal_val is not None and match:
        if hal_val == bool(int(match[1], 16) & 0x10):
            enabled, verified = hal_val, True
    else:
        try:
            result = subprocess.run(["/opt/mu300/bin/mu300-sqc-charge", "get"],
                                    text=True, capture_output=True, timeout=2)
            if result.returncode == 0 and result.stdout.strip() in ("0", "1") and match:
                hal = result.stdout.strip() == "1"
                if hal == bool(int(match[1], 16) & 0x10):
                    enabled, verified = hal, True
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass

    reason = policy.get("charge_off")
    full_once = str(conf.get("FULL_ONCE", 0)) == "1"
    manual_pause = str(conf.get("CHARGE_PAUSE", 0)) == "1"
    cur_ma = sysfs_ma if sysfs_ma is not None else number(battery.get("ma"))
    bat_temp_c = number(sysfs_temp if sysfs_temp is not None else battery.get("temp"), 10)
    power_mw = round(volts * cur_ma) if (volts is not None and cur_ma is not None) else None

    charging = False
    state, text = "unknown", "充电状态未知"
    if plugged is False:
        state, text = "discharging", "电池供电（未充电）"
    elif plugged is True and verified and not enabled:
        state = "paused"
        text = {"limit": "限充暂停", "manual": "手动暂停", "temp": "温度保护暂停"}.get(reason, "充电已关闭")
    elif plugged is True and verified and enabled:
        if raw.lower() == "full":
            state, text = "full", "已充满（充电开关开启）"
        elif raw.lower() == "charging" or (cur_ma or 0) > 20:
            charging = True
            state, text = "charging", "充电中（充满一次）" if full_once else "充电中"
        else:
            state, text = "enabled", "充电已允许（待充电）"
    elif plugged is True:
        text = "电源已连接，充电开关未核实"
    limit = number(conf.get("CHARGE_TO"))
    resume = number(conf.get("RESUME_AT"))
    saver_below = number(conf.get("SAVER_BELOW"))
    if limit is not None and (resume is None or resume <= 0 or resume >= limit):
        resume = max(0, limit - 5)
    detail = "硬件开关已核实" if verified else "硬件读取失败或当前驱动未适配"
    if verified and enabled and reason:
        detail += "；策略与硬件状态不一致，等待下一轮检查"
    if limit == 0:
        detail += "；禁止充电（低于 5% 的恢复保护除外）"
    elif limit == 100 and not conf.get("RESUME_AT"):
        detail += "；允许充至满电"
    elif limit is not None:
        detail += "；上限 %g%%，低于 %g%% 恢复" % (limit, resume if resume is not None else max(0, limit - 5))
    if state == "paused" and input_limit_ma and input_limit_ma >= 1500 and cur_ma is not None and abs(cur_ma) <= 5:
        detail += "；零循环旁路供电生效 (0mA)"
    elif charging and charge_current_limit_ma and charge_current_limit_ma <= 1020 and not full_once:
        detail += "；%dmA 低温柔充保护中" % charge_current_limit_ma
    if full_once:
        detail += "；充满一次已开启（仍受温度保护）"
    res = {"capacity": cap, "plugged": plugged, "charging": charging,
            "state": state, "status_text": text, "voltage_v": volts,
            "raw_charger": raw, "switch_enabled": enabled, "control_verified": verified,
            "pause_reason": reason, "charge_limit": limit, "resume_below": resume,
            "saver_below": saver_below, "manual_pause": manual_pause,
            "full_once": full_once, "current_ma": cur_ma, "power_mw": power_mw,
            "soh_pct": soh_pct, "charge_full_mah": full_mah, "charge_design_mah": design_mah,
            "input_limit_ma": input_limit_ma, "charge_current_limit_ma": charge_current_limit_ma,
            "temperature_c": bat_temp_c, "detail_text": detail}
    if "get_thermal_details" in globals():
        thm = get_thermal_details(bat_temp_override=res["temperature_c"])
        res["thermal"] = thm
        res["temp_c"] = res["temperature_c"] if res["temperature_c"] is not None else thm.get("battery", 35.0)
    if cache is not None:
        cache["time"] = now
        cache["data"] = res
    return res

def get_thermal_details(bat_temp_override=None):
    """读取 U30 Air (Unisoc T760) 全机多路硬件温度传感器 (纯 sysfs 内存读取，<1ms)"""
    raw_zones = {}
    thermal_base = "/sys/class/thermal"
    if os.path.exists(thermal_base):
        try:
            for entry in os.listdir(thermal_base):
                if not entry.startswith("thermal_zone"):
                    continue
                zdir = os.path.join(thermal_base, entry)
                t_file = os.path.join(zdir, "type")
                v_file = os.path.join(zdir, "temp")
                if os.path.exists(t_file) and os.path.exists(v_file):
                    try:
                        with open(t_file, "r") as f:
                            ztype = f.read().strip()
                        with open(v_file, "r") as f:
                            val_str = f.read().strip()
                        if val_str:
                            val = int(val_str)
                            if val > 1000:
                                deg = round(val / 1000.0, 1)
                                if 5.0 <= deg <= 125.0:
                                    raw_zones[ztype] = deg
                    except Exception:
                        pass
        except Exception:
            pass

    if bat_temp_override is not None:
        bat_c = bat_temp_override
    else:
        bat_c = 35.0
        for bp in ["/sys/class/power_supply/battery/temp", "/sys/class/power_supply/sc27xx-fgu/temp"]:
            if os.path.exists(bp):
                try:
                    with open(bp, "r") as f:
                        rv = int(f.read().strip())
                        if rv > 0:
                            bat_c = round(rv / 10.0, 1) if rv > 150 else float(rv)
                            break
                except Exception:
                    pass

    def _pick_max(keys, fallback=None):
        vals = [raw_zones[k] for k in keys if k in raw_zones]
        return round(max(vals), 1) if vals else fallback

    soc_c = _pick_max(["soc-thmzone", "big7mid4-thmzone"], 45.0)
    cpu_big_c = _pick_max(["big7-thmzone", "mid6-thmzone", "mid5-thmzone", "mid4-thmzone", "apcpu1-thmzone"], soc_c)
    cpu_lit_c = _pick_max(["lit0-thmzone", "lit1-thmzone", "lit2-thmzone", "lit3-thmzone", "apcpu0-thmzone"], soc_c)
    modem_c = _pick_max(["nr-thmzone", "nr0-thmzone", "nr1-thmzone", "lte-thmzone"], soc_c)
    pa_c = _pick_max(["pa-thmzone"], modem_c)
    chg_c = _pick_max(["chg-thmzone"], soc_c)
    board_c = _pick_max(["board-thmzone"], soc_c)
    gpu_c = _pick_max(["gpu-thmzone", "mm-thmzone"], cpu_lit_c)

    all_main = [x for x in [soc_c, cpu_big_c, cpu_lit_c, modem_c, pa_c, chg_c, board_c] if x is not None]
    max_c = round(max(all_main), 1) if all_main else 45.0

    if max_c < 43.0:
        level, level_text = "cool", "清凉"
    elif max_c < 55.0:
        level, level_text = "normal", "良好"
    elif max_c < 68.0:
        level, level_text = "warm", "偏热"
    else:
        level, level_text = "hot", "高温预警"

    sensors = [
        {"id": "soc", "name": "SoC 核心总温", "sub": "Unisoc T760 Die", "temp": soc_c},
        {"id": "cpu_big", "name": "CPU 大核集群", "sub": "4×Cortex-A76 (代理引擎)", "temp": cpu_big_c},
        {"id": "cpu_lit", "name": "CPU 小核集群", "sub": "4×Cortex-A55 (基带中断)", "temp": cpu_lit_c},
        {"id": "modem", "name": "5G NR 基带", "sub": "NR / LTE 调制解调器", "temp": modem_c},
        {"id": "pa", "name": "5G 射频功放", "sub": "RF Power Amplifier", "temp": pa_c},
        {"id": "battery", "name": "内置锂电池", "sub": "4500mAh 聚合物电芯", "temp": bat_c},
        {"id": "charger", "name": "充电管理芯片", "sub": "TI BQ2560X / SC2730", "temp": chg_c},
        {"id": "board", "name": "主板环境温度", "sub": "PCB Board Sensor", "temp": board_c},
    ]

    return {
        "soc": soc_c,
        "cpu_big": cpu_big_c,
        "cpu_lit": cpu_lit_c,
        "modem": modem_c,
        "pa": pa_c,
        "battery": bat_c,
        "charger": chg_c,
        "board": board_c,
        "gpu": gpu_c,
        "max_temp": max_c,
        "level": level,
        "level_text": level_text,
        "sensors": sensors
    }

# =========================================================================
# 在线连接设备与终端管理 (轻量级：0后台驻留，按需实时读取)
# =========================================================================
BLOCKLIST_FILE = os.path.join(BASE_DIR, "blocklist.json")

def load_blocklist():
    if os.path.exists(BLOCKLIST_FILE):
        try:
            with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_blocklist(data):
    try:
        with open(BLOCKLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _iptables_mac_rule(action, chain, mac):
    """安全执行 iptables MAC 规则，避免 shell=True 注入风险"""
    if not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac):
        return
    if action == "add":
        chk = subprocess.run(["iptables", "-C", chain, "-m", "mac", "--mac-source", mac, "-j", "DROP"], capture_output=True)
        if chk.returncode != 0:
            subprocess.run(["iptables", "-I", chain, "-m", "mac", "--mac-source", mac, "-j", "DROP"], capture_output=True)
    elif action == "del":
        subprocess.run(["iptables", "-D", chain, "-m", "mac", "--mac-source", mac, "-j", "DROP"], capture_output=True)

def init_blocklist_iptables():
    """启动时恢复黑名单设备的 iptables 封禁规则"""
    items = load_blocklist()
    for item in items:
        mac = item.get("mac", "").lower().strip()
        if mac:
            _iptables_mac_rule("add", "FORWARD", mac)
            _iptables_mac_rule("add", "INPUT", mac)

init_blocklist_iptables()

STATIONS_CACHE = {"time": 0, "data": []}
LAST_KNOWN_MAC_IP = {}
MIHOMO_CONNS_CACHE = {"time": 0, "data": {}}
CLIENT_POLICIES_FILE = "/etc/mu300/client_policies.json"
DHCP_HOSTS_FILE = "/etc/mu300/dhcp-hosts.conf"
MIHOMO_SOCK_PATH = "/run/mu300-vpn/mihomo-control.sock"

def get_mac_to_live_ips():
    """读取当前局域网各 MAC 正在使用的实时 IP (邻居表 IPv4/IPv6 + ARP + DHCP 租约 + 变更前缓存)"""
    subnet_prefix, gw_ip = get_lan_subnet()
    mac_ips = {}

    def _add(mac_str, ip_str):
        mac_str = str(mac_str or "").lower().strip()
        ip_str = str(ip_str or "").strip()
        if not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac_str) or mac_str == "00:00:00:00:00:00":
            return
        is_lan_v4 = ip_str.startswith(subnet_prefix) and ip_str != gw_ip
        is_global_v6 = ":" in ip_str and not ip_str.startswith("fe80:") and not ip_str.startswith("::")
        if not (is_lan_v4 or is_global_v6):
            return
        lst = mac_ips.setdefault(mac_str, [])
        if ip_str not in lst:
            lst.append(ip_str)

    # 1. 优先读取 ip -br neigh 获取所有活动邻居 (同时覆盖 IPv4 与 IPv6，耗时约 2ms)
    try:
        proc = subprocess.run(["ip", "-br", "neigh", "show", "dev", "br-lan"], capture_output=True, text=True, timeout=0.8)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    ip_p, mac_p = parts[0], parts[1].lower()
                    _add(mac_p, ip_p)
    except Exception:
        pass

    # 2. 读取 /proc/net/arp 中活跃的局域网邻居 IP
    if os.path.exists("/proc/net/arp"):
        try:
            with open("/proc/net/arp", "r", encoding="utf-8", errors="ignore") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 6:
                        ip, flags, mac, dev = parts[0], parts[2], parts[3].lower(), parts[5]
                        if dev == "br-lan" and flags != "0x0":
                            _add(mac, ip)
        except Exception:
            pass

    # 2. 补充内存记录的变更前活跃 IP (支持修改固定 IP 后免重连 Wi-Fi 立即生效)
    for m_k, ip_v in list(LAST_KNOWN_MAC_IP.items()):
        _add(m_k, ip_v)

    # 3. 补充 /run/mu300-lan.leases 中的租约 IP
    leases_file = "/run/mu300-lan.leases"
    if os.path.exists(leases_file):
        try:
            with open(leases_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3:
                        _add(parts[1].lower(), parts[2])
        except Exception:
            pass

    return mac_ips

def get_mihomo_client_stats(force=False):
    """按需通过 Mihomo Unix Socket 读取各局域网客户端的实时活跃连接数与命中的策略组/节点 (0 子进程开销，支持任意自定义策略组)"""
    now = time.time()
    if not force and MIHOMO_CONNS_CACHE["data"] and (now - MIHOMO_CONNS_CACHE["time"] < 2.5):
        return MIHOMO_CONNS_CACHE["data"]

    code, data = mihomo_controller_request("GET", "/connections", timeout=1.2)
    stats_by_ip = {}
    if code == 200 and isinstance(data, dict):
        conns = data.get("connections") or []
        for c in conns:
            if not isinstance(c, dict):
                continue
            meta = c.get("metadata") or {}
            src_ip = str(meta.get("sourceIP") or "").strip()
            if not src_ip:
                continue
            st = stats_by_ip.get(src_ip)
            if not st:
                st = {
                    "active_conns": 0,
                    "pure_direct_conns": 0,
                    "active_group": "",
                    "active_node": "",
                    "active_dl_bytes": 0,
                    "active_ul_bytes": 0
                }
                stats_by_ip[src_ip] = st

            st["active_conns"] += 1
            st["active_dl_bytes"] += int(c.get("download") or 0)
            st["active_ul_bytes"] += int(c.get("upload") or 0)
            chains = [str(x).strip() for x in (c.get("chains") or []) if str(x).strip()]
            if chains:
                top_grp = chains[-1]
                leaf_node = chains[0]
                if top_grp in ("DIRECT", "REJECT") and len(chains) == 1:
                    st["pure_direct_conns"] += 1
                    if not st["active_group"]:
                        st["active_group"] = top_grp
                else:
                    st["active_group"] = top_grp
                    if leaf_node not in (top_grp, "DIRECT", "REJECT"):
                        st["active_node"] = leaf_node
            else:
                st["pure_direct_conns"] += 1
                if not st["active_group"]:
                    st["active_group"] = "DIRECT"

    MIHOMO_CONNS_CACHE["time"] = now
    MIHOMO_CONNS_CACHE["data"] = stats_by_ip
    return stats_by_ip

def get_lan_subnet():
    """读取当前 br-lan 局域网网段前缀 (默认 192.168.78.)"""
    if os.path.exists("/etc/mu300/lan.conf"):
        try:
            with open("/etc/mu300/lan.conf", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("LAN_IP="):
                        ip = line.split("=", 1)[1].strip().strip("'\"")
                        parts = ip.split(".")
                        if len(parts) == 4:
                            return ".".join(parts[:3]) + ".", ip
        except Exception:
            pass
    return "192.168.78.", "192.168.78.1"

def get_default_policy_group_fallback():
    """动态推断当前配置下的默认策略组名称"""
    for yp in ["/run/mu300-vpn/mihomo.yaml", "/etc/mu300/vpn/profiles/mihomo/config.yaml"]:
        if os.path.exists(yp):
            try:
                _, pgs, _ = parse_mihomo_proxies_and_groups(yp)
                if pgs:
                    for g in pgs:
                        if g.get("name") in ("PROXY", "节点选择", "默认代理"):
                            return g["name"]
                    return pgs[0]["name"]
            except Exception:
                pass
    return "PROXY"

def load_client_policies():
    """读取终端静态 IP 绑定与 VPN 策略组分流配置 (支持任意策略组名称)"""
    fallback_grp = get_default_policy_group_fallback()
    default_data = {
        "default_group": fallback_grp,
        "clients": []
    }
    for path in [CLIENT_POLICIES_FILE, os.path.join(BASE_DIR, "client_policies.json")]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    clients = data.get("clients", [])
                    if isinstance(clients, list):
                        default_data["clients"] = clients
                    saved_def = str(data.get("default_group", "")).strip()
                    if saved_def:
                        default_data["default_group"] = saved_def
                    return default_data
            except Exception:
                pass
    return default_data

def sync_dhcp_hosts(policies=None, kick_mac=None):
    """将固定 IP 绑定同步至 /etc/mu300/dhcp-hosts.conf 并更新 dnsmasq 租约"""
    if policies is None:
        policies = load_client_policies()
    clients = policies.get("clients", [])
    os.makedirs("/etc/mu300", exist_ok=True)

    lines = ["# Auto-generated by u30air-webui static DHCP manager\n"]
    mac_to_ip = {}
    ip_to_mac = {}
    for c in clients:
        mac = str(c.get("mac", "")).lower().strip()
        ip = str(c.get("ip", "")).strip()
        static_dhcp = bool(c.get("static_dhcp", True))
        if static_dhcp and re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac) and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            lines.append(f"{mac},{ip},infinite\n")
            mac_to_ip[mac] = ip
            ip_to_mac[ip] = mac

    try:
        with open(DHCP_HOSTS_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        print(f"[DHCP Sync] 写入 {DHCP_HOSTS_FILE} 失败: {e}")

    # 确保 /opt/mu300/bin/lan-start 启用了 --dhcp-hostsfile
    lan_start = "/opt/mu300/bin/lan-start"
    need_lan_restart = False
    if os.path.exists(lan_start):
        try:
            with open(lan_start, "r", encoding="utf-8") as f:
                ls_txt = f.read()
            if "--dhcp-hostsfile=" not in ls_txt and "--dhcp-leasefile=/run/mu300-lan.leases" in ls_txt:
                ls_txt = ls_txt.replace(
                    "--dhcp-leasefile=/run/mu300-lan.leases",
                    f"--dhcp-leasefile=/run/mu300-lan.leases --dhcp-hostsfile={DHCP_HOSTS_FILE}"
                )
                with open(lan_start, "w", encoding="utf-8") as f:
                    f.write(ls_txt)
                need_lan_restart = True
        except Exception as e:
            print(f"[DHCP Sync] 更新 lan-start 失败: {e}")

    # 检查 /run/mu300-lan.leases 是否有旧租约冲突
    leases_file = "/run/mu300-lan.leases"
    lease_modified = False
    if os.path.exists(leases_file):
        try:
            with open(leases_file, "r", encoding="utf-8", errors="ignore") as f:
                lease_lines = f.readlines()
            new_leases = []
            for l in lease_lines:
                parts = l.split()
                if len(parts) >= 3:
                    l_mac = parts[1].lower()
                    l_ip = parts[2]
                    if l_mac in mac_to_ip and mac_to_ip[l_mac] != l_ip:
                        LAST_KNOWN_MAC_IP[l_mac] = l_ip
                        parts[2] = mac_to_ip[l_mac]
                        new_leases.append(" ".join(parts) + "\n")
                        lease_modified = True
                        continue
                    if l_ip in ip_to_mac and ip_to_mac[l_ip] != l_mac:
                        lease_modified = True
                        continue
                new_leases.append(l)
            if lease_modified:
                with open(leases_file, "w", encoding="utf-8") as f:
                    f.writelines(new_leases)
        except Exception as e:
            print(f"[DHCP Sync] 更新租约文件失败: {e}")

    try:
        if need_lan_restart or lease_modified:
            subprocess.run(["systemctl", "restart", "mu300-lan.service"], capture_output=True, timeout=6)
        else:
            subprocess.run(["killall", "-HUP", "dnsmasq"], capture_output=True, timeout=3)
    except Exception:
        pass

    if kick_mac and re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", kick_mac.lower().strip()):
        try:
            subprocess.run(["iw", "dev", "wlan0", "station", "del", kick_mac.lower().strip()], capture_output=True, timeout=3)
        except Exception:
            pass
    STATIONS_CACHE["time"] = 0

def save_client_policies(policies, apply_dhcp=True, apply_vpn=True, kick_mac=None):
    """保存终端静态 IP 与策略组配置，并联动生效 DHCP 与 Mihomo 分流规则"""
    os.makedirs("/etc/mu300", exist_ok=True)
    for path in [CLIENT_POLICIES_FILE, os.path.join(BASE_DIR, "client_policies.json")]:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(policies, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    if apply_dhcp:
        sync_dhcp_hosts(policies, kick_mac=kick_mac)
    if apply_vpn:
        build_merged_vpn_config(apply_live=True)
    STATIONS_CACHE["time"] = 0

def bind_client_policy(req):
    """新增或更新单台终端的固定 IP 绑定与 VPN 策略组分流 (支持任意自定义策略组名称或 DIRECT)"""
    mac = str(req.get("mac", "")).lower().strip()
    ip = str(req.get("ip", "")).strip()
    name = str(req.get("name", "")).strip()
    group = str(req.get("group", "")).strip() or get_default_policy_group_fallback()
    static_dhcp = bool(req.get("static_dhcp", True))
    reconnect = bool(req.get("reconnect", False))

    if not group or len(group) > 64 or any(ord(ch) < 32 for ch in group):
        return {"ok": 0, "error": "无效的策略组名称"}

    subnet_prefix, gw_ip = get_lan_subnet()
    if mac and not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac):
        return {"ok": 0, "error": "MAC 地址格式无效 (示例: e2:dc:b1:9d:4a:08)"}
    if not mac and not ip:
        return {"ok": 0, "error": "MAC 地址与 IP 地址不能同时为空"}

    if ip:
        try:
            ip_obj = ipaddress.IPv4Address(ip)
            if not ip.startswith(subnet_prefix) or ip == gw_ip or ip.endswith(".0") or ip.endswith(".255"):
                return {"ok": 0, "error": f"IP 地址必须在局域网段 {subnet_prefix}2 ~ {subnet_prefix}250 范围内，且不能为网关 {gw_ip}"}
        except Exception:
            return {"ok": 0, "error": "无效的 IPv4 地址"}
    else:
        static_dhcp = False

    if not mac:
        static_dhcp = False

    policies = load_client_policies()
    clients = policies.get("clients", [])

    # 检查 IP 是否已被其他 MAC 占用
    if ip and mac:
        for c in clients:
            c_mac = str(c.get("mac", "")).lower().strip()
            c_ip = str(c.get("ip", "")).strip()
            if c_ip == ip and c_mac and c_mac != mac:
                return {"ok": 0, "error": f"IP {ip} 已被设备 {c.get('name') or c_mac} 绑定，请更换其他 IP"}

    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    updated = False
    for c in clients:
        c_mac = str(c.get("mac", "")).lower().strip()
        c_ip = str(c.get("ip", "")).strip()
        if (mac and c_mac == mac) or (not mac and ip and c_ip == ip and not c_mac):
            c["mac"] = mac
            if ip:
                c["ip"] = ip
            if name:
                c["name"] = name
            c["group"] = group
            c["static_dhcp"] = static_dhcp
            c["updated"] = now_str
            updated = True
            break

    if not updated:
        clients.append({
            "mac": mac,
            "ip": ip,
            "name": name or (f"终端-{ip.split('.')[-1]}" if ip else f"终端-{mac[-5:].replace(':', '')}"),
            "group": group,
            "static_dhcp": static_dhcp,
            "updated": now_str
        })

    if req.get("default_group"):
        policies["default_group"] = str(req["default_group"]).strip()

    policies["clients"] = clients
    kick_target = mac if (reconnect and mac) else None
    save_client_policies(policies, apply_dhcp=True, apply_vpn=True, kick_mac=kick_target)
    return {
        "ok": 1,
        "msg": f"已保存：{name or mac or ip} -> {ip or '动态IP'} [{group}]",
        "policies": policies,
        "clients": get_connected_stations(force=True)
    }

def unbind_client_policy(req):
    """删除某台终端的固定 IP 绑定或策略组分流规则"""
    mac = str(req.get("mac", "")).lower().strip()
    ip = str(req.get("ip", "")).strip()
    policies = load_client_policies()
    def_grp = policies.get("default_group") or get_default_policy_group_fallback()
    before_len = len(policies.get("clients", []))
    new_clients = []
    for c in policies.get("clients", []):
        c_mac = str(c.get("mac", "")).lower().strip()
        c_ip = str(c.get("ip", "")).strip()
        if mac and c_mac == mac:
            continue
        if not mac and ip and c_ip == ip:
            continue
        new_clients.append(c)
    policies["clients"] = new_clients
    save_client_policies(policies, apply_dhcp=True, apply_vpn=True)
    return {
        "ok": 1,
        "removed": before_len - len(new_clients),
        "msg": f"已移除该设备的固定 IP 与分流绑定（恢复默认策略组：{def_grp}）",
        "policies": policies,
        "clients": get_connected_stations(force=True)
    }

def get_connected_stations(force=False):
    """实时读取局域网 (Wi-Fi AP + USB) 在线终端详情，合并 iw station dump、DHCP 租约、ARP 邻居表与策略组状态"""
    global STATIONS_CACHE
    now = time.time()
    if not force and STATIONS_CACHE["data"] and (now - STATIONS_CACHE["time"] < 2.0):
        return STATIONS_CACHE["data"]

    stations = {}
    subnet_prefix, gw_ip = get_lan_subnet()

    def ensure_station(mac_addr, conn_type="Wi-Fi"):
        mac_addr = mac_addr.lower().strip()
        if not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac_addr) or mac_addr == "00:00:00:00:00:00":
            return None
        if mac_addr not in stations:
            stations[mac_addr] = {
                "mac": mac_addr,
                "signal": None,
                "rx_bytes": 0,
                "tx_bytes": 0,
                "connected_time": 0,
                "tx_bitrate": "",
                "rx_bitrate": "",
                "ip": "",
                "name": "",
                "vendor": "无线终端",
                "conn_type": conn_type,
                "online": False,
                "is_static": False,
                "static_ip": "",
                "policy_group": "PROXY"
            }
        return stations[mac_addr]

    # 1. 读取 Wi-Fi 关联列表 (iw dev wlan0 station dump)
    try:
        proc = subprocess.run(["iw", "dev", "wlan0", "station", "dump"], capture_output=True, text=True, timeout=2.5)
        cur_mac = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("Station "):
                parts = line.split()
                cur_mac = parts[1].lower()
                st_obj = ensure_station(cur_mac, "Wi-Fi")
                if st_obj:
                    st_obj["online"] = True
            elif cur_mac and cur_mac in stations and ":" in line:
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.strip()
                if k == "signal":
                    try:
                        stations[cur_mac]["signal"] = int(v.split()[0])
                    except Exception:
                        pass
                elif k == "rx bytes":
                    try:
                        stations[cur_mac]["rx_bytes"] = int(v)
                    except Exception:
                        pass
                elif k == "tx bytes":
                    try:
                        stations[cur_mac]["tx_bytes"] = int(v)
                    except Exception:
                        pass
                elif k == "connected time":
                    try:
                        stations[cur_mac]["connected_time"] = int(v.split()[0])
                    except Exception:
                        pass
                elif k == "tx bitrate":
                    stations[cur_mac]["tx_bitrate"] = v
                elif k == "rx bitrate":
                    stations[cur_mac]["rx_bitrate"] = v
    except Exception as e:
        print(f"[Station Dump Error] {e}")

    # 2. 读取 DHCP 租约记录 (/run/mu300-lan.leases) 补充所有已分配租约的局域网终端
    leases_file = "/run/mu300-lan.leases"
    if os.path.exists(leases_file):
        try:
            with open(leases_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3:
                        mac = parts[1].lower()
                        ip = parts[2]
                        hostname = parts[3] if len(parts) > 3 and parts[3] != "*" else ""
                        st_obj = ensure_station(mac, "Wi-Fi/LAN")
                        if st_obj:
                            st_obj["ip"] = ip
                            if hostname:
                                st_obj["name"] = hostname
        except Exception:
            pass

    # 3. 读取 /proc/net/arp 及 ip neigh 补充 USB / 静态 IP 设备与在线状态
    arp_live_ip_by_mac = {}
    try:
        with open("/proc/net/arp", "r", encoding="utf-8", errors="ignore") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 6:
                    ip, flags, mac, dev = parts[0], parts[2], parts[3].lower(), parts[5]
                    if dev == "br-lan" and ip.startswith(subnet_prefix) and ip != gw_ip and mac != "00:00:00:00:00:00":
                        st_obj = ensure_station(mac, "Wi-Fi/LAN")
                        if st_obj:
                            if not st_obj["ip"]:
                                st_obj["ip"] = ip
                            if flags != "0x0":
                                st_obj["online"] = True
                                arp_live_ip_by_mac[mac] = ip
    except Exception:
        pass

    # 4. 关联用户配置的固定 IP、VPN 策略组与 Mihomo 实时连接状态
    policies = load_client_policies()
    default_group = policies.get("default_group") or get_default_policy_group_fallback()
    pol_by_mac = {}
    pol_by_ip = {}
    for c in policies.get("clients", []):
        c_mac = str(c.get("mac", "")).lower().strip()
        c_ip = str(c.get("ip", "")).strip()
        if c_mac:
            pol_by_mac[c_mac] = c
        if c_ip:
            pol_by_ip[c_ip] = c

    mihomo_stats = get_mihomo_client_stats(force=force)

    oui_map = {
        "Apple": ["f0:18:98", "8c:88:2b", "3c:06:30", "a4:83:e7", "f4:0f:24", "ac:bc:32", "bc:d0:74", "a8:66:7f", "14:7d:da", "28:cf:e9"],
        "Xiaomi": ["64:cc:2e", "74:23:44", "28:6c:07", "18:55:06", "58:41:20", "04:cf:8c"],
        "Huawei": ["00:e0:fc", "48:46:fb", "70:72:0d", "38:bc:01", "dc:d2:fc", "04:25:c5"],
        "Samsung": ["00:00:f0", "50:01:d9", "34:14:5f", "94:65:2d"],
        "Vivo": ["c0:84:7d", "80:ea:96", "44:78:3e"],
        "Oppo": ["2c:59:8a", "b0:19:c6", "48:2c:a0"]
    }

    for s in stations.values():
        mac = s["mac"]
        ip = s["ip"]
        pol = pol_by_mac.get(mac) or (pol_by_ip.get(ip) if ip else None)
        s["policy_group"] = default_group
        if pol:
            s["policy_group"] = pol.get("group", default_group)
            if pol.get("ip") and pol.get("static_dhcp", True):
                s["is_static"] = True
                s["static_ip"] = pol.get("ip", "")
                if not s["ip"]:
                    s["ip"] = pol["ip"]
            if pol.get("name"):
                s["name"] = pol["name"]

        live_ip = arp_live_ip_by_mac.get(mac) or LAST_KNOWN_MAC_IP.get(mac) or s["ip"]
        s["live_ip"] = live_ip
        s["pending_rebind"] = bool(s["is_static"] and s["static_ip"] and live_ip and live_ip != s["static_ip"])

        # 汇总该终端在静态 IP 与实时在线 IP 下的 Mihomo 活跃连接与真实命中策略组
        st_primary = mihomo_stats.get(live_ip) or mihomo_stats.get(s["ip"]) or {}
        st_secondary = (mihomo_stats.get(s["ip"]) if live_ip != s["ip"] else None) or {}
        s["active_conns"] = int(st_primary.get("active_conns", 0)) + int(st_secondary.get("active_conns", 0))
        s["active_group"] = st_primary.get("active_group") or st_secondary.get("active_group") or ""
        s["active_node"] = st_primary.get("active_node") or st_secondary.get("active_node") or ""
        s["active_dl_bytes"] = int(st_primary.get("active_dl_bytes", 0)) + int(st_secondary.get("active_dl_bytes", 0))
        s["active_ul_bytes"] = int(st_primary.get("active_ul_bytes", 0)) + int(st_secondary.get("active_ul_bytes", 0))

        prefix = mac[:8]
        matched = False
        for brand, prefixes in oui_map.items():
            if any(prefix.startswith(p) for p in prefixes):
                s["vendor"] = brand
                matched = True
                break
        if not matched:
            hn_upper = (s["name"] or "").upper()
            if any(k in hn_upper for k in ("ALN-", "HUAWEI", "HONOR", "BRA-", "CET-", "NOH-", "TAS-")):
                s["vendor"] = "Huawei"
            elif any(k in hn_upper for k in ("IPHONE", "IPAD", "MACBOOK", "AIRPODS")):
                s["vendor"] = "Apple"
            elif any(k in hn_upper for k in ("XIAOMI", "REDMI", "MI-")):
                s["vendor"] = "Xiaomi"
            else:
                try:
                    first_octet = int(mac.split(":")[0], 16)
                    s["vendor"] = "随机MAC终端" if (first_octet & 2) else "无线终端"
                except Exception:
                    s["vendor"] = "无线终端"
        if not s["name"]:
            s["name"] = f"{s['vendor']} ({mac[-5:].replace(':', '')})"

    # 按在线状态与是否配置专属分流排序（在线优先，绑定规则设备优先）
    res_list = sorted(
        stations.values(),
        key=lambda x: (0 if x.get("online") else 1, 0 if (x.get("is_static") or x.get("policy_group") != default_group) else 1, x.get("ip") or "255.255.255.255")
    )
    STATIONS_CACHE["time"] = now
    STATIONS_CACHE["data"] = res_list
    return res_list

def kick_station(mac):
    """踢出指定 MAC 设备 (802.11 Deauth 帧)"""
    mac = mac.lower().strip()
    if not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac):
        return False, "无效的 MAC 地址"
    subprocess.run(["iw", "dev", "wlan0", "station", "del", mac], capture_output=True, text=True)
    STATIONS_CACHE["time"] = 0
    return True, f"设备 {mac} 已被成功踢下线"

def block_station(mac, name="", ip=""):
    """拉黑并断开设备连接 (阻止转发与通信)"""
    mac = mac.lower().strip()
    if not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac):
        return False, "无效的 MAC 地址"
    
    # 1. 踢掉设备
    subprocess.run(["iw", "dev", "wlan0", "station", "del", mac], capture_output=True)
    
    # 2. 写入 iptables 拦截规则
    _iptables_mac_rule("add", "FORWARD", mac)
    _iptables_mac_rule("add", "INPUT", mac)
    
    # 3. 持久化至 blocklist.json
    bl = load_blocklist()
    if not any(x.get("mac") == mac for x in bl):
        bl.append({
            "mac": mac,
            "name": name or "黑名单设备",
            "ip": ip or "--",
            "time": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        save_blocklist(bl)
    STATIONS_CACHE["time"] = 0
    return True, f"设备 {mac} 已被拉黑并断开网络"

def unblock_station(mac):
    """解除设备封禁"""
    mac = mac.lower().strip()
    if not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac):
        return False, "无效的 MAC 地址"
    _iptables_mac_rule("del", "FORWARD", mac)
    _iptables_mac_rule("del", "INPUT", mac)
    bl = load_blocklist()
    bl = [x for x in bl if x.get("mac") != mac]
    save_blocklist(bl)
    STATIONS_CACHE["time"] = 0
    return True, f"设备 {mac} 已解除封禁"

def load_sessions():
    global SESSIONS
    with STATE_LOCK:
        if os.path.exists(SESSIONS_FILE):
            try:
                try:
                    os.chmod(SESSIONS_FILE, 0o600)
                except Exception:
                    pass
                with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    now = time.time()
                    SESSIONS = {k: v for k, v in data.items() if v.get("expire", 0) > now}
            except Exception:
                SESSIONS = {}

def save_sessions():
    try:
        with STATE_LOCK:
            now = time.time()
            valid = {k: v for k, v in SESSIONS.items() if v.get("expire", 0) > now}
            payload = json.dumps(valid)
        _atomic_write_text(SESSIONS_FILE, payload, mode=0o600)
    except Exception:
        pass

load_sessions()

def verify_system_password(username, password):
    """通过 Linux /etc/shadow 校验系统用户密码"""
    if not username or not password:
        return False
    if spwd and crypt:
        try:
            entry = spwd.getspnam(username)
            return crypt.crypt(password, entry.sp_pwdp) == entry.sp_pwdp
        except KeyError:
            return False
        except Exception as e:
            print(f"[Auth Error] {e}")
            return False
    return False

def update_system_password(username, new_password):
    """通过 Linux chpasswd 原生更新系统用户密码 (同步生效于 SSH / sudo 与 Web 面板)"""
    if not username or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(username)):
        return False, "无效的系统用户名"
    if not new_password or any(c in new_password for c in ("\n", "\r", "\x00")):
        return False, "新密码不能包含换行或控制字符"
    chpasswd_bin = "/usr/sbin/chpasswd"
    if not os.path.exists(chpasswd_bin):
        chpasswd_bin = "/sbin/chpasswd" if os.path.exists("/sbin/chpasswd") else "chpasswd"
    try:
        proc = subprocess.run(
            [chpasswd_bin],
            input=f"{username}:{new_password}\n",
            text=True,
            capture_output=True,
            timeout=10,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            return False, f"系统改密执行失败: {detail or f'exit {proc.returncode}'}"
        if spwd and crypt and not verify_system_password(username, new_password):
            return False, "密码写入后校验未通过，请重试"
        return True, "密码已更新"
    except Exception as e:
        return False, f"调用 chpasswd 异常: {e}"

def get_system_version():
    """读取当前底层系统真实版本 (固件镜像与发布版本)"""
    for p in ["/etc/mu300/image-version", "/etc/mu300/release"]:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    v = f.read().strip()
                    if v:
                        return v
            except Exception:
                pass
    return "v2026.10.26.1"

# =========================================================================
# VPN 单/多订阅自适应融合、IP 策略分流与零断流节点切换
# =========================================================================
class _MihomoUnixHTTP(http.client.HTTPConnection):
    def __init__(self, sock_path=MIHOMO_SOCK_PATH, timeout=3.0):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.sock_path)

def mihomo_controller_request(method, path, body=None, timeout=3.0):
    """通过 /run/mu300-vpn/mihomo-control.sock 与运行中的 Mihomo 通信 (处理 chunked 编码，零进程开销)"""
    if not os.path.exists(MIHOMO_SOCK_PATH):
        return None, None
    conn = _MihomoUnixHTTP(MIHOMO_SOCK_PATH, timeout=timeout)
    try:
        conn.connect()
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        raw = resp.read(1048576)
        data = json.loads(raw.decode("utf-8", errors="ignore")) if raw else {}
        return resp.status, data
    except Exception:
        return None, None
    finally:
        try:
            conn.close()
        except Exception:
            pass

def extract_mihomo_sub_data(yaml_path):
    """提取单个 Clash/Mihomo 订阅 YAML 中的 proxies 节点块列表与 rules 规则列表"""
    if not os.path.exists(yaml_path):
        return [], []
    try:
        with open(yaml_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return [], []

    proxy_items = []
    rules = []
    current_section = None
    curr_item = None

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # 顶层键识别
        if not line.startswith(" ") and not line.startswith("\t") and not line.startswith("-"):
            if ":" in stripped:
                sec = stripped.split(":", 1)[0].strip()
                current_section = sec
                curr_item = None
                continue

        if current_section == "proxies":
            if stripped.startswith("- "):
                curr_item = {"name": "", "lines": []}
                proxy_items.append(curr_item)
                base_sub_indent = None
                # 统一标准化为 2 空格缩进 "  - "
                curr_item["lines"].append("  - " + stripped[2:] + "\n")
                after_dash = stripped[2:].strip()
                if after_dash.startswith("name:"):
                    curr_item["name"] = after_dash.split(":", 1)[1].strip().strip("'\"")
            elif curr_item is not None:
                lead_spaces = len(line) - len(line.lstrip(" "))
                if base_sub_indent is None:
                    base_sub_indent = lead_spaces
                # 保持相对缩进：直接子属性设为 4 空格，深层嵌套属性（如 reality-opts）按相对差值递增
                rel_indent = max(0, lead_spaces - base_sub_indent)
                norm_line = (" " * (4 + rel_indent)) + stripped
                curr_item["lines"].append(norm_line + "\n")
                if stripped.startswith("name:"):
                    curr_item["name"] = stripped.split(":", 1)[1].strip().strip("'\"")

        elif current_section == "rules":
            if stripped.startswith("- "):
                rule_str = stripped[2:].strip()
                if rule_str:
                    rules.append(rule_str)

    return [p for p in proxy_items if p.get("name")], rules

def get_tailscale_status():
    """检测可选组件 Tailscale 的安装状态、服务运行状态与虚拟组网 IP"""
    installed = any(os.path.exists(p) for p in ("/usr/bin/tailscale", "/usr/sbin/tailscaled", "/bin/tailscale"))
    if not installed:
        return {"ok": 1, "installed": False, "running": False, "ip": ""}
    running = False
    ip_addr = ""
    try:
        proc = subprocess.run(["systemctl", "is-active", "tailscaled.service"], capture_output=True, text=True, timeout=1.5)
        running = (proc.stdout.strip() == "active")
    except Exception:
        pass
    if running:
        try:
            proc_ip = subprocess.run(["ip", "-4", "addr", "show", "tailscale0"], capture_output=True, text=True, timeout=1.5)
            m = re.search(r"inet\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", proc_ip.stdout)
            if m:
                ip_addr = m.group(1)
        except Exception:
            pass
    return {"ok": 1, "installed": True, "running": running, "ip": ip_addr}

def _read_kv_meta(meta_path):
    """解析 /etc/mu300/vpn/**/meta 键值文件"""
    info = {}
    if not os.path.exists(meta_path):
        return info
    try:
        with open(meta_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip().strip("'\"")
    except Exception:
        pass
    return info

def _write_kv_meta(meta_path, data):
    """保存 /etc/mu300/vpn/**/meta 键值文件"""
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    lines = []
    for k, v in data.items():
        val_s = str(v).replace("'", "")
        lines.append(f"{k}='{val_s}'\n")
    with open(meta_path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(lines)

def discover_vpn_sources():
    """
    自动发现所有已配置的订阅或独立 Profile 数据源。
    支持：
    1) 单订阅模式（自动保留原订阅名称或自定义策略组）
    2) 多订阅自适应融合模式（每个订阅生成独立策略组，支持按客户端 IP 分流）
    """
    subs_dir = "/etc/mu300/vpn/subscriptions"
    profs_dir = "/etc/mu300/vpn/profiles"
    sources = []

    if os.path.exists(subs_dir):
        try:
            sub_ids = sorted([d for d in os.listdir(subs_dir) if os.path.isdir(os.path.join(subs_dir, d))])
        except Exception:
            sub_ids = []
        for sid in sub_ids:
            s_dir = os.path.join(subs_dir, sid)
            meta = _read_kv_meta(os.path.join(s_dir, "meta"))
            url_file = os.path.join(s_dir, "url")
            url_val = ""
            if os.path.exists(url_file):
                try:
                    with open(url_file, "r", encoding="utf-8") as uf:
                        url_val = uf.read().strip()
                except Exception:
                    pass
            yaml_path = os.path.join(profs_dir, sid, "config.yaml")
            name = meta.get("NAME") or sid
            group_name = meta.get("GROUP") or name
            sources.append({
                "id": sid,
                "name": name,
                "group": group_name,
                "url": url_val,
                "interval": meta.get("INTERVAL", "24"),
                "updated": meta.get("UPDATED", ""),
                "yaml_path": yaml_path
            })

    # 若 subscriptions 目录为空，回退扫描 profiles 目录中非 merged 的独立配置
    if not sources and os.path.exists(profs_dir):
        try:
            prof_ids = sorted([d for d in os.listdir(profs_dir) if os.path.isdir(os.path.join(profs_dir, d))])
        except Exception:
            prof_ids = []
        for pid in prof_ids:
            p_dir = os.path.join(profs_dir, pid)
            meta = _read_kv_meta(os.path.join(p_dir, "meta"))
            if pid == "mihomo" and meta.get("SOURCE") == "merged":
                continue
            yaml_path = os.path.join(p_dir, "config.yaml")
            if os.path.exists(yaml_path):
                name = meta.get("NAME") or pid
                group_name = meta.get("GROUP") or (name if pid != "mihomo" else "PROXY")
                sources.append({
                    "id": pid,
                    "name": name,
                    "group": group_name,
                    "url": "",
                    "interval": "0",
                    "updated": meta.get("UPDATED", ""),
                    "yaml_path": yaml_path
                })
    return sources

def build_merged_vpn_config(apply_live=True):
    """
    自适应构建 Mihomo 运行配置：
    - 支持任意数量的订阅源（单订阅或多订阅融合分流）
    - 自动置顶局域网直连规则与（可选）Linux Tailscale 直连防断联规则
    - 自动注入客户端固定 IP 分流规则 (SRC-IP-CIDR)
    - 限制 Sniffer 仅嗅探 80/443/8443，杜绝 8080 WebUI 出现 502 Bad Gateway
    """
    merged_dir = "/etc/mu300/vpn/profiles/mihomo"
    merged_yaml = os.path.join(merged_dir, "config.yaml")
    sources = discover_vpn_sources()

    valid_sources = []
    for src in sources:
        proxies, rules = extract_mihomo_sub_data(src["yaml_path"])
        if proxies:
            src_copy = dict(src)
            src_copy["proxies"] = proxies
            src_copy["rules"] = rules
            valid_sources.append(src_copy)

    # 若尚未添加独立订阅但 mihomo/config.yaml 已存在，直接对其做安全规则加固
    if not valid_sources and os.path.exists(merged_yaml):
        proxies, rules = extract_mihomo_sub_data(merged_yaml)
        if proxies:
            valid_sources.append({
                "id": "mihomo",
                "name": "默认配置",
                "group": "PROXY",
                "yaml_path": merged_yaml,
                "proxies": proxies,
                "rules": rules
            })

    if not valid_sources:
        return {"ok": 0, "error": "未找到可用的订阅节点数据，请先在「订阅管理」中添加订阅"}

    sel_path = os.path.join(merged_dir, "panel-selection.json")
    saved_sel = {}
    if os.path.exists(sel_path):
        try:
            with open(sel_path, "r", encoding="utf-8") as f:
                saved_sel = json.load(f)
        except Exception:
            saved_sel = {}

    used_names = set()
    all_proxy_lines = []
    built_groups = []  # [{"name": group_name, "nodes": [...], "source_id": ...}]
    base_rules = []

    for src in valid_sources:
        grp_name = str(src.get("group") or src.get("name") or "PROXY").strip()
        if any(g["name"] == grp_name for g in built_groups):
            grp_name = f"{grp_name}-{src['id']}"

        port443_nodes = []
        other_nodes = []
        for p in src["proxies"]:
            pname = p["name"]
            if pname in used_names:
                new_name = f"{pname} ({grp_name})"
                p_lines = []
                for l in p["lines"]:
                    if l.strip().startswith("name:") or l.strip().startswith("- name:"):
                        l = l.replace(pname, new_name, 1)
                    p_lines.append(l)
                pname = new_name
                all_proxy_lines.extend(p_lines)
            else:
                all_proxy_lines.extend(p["lines"])
            used_names.add(pname)
            p_text = "".join(p.get("lines", []))
            if "port: 443" in p_text and "ip-version: ipv6" not in p_text and ":" not in p_text.split("server:")[-1].splitlines()[0]:
                port443_nodes.append(pname)
            else:
                other_nodes.append(pname)

        ordered_nodes = port443_nodes + other_nodes
        if "DIRECT" not in ordered_nodes:
            ordered_nodes.append("DIRECT")

        sel_node = saved_sel.get(grp_name)
        if sel_node and sel_node in ordered_nodes:
            ordered_nodes.remove(sel_node)
            ordered_nodes.insert(0, sel_node)
        elif ordered_nodes:
            saved_sel[grp_name] = ordered_nodes[0]

        built_groups.append({
            "name": grp_name,
            "nodes": ordered_nodes,
            "source_id": src["id"],
            "count": max(0, len(ordered_nodes) - 1)
        })

        for r in src["rules"]:
            if r not in base_rules:
                base_rules.append(r)

    try:
        os.makedirs(merged_dir, exist_ok=True)
        with open(sel_path, "w", encoding="utf-8") as f:
            json.dump(saved_sel, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    group_names = [g["name"] for g in built_groups]
    policies = load_client_policies()
    default_group = policies.get("default_group", "")
    if default_group not in group_names and default_group != "DIRECT":
        # 默认使用第一个策略组作为局域网全局默认策略组
        default_group = group_names[0] if group_names else "DIRECT"

    subnet_prefix, _ = get_lan_subnet()
    direct_rule_list = [
        f"IP-CIDR,{subnet_prefix}0/24,DIRECT,no-resolve"
    ]
    # 若设备安装了可选组件 Tailscale，则自动置顶 Linux tailscaled 直连规则防止断联
    ts_info = get_tailscale_status()
    if ts_info.get("installed"):
        direct_rule_list.extend([
            "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve",
            "PROCESS-NAME,tailscaled,DIRECT",
            "PROCESS-NAME,tailscale,DIRECT",
            "DST-PORT,3478,DIRECT",
            "DOMAIN-SUFFIX,tailscale.com,DIRECT",
            "DOMAIN-SUFFIX,tailscale.io,DIRECT",
            "DOMAIN-SUFFIX,tailscale.net,DIRECT"
        ])

    # 为前两个策略组绑定按需出口探测专用域名
    proxy_rule_list = []
    if len(group_names) >= 1:
        proxy_rule_list.append(f"DOMAIN,checkip.amazonaws.com,{group_names[0]}")
        proxy_rule_list.append(f"DOMAIN,api64.ipify.org,{group_names[0]}")
    if len(group_names) >= 2:
        proxy_rule_list.append(f"DOMAIN,ipv4.icanhazip.com,{group_names[1]}")
        proxy_rule_list.append(f"DOMAIN,ident.me,{group_names[1]}")

    for r in base_rules:
        if r.startswith("MATCH,"):
            continue
        parts = [x.strip() for x in r.split(",")]
        if len(parts) >= 3 and parts[2].upper() == "DIRECT":
            if r not in direct_rule_list:
                direct_rule_list.append(r)
        elif len(parts) >= 3:
            parts[2] = default_group if default_group != "DIRECT" else (group_names[0] if group_names else "DIRECT")
            rewritten = ",".join(parts)
            if rewritten not in proxy_rule_list:
                proxy_rule_list.append(rewritten)

    # 生成客户端静态 IP 分流规则
    mac_live_ips = get_mac_to_live_ips()
    bound_ip_owner = {}
    for c in policies.get("clients", []):
        c_ip = str(c.get("ip", "")).strip()
        c_mac = str(c.get("mac", "")).lower().strip()
        if c_ip and c_mac:
            bound_ip_owner[c_ip] = c_mac

    client_pure_direct_rules = []
    client_proxy_rules = []

    for c in policies.get("clients", []):
        ip = str(c.get("ip", "")).strip()
        mac = str(c.get("mac", "")).lower().strip()
        grp = str(c.get("group", default_group)).strip()
        if grp not in group_names and grp != "DIRECT":
            grp = default_group
        target_ips = []
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) or (":" in ip and not ip.startswith("fe80:")):
            target_ips.append(ip)
        if mac and mac in mac_live_ips:
            for lip in mac_live_ips[mac]:
                if lip not in target_ips:
                    if lip not in bound_ip_owner or bound_ip_owner[lip] == mac:
                        target_ips.append(lip)
        for tip in target_ips:
            cidr_suffix = "/128" if ":" in tip else "/32"
            rule_s = f"SRC-IP-CIDR,{tip}{cidr_suffix},{grp}"
            if grp == "DIRECT":
                if rule_s not in client_pure_direct_rules:
                    client_pure_direct_rules.append(rule_s)
            else:
                if rule_s not in client_proxy_rules:
                    client_proxy_rules.append(rule_s)

    final_rules = []
    final_rules.extend(client_pure_direct_rules)
    final_rules.extend(direct_rule_list)
    final_rules.extend(client_proxy_rules)
    final_rules.extend(proxy_rule_list)
    final_rules.append(f"MATCH,{default_group}")

    log_pol = load_log_policy() if "load_log_policy" in globals() else {}
    log_level = log_pol.get("mihomo_log_level", "warning")
    if log_level not in ("silent", "error", "warning", "info"):
        log_level = "warning"

    out = []
    out.append(f"log-level: {log_level}\n")
    out.append("proxies:\n")
    out.extend(all_proxy_lines)
    out.append("proxy-groups:\n")
    for bg in built_groups:
        out.append(f"  - name: {bg['name']}\n")
        out.append("    type: select\n")
        out.append("    proxies:\n")
        for n in bg["nodes"]:
            out.append(f"    - {n}\n")
    out.append("rules:\n")
    for r in final_rules:
        out.append(f"  - {r}\n")
    out.append(
        "sniffer:\n"
        "  enable: true\n"
        "  force-dns-mapping: true\n"
        "  override-destination: true\n"
        "  parse-pure-ip: true\n"
        "  sniff:\n"
        "    HTTP:\n"
        "      ports:\n"
        "      - 80\n"
        "    TLS:\n"
        "      ports:\n"
        "      - 443\n"
        "      - 8443\n"
    )

    os.makedirs(merged_dir, exist_ok=True)
    with open(merged_yaml, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(out)

    profile_title = " + ".join(group_names) if len(group_names) > 1 else (group_names[0] if group_names else "Mihomo")
    meta_path = os.path.join(merged_dir, "meta")
    _write_kv_meta(meta_path, {
        "NAME": f"自适应分流 ({profile_title})" if len(group_names) > 1 else profile_title,
        "TYPE": "mihomo",
        "SOURCE": "merged",
        "ADDED": str(int(time.time()))
    })

    if apply_live:
        try:
            active_file = "/etc/mu300/vpn/active"
            need_full_restart = False
            if os.path.exists(active_file):
                with open(active_file, "r", encoding="utf-8") as af:
                    cur_act = af.read().strip()
                if cur_act != "mihomo":
                    with open(active_file, "w", encoding="utf-8", newline="\n") as af:
                        af.write("mihomo\n")
                    need_full_restart = True

            hot_reloaded = False
            runtime_yaml_path = "/run/mu300-vpn/mihomo.yaml"
            if not need_full_restart and os.path.exists(runtime_yaml_path) and os.path.exists(MIHOMO_SOCK_PATH):
                try:
                    with open(runtime_yaml_path, "r", encoding="utf-8", errors="ignore") as rf:
                        rt_content = rf.read()
                    m_tail = re.search(r"(?m)^routing-mark:\s*", rt_content)
                    if m_tail:
                        rt_tail = rt_content[m_tail.start():]
                        full_rt_yaml = "".join(out) + "\n" + rt_tail
                        with open(runtime_yaml_path, "w", encoding="utf-8", newline="\n") as wf:
                            wf.write(full_rt_yaml)
                        code, _ = mihomo_controller_request("PUT", "/configs?force=true", body={"payload": full_rt_yaml}, timeout=3.5)
                        if code in (200, 204):
                            hot_reloaded = True
                            for g_k, n_v in saved_sel.items():
                                if g_k and n_v:
                                    mihomo_controller_request("PUT", f"/proxies/{urllib.parse.quote(g_k, safe='')}", body={"name": n_v}, timeout=1.5)
                except Exception as he:
                    print(f"[VPN Hot Reload] 回退重启: {he}")

            if not hot_reloaded:
                subprocess.run(["/opt/mu300/bin/mu300-vpn", "restart"], capture_output=True, timeout=15)
        except Exception as e:
            print(f"[VPN Merge Apply] 应用 VPN 配置失败: {e}")

    return {
        "ok": 1,
        "groups": [{"name": g["name"], "count": g["count"]} for g in built_groups],
        "default_group": default_group,
        "client_rules_count": len(client_pure_direct_rules) + len(client_proxy_rules)
    }

def parse_mihomo_proxies_and_groups(yaml_path):
    if not os.path.exists(yaml_path):
        return [], [], {}
    try:
        with open(yaml_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except Exception:
        return [], [], {}

    proxies = []
    proxy_groups = []
    current_section = None
    curr_proxy = None
    curr_group = None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        
        if not line.startswith(' ') and not line.startswith('\t') and stripped.endswith(':'):
            sec = stripped[:-1].strip()
            if sec in ('proxies', 'proxy-groups', 'rules', 'dns', 'tun', 'sniffer'):
                current_section = sec
                curr_proxy = None
                curr_group = None
                continue

        if current_section == 'proxies':
            if stripped.startswith('- '):
                curr_proxy = {}
                proxies.append(curr_proxy)
                item_content = stripped[2:].strip()
            else:
                item_content = stripped

            if ':' in item_content and curr_proxy is not None:
                k, v = item_content.split(':', 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k in ('name', 'type', 'server', 'port'):
                    curr_proxy[k] = v

        elif current_section == 'proxy-groups':
            if stripped.startswith('- name:'):
                g_name = stripped.split(':', 1)[1].strip().strip("'\"")
                curr_group = {'name': g_name, 'proxies': []}
                proxy_groups.append(curr_group)
            elif curr_group is not None:
                if stripped.startswith('- '):
                    node_name = stripped[2:].strip().strip("'\"")
                    curr_group['proxies'].append(node_name)

    proxy_map = {}
    for p in proxies:
        name = p.get('name')
        if name:
            proxy_map[name] = p
    return proxies, proxy_groups, proxy_map

def get_vpn_nodes(profile_id=None):
    if not profile_id:
        profile_id = 'mihomo'
    # 若请求的是某个已融合的子订阅，统一展示活动合成配置
    sources = discover_vpn_sources()
    if len(sources) > 1 and any(s["id"] == profile_id for s in sources):
        profile_id = 'mihomo'

    merged_cfg = f'/etc/mu300/vpn/profiles/{profile_id}/config.yaml'
    if not os.path.exists(merged_cfg) and sources:
        build_merged_vpn_config(apply_live=True)

    yaml_path = '/run/mu300-vpn/mihomo.yaml'
    if not os.path.exists(yaml_path) or os.path.getsize(yaml_path) == 0:
        if os.path.exists(merged_cfg):
            yaml_path = merged_cfg
    if not os.path.exists(yaml_path):
        return {"ok": 0, "error": "暂未配置 VPN 订阅，请先在「订阅管理」中添加订阅链接"}

    proxies, proxy_groups, proxy_map = parse_mihomo_proxies_and_groups(yaml_path)

    sel_path = f'/etc/mu300/vpn/profiles/{profile_id}/panel-selection.json'
    saved_selection = {}
    if os.path.exists(sel_path):
        try:
            with open(sel_path, 'r', encoding='utf-8') as sf:
                saved_selection = json.load(sf)
        except Exception:
            pass

    live_proxies = {}
    status_code, live_data = mihomo_controller_request("GET", "/proxies", timeout=1.5)
    if status_code == 200 and isinstance(live_data, dict):
        live_proxies = live_data.get("proxies", {})

    groups = []
    for g in proxy_groups:
        g_name = g['name']
        if not g_name:
            continue
        live_group = live_proxies.get(g_name, {}) if isinstance(live_proxies, dict) else {}
        g_nodes = []
        for n_name in g['proxies']:
            live_node = live_proxies.get(n_name, {}) if isinstance(live_proxies, dict) else {}
            hist = live_node.get("history") or []
            last_delay = hist[-1].get("delay") if hist and isinstance(hist[-1], dict) else None
            delay_val = last_delay if isinstance(last_delay, int) and last_delay > 0 else None
            if n_name in proxy_map:
                p_info = proxy_map[n_name]
                g_nodes.append({
                    'name': p_info['name'],
                    'type': p_info.get('type', 'vless'),
                    'delay': delay_val,
                    'tested': bool(hist)
                })
            else:
                g_nodes.append({
                    'name': n_name,
                    'type': 'Selector' if n_name not in ('DIRECT', 'REJECT') else n_name.capitalize(),
                    'delay': delay_val,
                    'tested': bool(hist)
                })
        
        selected = live_group.get("now") or (saved_selection.get(g_name) if isinstance(saved_selection, dict) else None)
        if not selected and g['proxies']:
            selected = g['proxies'][0]

        groups.append({
            'name': g_name,
            'selected': selected,
            'nodes': g_nodes
        })

    if not groups and proxies:
        all_nodes = [{'name': p['name'], 'type': p.get('type', 'vless'), 'delay': None, 'tested': False} for p in proxy_map.values()]
        groups.append({
            'name': 'PROXY',
            'selected': all_nodes[0]['name'] if all_nodes else '',
            'nodes': all_nodes
        })

    return {
        "ok": 1,
        "id": profile_id,
        "groups": groups,
        "policies": load_client_policies()
    }

def test_node_latency(profile_id, group_name, node_name):
    if not node_name:
        return {"ok": 0, "error": "missing node"}
    if node_name == 'DIRECT':
        return {"ok": 1, "delay": 1}
    if node_name == 'REJECT':
        return {"ok": 1, "delay": None}

    # 优先通过 Mihomo 控制 socket 进行真实的代理链路延迟测速 (真实反映经过代理节点的端到端延迟)
    for test_url in ["http://cp.cloudflare.com/generate_204", "https://www.gstatic.com/generate_204"]:
        q = urllib.parse.urlencode({"url": test_url, "timeout": 3000})
        code, res = mihomo_controller_request("GET", f"/proxies/{urllib.parse.quote(node_name, safe='')}/delay?{q}", timeout=3.5)
        if code == 200 and isinstance(res, dict) and isinstance(res.get("delay"), int) and res["delay"] > 0:
            return {"ok": 1, "delay": res["delay"]}

    # Fallback: 若控制 socket 未响应或临时异常，退化为 TCP 握手测速
    yaml_candidates = [
        '/run/mu300-vpn/mihomo.yaml',
        f'/etc/mu300/vpn/profiles/{profile_id or "mihomo"}/config.yaml'
    ]
    p_info = None
    for yp in yaml_candidates:
        _, _, p_map = parse_mihomo_proxies_and_groups(yp)
        if node_name in p_map:
            p_info = p_map[node_name]
            break

    if p_info and p_info.get('server'):
        server = p_info['server']
        port = int(p_info.get('port', 443))
        t0 = time.time()
        try:
            s_conn = socket.create_connection((server, port), timeout=2.5)
            s_conn.close()
            ms = max(1, round((time.time() - t0) * 1000))
            return {"ok": 1, "delay": ms}
        except Exception:
            pass

    return {"ok": 1, "delay": None, "error": "节点连接超时"}

def select_vpn_node(profile_id, group_name, node_name):
    if not profile_id:
        profile_id = 'mihomo'
    sources = discover_vpn_sources()
    if len(sources) > 1 and any(s["id"] == profile_id for s in sources):
        profile_id = 'mihomo'

    sel_path = f'/etc/mu300/vpn/profiles/{profile_id}/panel-selection.json'
    saved = {}
    if os.path.exists(sel_path):
        try:
            with open(sel_path, 'r', encoding='utf-8') as f:
                saved = json.load(f)
        except Exception:
            pass
    saved[group_name] = node_name
    try:
        os.makedirs(os.path.dirname(sel_path), exist_ok=True)
        with open(sel_path, 'w', encoding='utf-8') as f:
            json.dump(saved, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[VPN Node Select] 保存选择配置失败: {e}")

    for cfg_path in [f'/etc/mu300/vpn/profiles/{profile_id}/config.yaml', '/run/mu300-vpn/mihomo.yaml']:
        if not os.path.exists(cfg_path):
            continue
        try:
            with open(cfg_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            
            new_lines = []
            in_pg = False
            cur_g_name = None
            cur_g_proxies = []
            in_g_proxies = False

            def flush_group():
                if cur_g_name and cur_g_proxies:
                    reordered = list(cur_g_proxies)
                    if cur_g_name == group_name and node_name in reordered:
                        reordered.remove(node_name)
                        reordered.insert(0, node_name)
                    for px in reordered:
                        new_lines.append(f"    - {px}\n")

            for line in lines:
                st = line.strip()
                if not line.startswith(' ') and not line.startswith('\t') and st.endswith(':'):
                    sec = st[:-1].strip()
                    if in_g_proxies:
                        flush_group()
                        in_g_proxies = False
                    in_pg = (sec == 'proxy-groups')
                    new_lines.append(line)
                    continue

                if in_pg:
                    if st.startswith('- name:'):
                        if in_g_proxies:
                            flush_group()
                            in_g_proxies = False
                        cur_g_name = st.split(':', 1)[1].strip().strip("'\"")
                        cur_g_proxies = []
                        new_lines.append(line)
                    elif st.startswith('proxies:'):
                        in_g_proxies = True
                        new_lines.append(line)
                    elif in_g_proxies:
                        if st.startswith('- '):
                            pname = st[2:].strip().strip("'\"")
                            cur_g_proxies.append(pname)
                        else:
                            flush_group()
                            in_g_proxies = False
                            new_lines.append(line)
                    else:
                        new_lines.append(line)
                else:
                    new_lines.append(line)

            if in_g_proxies:
                flush_group()

            with open(cfg_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
        except Exception as e:
            print(f"[VPN Node Select] 重排失败: {e}")

    code, _ = mihomo_controller_request(
        "PUT",
        f"/proxies/{urllib.parse.quote(group_name, safe='')}",
        body={"name": node_name},
        timeout=2.5
    )
    if code not in (200, 204):
        try:
            subprocess.run(["/opt/mu300/bin/mu300-vpn", "restart"], capture_output=True, timeout=12)
        except Exception as e:
            print(f"[VPN Restart] 失败: {e}")

    return get_vpn_nodes(profile_id)

def list_vpn_subscriptions():
    """返回所有已保存的订阅列表及对应策略组名称、节点数"""
    sources = discover_vpn_sources()
    subs = []
    for s in sources:
        if not s.get("url"):
            continue
        proxies, _ = extract_mihomo_sub_data(s["yaml_path"])
        host = ""
        try:
            host = urllib.parse.urlparse(s["url"]).netloc
        except Exception:
            pass
        subs.append({
            "id": s["id"],
            "name": s["name"],
            "group": s["group"],
            "host": host,
            "interval": s["interval"],
            "updated": s["updated"],
            "count": len(proxies)
        })
    return {"ok": 1, "subscriptions": subs}

def add_vpn_subscription(req):
    """新增或更新一条 Clash/Mihomo 订阅并立即拉取合成"""
    name = str(req.get("name", "")).strip()
    url = str(req.get("url", "")).strip()
    group = str(req.get("group", "")).strip() or name
    interval = str(req.get("interval", 24)).strip()
    sub_id = str(req.get("id", "")).strip().lower()

    if not name or not url or not url.startswith(("http://", "https://")):
        return {"ok": 0, "error": "请提供有效的订阅名称和 HTTP/HTTPS 订阅链接"}

    if not sub_id or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", sub_id):
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if not slug:
            slug = f"sub-{int(time.time()) % 100000}"
        sub_id = slug[:24]

    s_dir = f"/etc/mu300/vpn/subscriptions/{sub_id}"
    os.makedirs(s_dir, exist_ok=True)
    with open(os.path.join(s_dir, "url"), "w", encoding="utf-8", newline="\n") as f:
        f.write(url + "\n")
    _write_kv_meta(os.path.join(s_dir, "meta"), {
        "NAME": name,
        "GROUP": group,
        "INTERVAL": interval,
        "ADDED": str(int(time.time()))
    })

    ref_res = refresh_vpn_subscription(sub_id)
    if not ref_res.get("ok"):
        return ref_res
    return {"ok": 1, "id": sub_id, "name": name, "group": group, "refresh": ref_res}

def remove_vpn_subscription(req):
    """删除指定订阅及其缓存 Profile，并重新合成活动配置"""
    sub_id = str(req.get("id", "")).strip()
    if not sub_id or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", sub_id):
        return {"ok": 0, "error": "无效的订阅 ID"}
    import shutil
    for target_dir in [f"/etc/mu300/vpn/subscriptions/{sub_id}", f"/etc/mu300/vpn/profiles/{sub_id}"]:
        if os.path.exists(target_dir):
            try:
                shutil.rmtree(target_dir)
            except Exception as e:
                return {"ok": 0, "error": f"删除失败: {e}"}
    build_merged_vpn_config(apply_live=True)
    return {"ok": 1, "id": sub_id}

def refresh_vpn_subscription(sub_id="all"):
    """拉取一个或多个订阅最新节点，并自动合成自适应策略组配置"""
    subs_dir = "/etc/mu300/vpn/subscriptions"
    if not os.path.exists(subs_dir):
        return {"ok": 0, "error": "订阅目录不存在，请先添加订阅"}

    if isinstance(sub_id, dict):
        sub_id = sub_id.get("id", "all")

    targets = []
    if sub_id in ("all", "mihomo", "") or not sub_id:
        targets = [d for d in os.listdir(subs_dir) if os.path.isdir(os.path.join(subs_dir, d))]
    else:
        if os.path.isdir(os.path.join(subs_dir, str(sub_id))):
            targets = [str(sub_id)]
        else:
            targets = [d for d in os.listdir(subs_dir) if os.path.isdir(os.path.join(subs_dir, d))]

    if not targets:
        return {"ok": 0, "error": "未找到匹配的订阅"}

    results = []
    for sid in targets:
        s_path = os.path.join(subs_dir, sid)
        url_file = os.path.join(s_path, "url")
        if not os.path.exists(url_file):
            continue
        try:
            with open(url_file, "r", encoding="utf-8") as f:
                url = f.read().strip()
            if not url:
                continue

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url, headers={"User-Agent": "ClashMeta/1.18.0 (U30Air)"})
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="ignore")

            lines = content.splitlines(keepends=False)
            top_keys = ('proxies:', 'proxy-groups:', 'rules:', 'dns:', 'tun:')
            out_lines = []
            current_top = False

            for line in lines:
                st = line.strip()
                if not st or st.startswith('#'):
                    out_lines.append(line + "\n")
                    continue

                if any(line.startswith(tk) for tk in top_keys):
                    current_top = True
                    out_lines.append(line + "\n")
                    continue
                elif not line.startswith(' ') and not line.startswith('\t') and ':' in line and not line.startswith('- '):
                    current_top = False
                    out_lines.append(line + "\n")
                    continue

                if current_top and not line.startswith('  '):
                    out_lines.append('  ' + line + "\n")
                else:
                    out_lines.append(line + "\n")

            prof_target_dir = f"/etc/mu300/vpn/profiles/{sid}"
            os.makedirs(prof_target_dir, exist_ok=True)
            with open(os.path.join(prof_target_dir, "config.yaml"), "w", encoding="utf-8", newline="\n") as f:
                f.writelines(out_lines)

            now_ts = str(int(time.time()))
            meta_file = os.path.join(s_path, "meta")
            meta_data = _read_kv_meta(meta_file)
            meta_data["UPDATED"] = now_ts
            _write_kv_meta(meta_file, meta_data)

            results.append({"id": sid, "ok": 1})
        except Exception as e:
            results.append({"id": sid, "ok": 0, "error": str(e)})

    merge_res = build_merged_vpn_config(apply_live=True)
    return {"ok": 1, "results": results, "merged": merge_res}

def probe_vpn_group_exit_ips():
    """按需并发探测当前活动策略组各自真实的出口公网 IP 及 ISP 属性 (自适应 1~2 个主力策略组)"""
    rt_yaml = "/run/mu300-vpn/mihomo.yaml"
    if os.path.exists(rt_yaml):
        try:
            with open(rt_yaml, "r", encoding="utf-8", errors="ignore") as f:
                if "checkip.amazonaws.com" not in f.read():
                    build_merged_vpn_config(apply_live=True)
        except Exception:
            pass

    _, pgs, _ = parse_mihomo_proxies_and_groups(rt_yaml if os.path.exists(rt_yaml) else "/etc/mu300/vpn/profiles/mihomo/config.yaml")
    group_names = [g["name"] for g in pgs if g.get("name")] or ["PROXY"]
    grp_a = group_names[0]
    grp_b = group_names[1] if len(group_names) > 1 else group_names[0]

    node_a = "--"
    node_b = "--"
    code, pdata = mihomo_controller_request("GET", "/proxies", timeout=1.5)
    if code == 200 and isinstance(pdata, dict):
        prx = pdata.get("proxies") or {}
        if isinstance(prx.get(grp_a), dict):
            node_a = prx[grp_a].get("now") or node_a
        if isinstance(prx.get(grp_b), dict):
            node_b = prx[grp_b].get("now") or node_b

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def _fetch_ip(urls):
        for u in urls:
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "curl/8.5.0"})
                with urllib.request.urlopen(req, context=ctx, timeout=4.5) as resp:
                    txt = resp.read(256).decode("utf-8", errors="ignore").strip()
                    if re.match(r"^\d+\.\d+\.\d+\.\d+$", txt) or ":" in txt:
                        return txt
            except Exception:
                pass
        return ""

    def _enrich_ip(ip_addr, group_name, node_name):
        info = {
            "group": group_name,
            "node": node_name,
            "ip": ip_addr,
            "country": "",
            "region": "",
            "city": "",
            "isp": "",
            "org": "",
            "hosting": None,
            "is_residential": False
        }
        if not ip_addr:
            return info
        try:
            api_url = f"http://ip-api.com/json/{urllib.parse.quote(ip_addr)}?fields=status,country,regionName,city,isp,org,hosting,query&lang=zh-CN"
            req = urllib.request.Request(api_url, headers={"User-Agent": "curl/8.5.0"})
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                meta = json.loads(resp.read(4096).decode("utf-8", errors="ignore"))
            if isinstance(meta, dict) and meta.get("status") == "success":
                info["country"] = meta.get("country") or ""
                info["region"] = meta.get("regionName") or ""
                info["city"] = meta.get("city") or ""
                info["isp"] = meta.get("isp") or ""
                info["org"] = meta.get("org") or ""
                hosting = bool(meta.get("hosting", False))
                info["hosting"] = hosting
                info["is_residential"] = not hosting
        except Exception:
            pass
        return info

    results = {}

    def _worker_a():
        ip = _fetch_ip(["https://checkip.amazonaws.com", "https://api64.ipify.org"])
        results["a"] = _enrich_ip(ip, grp_a, node_a)

    def _worker_b():
        if grp_b == grp_a:
            return
        ip = _fetch_ip(["https://ipv4.icanhazip.com", "https://ident.me"])
        results["b"] = _enrich_ip(ip, grp_b, node_b)

    t1 = threading.Thread(target=_worker_a, daemon=True)
    t2 = threading.Thread(target=_worker_b, daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=8.0)
    t2.join(timeout=8.0)

    info_a = results.get("a") or _enrich_ip("", grp_a, node_a)
    info_b = results.get("b") or (info_a if grp_b == grp_a else _enrich_ip("", grp_b, node_b))
    return {
        "ok": 1,
        "groups": [info_a] + ([info_b] if grp_b != grp_a else []),
        "home": info_a,
        "direct": info_b,
        "exit_ip": info_a.get("ip") or info_b.get("ip") or ""
    }

def get_mem_available_mb():
    """从 /proc/meminfo 读取当前可用内存 MB (零子进程开销)"""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return round(kb / 1024.0, 1)
    except Exception:
        pass
    return None

def drop_memory_caches():
    """安全释放 Linux 内核 PageCache / dentries / inodes 缓存，不影响任何运行中进程"""
    before_mb = get_mem_available_mb()
    try:
        if hasattr(os, "sync"):
            os.sync()
        with open("/proc/sys/vm/drop_caches", "w", encoding="utf-8") as f:
            f.write("3\n")
    except Exception as e:
        return {"ok": 0, "error": f"释放缓存失败: {e}"}
    time.sleep(0.08)
    after_mb = get_mem_available_mb()
    freed_mb = round(max(0.0, (after_mb or 0.0) - (before_mb or 0.0)), 1)
    OVERVIEW_API_CACHE["time"] = 0
    return {
        "ok": 1,
        "before_avail_mb": before_mb,
        "after_avail_mb": after_mb,
        "freed_mb": freed_mb,
        "msg": f"已释放 {freed_mb} MB 内核缓存，当前可用内存 {after_mb} MB"
    }

def run_mu300_api(method, req=None):
    """调用底层的 mu300-api（支持 sms.* / ussd.* / at.* 按卡槽透明透传 MU300_SIM_SLOT 与 MU300_AT_DIR）"""
    if not os.path.exists(API_BIN):
        return {"ok": 0, "error": f"API binary {API_BIN} not found"}
    try:
        req_clean = dict(req) if isinstance(req, dict) else (req or {})
        env = None
        if isinstance(req_clean, dict) and method.startswith(("sms.", "ussd.", "at.")) and "slot" in req_clean:
            s_val = req_clean.pop("slot")
            if str(s_val) in ("1", "2"):
                s_idx = int(s_val) - 1
                env = dict(os.environ, MU300_SIM_SLOT=str(s_idx), MU300_AT_DIR=get_slot_at_dir(s_idx))
        payload = json.dumps(req_clean)
        cmd_timeout = 95 if method.startswith(("ussd.", "sms.", "radio.", "sim.", "at.")) else 25
        proc = subprocess.Popen(
            [API_BIN, method],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        stdout, stderr = proc.communicate(input=payload, timeout=cmd_timeout)
        for line in reversed(stdout.strip().split("\n")):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except Exception:
                    pass
        if stdout.strip():
            return {"ok": 1, "text": stdout.strip()}
        return {"ok": 0, "error": stderr.strip() or "empty_response"}
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"ok": 0, "error": "timeout"}
    except Exception as e:
        return {"ok": 0, "error": str(e)}

OVERVIEW_API_CACHE = {"time": 0, "data": None}
LOGIN_FAILURES = {}
LOG_POLICY_FILE = os.path.join(BASE_DIR, "log_policy.json")
DEFAULT_LOG_POLICY = {
    "auto_clean": True,
    "max_journal_mb": 8,
    "retention_days": 3,
    "max_logcat_kb": 512,
    "min_free_disk_mb": 500,
    "mihomo_log_level": "warning",
    "last_clean_time": "--",
    "last_freed_kb": 0
}

def load_log_policy():
    policy = dict(DEFAULT_LOG_POLICY)
    if os.path.exists(LOG_POLICY_FILE):
        try:
            with open(LOG_POLICY_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    policy.update(saved)
        except Exception:
            pass
    return policy

def save_log_policy(policy):
    try:
        with open(LOG_POLICY_FILE, "w", encoding="utf-8") as f:
            json.dump(policy, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _dir_or_file_size_bytes(path):
    """快速统计文件或目录真实磁盘占用字节数 (优先 st_blocks*512 识别 journal 稀疏文件，零子进程开销)"""
    if not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        try:
            st = os.stat(path)
            return (st.st_blocks * 512) if hasattr(st, "st_blocks") and st.st_blocks > 0 else st.st_size
        except Exception:
            return 0
    total = 0
    try:
        for root, _, files in os.walk(path):
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    if not os.path.islink(fp):
                        st = os.stat(fp)
                        total += (st.st_blocks * 512) if hasattr(st, "st_blocks") and st.st_blocks > 0 else st.st_size
                except Exception:
                    pass
    except Exception:
        pass
    return total

def truncate_file_inplace(filepath, max_bytes=524288, keep_lines=500):
    """
    原地截断正在被守护进程追加写入(O_APPEND)的日志文件(如 android-logcat.txt)。
    保持 inode 不变，防止 logdw 等进程继续向已删除的孤立文件描述符写入导致空间无法释放。
    返回释放的字节数。
    """
    if not os.path.exists(filepath) or not os.path.isfile(filepath):
        return 0
    try:
        sz = os.path.getsize(filepath)
        if sz <= max_bytes:
            return 0
        with open(filepath, "r+", encoding="utf-8", errors="ignore") as f:
            # 仅读取末尾最多 128KB 以提取最后 keep_lines 行
            seek_pos = max(0, sz - 131072)
            f.seek(seek_pos)
            tail_lines = f.readlines()[-keep_lines:]
            new_content = "".join(tail_lines)
            f.seek(0)
            f.write(new_content)
            f.truncate()
        new_sz = os.path.getsize(filepath)
        return max(0, sz - new_sz)
    except Exception as e:
        print(f"[Log Truncate Error] {filepath}: {e}")
        return 0

def apply_journald_policy(max_journal_mb=8, retention_days=3):
    """更新 systemd-journald 限额配置"""
    conf_dir = "/etc/systemd/journald.conf.d"
    conf_file = os.path.join(conf_dir, "10-mu300.conf")
    try:
        os.makedirs(conf_dir, exist_ok=True)
        file_max = max(1, int(max_journal_mb) // 4)
        content = (
            "[Journal]\n"
            "SyncIntervalSec=5min\n"
            "ReadKMsg=no\n"
            f"RuntimeMaxUse={int(max_journal_mb)}M\n"
            f"SystemMaxUse={int(max_journal_mb)}M\n"
            f"SystemMaxFileSize={file_max}M\n"
            f"MaxRetentionSec={int(retention_days)}day\n"
        )
        with open(conf_file, "w", encoding="utf-8") as f:
            f.write(content)
        subprocess.run(["systemctl", "kill", "--kill-who=main", "--signal=SIGUSR2", "systemd-journald.service"], capture_output=True, timeout=5)
    except Exception as e:
        print(f"[Journald Policy Error] {e}")

def apply_mihomo_log_level(level="warning"):
    """设置 Mihomo 日志级别 (默认 warning)，避免海量 [TCP] dial 刷屏 journald 与磨损 eMMC"""
    if level not in ("silent", "error", "warning", "info", "debug"):
        level = "warning"
    candidates = ["/run/mu300-vpn/mihomo.yaml"]
    prof_dir = "/etc/mu300/vpn/profiles"
    if os.path.exists(prof_dir):
        try:
            for d in os.listdir(prof_dir):
                p = os.path.join(prof_dir, d, "config.yaml")
                if os.path.exists(p):
                    candidates.append(p)
        except Exception:
            pass
    for yp in candidates:
        if not os.path.exists(yp):
            continue
        try:
            with open(yp, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            if re.search(r"(?m)^log-level:\s*\S+", txt):
                new_txt = re.sub(r"(?m)^log-level:\s*\S+", f"log-level: {level}", txt)
            else:
                new_txt = f"log-level: {level}\n" + txt
            if new_txt != txt:
                with open(yp, "w", encoding="utf-8") as f:
                    f.write(new_txt)
        except Exception:
            pass

def run_log_cleanup(target="all", force_truncate=False):
    """
    执行日志与缓存清理
    target: 'all' | 'journal' | 'logcat' | 'apt'
    """
    policy = load_log_policy()
    max_j_mb = int(policy.get("max_journal_mb", 8))
    ret_days = int(policy.get("retention_days", 3))
    max_lc_kb = int(policy.get("max_logcat_kb", 512))
    freed_bytes = 0

    # 1. 处理 android-logcat.txt 及常规 /var/log 日志
    if target in ("all", "logcat"):
        threshold = 64 * 1024 if force_truncate else max_lc_kb * 1024
        keep_n = 200 if force_truncate else 500
        freed_bytes += truncate_file_inplace("/var/log/android-logcat.txt", max_bytes=threshold, keep_lines=keep_n)
        for extra_log in ["/var/log/dpkg.log", "/var/log/alternatives.log", "/var/log/bootstrap.log"]:
            freed_bytes += truncate_file_inplace(extra_log, max_bytes=128 * 1024 if force_truncate else 512 * 1024, keep_lines=200)

    # 2. 处理 systemd-journald 归档与超限日志
    if target in ("all", "journal"):
        before_j = _dir_or_file_size_bytes("/var/log/journal") + _dir_or_file_size_bytes("/run/log/journal")
        try:
            if force_truncate:
                subprocess.run(["journalctl", "--rotate"], capture_output=True, timeout=8)
                subprocess.run(["journalctl", "--vacuum-size=2M", f"--vacuum-time={ret_days}d"], capture_output=True, timeout=10)
            else:
                j_limit_bytes = max_j_mb * 1024 * 1024
                if before_j > j_limit_bytes:
                    subprocess.run(["journalctl", "--rotate"], capture_output=True, timeout=8)
                    subprocess.run(["journalctl", f"--vacuum-size={max_j_mb}M", f"--vacuum-time={ret_days}d"], capture_output=True, timeout=10)
                else:
                    subprocess.run(["journalctl", f"--vacuum-time={ret_days}d"], capture_output=True, timeout=10)
        except Exception:
            pass
        after_j = _dir_or_file_size_bytes("/var/log/journal") + _dir_or_file_size_bytes("/run/log/journal")
        freed_bytes += max(0, before_j - after_j)

    # 3. 清理 APT 缓存列表与临时文件
    if target in ("all", "apt"):
        apt_lists = "/var/lib/apt/lists"
        before_apt = _dir_or_file_size_bytes(apt_lists) + _dir_or_file_size_bytes("/var/cache/apt/archives")
        if force_truncate or target == "apt":
            try:
                if os.path.exists(apt_lists):
                    for root, _, files in os.walk(apt_lists):
                        for fn in files:
                            if fn != "lock":
                                try:
                                    os.remove(os.path.join(root, fn))
                                except Exception:
                                    pass
                subprocess.run(["apt-get", "clean"], capture_output=True, timeout=10)
            except Exception:
                pass
        after_apt = _dir_or_file_size_bytes(apt_lists) + _dir_or_file_size_bytes("/var/cache/apt/archives")
        freed_bytes += max(0, before_apt - after_apt)

    # 清理 /tmp 下的订阅缓存文件
    try:
        for fn in os.listdir("/tmp"):
            if fn.startswith("norm_sub_") and fn.endswith(".yaml"):
                fp = os.path.join("/tmp", fn)
                freed_bytes += _dir_or_file_size_bytes(fp)
                os.remove(fp)
    except Exception:
        pass

    policy["last_clean_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    policy["last_freed_kb"] = round(freed_bytes / 1024.0, 1)
    save_log_policy(policy)
    return {
        "ok": 1,
        "freed_bytes": freed_bytes,
        "freed_kb": round(freed_bytes / 1024.0, 1),
        "freed_str": f"{round(freed_bytes / 1024.0, 1)} KB" if freed_bytes < 1048576 else f"{round(freed_bytes / 1048576.0, 2)} MB",
        "last_clean_time": policy["last_clean_time"]
    }

def get_storage_and_logs_status():
    """获取当前磁盘空间、各类日志详细占用及自动清理策略"""
    policy = load_log_policy()
    disk_total = 0
    disk_used = 0
    disk_avail = 0
    try:
        st = os.statvfs("/")
        disk_total = st.f_blocks * st.f_frsize
        disk_avail = st.f_bavail * st.f_frsize
        disk_used = disk_total - (st.f_bfree * st.f_frsize)
    except Exception:
        pass

    journal_bytes = _dir_or_file_size_bytes("/var/log/journal") + _dir_or_file_size_bytes("/run/log/journal")
    logcat_bytes = _dir_or_file_size_bytes("/var/log/android-logcat.txt")
    var_log_bytes = _dir_or_file_size_bytes("/var/log")
    apt_bytes = _dir_or_file_size_bytes("/var/lib/apt/lists") + _dir_or_file_size_bytes("/var/cache/apt/archives")
    used_pct = round((disk_used / disk_total) * 100, 1) if disk_total > 0 else 0

    return {
        "ok": 1,
        "disk": {
            "total_bytes": disk_total,
            "used_bytes": disk_used,
            "avail_bytes": disk_avail,
            "pct": used_pct,
            "used_pct": used_pct
        },
        "logs": {
            "journal_bytes": journal_bytes,
            "logcat_bytes": logcat_bytes,
            "var_log_bytes": var_log_bytes,
            "apt_cache_bytes": apt_bytes,
            "total_cleanable_bytes": journal_bytes + logcat_bytes + apt_bytes
        },
        "policy": policy
    }

def start_log_guardian():
    """后台日志与磁盘守护线程 (每 15 分钟自动巡检一次，0 额外进程开销)"""
    def _guardian_loop():
        try:
            pol = load_log_policy()
            apply_journald_policy(pol.get("max_journal_mb", 8), pol.get("retention_days", 3))
            apply_mihomo_log_level(pol.get("mihomo_log_level", "warning"))
            sync_dhcp_hosts()
            if discover_vpn_sources():
                merged_cfg = "/etc/mu300/vpn/profiles/mihomo/config.yaml"
                need_merge = True
                if os.path.exists(merged_cfg):
                    with open(merged_cfg, "r", encoding="utf-8", errors="ignore") as f:
                        c_txt = f.read()
                    if "8080-8880" not in c_txt and "checkip.amazonaws.com" in c_txt:
                        need_merge = False
                if need_merge:
                    build_merged_vpn_config(apply_live=True)
            if pol.get("auto_clean", True):
                run_log_cleanup(target="all", force_truncate=False)
        except Exception as e:
            print(f"[Guardian Init] {e}")

        while True:
            time.sleep(900)
            try:
                pol = load_log_policy()
                if not pol.get("auto_clean", True):
                    continue
                st = os.statvfs("/")
                avail_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)
                min_free = int(pol.get("min_free_disk_mb", 500))
                low_disk = avail_mb < min_free
                run_log_cleanup(target="all", force_truncate=low_disk)
                avail_mem_mb = get_mem_available_mb()
                if avail_mem_mb is not None and avail_mem_mb < 400:
                    drop_memory_caches()
            except Exception:
                pass

    t = threading.Thread(target=_guardian_loop, daemon=True)
    t.start()

def get_bluetooth_status():
    """读取蓝牙服务状态 (bluetooth.service & mu300-bluetooth.service)"""
    proc = subprocess.run(["systemctl", "is-active", "bluetooth.service"], capture_output=True, text=True)
    proc_mu = subprocess.run(["systemctl", "is-active", "mu300-bluetooth.service"], capture_output=True, text=True)
    running = (proc.stdout.strip() == "active") or (proc_mu.stdout.strip() == "active")
    proc_en = subprocess.run(["systemctl", "is-enabled", "mu300-bluetooth.service"], capture_output=True, text=True)
    enabled = proc_en.stdout.strip() == "enabled"
    return {"ok": 1, "running": running, "enabled": enabled}

def toggle_bluetooth_service(desired=None):
    """切换蓝牙服务启停状态 (默认禁用以节省功耗并消除 SC2355_TX_THREA D态挂起)"""
    st = get_bluetooth_status()
    turn_on = (not st["running"]) if desired is None else bool(desired)
    if turn_on:
        subprocess.run(["systemctl", "enable", "mu300-bluetooth.service", "bluetooth.service"], capture_output=True, timeout=10)
        subprocess.run(["systemctl", "start", "mu300-bluetooth.service", "bluetooth.service"], capture_output=True, timeout=15)
        return {"ok": 1, "running": True, "enabled": True, "msg": "蓝牙服务已开启"}
    else:
        subprocess.run(["systemctl", "stop", "bluetooth.service", "mu300-bluetooth.service"], capture_output=True, timeout=10)
        subprocess.run(["systemctl", "disable", "bluetooth.service", "mu300-bluetooth.service"], capture_output=True, timeout=10)
        return {"ok": 1, "running": False, "enabled": False, "msg": "蓝牙服务已关闭并设为默认禁用"}

def get_overview_data(req=None):
    """The homepage POST and direct GET must expose the same live details (带 4.5s 缓存降低子进程开销)."""
    now = time.time()
    cache = globals().get("OVERVIEW_API_CACHE")
    if req is None and cache and cache.get("data") is not None and (now - cache.get("time", 0) < 4.5):
        data = dict(cache["data"])
    else:
        data = run_mu300_api("overview", req) if req is not None else run_mu300_api("overview")
        if not isinstance(data, dict):
            return {"ok": 0, "error": "概览数据不可用"}
        if not data.get("ok"):
            return data
        if cache is not None and req is None:
            cache["time"] = now
            cache["data"] = dict(data)
        data = dict(data)

    data["system_version"] = get_system_version()
    data["realtime"] = get_realtime_traffic()
    data["cellular_qos"] = get_cellular_qos()
    data["tailscale"] = get_tailscale_status()
    bat_det = get_battery_info()
    data["battery_details"] = bat_det
    if isinstance(bat_det, dict) and bat_det.get("thermal"):
        data["thermal_details"] = bat_det["thermal"]
    elif os.path.exists("/sys/class/thermal"):
        data["thermal_details"] = get_thermal_details()
    data["wifi_clients_count"] = len(get_connected_stations())
    data["active_sim_slot"] = get_active_sim_slot_idx() + 1
    if os.path.exists("/sys/class/net/sipa_eth0") or os.path.exists("/sys/class/net/sipa_eth8"):
        cell_ip = get_cellular_ip_info()
        data["cellular_ip"] = cell_ip
        if "data" not in data or not isinstance(data["data"], dict):
            data["data"] = {}
        else:
            data["data"] = dict(data["data"])
        if not data["data"].get("address") and cell_ip.get("ipv4"):
            data["data"]["address"] = cell_ip["ipv4"]
        data["data"]["ipv4"] = cell_ip.get("ipv4", "")
        data["data"]["public_ipv4"] = cell_ip.get("public_ipv4", "")
        data["data"]["ipv6"] = cell_ip.get("ipv6", "")
    return data

get_overview_info = get_overview_data

def _read_power_conf_dict():
    conf = {
        "CHARGE_TO": 80,
        "RESUME_AT": 0,
        "SAVER_BELOW": 20,
        "CHARGE_PAUSE": 0,
        "FULL_ONCE": 0,
    }
    if os.path.exists("/etc/mu300/power.conf"):
        try:
            with open("/etc/mu300/power.conf", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k in conf:
                        try:
                            conf[k] = int(v)
                        except Exception:
                            pass
                    else:
                        conf[k] = v
        except Exception:
            pass
    return conf

def _apply_power_guard_reload():
    if os.path.exists("/opt/mu300/bin/mu300-power"):
        try:
            subprocess.run(["systemctl", "restart", "mu300-power.service"], capture_output=True, timeout=8)
            time.sleep(0.25)
        except Exception:
            pass
    BATTERY_INFO_CACHE["time"] = 0
    OVERVIEW_API_CACHE["time"] = 0

def handle_power_api(method, req_data):
    """增强电源与电池养护控制：支持 60%/50% 长期插电养护、80%/70% 随身备电、回充滞回与单次充满"""
    req_data = req_data if isinstance(req_data, dict) else {}
    power_bin = "/opt/mu300/bin/mu300-power"

    if method == "power.get":
        resp = run_mu300_api("power.get", req_data)
        if not isinstance(resp, dict):
            resp = {"ok": 1}
        pconf = _read_power_conf_dict()
        conf_obj = dict(resp.get("conf") or {})
        conf_obj.update(pconf)
        resp["conf"] = conf_obj
        resp["charge_to"] = conf_obj.get("CHARGE_TO", 80)
        resp["resume_at"] = conf_obj.get("RESUME_AT", 0)
        resp["saver_below"] = conf_obj.get("SAVER_BELOW", 20)
        resp["charge_pause"] = conf_obj.get("CHARGE_PAUSE", 0)
        resp["full_once"] = conf_obj.get("FULL_ONCE", 0)
        return resp

    if method == "power.set" and os.path.exists(power_bin):
        key = str(req_data.get("key", "")).strip()
        val = req_data.get("value", "")
        if key in ("CHARGE_TO", "RESUME_AT", "SAVER_BELOW", "CHARGE_PAUSE", "FULL_ONCE"):
            proc = subprocess.run([power_bin, "set", key, str(val)], capture_output=True, text=True, timeout=6)
            if proc.returncode != 0:
                return {"ok": 0, "error": (proc.stderr or proc.stdout or "power_set_failed").strip()}
            if key in ("CHARGE_TO", "RESUME_AT", "CHARGE_PAUSE", "FULL_ONCE") and req_data.get("reload", True):
                _apply_power_guard_reload()
            else:
                BATTERY_INFO_CACHE["time"] = 0
                OVERVIEW_API_CACHE["time"] = 0
            return {"ok": 1, "conf": _read_power_conf_dict()}

    if method == "power.control":
        if not os.path.exists(power_bin):
            return {"ok": 0, "error": "mu300-power 未安装"}
        action = str(req_data.get("action", "")).strip()
        steps = []
        msg = "电池养护配置已生效"
        if action == "preset_server_60":
            steps = [("set", "CHARGE_TO", "60"), ("set", "RESUME_AT", "50"), ("set", "CHARGE_PAUSE", "0"), ("set", "FULL_ONCE", "0")]
            msg = "已切换至「长期插电养护模式」(60% 停充零循环旁路 / 低于 50% 涓流补电)"
        elif action == "preset_portable_80":
            steps = [("set", "CHARGE_TO", "80"), ("set", "RESUME_AT", "70"), ("set", "CHARGE_PAUSE", "0"), ("set", "FULL_ONCE", "0")]
            msg = "已切换至「日常随身均衡模式」(80% 停充旁路 / 低于 70% 自动回充)"
        elif action == "full_once":
            steps = [("charge", "full")]
            msg = "已开启「出门前单次充满 100%」，充满拔电后自动恢复日常养护上限"
        elif action == "cancel_full_once":
            steps = [("set", "FULL_ONCE", "0")]
            msg = "已取消单次充满，恢复按设定上限停充保护"
        elif action == "pause":
            steps = [("charge", "pause")]
            msg = "已手动暂停充电（插电状态直供主板，电池零循环）"
        elif action == "resume":
            steps = [("charge", "resume")]
            msg = "已恢复自动充电护航策略"
        else:
            if "charge_to" in req_data:
                ct = max(50, min(100, int(req_data["charge_to"])))
                steps.append(("set", "CHARGE_TO", str(ct)))
                if "resume_at" in req_data:
                    ra = max(0, min(99, int(req_data["resume_at"])))
                    if ct < 100 and ra >= ct:
                        ra = max(40, ct - 5)
                    elif ct == 100:
                        ra = 0
                    steps.append(("set", "RESUME_AT", str(ra)))
            elif "resume_at" in req_data:
                ra = max(0, min(99, int(req_data["resume_at"])))
                steps.append(("set", "RESUME_AT", str(ra)))
            if "saver_below" in req_data:
                sb = max(5, min(50, int(req_data["saver_below"])))
                steps.append(("set", "SAVER_BELOW", str(sb)))

        for st_cmd in steps:
            proc = subprocess.run([power_bin] + list(st_cmd), capture_output=True, text=True, timeout=6)
            if proc.returncode != 0:
                return {"ok": 0, "error": (proc.stderr or proc.stdout or "power_control_failed").strip()}

        _apply_power_guard_reload()
        return {
            "ok": 1,
            "msg": msg,
            "conf": _read_power_conf_dict(),
            "battery": get_battery_info(force=True),
        }

    return None

# =========================================================================
# Phase 1: 双卡数字档案、无损触点检测、Ubuntu 在线热切/闪切接码、按 ICCID 独立流量账本、
#          5 步蜂窝健康诊断、短信 OTP 识别、Bark/TG/Webhook 推送与信令语音保号
# =========================================================================
SIM_CARDS_FILE = "/etc/mu300/sim-cards.json"
SIM_SLOT_CACHE_FILE = "/etc/mu300/sim-slot-cache.json"
COMM_PUSH_CONF_FILE = "/etc/mu300/webui-comm-push.json"
COMM_PUSH_SEEN_FILE = "/etc/mu300/webui-comm-seen.json"
COMM_EVENTS_FILE = "/run/mu300/webui-comm-events.json"

AT_SLOT_LOCKS = [AT_LOCK, threading.Lock()]
SIM_SWITCH_LOCK = threading.Lock()
FLASH_CANCEL_EVENT = threading.Event()
TRAFFIC_SWAP_LOCK = threading.Lock()
TRAFFIC_API_CACHE = {"time": 0.0, "data": None}

SIM_SWITCH_STATE = {
    "running": False,
    "phase": "idle",
    "from_slot": 1,
    "target_slot": 1,
    "started_at": 0.0,
    "finished_at": 0.0,
    "ok": None,
    "msg": "",
    "error": "",
    "reason": "",
}

FLASH_SWITCH_STATE = {
    "running": False,
    "phase": "idle",
    "orig_slot": 1,
    "target_slot": 2,
    "duration_sec": 120,
    "started_at": 0.0,
    "deadline": 0.0,
    "finished_at": 0.0,
    "return_on_sms": True,
    "radio_only": True,
    "captured_sms": [],
    "msg": "",
    "error": "",
}

EUICC_ISDR_AIDS = (
    "A0000005591010FFFFFFFF8900000100",
    "A0000005591010FFFFFFFF8900050500",
)

def mask_iccid(iccid):
    s = re.sub(r"[^0-9A-Fa-f]", "", str(iccid or "")).upper()
    if len(s) >= 12:
        return f"{s[:6]}••••••{s[-4:]}"
    return s

def mask_imsi(imsi):
    s = re.sub(r"[^0-9]", "", str(imsi or ""))
    if len(s) >= 10:
        return f"{s[:5]}••••••{s[-2:]}"
    return s

def guess_carrier_from_ids(iccid="", imsi="", oper_code=""):
    imsi_s = re.sub(r"[^0-9]", "", str(imsi or ""))
    iccid_s = re.sub(r"[^0-9A-Fa-f]", "", str(iccid or "")).upper()
    plmn = str(oper_code or "")[:5] or imsi_s[:5]
    plmn_map = {
        "46000": "中国移动", "46002": "中国移动", "46004": "中国移动", "46007": "中国移动", "46008": "中国移动", "46013": "中国移动",
        "46001": "中国联通", "46006": "中国联通", "46009": "中国联通",
        "46003": "中国电信", "46005": "中国电信", "46011": "中国电信",
        "46015": "中国广电",
        "45400": "CSL HK", "45403": "3HK", "45406": "SmarTone", "45412": "中国移动香港", "45416": "PCCW-HKT",
        "45501": "CTM 澳门电讯", "46692": "中华电信",
        "31026": "T-Mobile US", "31024": "T-Mobile US", "31041": "AT&T US", "31015": "AT&T US", "31148": "Verizon US",
        "23410": "O2 / giffgaff UK", "23415": "Vodafone UK", "23430": "EE UK",
        "44010": "NTT Docomo", "44020": "SoftBank", "44050": "KDDI au",
    }
    if plmn in plmn_map:
        return plmn_map[plmn]
    if iccid_s.startswith(("898600", "898602", "898604", "898607", "898608")):
        return "中国移动"
    if iccid_s.startswith(("898601", "898606", "898609")):
        return "中国联通"
    if iccid_s.startswith(("898603", "898611")):
        return "中国电信"
    if iccid_s.startswith("898615"):
        return "中国广电"
    if iccid_s.startswith("89852"):
        return "中国香港运营商"
    if iccid_s.startswith("8901"):
        return "美国运营商 (US)"
    if iccid_s.startswith("8944"):
        return "英国运营商 (UK)"
    return ""

def extract_sms_otp(text):
    """智能提取短信中的 4~8 位数字验证码 (兼容中英文常见模板，避开手机号与年份)"""
    msg = str(text or "")
    if not msg:
        return ""
    kw_re = re.compile(
        r"(?:验证码|校验码|动态码|确认码|安全码|认证码|随机码|口令|verification\s*code|security\s*code|auth\s*code|passcode|otp|code|pin)",
        re.IGNORECASE,
    )
    if not kw_re.search(msg):
        return ""
    # 优先匹配紧邻关键词前后的 4~8 位验证码
    near_patterns = [
        r"(?:验证码|校验码|动态码|确认码|安全码|认证码|随机码|code|otp|pin|passcode)(?:\s*(?:is|for|为|是))?[\s:：\-]*([0-9]{4,8})(?![0-9])",
        r"(?<![0-9])([0-9]{4,8})[\s,，。.)）]*(?:为|是)?(?:您的|本次|登录|注册|动态)?(?:验证码|校验码|动态码|确认码|verification\s*code|otp)",
    ]
    for pat in near_patterns:
        m = re.search(pat, msg, re.IGNORECASE)
        if m:
            cand = m.group(1)
            if not re.fullmatch(r"202[0-9]", cand):
                return cand
    # 兜底：移除 URL、手机号与日期后再寻找独立 4~8 位数字
    cleaned = re.sub(r"https?://\S+", " ", msg)
    cleaned = re.sub(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])", " ", cleaned)
    cleaned = re.sub(r"20[2-3][0-9][-/年.][0-1]?[0-9][-/月.][0-3]?[0-9]", " ", cleaned)
    for m in re.finditer(r"(?<![0-9.])([0-9]{4,8})(?![0-9]|\.[0-9])", cleaned):
        cand = m.group(1)
        if not re.fullmatch(r"202[0-9]", cand):
            return cand
    return ""

def _load_json_dict(path, default=None):
    if default is None:
        default = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return dict(default)

def load_sim_cards_db():
    return _load_json_dict(SIM_CARDS_FILE, {"cards": {}})

def save_sim_cards_db(db):
    if not isinstance(db, dict):
        db = {"cards": {}}
    if not isinstance(db.get("cards"), dict):
        db["cards"] = {}
    _atomic_write_text(SIM_CARDS_FILE, json.dumps(db, ensure_ascii=False, indent=2), mode=0o600)

def load_sim_slot_cache():
    return _load_json_dict(SIM_SLOT_CACHE_FILE, {"0": {}, "1": {}})

def save_sim_slot_cache(cache_obj):
    _atomic_write_text(SIM_SLOT_CACHE_FILE, json.dumps(cache_obj, ensure_ascii=False, indent=2), mode=0o600)

def _bcd_swap_decode(hex_str):
    h = re.sub(r"[^0-9A-Fa-f]", "", str(hex_str or "")).upper()
    out = []
    for i in range(0, len(h) - 1, 2):
        b = h[i:i + 2]
        out.append(b[1] + b[0])
    return "".join(out).rstrip("F")

def _classify_uicc_aid(aid_hex):
    a = str(aid_hex or "").upper()
    if a.startswith("A0000005591010"):
        return "ISD-R(eUICC)"
    if a.startswith("A0000000871002"):
        return "USIM"
    if a.startswith("A0000000871004"):
        return "ISIM"
    return "UICC"

def _parse_ef_dir_aids(hex_payload):
    """解析 EF_DIR (2F00) TLV 中的 4F <len> <AID> 应用标识符列表，返回 (aid_items, is_euicc)"""
    chunks = hex_payload if isinstance(hex_payload, (list, tuple)) else [hex_payload]
    seen_hex = set()
    aid_items = []
    is_euicc = False
    for item in chunks:
        h = re.sub(r"[^0-9A-Fa-f]", "", str(item or "")).upper()
        idx = 0
        while idx + 4 <= len(h):
            pos = h.find("4F", idx)
            if pos < 0 or (pos % 2 != 0):
                idx = (pos + 1) if pos >= 0 else len(h)
                continue
            if pos + 4 > len(h):
                break
            ln = int(h[pos + 2:pos + 4], 16)
            end = pos + 4 + ln * 2
            if 5 <= ln <= 16 and end <= len(h):
                aid = h[pos + 4:end]
                if aid.startswith("A000000") and aid not in seen_hex:
                    seen_hex.add(aid)
                    atype = _classify_uicc_aid(aid)
                    if atype == "ISD-R(eUICC)":
                        is_euicc = True
                    aid_items.append({"aid": aid, "type": atype})
                idx = end
            else:
                idx = pos + 2
    return aid_items, is_euicc

def _slot_broker_pipe(slot_idx):
    return "/run/mu300-at4/cmd" if int(slot_idx) == 1 else "/run/mu300-at/cmd"

def _slot_at(slot_idx, cmd, timeout=4, lock_wait=6):
    """向指定卡槽的独立 AT 守护管道发送单条命令 (卡1: /run/mu300-at, 卡2: /run/mu300-at4)"""
    idx = 1 if int(slot_idx) == 1 else 0
    if os.path.exists("/run/mu300/sim-switching"):
        return False, "SIM_SWITCHING_BUSY"
    pipe_path = _slot_broker_pipe(idx)
    if not os.path.exists(pipe_path):
        return False, f"BROKER_NOT_READY:{pipe_path}"
    at_bin = "/opt/mu300/bin/mu300-at"
    if not os.path.exists(at_bin):
        return False, "MU300_AT_MISSING"
    lk = AT_SLOT_LOCKS[idx]
    if not lk.acquire(timeout=max(1.5, float(lock_wait))):
        return False, "AT_LOCK_BUSY"
    try:
        at_dir = "/run/mu300-at4" if idx == 1 else "/run/mu300-at"
        env = dict(
            os.environ,
            MU300_AT_DIR=at_dir,
            MU300_SIM_SLOT=str(idx),
            MU300_AT_LOCK_WAIT=str(int(lock_wait)),
        )
        proc = subprocess.run(
            [at_bin, "-t", str(max(1, min(30, int(timeout)))), cmd],
            capture_output=True,
            text=True,
            timeout=max(4, int(timeout) + int(lock_wait) + 4),
            env=env,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).replace("\r", "").strip()
        return proc.returncode == 0, out
    except Exception as e:
        return False, str(e)
    finally:
        lk.release()

def ensure_dual_sim_brokers_ready(save_hot=True):
    """在已启动的 Ubuntu U30 Air 上无损拉起 mu300-atd4/atd5 双卡通道（零重启、不断流）"""
    prep_bin = "/opt/mu300/bin/mu300-sim-boot-prep"
    if not os.path.exists(prep_bin):
        return False, "mu300-sim-boot-prep 未安装"
    args = [prep_bin, "enable-live"]
    if not save_hot:
        args.append("--no-save")
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
        out = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode == 0, out
    except Exception as e:
        return False, str(e)

def probe_sim_slot(slot_num, power_sim=False, check_euicc=True):
    """
    无损检测指定卡槽 (1 或 2) 的 8 脚物理触点、CPIN 鉴权、ICCID、运营商及 eUICC (9eSIM/ESTK/5ber ISD-R) 能力。
    - 当检测非活动卡槽时，全程仅操作该卡独立 AT 通道 (不切射频、不断当前卡 1 的 5G 网络与 Tailscale)。
    """
    s_num = 2 if int(slot_num) == 2 else 1
    idx = s_num - 1
    active_idx = get_active_sim_slot_idx()
    is_active = (idx == active_idx)
    checks = []

    # 1. 检查 AT 通道守护进程
    pipe_path = _slot_broker_pipe(idx)
    if not os.path.exists(pipe_path) and idx == 1 and power_sim:
        ensure_dual_sim_brokers_ready(save_hot=True)
    broker_ok = os.path.exists(pipe_path)
    checks.append({
        "step": "broker",
        "title": "AT 独立通道守护",
        "ok": broker_ok,
        "detail": f"{pipe_path} 在线" if broker_ok else f"{pipe_path} 未就绪（可点击开启双卡热备自动拉起）",
    })
    if not broker_ok:
        return {
            "ok": 0,
            "slot": s_num,
            "active": is_active,
            "pin_ok": False,
            "cpin": "BROKER_DOWN",
            "checks": checks,
            "summary": f"SIM {s_num} 独立 AT 守护通道尚未就绪",
        }

    # 2. 读取当前射频/功能栈状态 CFUN
    ok_cfun, out_cfun = _slot_at(idx, "AT+CFUN?", timeout=4)
    m_cfun = re.search(r"\+CFUN:\s*(\d+)", out_cfun)
    cfun_val = int(m_cfun.group(1)) if m_cfun else None
    checks.append({
        "step": "cfun",
        "title": "基带协议栈响应",
        "ok": bool(ok_cfun and cfun_val is not None),
        "detail": f"CFUN={cfun_val} ({'主用全功能射频' if cfun_val == 1 else ('仅 SIM 接口上电' if cfun_val == 2 else '待机零射频')})" if cfun_val is not None else (out_cfun[:80] or "无响应"),
    })

    # 3. 读取 SIM 触点状态 CPIN (若为待机副卡且 CFUN=0 未给卡槽供电，按需发送 AT+SFUN=2 单独给 SIM 触点上电，不影响主卡上网)
    ok_cpin, out_cpin = _slot_at(idx, "AT+CPIN?", timeout=4)
    cpin_state = "UNKNOWN"
    if "+CPIN: READY" in out_cpin:
        cpin_state = "READY"
    elif re.search(r"\+CPIN:\s*(SIM PIN|SIM PUK)", out_cpin):
        cpin_state = "LOCKED"
    elif re.search(r"(\+CME ERROR:\s*(10|13)|NOT INSERTED)", out_cpin, re.IGNORECASE):
        cpin_state = "ABSENT"

    powered_up_sim = False
    if cpin_state != "READY" and (not is_active) and power_sim:
        _slot_at(idx, "AT+SFUN=2", timeout=10, lock_wait=8)
        time.sleep(1.2)
        powered_up_sim = True
        ok_cpin, out_cpin = _slot_at(idx, "AT+CPIN?", timeout=4)
        if "+CPIN: READY" in out_cpin:
            cpin_state = "READY"
        elif re.search(r"\+CPIN:\s*(SIM PIN|SIM PUK)", out_cpin):
            cpin_state = "LOCKED"
        elif re.search(r"(\+CME ERROR:\s*(10|13)|NOT INSERTED)", out_cpin, re.IGNORECASE):
            cpin_state = "ABSENT"

    pin_ok = cpin_state in ("READY", "LOCKED")
    checks.append({
        "step": "cpin",
        "title": "8 脚卡槽触点 (VCC/RST/CLK/IO)",
        "ok": pin_ok,
        "detail": (
            f"触点导通正常 (+CPIN: {cpin_state})" + (" [已独立上电检测，主卡不断网]" if powered_up_sim else "")
            if pin_ok else f"未检测到有效卡片响应 ({out_cpin.splitlines()[-1] if out_cpin else '无应答'}) — 改装后请检查 VCC/RST/CLK/IO 焊点与卡托方向"
        ),
    })

    iccid = ""
    imsi = ""
    aids = []
    is_euicc = False

    if cpin_state == "READY":
        # 4. 读取 ICCID (优先 AT+CCID，兜底 CRSM EF_ICCID 2FE2)
        _, out_ccid = _slot_at(idx, "AT+CCID", timeout=4)
        m_ccid = re.search(r"\+CCID:\s*([0-9A-Fa-f]{15,22})", out_ccid)
        if not m_ccid:
            m_ccid = re.search(r"(?<![0-9A-Fa-f])(89[0-9A-Fa-f]{16,20})(?![0-9A-Fa-f])", out_ccid)
        if m_ccid:
            iccid = m_ccid.group(1).upper().rstrip("F")
        if not iccid:
            _, out_crsm_iccid = _slot_at(idx, "AT+CRSM=176,12258,0,0,10", timeout=4)
            m_hex = re.search(r'\+CRSM:\s*144,\s*0,\s*"?([0-9A-Fa-f]{16,20})"?', out_crsm_iccid)
            if m_hex:
                iccid = _bcd_swap_decode(m_hex.group(1))

        # 5. 读取 IMSI
        _, out_cimi = _slot_at(idx, "AT+CIMI", timeout=4)
        m_imsi = re.search(r"(?<![0-9])([0-9]{14,15})(?![0-9])", out_cimi)
        if m_imsi:
            imsi = m_imsi.group(1)

        checks.append({
            "step": "identity",
            "title": "卡片身份标识 (ICCID / IMSI)",
            "ok": bool(iccid),
            "detail": f"ICCID: {mask_iccid(iccid)} · IMSI: {mask_imsi(imsi) or '--'}" if iccid else "未能读出 EF_ICCID",
        })

        # 6. 读取 EF_DIR 应用目录与 ISD-R (eUICC 转接卡识别：9eSIM / ESTK / 5ber)
        raw_dir_recs = []
        for rec_no in (1, 2, 3, 4):
            _, out_dir = _slot_at(idx, f"AT+CRSM=178,12032,{rec_no},4,0", timeout=4)
            m_dir = re.search(r'\+CRSM:\s*144,\s*0,\s*"?([0-9A-Fa-f]+)"?', out_dir)
            if m_dir:
                raw_dir_recs.append(m_dir.group(1))
        aids, dir_euicc = _parse_ef_dir_aids(raw_dir_recs)
        if dir_euicc:
            is_euicc = True

        if check_euicc and not is_euicc:
            seen_aid_set = {a["aid"] for a in aids if isinstance(a, dict)}
            for isdr_aid in EUICC_ISDR_AIDS:
                _, out_ccho = _slot_at(idx, f'AT+CCHO="{isdr_aid}"', timeout=5)
                m_ch = re.search(r"\+CCHO:\s*(\d+)", out_ccho)
                if m_ch:
                    ch_id = m_ch.group(1)
                    is_euicc = True
                    if isdr_aid not in seen_aid_set:
                        aids.append({"aid": isdr_aid, "type": "ISD-R(eUICC)"})
                    _slot_at(idx, f"AT+CCHC={ch_id}", timeout=4)
                    break

        first_aid_str = (aids[0]["aid"][:16] + "...") if aids else ""
        checks.append({
            "step": "euicc",
            "title": "USIM / eUICC (ISD-R) 应用能力",
            "ok": True,
            "detail": (
                "检测到 GSMA ISD-R 通道！支持 9eSIM / ESTK / 5ber 实体 eSIM 管理"
                if is_euicc else f"标准实体 USIM 卡 (AID: {first_aid_str})" if first_aid_str else "标准 SIM/USIM 卡"
            ),
        })

    carrier = guess_carrier_from_ids(iccid, imsi)
    cache_obj = load_sim_slot_cache()
    prev_slot_cache = cache_obj.get(str(idx), {}) if isinstance(cache_obj.get(str(idx)), dict) else {}
    slot_record = {
        "cpin": cpin_state,
        "cfun": cfun_val,
        "iccid": iccid or (prev_slot_cache.get("iccid", "") if cpin_state == "UNKNOWN" else ""),
        "imsi_masked": mask_imsi(imsi) if imsi else prev_slot_cache.get("imsi_masked", ""),
        "imsi_plmn": imsi[:5] if len(imsi) >= 5 else prev_slot_cache.get("imsi_plmn", ""),
        "carrier": carrier or prev_slot_cache.get("carrier", ""),
        "is_euicc": bool(is_euicc),
        "aids": aids,
        "probed_at": int(time.time()),
    }
    cache_obj[str(idx)] = slot_record
    save_sim_slot_cache(cache_obj)

    if is_active and iccid:
        sync_iccid_traffic_ledger(iccid, idx)

    contact_ok = bool((cpin_state == "READY" and iccid) or cpin_state == "LOCKED")
    return {
        "ok": 1,
        "slot": s_num,
        "active": is_active,
        "pin_ok": pin_ok,
        "contact_ok": contact_ok,
        "cpin": cpin_state,
        "cfun": cfun_val,
        "iccid": iccid,
        "iccid_masked": mask_iccid(iccid),
        "imsi_masked": mask_imsi(imsi),
        "carrier": slot_record["carrier"],
        "is_euicc": bool(is_euicc),
        "aids": aids,
        "checks": checks,
        "summary": (
            f"SIM {s_num} 触点与鉴权全部正常 ({slot_record['carrier'] or '已识别'} · {mask_iccid(iccid)})"
            if cpin_state == "READY" and iccid
            else (f"SIM {s_num} 触点导通，但开启了 PIN 锁" if cpin_state == "LOCKED" else f"SIM {s_num} 未检测到卡片 ({cpin_state})")
        ),
    }

# -------------------------------------------------------------------------
# 按 ICCID 独立隔离的流量账本与套餐策略 (自动随切卡/换卡无缝切换)
# -------------------------------------------------------------------------
def get_traffic_store_dir():
    for d in ("/mnt/mu300-disk", ""):
        cand = f"{d}/.mu300/traffic" if d else "/.mu300/traffic"
        if os.path.isdir(f"{d}/.mu300" if d else "/.mu300"):
            return cand
    return "/etc/mu300/traffic"

def _parse_kv_file(path):
    res = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    res[k.strip()] = v.strip().strip("'\"")
        except Exception:
            pass
    return res

def _cycle_start_ymd(reset_day):
    import calendar
    import datetime
    today = datetime.date.today()
    rd = max(1, min(31, int(reset_day or 1)))
    max_this = calendar.monthrange(today.year, today.month)[1]
    eff_this = min(rd, max_this)
    if today.day >= eff_this:
        return datetime.date(today.year, today.month, eff_this).strftime("%Y-%m-%d")
    if today.month == 1:
        py, pm = today.year - 1, 12
    else:
        py, pm = today.year, today.month - 1
    eff_prev = min(rd, calendar.monthrange(py, pm)[1])
    return datetime.date(py, pm, eff_prev).strftime("%Y-%m-%d")

def read_iccid_traffic_summary(iccid, is_active=False, force=False):
    """读取指定 ICCID 的流量账本摘要（当前活动卡直接调 mu300-traffic 实时统计，非活动卡从归档账本零进程秒读）"""
    if is_active:
        now = time.time()
        if not force and TRAFFIC_API_CACHE["data"] and (now - TRAFFIC_API_CACHE["time"] < 4.0):
            raw = TRAFFIC_API_CACHE["data"]
        else:
            raw = run_mu300_api("traffic.get")
            if isinstance(raw, dict) and raw.get("ok"):
                TRAFFIC_API_CACHE["time"] = now
                TRAFFIC_API_CACHE["data"] = raw
        t_obj = raw.get("traffic") if isinstance(raw, dict) and isinstance(raw.get("traffic"), dict) else {}
        cyc = t_obj.get("cycle") if isinstance(t_obj.get("cycle"), dict) else {}
        tdy = t_obj.get("today") if isinstance(t_obj.get("today"), dict) else {}
        cap_obj = t_obj.get("cap") if isinstance(t_obj.get("cap"), dict) else {}
        dly_obj = t_obj.get("daily") if isinstance(t_obj.get("daily"), dict) else {}
        used_b = int(
            cyc.get(
                "used",
                cyc.get(
                    "counted",
                    cyc.get("total", (int(cyc.get("rx", 0) or 0) + int(cyc.get("tx", 0) or 0))),
                ),
            )
            or 0
        )
        today_b = int(
            dly_obj.get(
                "used",
                tdy.get("total", (int(tdy.get("rx", 0) or 0) + int(tdy.get("tx", 0) or 0))),
            )
            or 0
        )
        cap_b = int(cap_obj.get("bytes", 0) or 0)
        pct = round((used_b / cap_b) * 100.0, 1) if cap_b > 0 else 0.0
        return {
            "active": True,
            "today_bytes": today_b,
            "cycle_bytes": used_b,
            "cap_bytes": cap_b,
            "daily_cap_bytes": int(dly_obj.get("bytes", 0) or 0),
            "pct": pct,
            "used_pct": pct,
            "warn_pct": int(cap_obj.get("warn", cap_obj.get("warn_pct", 90)) or 90),
            "reset_day": int(t_obj.get("reset_day", 1) or 1),
            "plan": str(t_obj.get("plan") or ""),
            "device": str(t_obj.get("device") or ""),
            "cut_active": bool(cap_obj.get("cut_active") or dly_obj.get("cut_active")),
        }

    clean_id = re.sub(r"[^0-9A-Fa-f]", "", str(iccid or "")).upper()
    if not clean_id:
        return {"active": False, "today_bytes": 0, "cycle_bytes": 0, "cap_bytes": 0, "daily_cap_bytes": 0, "pct": 0.0, "used_pct": 0.0, "warn_pct": 90, "reset_day": 1, "plan": ""}
    arc_dir = os.path.join(get_traffic_store_dir(), "by-iccid", clean_id)
    conf = _parse_kv_file(os.path.join(arc_dir, "conf"))
    reset_day = int(conf.get("RESET_DAY", 1) or 1)
    cap_b = int(conf.get("CAP", 0) or 0)
    daily_b = int(conf.get("DAILY", 0) or 0)
    warn_pct = int(conf.get("WARN", 90) or 90)
    plan = str(conf.get("PLAN", "") or "")
    start_ymd = _cycle_start_ymd(reset_day)
    today_ymd = time.strftime("%Y-%m-%d")
    cyc_total = 0
    tdy_total = 0
    ledger_p = os.path.join(arc_dir, "ledger")
    if os.path.exists(ledger_p):
        try:
            with open(ledger_p, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 4 and parts[0] in ("day", "d"):
                        d_str = parts[1]
                        rx_b, tx_b = int(parts[2]), int(parts[3])
                        if d_str >= start_ymd:
                            cyc_total += (rx_b + tx_b)
                        if d_str == today_ymd:
                            tdy_total += (rx_b + tx_b)
                    elif len(parts) >= 4 and parts[0] == "pending":
                        cyc_total += (int(parts[2]) + int(parts[3]))
                        tdy_total += (int(parts[2]) + int(parts[3]))
        except Exception:
            pass
    pct = round((cyc_total / cap_b) * 100.0, 1) if cap_b > 0 else 0.0
    return {
        "active": False,
        "today_bytes": tdy_total,
        "cycle_bytes": cyc_total,
        "cap_bytes": cap_b,
        "daily_cap_bytes": daily_b,
        "pct": pct,
        "used_pct": pct,
        "warn_pct": warn_pct,
        "reset_day": reset_day,
        "plan": plan,
        "device": conf.get("DEVICE", ""),
        "cut_active": False,
    }

def _build_traffic_conf_from_card_profile(card_prof, device_name):
    lines = []
    rd = max(1, min(31, int(card_prof.get("reset_day", 1) or 1)))
    cap_gb = float(card_prof.get("cap_gb", 0) or 0)
    daily_gb = float(card_prof.get("daily_gb", 0) or 0)
    warn_pct = max(1, min(100, int(card_prof.get("warn_pct", 90) or 90)))
    action = str(card_prof.get("over_limit_action", "none") or "none")
    cut = 1 if action == "cut_data" else 0
    plan = re.sub(r"[\r\n\t]", " ", str(card_prof.get("nickname", "") or "")).strip()[:64]
    lines.append(f"RESET_DAY={rd}")
    lines.append(f"CAP={int(round(cap_gb * 1073741824)) if cap_gb > 0 else 0}")
    lines.append(f"WARN={warn_pct}")
    lines.append(f"CUT={cut}")
    lines.append(f"DAILY={int(round(daily_gb * 1073741824)) if daily_gb > 0 else 0}")
    lines.append(f"DAILY_CUT={cut}")
    lines.append("COUNT=total")
    lines.append(f"PLAN={plan}")
    lines.append(f"DEVICE={device_name}")
    return "\n".join(lines) + "\n"

def sync_iccid_traffic_ledger(active_iccid, slot_idx=None):
    """
    当检测到活动 SIM 卡的 ICCID 或卡槽变化时：
    1. 将原卡账本归档至 .mu300/traffic/by-iccid/<old_iccid>/
    2. 恢复新卡专属账本（首次升级自动继承原卡既有历史流量，新换上的副卡使用干净独立账本）
    3. 自动同步统计网卡 DEVICE=sipa_eth0 (SIM1) 或 sipa_eth8 (SIM2)
    """
    clean_id = re.sub(r"[^0-9A-Fa-f]", "", str(active_iccid or "")).upper()
    if len(clean_id) < 15:
        return False
    idx = get_active_sim_slot_idx() if slot_idx is None else int(slot_idx)
    target_dev = "sipa_eth8" if idx == 1 else "sipa_eth0"
    store = get_traffic_store_dir()
    by_dir = os.path.join(store, "by-iccid")
    active_marker = os.path.join(store, "active-iccid")
    tr_bin = "/opt/mu300/bin/mu300-traffic"

    with TRAFFIC_SWAP_LOCK:
        os.makedirs(by_dir, exist_ok=True)
        prev_id = ""
        if os.path.exists(active_marker):
            try:
                with open(active_marker, "r", encoding="utf-8", errors="ignore") as f:
                    prev_id = re.sub(r"[^0-9A-Fa-f]", "", f.read()).upper()
            except Exception:
                prev_id = ""

        conf_path = os.path.join(store, "conf")
        ledger_path = os.path.join(store, "ledger")

        if not prev_id:
            # 首次接管当前主卡：零损保留当前账本，建立当前 ICCID 绑定并确保 DEVICE 对齐
            cur_dir = os.path.join(by_dir, clean_id)
            os.makedirs(cur_dir, exist_ok=True)
            for fn in ("conf", "ledger"):
                src_p = os.path.join(store, fn)
                if os.path.exists(src_p):
                    try:
                        with open(src_p, "r", encoding="utf-8", errors="ignore") as sf:
                            _atomic_write_text(os.path.join(cur_dir, fn), sf.read(), mode=0o644)
                    except Exception:
                        pass
            _atomic_write_text(active_marker, clean_id + "\n", mode=0o644)
            cur_conf = _parse_kv_file(conf_path)
            if cur_conf.get("DEVICE", "sipa_eth0") != target_dev and os.path.exists(tr_bin):
                subprocess.run([tr_bin, "set", "device", target_dev], capture_output=True, timeout=6)
                TRAFFIC_API_CACHE["time"] = 0
            return True

        if prev_id == clean_id:
            cur_conf = _parse_kv_file(conf_path)
            if cur_conf.get("DEVICE", "sipa_eth0") != target_dev and os.path.exists(tr_bin):
                subprocess.run([tr_bin, "set", "device", target_dev], capture_output=True, timeout=6)
                TRAFFIC_API_CACHE["time"] = 0
            return True

        # ICCID 发生切换：落盘并归档旧卡 -> 装载新卡独立账本 -> 切换统计网卡
        if os.path.exists(tr_bin):
            subprocess.run([tr_bin, "save"], capture_output=True, timeout=8)
        subprocess.run(["systemctl", "stop", "mu300-traffic.service"], capture_output=True, timeout=8)
        try:
            old_dir = os.path.join(by_dir, prev_id)
            os.makedirs(old_dir, exist_ok=True)
            for fn in ("conf", "ledger"):
                src_p = os.path.join(store, fn)
                if os.path.exists(src_p):
                    with open(src_p, "r", encoding="utf-8", errors="ignore") as sf:
                        _atomic_write_text(os.path.join(old_dir, fn), sf.read(), mode=0o644)

            new_dir = os.path.join(by_dir, clean_id)
            os.makedirs(new_dir, exist_ok=True)
            new_conf_p = os.path.join(new_dir, "conf")
            new_ledger_p = os.path.join(new_dir, "ledger")

            cards_db = load_sim_cards_db().get("cards", {})
            card_prof = cards_db.get(clean_id, {}) if isinstance(cards_db.get(clean_id), dict) else {}

            if os.path.exists(new_conf_p):
                with open(new_conf_p, "r", encoding="utf-8", errors="ignore") as nf:
                    raw_c = nf.read()
                if re.search(r"(?m)^DEVICE=", raw_c):
                    raw_c = re.sub(r"(?m)^DEVICE=.*$", f"DEVICE={target_dev}", raw_c)
                else:
                    raw_c = raw_c.rstrip("\n") + f"\nDEVICE={target_dev}\n"
                _atomic_write_text(conf_path, raw_c, mode=0o644)
            else:
                _atomic_write_text(conf_path, _build_traffic_conf_from_card_profile(card_prof, target_dev), mode=0o644)

            if os.path.exists(new_ledger_p):
                with open(new_ledger_p, "r", encoding="utf-8", errors="ignore") as lf:
                    _atomic_write_text(ledger_path, lf.read(), mode=0o644)
            else:
                _atomic_write_text(ledger_path, "v 1\n", mode=0o644)

            for run_f in ("/run/mu300/traffic/ledger", "/run/mu300/traffic/summary"):
                if os.path.exists(run_f):
                    try:
                        os.remove(run_f)
                    except OSError:
                        pass

            _atomic_write_text(active_marker, clean_id + "\n", mode=0o644)
        finally:
            subprocess.run(["systemctl", "start", "mu300-traffic.service"], capture_output=True, timeout=8)
            TRAFFIC_API_CACHE["time"] = 0
        return True

def save_sim_card_profile(req_data):
    """保存单张 SIM 卡 (按 ICCID 绑定) 的昵称、手机号、备注、账单日、流量上限与超额自动切卡策略"""
    iccid = re.sub(r"[^0-9A-Fa-f]", "", str(req_data.get("iccid", "") or "")).upper()
    slot_hint = req_data.get("slot")
    if len(iccid) < 15 and slot_hint in (1, 2, "1", "2"):
        slot_cache = load_sim_slot_cache().get(str(int(slot_hint) - 1), {})
        iccid = re.sub(r"[^0-9A-Fa-f]", "", str(slot_cache.get("iccid", "") or "")).upper()
    if len(iccid) < 15:
        return {"ok": 0, "error": "请先点击「检测卡槽触点」读取有效 ICCID 后再保存卡片档案"}

    db = load_sim_cards_db()
    cards = db.setdefault("cards", {})
    cur = dict(cards.get(iccid) or {})
    if "nickname" in req_data:
        cur["nickname"] = str(req_data.get("nickname") or "").strip()[:40]
    if "phone" in req_data:
        cur["phone"] = re.sub(r"[^0-9+\- ]", "", str(req_data.get("phone") or ""))[:24]
    if "carrier_custom" in req_data:
        cur["carrier_custom"] = str(req_data.get("carrier_custom") or "").strip()[:32]
    if "notes" in req_data:
        cur["notes"] = str(req_data.get("notes") or "").strip()[:120]
    if "reset_day" in req_data:
        cur["reset_day"] = max(1, min(31, int(req_data.get("reset_day") or 1)))
    if "cap_gb" in req_data:
        cur["cap_gb"] = max(0.0, round(float(req_data.get("cap_gb") or 0), 2))
    if "daily_gb" in req_data:
        cur["daily_gb"] = max(0.0, round(float(req_data.get("daily_gb") or 0), 2))
    if "warn_pct" in req_data:
        cur["warn_pct"] = max(1, min(100, int(req_data.get("warn_pct") or 90)))
    if "over_limit_action" in req_data:
        act = str(req_data.get("over_limit_action") or "none")
        if act in ("none", "cut_data", "switch_sim"):
            cur["over_limit_action"] = act
    cur["updated_at"] = int(time.time())
    cards[iccid] = cur
    save_sim_cards_db(db)

    # 若该 ICCID 正是当前主用上网卡，立即同步至 mu300-traffic；否则同步至其归档账本配置
    active_idx = get_active_sim_slot_idx()
    active_cache = load_sim_slot_cache().get(str(active_idx), {})
    active_iccid = re.sub(r"[^0-9A-Fa-f]", "", str(active_cache.get("iccid", "") or "")).upper()
    if active_iccid == iccid:
        sync_iccid_traffic_ledger(iccid, active_idx)
        cut_flag = "on" if cur.get("over_limit_action") == "cut_data" else "off"
        kv_pairs = [
            ("reset_day", int(cur.get("reset_day", 1) or 1)),
            ("cap", str(int(round(float(cur.get("cap_gb", 0) or 0) * 1073741824)))),
            ("daily", str(int(round(float(cur.get("daily_gb", 0) or 0) * 1073741824)))),
            ("warn", int(cur.get("warn_pct", 90) or 90)),
            ("cut", cut_flag),
            ("daily_cut", cut_flag),
            ("plan", str(cur.get("nickname", "") or "")),
        ]
        for k_name, v_val in kv_pairs:
            run_mu300_api("traffic.set", {"key": k_name, "value": v_val})
        arc_dir = os.path.join(get_traffic_store_dir(), "by-iccid", iccid)
        os.makedirs(arc_dir, exist_ok=True)
        conf_live = os.path.join(get_traffic_store_dir(), "conf")
        if os.path.exists(conf_live):
            with open(conf_live, "r", encoding="utf-8", errors="ignore") as cf:
                _atomic_write_text(os.path.join(arc_dir, "conf"), cf.read(), mode=0o644)
        TRAFFIC_API_CACHE["time"] = 0
    else:
        arc_dir = os.path.join(get_traffic_store_dir(), "by-iccid", iccid)
        os.makedirs(arc_dir, exist_ok=True)
        dev_guess = "sipa_eth8" if str(slot_hint) == "2" else "sipa_eth0"
        _atomic_write_text(os.path.join(arc_dir, "conf"), _build_traffic_conf_from_card_profile(cur, dev_guess), mode=0o644)

    return {"ok": 1, "iccid": iccid, "iccid_masked": mask_iccid(iccid), "profile": cur, "msg": "SIM 卡片档案与独立流量策略已保存"}

def _read_slot_apn_conf(slot_idx):
    p = "/etc/mu300/mobile-data-sim2.conf" if int(slot_idx) == 1 else "/etc/mu300/mobile-data.conf"
    kv = _parse_kv_file(p)
    roam_raw = str(kv.get("MU300_DATA_ROAMING", "1")).lower()
    return {
        "apn": kv.get("MU300_APN", ""),
        "pdp": kv.get("MU300_PDP_TYPE", "IPV4V6") or "IPV4V6",
        "auth": kv.get("MU300_APN_AUTH", "none") or "none",
        "user": kv.get("MU300_APN_USER", ""),
        "password_set": bool(kv.get("MU300_APN_PASSWORD")),
        "roaming": roam_raw not in ("0", "off", "false", "no"),
    }

def _invalidate_cellular_caches():
    CELLULAR_CACHE["time"] = 0
    CELLULAR_CACHE["static_time"] = 0
    CELLULAR_IP_CACHE["time"] = 0
    PUBLIC_IP_CACHE["time"] = 0
    PUBLIC_IP_CACHE["ip"] = ""
    TRAFFIC_API_CACHE["time"] = 0
    if "OVERVIEW_API_CACHE" in globals() and isinstance(globals()["OVERVIEW_API_CACHE"], dict):
        globals()["OVERVIEW_API_CACHE"]["time"] = 0

def get_sim_switch_status():
    now = time.time()
    with SIM_SWITCH_LOCK:
        sw = dict(SIM_SWITCH_STATE)
        fl = dict(FLASH_SWITCH_STATE)
        fl["captured_sms"] = list(FLASH_SWITCH_STATE.get("captured_sms") or [])
    if sw.get("running") and sw.get("started_at"):
        sw["elapsed_s"] = round(max(0.0, now - sw["started_at"]), 1)
    else:
        sw["elapsed_s"] = round(max(0.0, (sw.get("finished_at") or 0) - (sw.get("started_at") or 0)), 1)
    if fl.get("running") and fl.get("deadline"):
        fl["remaining_s"] = max(0, int(round(fl["deadline"] - now)))
    else:
        fl["remaining_s"] = 0
    return {"ok": 1, "active_slot": get_active_sim_slot_idx() + 1, "switch": sw, "flash": fl}

def get_sim_overview(refresh_active=False):
    """汇总双卡控制中心全量视图：热备状态、双卡触点/ICCID/eUICC、独立流量账本、APN、射频与 VoLTE/VoNR"""
    sim_res = run_mu300_api("sim.get")
    sim_info = sim_res.get("sim") if isinstance(sim_res, dict) and isinstance(sim_res.get("sim"), dict) else {}
    radio_res = run_mu300_api("radio.get")
    radio_info = radio_res.get("radio") if isinstance(radio_res, dict) and isinstance(radio_res.get("radio"), dict) else {}
    voice_res = run_mu300_api("voice.get")
    voice_info = voice_res.get("voice") if isinstance(voice_res, dict) and isinstance(voice_res.get("voice"), dict) else {}

    active_idx = get_active_sim_slot_idx()
    boot_slot = int(sim_info.get("default") or (active_idx + 1))

    slot_cache = load_sim_slot_cache()
    # 若当前活动卡尚未缓存 ICCID（或强制刷新），自动执行一次轻量读取并绑定独立流量账本
    active_cached = slot_cache.get(str(active_idx), {}) if isinstance(slot_cache.get(str(active_idx)), dict) else {}
    if (refresh_active or not active_cached.get("iccid")) and not os.path.exists("/run/mu300/sim-switching"):
        probe_sim_slot(active_idx + 1, power_sim=False, check_euicc=not bool(active_cached.get("probed_at")))
        slot_cache = load_sim_slot_cache()
    elif active_cached.get("iccid"):
        sync_iccid_traffic_ledger(active_cached["iccid"], active_idx)

    cards_db = load_sim_cards_db().get("cards", {})
    radio_enabled_arr = radio_info.get("enabled") if isinstance(radio_info.get("enabled"), list) and len(radio_info.get("enabled")) >= 2 else [True, True]
    voice_slots = voice_info.get("slots") if isinstance(voice_info.get("slots"), list) else []

    slots_out = []
    for idx in (0, 1):
        s_num = idx + 1
        c_rec = slot_cache.get(str(idx), {}) if isinstance(slot_cache.get(str(idx)), dict) else {}
        iccid = str(c_rec.get("iccid") or "")
        prof = dict(cards_db.get(iccid) or {}) if iccid else {}
        v_obj = voice_slots[idx] if idx < len(voice_slots) and isinstance(voice_slots[idx], dict) else {}
        is_act = (idx == active_idx)
        qos_oper = CELLULAR_CACHE["data"].get("operator_name", "") if is_act else ""
        carrier = prof.get("carrier_custom") or c_rec.get("carrier") or (qos_oper if is_act else "") or guess_carrier_from_ids(iccid, c_rec.get("imsi_plmn", ""))
        slots_out.append({
            "slot": s_num,
            "label": "外置 Nano 卡槽 (SIM 1)" if idx == 0 else "内置/改装小卡槽 (SIM 2)",
            "iface": "sipa_eth0" if idx == 0 else "sipa_eth8",
            "at_dir": "/run/mu300-at" if idx == 0 else "/run/mu300-at4",
            "broker_up": os.path.exists(_slot_broker_pipe(idx)),
            "active": is_act,
            "boot_default": (boot_slot == s_num),
            "radio_enabled": bool(radio_enabled_arr[idx]),
            "cpin": c_rec.get("cpin") or ("READY" if is_act else "UNPROBED"),
            "cfun": 1 if is_act else c_rec.get("cfun", 0),
            "iccid": iccid,
            "iccid_masked": mask_iccid(iccid),
            "imsi_masked": c_rec.get("imsi_masked", ""),
            "carrier": carrier,
            "is_euicc": bool(c_rec.get("is_euicc")),
            "aids": c_rec.get("aids") or [],
            "probed_at": c_rec.get("probed_at") or 0,
            "apn_conf": _read_slot_apn_conf(idx),
            "volte": bool(v_obj.get("volte", True)),
            "vonr": bool(v_obj.get("vonr", False)),
            "profile": {
                "nickname": prof.get("nickname", ""),
                "phone": prof.get("phone", ""),
                "carrier_custom": prof.get("carrier_custom", ""),
                "notes": prof.get("notes", ""),
                "reset_day": int(prof.get("reset_day", 1) or 1),
                "cap_gb": float(prof.get("cap_gb", 0) or 0),
                "daily_gb": float(prof.get("daily_gb", 0) or 0),
                "warn_pct": int(prof.get("warn_pct", 90) or 90),
                "over_limit_action": prof.get("over_limit_action", "none"),
            },
            "traffic": read_iccid_traffic_summary(iccid, is_active=is_act),
        })

    sw_status = get_sim_switch_status()
    return {
        "ok": 1,
        "supported": bool(sim_info.get("supported", 1)),
        "available": bool(sim_info.get("available", 0)),
        "hot": bool(sim_info.get("hot", 0)),
        "active_slot": active_idx + 1,
        "boot_slot": boot_slot,
        "airplane_mode": bool(radio_info.get("all_off", False)),
        "wan_user_down": os.path.exists("/run/mu300/wan-user-down"),
        "slots": slots_out,
        "switch": sw_status["switch"],
        "flash": sw_status["flash"],
    }

def start_live_sim_switch(target_slot, wan_wait=45, make_default=False, reason="manual"):
    """异步执行 Ubuntu 双卡热切换 (SIM1 sipa_eth0 <-> SIM2 sipa_eth8)，目标卡拨号超时自动回滚原卡保底"""
    t_slot = int(target_slot)
    if t_slot not in (1, 2):
        return {"ok": 0, "error": "无效的目标卡槽 (仅支持 1 或 2)"}
    w_wait = max(15, min(120, int(wan_wait or 45)))
    orig_slot = get_active_sim_slot_idx() + 1

    with SIM_SWITCH_LOCK:
        if SIM_SWITCH_STATE["running"] or FLASH_SWITCH_STATE["running"]:
            return {"ok": 0, "error": "当前已有切卡或闪切接码任务正在执行，请稍候"}
        SIM_SWITCH_STATE.update({
            "running": True,
            "phase": "preparing",
            "from_slot": orig_slot,
            "target_slot": t_slot,
            "started_at": time.time(),
            "finished_at": 0.0,
            "ok": None,
            "msg": f"正在准备切换至 SIM {t_slot} ...",
            "error": "",
            "reason": reason,
        })

    def _worker():
        try:
            ok_prep, prep_msg = ensure_dual_sim_brokers_ready(save_hot=True)
            if not ok_prep:
                with SIM_SWITCH_LOCK:
                    SIM_SWITCH_STATE.update({
                        "running": False,
                        "phase": "failed",
                        "finished_at": time.time(),
                        "ok": 0,
                        "error": f"双卡守护通道未就绪: {prep_msg}",
                        "msg": "切卡前自检未通过，保持原卡在线",
                    })
                return

            if os.path.exists("/opt/mu300/bin/mu300-traffic"):
                subprocess.run(["/opt/mu300/bin/mu300-traffic", "save"], capture_output=True, timeout=8)

            with SIM_SWITCH_LOCK:
                SIM_SWITCH_STATE["phase"] = "switching"
                SIM_SWITCH_STATE["msg"] = f"正在切换基带射频与数据路由至 SIM {t_slot}（若 {w_wait}s 内无法上网将自动回退至 SIM {orig_slot}）..."

            env = dict(os.environ, MU300_SIM_WAN_WAIT=str(w_wait))
            proc = subprocess.run(
                ["/opt/mu300/bin/mu300-sim", "switch", str(t_slot)],
                capture_output=True,
                text=True,
                timeout=w_wait + 95,
                env=env,
            )
            out_txt = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            _invalidate_cellular_caches()

            if proc.returncode == 0:
                if make_default:
                    subprocess.run(["/opt/mu300/bin/mu300-sim", "default", str(t_slot)], capture_output=True, timeout=10)
                probe_res = probe_sim_slot(t_slot, power_sim=False, check_euicc=False)
                new_iccid = probe_res.get("iccid", "")
                if new_iccid:
                    sync_iccid_traffic_ledger(new_iccid, t_slot - 1)
                update_public_ip_async()
                with SIM_SWITCH_LOCK:
                    SIM_SWITCH_STATE.update({
                        "running": False,
                        "phase": "done",
                        "finished_at": time.time(),
                        "ok": 1,
                        "msg": f"已成功切换至 SIM {t_slot} ({'sipa_eth8' if t_slot == 2 else 'sipa_eth0'}) 提供蜂窝网络！",
                        "error": "",
                    })
            else:
                # 回退后重新对齐原卡账本与缓存
                cur_slot = get_active_sim_slot_idx() + 1
                probe_res = probe_sim_slot(cur_slot, power_sim=False, check_euicc=False)
                if probe_res.get("iccid"):
                    sync_iccid_traffic_ledger(probe_res["iccid"], cur_slot - 1)
                update_public_ip_async()
                last_err = out_txt.splitlines()[-1] if out_txt else "目标卡未就绪或未能获取数据承载 IP"
                with SIM_SWITCH_LOCK:
                    SIM_SWITCH_STATE.update({
                        "running": False,
                        "phase": "rolled_back",
                        "finished_at": time.time(),
                        "ok": 0,
                        "msg": f"SIM {t_slot} 未能在 {w_wait}s 内建立数据连接，已安全自动回滚至 SIM {cur_slot}",
                        "error": last_err,
                    })
        except Exception as e:
            _invalidate_cellular_caches()
            with SIM_SWITCH_LOCK:
                SIM_SWITCH_STATE.update({
                    "running": False,
                    "phase": "failed",
                    "finished_at": time.time(),
                    "ok": 0,
                    "msg": "切卡任务执行异常",
                    "error": str(e),
                })

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": 1, "started": True, "target_slot": t_slot, "wan_wait": w_wait, "msg": f"已启动切换至 SIM {t_slot}"}

# -------------------------------------------------------------------------
# 短信 (含 OTP 提取与指定卡槽读取)、Bark / Telegram / Webhook 推送、来电与闪切接码
# -------------------------------------------------------------------------
def list_slot_sms(slot_num=None):
    s_idx = get_active_sim_slot_idx() if slot_num is None else (1 if int(slot_num) == 2 else 0)
    sms_bin = "/opt/mu300/bin/sms"
    if not os.path.exists(sms_bin):
        return {"ok": 0, "error": "sms 工具未安装", "messages": []}
    if os.path.exists("/run/mu300/sim-switching"):
        return {"ok": 1, "slot": s_idx + 1, "busy": True, "messages": []}
    at_dir = "/run/mu300-at4" if (s_idx == 1 and os.path.exists("/run/mu300-at4/cmd")) else "/run/mu300-at"
    env = dict(os.environ, MU300_SIM_SLOT=str(s_idx), MU300_AT_DIR=at_dir)
    try:
        proc = subprocess.run([sms_bin, "json"], capture_output=True, text=True, timeout=25, env=env)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "读取短信失败").strip()
            return {"ok": 0, "slot": s_idx + 1, "error": err, "messages": []}
        parsed = json.loads(proc.stdout or "{}")
        msgs = parsed.get("messages", []) if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])
        if not isinstance(msgs, list):
            msgs = []
        enriched = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            item = dict(m)
            idx_val = item.get("index")
            if "id" not in item:
                if isinstance(idx_val, list) and idx_val:
                    item["id"] = idx_val[0]
                elif idx_val is not None:
                    item["id"] = idx_val
            item["slot"] = s_idx + 1
            item["otp_code"] = extract_sms_otp(item.get("text", ""))
            enriched.append(item)
        enriched.sort(key=lambda x: str(x.get("time", "")), reverse=True)
        return {"ok": 1, "slot": s_idx + 1, "messages": enriched}
    except Exception as e:
        return {"ok": 0, "slot": s_idx + 1, "error": str(e), "messages": []}

def load_comm_push_conf():
    default_cfg = {
        "enabled": False,
        "bark_url": "",
        "tg_bot_token": "",
        "tg_chat_id": "",
        "webhook_url": "",
        "webhook_secret": "",
        "push_sms": True,
        "push_calls": True,
        "auto_hangup_calls": False,
    }
    cfg = _load_json_dict(COMM_PUSH_CONF_FILE, default_cfg)
    for k, v in default_cfg.items():
        cfg.setdefault(k, v)
    return cfg

def get_comm_push_public_view():
    cfg = load_comm_push_conf()
    events = _load_json_dict(COMM_EVENTS_FILE, {"items": []}).get("items", [])
    def _mask_secret_url(u):
        s = str(u or "").strip()
        if not s:
            return ""
        try:
            p = urllib.parse.urlsplit(s)
            path_s = p.path or ""
            if len(path_s) > 10:
                path_s = path_s[:6] + "••••" + path_s[-4:]
            return f"{p.scheme}://{p.netloc}{path_s}"
        except Exception:
            return s[:12] + "••••"

    tok = str(cfg.get("tg_bot_token") or "").strip()
    tok_masked = (tok[:6] + "••••" + tok[-4:]) if len(tok) > 12 else ("••••" if tok else "")
    return {
        "ok": 1,
        "config": {
            "enabled": bool(cfg.get("enabled")),
            "bark_configured": bool(cfg.get("bark_url")),
            "bark_preview": _mask_secret_url(cfg.get("bark_url")),
            "tg_configured": bool(tok and cfg.get("tg_chat_id")),
            "tg_token_preview": tok_masked,
            "tg_chat_id": str(cfg.get("tg_chat_id") or ""),
            "webhook_configured": bool(cfg.get("webhook_url")),
            "webhook_preview": _mask_secret_url(cfg.get("webhook_url")),
            "webhook_secret_set": bool(cfg.get("webhook_secret")),
            "push_sms": bool(cfg.get("push_sms", True)),
            "push_calls": bool(cfg.get("push_calls", True)),
            "auto_hangup_calls": bool(cfg.get("auto_hangup_calls", False)),
        },
        "events": events[:20] if isinstance(events, list) else [],
    }

def save_comm_push_conf(req_data):
    cfg = load_comm_push_conf()
    for bool_k in ("enabled", "push_sms", "push_calls", "auto_hangup_calls"):
        if bool_k in req_data:
            cfg[bool_k] = bool(req_data[bool_k])
    if "bark_url" in req_data:
        v = str(req_data["bark_url"] or "").strip()
        if v == "" or v.startswith(("http://", "https://")):
            cfg["bark_url"] = v
        else:
            return {"ok": 0, "error": "Bark 地址需以 http:// 或 https:// 开头（例如 https://api.day.app/您的Key）"}
    if "tg_bot_token" in req_data:
        v = str(req_data["tg_bot_token"] or "").strip()
        if "••••" not in v:
            cfg["tg_bot_token"] = v
    if "tg_chat_id" in req_data:
        cfg["tg_chat_id"] = str(req_data["tg_chat_id"] or "").strip()[:48]
    if "webhook_url" in req_data:
        v = str(req_data["webhook_url"] or "").strip()
        if v == "" or v.startswith(("http://", "https://")):
            cfg["webhook_url"] = v
        else:
            return {"ok": 0, "error": "Webhook 地址需以 http:// 或 https:// 开头"}
    if "webhook_secret" in req_data:
        v = str(req_data["webhook_secret"] or "").strip()
        if v != "••••":
            cfg["webhook_secret"] = v[:128]
    if req_data.get("clear_bark"):
        cfg["bark_url"] = ""
    if req_data.get("clear_tg"):
        cfg["tg_bot_token"] = ""
        cfg["tg_chat_id"] = ""
    if req_data.get("clear_webhook"):
        cfg["webhook_url"] = ""
        cfg["webhook_secret"] = ""
    _atomic_write_text(COMM_PUSH_CONF_FILE, json.dumps(cfg, ensure_ascii=False, indent=2), mode=0o600)
    res = get_comm_push_public_view()
    res["msg"] = "通知推送与来电规则已保存"
    return res

def record_comm_event(kind, title, detail, ok=True):
    os.makedirs("/run/mu300", exist_ok=True)
    cur = _load_json_dict(COMM_EVENTS_FILE, {"items": []})
    items = cur.get("items") if isinstance(cur.get("items"), list) else []
    items.insert(0, {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "title": str(title)[:80],
        "detail": str(detail)[:160],
        "ok": bool(ok),
    })
    cur["items"] = items[:30]
    _atomic_write_text(COMM_EVENTS_FILE, json.dumps(cur, ensure_ascii=False), mode=0o600)

def _post_json_url(url, payload, headers=None, timeout=8):
    hdrs = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "U30Air-WebUI/1.0"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.status, resp.read(4096).decode("utf-8", errors="ignore")

def dispatch_comm_push(title, body, category="sms", otp_code="", meta=None, force=False):
    """向已配置的 Bark / Telegram Bot / 自定义 Webhook 并发投递短信、验证码、来电或切卡告警"""
    cfg = load_comm_push_conf()
    if not force and not cfg.get("enabled"):
        return {"ok": 0, "skipped": True, "error": "推送总开关未启用"}

    results = []
    bark_url = str(cfg.get("bark_url") or "").strip().rstrip("/")
    if bark_url:
        try:
            # 兼容 https://api.day.app/<device_key> 与直接传入 /push 端点
            p = urllib.parse.urlsplit(bark_url)
            segs = [x for x in (p.path or "").split("/") if x]
            if segs and segs[-1] == "push":
                target_u = bark_url
                b_payload = {"title": title, "body": body, "group": "U30Air"}
            elif segs:
                dev_key = segs[-1]
                base_path = "/" + "/".join(segs[:-1]) if len(segs) > 1 else ""
                target_u = f"{p.scheme}://{p.netloc}{base_path}/push"
                b_payload = {"device_key": dev_key, "title": title, "body": body, "group": "U30Air"}
            else:
                target_u = bark_url + "/push"
                b_payload = {"title": title, "body": body, "group": "U30Air"}
            if otp_code:
                b_payload["copy"] = otp_code
                b_payload["autoCopy"] = "1"
            code, _ = _post_json_url(target_u, b_payload, timeout=8)
            results.append(("Bark", 200 <= code < 300, f"HTTP {code}"))
        except Exception as e:
            results.append(("Bark", False, str(e)[:80]))

    tg_tok = str(cfg.get("tg_bot_token") or "").strip()
    tg_chat = str(cfg.get("tg_chat_id") or "").strip()
    if tg_tok and tg_chat:
        try:
            tg_u = f"https://api.telegram.org/bot{tg_tok}/sendMessage"
            tg_text = f"📡 {title}\n\n{body}" + (f"\n\n🔑 验证码: {otp_code}" if otp_code else "")
            code, _ = _post_json_url(tg_u, {"chat_id": tg_chat, "text": tg_text}, timeout=8)
            results.append(("Telegram", 200 <= code < 300, f"HTTP {code}"))
        except Exception as e:
            results.append(("Telegram", False, str(e)[:80]))

    wh_url = str(cfg.get("webhook_url") or "").strip()
    if wh_url:
        try:
            wh_hdrs = {}
            if cfg.get("webhook_secret"):
                wh_hdrs["X-Webhook-Secret"] = str(cfg["webhook_secret"])
            wh_payload = {
                "event": category,
                "title": title,
                "body": body,
                "otp_code": otp_code,
                "meta": meta or {},
                "timestamp": int(time.time()),
            }
            code, _ = _post_json_url(wh_url, wh_payload, headers=wh_hdrs, timeout=8)
            results.append(("Webhook", 200 <= code < 300, f"HTTP {code}"))
        except Exception as e:
            results.append(("Webhook", False, str(e)[:80]))

    if not results:
        return {"ok": 0, "error": "尚未配置任何推送通道（Bark / Telegram / Webhook）"}

    any_ok = any(r[1] for r in results)
    summary = "；".join(f"{name}: {'成功' if ok_f else ('失败 ' + det)}" for name, ok_f, det in results)
    record_comm_event(category, title, summary, ok=any_ok)
    return {"ok": 1 if any_ok else 0, "summary": summary, "channels": [{"name": n, "ok": o, "detail": d} for n, o, d in results]}

def start_flash_switch(target_slot=2, duration_sec=120, return_on_sms=True, radio_only=True):
    """
    「⏱️ 闪切接码防失联模式」：
    临时切换至副卡驻网等待验证码短信（最长 duration_sec 秒，收到新短信可提前秒切回），
    无论超时、异常还是手动取消，finally 块 100% 强制切回主流量卡并推送刚收到的短信。
    """
    t_slot = int(target_slot or 2)
    if t_slot not in (1, 2):
        return {"ok": 0, "error": "无效的目标卡槽"}
    orig_slot = get_active_sim_slot_idx() + 1
    if orig_slot == t_slot:
        return {"ok": 0, "error": f"当前已经在 SIM {t_slot} 上，无需闪切"}
    dur = max(30, min(300, int(duration_sec or 120)))

    with SIM_SWITCH_LOCK:
        if SIM_SWITCH_STATE["running"] or FLASH_SWITCH_STATE["running"]:
            return {"ok": 0, "error": "已有切卡或闪切任务在运行"}
        FLASH_CANCEL_EVENT.clear()
        FLASH_SWITCH_STATE.update({
            "running": True,
            "phase": "switching_out",
            "orig_slot": orig_slot,
            "target_slot": t_slot,
            "duration_sec": dur,
            "started_at": time.time(),
            "deadline": time.time() + dur + 35,
            "finished_at": 0.0,
            "return_on_sms": bool(return_on_sms),
            "radio_only": bool(radio_only),
            "captured_sms": [],
            "msg": f"正在临时切往 SIM {t_slot} 驻网接码（期间远端网络短暂挂起，到时将自动切回 SIM {orig_slot}）...",
            "error": "",
        })

    def _flash_worker():
        had_wan_down = os.path.exists("/run/mu300/wan-user-down")
        captured = []
        try:
            ok_prep, prep_msg = ensure_dual_sim_brokers_ready(save_hot=True)
            if not ok_prep:
                raise RuntimeError(f"双卡通道未就绪: {prep_msg}")

            base_sms = list_slot_sms(t_slot).get("messages") or []
            seen_ids = {str(m.get("id")) + ":" + str(m.get("time")) for m in base_sms if isinstance(m, dict)}

            if radio_only and not had_wan_down:
                os.makedirs("/run/mu300", exist_ok=True)
                _atomic_write_text("/run/mu300/wan-user-down", "1\n", mode=0o644)

            proc = subprocess.run(
                ["/opt/mu300/bin/mu300-sim", "switch", str(t_slot)],
                capture_output=True,
                text=True,
                timeout=95,
                env=dict(os.environ, MU300_SIM_WAN_WAIT="25"),
            )
            if proc.returncode != 0:
                err_s = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
                raise RuntimeError(err_s.splitlines()[-1] if err_s else f"切往 SIM {t_slot} 失败")

            wait_end = time.time() + dur
            with SIM_SWITCH_LOCK:
                FLASH_SWITCH_STATE["phase"] = "listening"
                FLASH_SWITCH_STATE["deadline"] = wait_end
                FLASH_SWITCH_STATE["msg"] = f"SIM {t_slot} 正在驻网监听短信验证码..."

            while time.time() < wait_end and not FLASH_CANCEL_EVENT.is_set():
                time.sleep(4.5)
                cur_msgs = list_slot_sms(t_slot).get("messages") or []
                for m in cur_msgs:
                    key = str(m.get("id")) + ":" + str(m.get("time"))
                    if key not in seen_ids:
                        seen_ids.add(key)
                        captured.append(m)
                if captured:
                    with SIM_SWITCH_LOCK:
                        FLASH_SWITCH_STATE["captured_sms"] = list(captured)
                        FLASH_SWITCH_STATE["msg"] = f"已捕获 {len(captured)} 条新短信，准备切回 SIM {orig_slot}..."
                    if return_on_sms:
                        break
        except Exception as e:
            with SIM_SWITCH_LOCK:
                FLASH_SWITCH_STATE["error"] = str(e)
        finally:
            with SIM_SWITCH_LOCK:
                FLASH_SWITCH_STATE["phase"] = "returning"
                FLASH_SWITCH_STATE["msg"] = f"正在自动切回主流量卡 SIM {orig_slot} 并恢复数据连接..."
            if not had_wan_down and os.path.exists("/run/mu300/wan-user-down"):
                try:
                    os.remove("/run/mu300/wan-user-down")
                except OSError:
                    pass
            cur_active = get_active_sim_slot_idx() + 1
            if cur_active != orig_slot:
                for _attempt in range(2):
                    r_back = subprocess.run(
                        ["/opt/mu300/bin/mu300-sim", "switch", str(orig_slot)],
                        capture_output=True,
                        text=True,
                        timeout=120,
                        env=dict(os.environ, MU300_SIM_WAN_WAIT="45"),
                    )
                    if r_back.returncode == 0 or (get_active_sim_slot_idx() + 1) == orig_slot:
                        break
                    time.sleep(2)
            _invalidate_cellular_caches()
            p_orig = probe_sim_slot(orig_slot, power_sim=False, check_euicc=False)
            if p_orig.get("iccid"):
                sync_iccid_traffic_ledger(p_orig["iccid"], orig_slot - 1)
            update_public_ip_async()

            with SIM_SWITCH_LOCK:
                FLASH_SWITCH_STATE.update({
                    "running": False,
                    "phase": "done",
                    "finished_at": time.time(),
                    "captured_sms": list(captured),
                    "msg": (
                        f"闪切完成！已捕获 {len(captured)} 条新短信并安全切回 SIM {orig_slot}"
                        if captured else f"闪切结束，已安全切回 SIM {orig_slot} 恢复上网"
                    ),
                })

            # 待主卡网络恢复后，立即将捕获到的副卡验证码短信推送出去
            if captured:
                time.sleep(3)
                for m in captured:
                    otp = m.get("otp_code") or extract_sms_otp(m.get("text", ""))
                    sender = m.get("sender") or "未知号码"
                    title = f"[SIM {t_slot} 闪切接码] {('验证码 ' + otp) if otp else ('来自 ' + sender)}"
                    body = f"发件人: {sender}\n时间: {m.get('time', '')}\n内容: {m.get('text', '')}"
                    dispatch_comm_push(title, body, category="flash_sms", otp_code=otp, meta={"slot": t_slot, "sender": sender})

    threading.Thread(target=_flash_worker, daemon=True).start()
    return {"ok": 1, "started": True, "orig_slot": orig_slot, "target_slot": t_slot, "duration_sec": dur}

# -------------------------------------------------------------------------
# Vonode 风格 5 步端到端蜂窝健康自检 & 秒级快速重拨换 IP & 信令语音保号
# -------------------------------------------------------------------------
def get_cellular_health():
    """5 步端到端蜂窝链路健康诊断：1.SIM 鉴权 -> 2.基站驻网 -> 3.数据双栈承载 -> 4.IMS 核心网 -> 5.5G 射频评级"""
    slot_idx = get_active_sim_slot_idx()
    slot_num = slot_idx + 1
    iface = "sipa_eth8" if slot_idx == 1 else "sipa_eth0"
    slot_cache = load_sim_slot_cache().get(str(slot_idx), {}) if isinstance(load_sim_slot_cache().get(str(slot_idx)), dict) else {}
    qos = get_cellular_qos()
    ip_info = get_cellular_ip_info()

    # Step 1: SIM Ready
    cpin = slot_cache.get("cpin") or "READY"
    if not slot_cache.get("iccid") and not os.path.exists("/run/mu300/sim-switching"):
        p_res = probe_sim_slot(slot_num, power_sim=False, check_euicc=False)
        cpin = p_res.get("cpin", cpin)
        slot_cache = load_sim_slot_cache().get(str(slot_idx), {})
    s1_ok = (cpin == "READY")
    step_sim = {
        "id": "sim",
        "title": "1. SIM 触点与鉴权",
        "status": "ok" if s1_ok else "error",
        "summary": f"SIM {slot_num} 就绪 ({mask_iccid(slot_cache.get('iccid', '')) or 'CPIN READY'})" if s1_ok else f"SIM {slot_num} 异常 ({cpin})",
    }

    # Step 2: Network Registration
    _, out_c5g = _slot_at(slot_idx, "AT+C5GREG?", timeout=3)
    _, out_c4g = _slot_at(slot_idx, "AT+CEREG?", timeout=3)
    m5 = re.search(r"\+C5GREG:\s*\d+,(\d+)", out_c5g)
    m4 = re.search(r"\+CEREG:\s*\d+,(\d+)", out_c4g)
    st5 = int(m5.group(1)) if m5 else 0
    st4 = int(m4.group(1)) if m4 else 0
    reg_ok = (st5 in (1, 5)) or (st4 in (1, 5))
    roam_tag = "漫游驻网" if (st5 == 5 or st4 == 5) else "本地驻网"
    step_reg = {
        "id": "reg",
        "title": "2. 运营商驻网",
        "status": "ok" if reg_ok else "error",
        "summary": f"{qos.get('operator_name', '未知')} · {qos.get('rat', '5G')} {qos.get('band', '')} ({roam_tag})" if reg_ok else f"未驻网 (5G={st5}, 4G={st4})",
    }

    # Step 3: Packet Bearer & Dual-Stack IP
    v4 = ip_info.get("ipv4", "")
    v6 = ip_info.get("ipv6", "")
    user_down = os.path.exists("/run/mu300/wan-user-down")
    s3_ok = bool(v4 or v6)
    step_data = {
        "id": "data",
        "title": "3. 数据承载与双栈",
        "status": "ok" if s3_ok else ("warn" if user_down else "error"),
        "summary": (
            f"{iface}: {v4 or '无v4'}" + (" + IPv6 双栈" if v6 else " (单栈 IPv4)")
            if s3_ok else ("已手动关闭移动数据" if user_down else f"{iface} 未获取到运营商 IP")
        ),
    }

    # Step 4: IMS (VoLTE / SMS over IP)
    _, out_ims = _slot_at(slot_idx, "AT+CAVIMS?", timeout=3)
    ims_on = "+CAVIMS: 1" in out_ims
    step_ims = {
        "id": "ims",
        "title": "4. IMS 短信/语音域",
        "status": "ok" if ims_on else "warn",
        "summary": "IMS 已激活 (5G 短信 / VoLTE 就绪)" if ims_on else "IMS 未激活 (回落传统信令)",
    }

    # Step 5: 5G RF Signal Grade
    rsrp = int(qos.get("rsrp", -110) or -110)
    sinr = float(qos.get("sinr", 0.0) or 0.0)
    if rsrp >= -85 and sinr >= 10:
        sig_st, sig_grade = "ok", "极佳"
    elif rsrp >= -98 and sinr >= 3:
        sig_st, sig_grade = "ok", "良好"
    elif rsrp >= -108:
        sig_st, sig_grade = "warn", "一般"
    else:
        sig_st, sig_grade = "error", "偏弱"
    step_sig = {
        "id": "signal",
        "title": "5. 射频信号质量",
        "status": sig_st,
        "summary": f"{sig_grade} · RSRP {rsrp} dBm / SINR {sinr} dB",
    }

    steps = [step_sim, step_reg, step_data, step_ims, step_sig]
    ok_cnt = sum(1 for s in steps if s["status"] == "ok")
    overall = "healthy" if (s1_ok and reg_ok and s3_ok) else ("degraded" if (s1_ok and reg_ok) else "offline")
    return {
        "ok": 1,
        "slot": slot_num,
        "iface": iface,
        "overall": overall,
        "score": ok_cnt,
        "steps": steps,
    }

def fast_cellular_redial():
    """秒级快速重拨刷新运营商公网 IPv4 / IPv6（配合 fast-redial.conf 跳过 15s 冷启动等待）"""
    old_info = get_cellular_ip_info()
    if os.path.exists("/run/mu300/wan-user-down"):
        try:
            os.remove("/run/mu300/wan-user-down")
        except OSError:
            pass
    t0 = time.time()
    proc = subprocess.run(["systemctl", "restart", "mu300-mobile-data.service"], capture_output=True, text=True, timeout=45)
    _invalidate_cellular_caches()
    new_info = get_cellular_ip_info()
    pub_v4 = fetch_public_ipv4()
    if pub_v4:
        PUBLIC_IP_CACHE["ip"] = pub_v4
        PUBLIC_IP_CACHE["time"] = time.time()
        new_info["public_ipv4"] = pub_v4
    elapsed = round(time.time() - t0, 1)
    if proc.returncode != 0:
        return {"ok": 0, "error": (proc.stderr or proc.stdout or "重拨失败").strip(), "elapsed_s": elapsed}
    return {
        "ok": 1,
        "elapsed_s": elapsed,
        "old_ipv4": old_info.get("public_ipv4") or old_info.get("ipv4", ""),
        "new_ipv4": new_info.get("public_ipv4") or new_info.get("ipv4", ""),
        "new_ipv6": new_info.get("ipv6", ""),
        "iface": new_info.get("iface", ""),
        "msg": f"快速重拨完成（耗时 {elapsed}s），已刷新数据承载 IP",
    }

def get_call_status(slot_num=None):
    idx = get_active_sim_slot_idx() if slot_num is None else (1 if int(slot_num) == 2 else 0)
    ok_at, out = _slot_at(idx, "AT+CLCC", timeout=4)
    state_map = {"0": "通话中", "1": "保持中", "2": "拨号呼叫中", "3": "对方振铃中", "4": "来电振铃中", "5": "等待接听"}
    calls = []
    if ok_at:
        for line in out.splitlines():
            m = re.match(r'\+CLCC:\s*(\d+),(\d+),(\d+),(\d+),(\d+)(?:,"([^"]*)")?', line.strip())
            if m:
                calls.append({
                    "id": int(m.group(1)),
                    "direction": "incoming" if m.group(2) == "1" else "outgoing",
                    "state_code": int(m.group(3)),
                    "state_text": state_map.get(m.group(3), "未知"),
                    "mode": "voice" if m.group(4) == "0" else "other",
                    "number": m.group(6) or "未知号码",
                })
    events = [
        e for e in (_load_json_dict(COMM_EVENTS_FILE, {"items": []}).get("items") or [])
        if isinstance(e, dict) and e.get("kind") in ("call", "dial")
    ]
    return {"ok": 1, "slot": idx + 1, "calls": calls, "recent_calls": events[:10]}

def dial_voice_call(number, slot_num=None):
    num = re.sub(r"[\s\-()]", "", str(number or ""))
    if not re.fullmatch(r"\+?[0-9*#]{2,24}", num):
        return {"ok": 0, "error": "号码格式无效（仅允许数字、+、*、#，长度 2~24 位）"}
    idx = get_active_sim_slot_idx() if slot_num is None else (1 if int(slot_num) == 2 else 0)
    _slot_at(idx, "AT+CLIP=1", timeout=3)
    ok_at, out = _slot_at(idx, f"ATD{num};", timeout=8)
    if not ok_at or ("ERROR" in out and "OK" not in out):
        return {"ok": 0, "error": out.splitlines()[-1] if out else "拨号指令未获响应"}
    record_comm_event("dial", f"SIM {idx + 1} 外呼保号", f"已拨打 {num}", ok=True)
    return {"ok": 1, "slot": idx + 1, "number": num, "msg": f"已通过 SIM {idx + 1} 发起信令语音呼叫: {num}"}

def send_call_dtmf(digits, slot_num=None):
    d_str = str(digits or "").strip().upper()
    if not re.fullmatch(r"[0-9A-D*#]{1,8}", d_str):
        return {"ok": 0, "error": "DTMF 按键仅支持 0-9、A-D、*、#"}
    idx = get_active_sim_slot_idx() if slot_num is None else (1 if int(slot_num) == 2 else 0)
    for ch in d_str:
        ok_at, out = _slot_at(idx, f'AT+VTS="{ch}"', timeout=4)
        if not ok_at:
            return {"ok": 0, "error": out or "发送 DTMF 失败"}
    return {"ok": 1, "digits": d_str}

def hangup_voice_call(slot_num=None):
    idx = get_active_sim_slot_idx() if slot_num is None else (1 if int(slot_num) == 2 else 0)
    ok_at, out = _slot_at(idx, "AT+CHUP", timeout=5)
    if not ok_at:
        ok_at, out = _slot_at(idx, "ATH", timeout=5)
    return {"ok": 1 if ok_at else 0, "msg": "已挂断当前通话", "detail": out[:80]}

def handle_sim_comm_api(method, req_data):
    """统一路由 Phase 1 新增的 SIM 双卡、蜂窝健康、短信 OTP 推送与语音信令 API"""
    req_data = req_data if isinstance(req_data, dict) else {}

    if method == "sim.overview":
        return get_sim_overview(refresh_active=bool(req_data.get("refresh")))
    if method == "sim.probe_slot":
        return probe_sim_slot(
            req_data.get("slot", 2),
            power_sim=bool(req_data.get("power_sim", True)),
            check_euicc=bool(req_data.get("check_euicc", True)),
        )
    if method == "sim.enable_live":
        ok_p, msg_p = ensure_dual_sim_brokers_ready(save_hot=True)
        return {"ok": 1 if ok_p else 0, "msg": "双卡在线热切守护已就绪" if ok_p else msg_p, "overview": get_sim_overview()}
    if method == "sim.switch":
        return start_live_sim_switch(
            req_data.get("slot", 1),
            wan_wait=req_data.get("wan_wait", 45),
            make_default=bool(req_data.get("make_default", False)),
            reason="manual",
        )
    if method == "sim.switch_status":
        return get_sim_switch_status()
    if method == "sim.flash_switch":
        return start_flash_switch(
            target_slot=req_data.get("slot", 2),
            duration_sec=req_data.get("duration_sec", 120),
            return_on_sms=bool(req_data.get("return_on_sms", True)),
            radio_only=bool(req_data.get("radio_only", True)),
        )
    if method == "sim.flash_cancel":
        FLASH_CANCEL_EVENT.set()
        return {"ok": 1, "msg": "已发送提前切回指令，正在恢复主流量卡..."}
    if method == "sim.card_save":
        return save_sim_card_profile(req_data)
    if method == "sim.set":
        return run_mu300_api("sim.select", req_data)

    if method == "cellular.health":
        return get_cellular_health()
    if method == "cellular.redial":
        return fast_cellular_redial()

    if method == "sms.list":
        return list_slot_sms(req_data.get("slot"))
    if method in ("comm.push_get", "sms.push_get"):
        return get_comm_push_public_view()
    if method in ("comm.push_set", "sms.push_set"):
        return save_comm_push_conf(req_data)
    if method in ("comm.push_test", "sms.push_test"):
        return dispatch_comm_push(
            "U30 Air 通信中心推送测试",
            f"测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n当前主用卡槽: SIM {get_active_sim_slot_idx() + 1}",
            category="test",
            otp_code="864209",
            force=True,
        )

    if method == "call.status":
        return get_call_status(req_data.get("slot"))
    if method == "call.dial":
        return dial_voice_call(req_data.get("number", ""), req_data.get("slot"))
    if method == "call.dtmf":
        return send_call_dtmf(req_data.get("digits", ""), req_data.get("slot"))
    if method == "call.hangup":
        return hangup_voice_call(req_data.get("slot"))

    return None

def start_comm_and_quota_watcher():
    """
    极轻量后台通信与配额守护线程 (零额外常驻进程):
    1. 实时监控 URC 日志中的来电 (+CLIP / RING) 与新短信提示 (+CMTI)，按策略自动拒接来电并推送通知
    2. 新短信自动识别 4~8 位验证码推送到 Bark / Telegram / Webhook
    3. 检查当前 SIM 独立流量上限：若配置了 over_limit_action == 'switch_sim' 且达到 100%，自动热切至备用流量卡
    """
    def _watcher_loop():
        time.sleep(8)
        urc_offsets = {}
        clip_armed_slot = None
        last_sms_poll = 0.0
        last_quota_poll = 0.0
        auto_switched_iccids = set()

        # 初始化已有短信已读指纹，避免服务重启时重复推送历史旧短信
        seen_data = _load_json_dict(COMM_PUSH_SEEN_FILE, {"sms": []})
        seen_sms = set(seen_data.get("sms") or [])
        if not seen_sms:
            init_msgs = list_slot_sms().get("messages") or []
            for m in init_msgs:
                seen_sms.add(f"{m.get('slot', 1)}:{m.get('id')}:{m.get('time')}")
            if seen_sms:
                _atomic_write_text(COMM_PUSH_SEEN_FILE, json.dumps({"sms": list(seen_sms)[-200:]}), mode=0o600)

        while True:
            time.sleep(10)
            try:
                if os.path.exists("/run/mu300/sim-switching") or FLASH_SWITCH_STATE.get("running"):
                    continue

                slot_idx = get_active_sim_slot_idx()
                if clip_armed_slot != slot_idx:
                    ok_clip, _ = _slot_at(slot_idx, "AT+CLIP=1", timeout=3)
                    if ok_clip:
                        clip_armed_slot = slot_idx

                # 1. 零 AT 开销增量扫描内存 URC 日志 (/run/mu300-at*/urc/*.log)
                cmti_hit = False
                for s_i, urc_p in ((0, "/run/mu300-at/urc/stty_nr0.log"), (1, "/run/mu300-at4/urc/stty_nr3.log")):
                    if not os.path.exists(urc_p):
                        continue
                    st_sz = os.path.getsize(urc_p)
                    prev_off = urc_offsets.get(urc_p)
                    if prev_off is None:
                        urc_offsets[urc_p] = st_sz
                        continue
                    if st_sz < prev_off:
                        prev_off = 0
                    if st_sz == prev_off:
                        continue
                    with open(urc_p, "r", encoding="utf-8", errors="ignore") as uf:
                        uf.seek(prev_off)
                        chunk = uf.read(65536)
                        urc_offsets[urc_p] = uf.tell()
                    caller_num = ""
                    ring_seen = False
                    for raw_l in chunk.splitlines():
                        l = raw_l.strip()
                        if "+CMTI:" in l:
                            cmti_hit = True
                        elif l.startswith("+CLIP:"):
                            m_cl = re.search(r'\+CLIP:\s*"([^"]*)"', l)
                            if m_cl and m_cl.group(1):
                                caller_num = m_cl.group(1)
                            ring_seen = True
                        elif l == "RING":
                            ring_seen = True
                    if ring_seen:
                        p_cfg = load_comm_push_conf()
                        disp_num = caller_num or "未知来电号码"
                        hung = False
                        if p_cfg.get("auto_hangup_calls"):
                            _slot_at(s_i, "AT+CHUP", timeout=4)
                            hung = True
                        act_str = "已自动挂断防打扰" if hung else "正在振铃"
                        record_comm_event("call", f"SIM {s_i + 1} 来电: {disp_num}", act_str, ok=True)
                        if p_cfg.get("enabled") and p_cfg.get("push_calls", True):
                            dispatch_comm_push(
                                f"[SIM {s_i + 1} 来电提醒] {disp_num}",
                                f"来电号码: {disp_num}\n处理状态: {act_str}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                                category="call",
                                meta={"slot": s_i + 1, "caller": disp_num, "auto_hung_up": hung},
                            )

                # 2. 新短信轮询与 Bark / TG / Webhook 推送 (URC 命中立即读，平时每 35s 巡检一次)
                now = time.time()
                p_cfg = load_comm_push_conf()
                if cmti_hit or (p_cfg.get("enabled") and p_cfg.get("push_sms", True) and (now - last_sms_poll >= 35)):
                    last_sms_poll = now
                    sms_res = list_slot_sms(slot_idx + 1)
                    msgs = sms_res.get("messages") or []
                    dirty_seen = False
                    for m in reversed(msgs):
                        sig = f"{slot_idx + 1}:{m.get('id')}:{m.get('time')}"
                        if sig in seen_sms:
                            continue
                        seen_sms.add(sig)
                        dirty_seen = True
                        if p_cfg.get("enabled") and p_cfg.get("push_sms", True):
                            otp = m.get("otp_code") or extract_sms_otp(m.get("text", ""))
                            sender = m.get("sender") or "未知号码"
                            title = f"[SIM {slot_idx + 1} 新短信] {('验证码 ' + otp) if otp else ('来自 ' + sender)}"
                            body = f"发件人: {sender}\n时间: {m.get('time', '')}\n内容: {m.get('text', '')}"
                            dispatch_comm_push(title, body, category="sms", otp_code=otp, meta={"slot": slot_idx + 1, "sender": sender})
                    if dirty_seen:
                        _atomic_write_text(COMM_PUSH_SEEN_FILE, json.dumps({"sms": list(seen_sms)[-200:]}), mode=0o600)

                # 3. 独立流量超额自动切换备用 SIM 卡巡检 (每 60s 一次)
                if now - last_quota_poll >= 60:
                    last_quota_poll = now
                    slot_cache = load_sim_slot_cache()
                    cur_iccid = str((slot_cache.get(str(slot_idx)) or {}).get("iccid") or "")
                    if cur_iccid and cur_iccid not in auto_switched_iccids:
                        sync_iccid_traffic_ledger(cur_iccid, slot_idx)
                        prof = (load_sim_cards_db().get("cards") or {}).get(cur_iccid) or {}
                        if prof.get("over_limit_action") == "switch_sim":
                            tr = read_iccid_traffic_summary(cur_iccid, is_active=True, force=True)
                            over_month = tr["cap_bytes"] > 0 and tr["cycle_bytes"] >= tr["cap_bytes"]
                            over_day = tr["daily_cap_bytes"] > 0 and tr["today_bytes"] >= tr["daily_cap_bytes"]
                            if over_month or over_day:
                                backup_slot = 2 if slot_idx == 0 else 1
                                auto_switched_iccids.add(cur_iccid)
                                sw_r = start_live_sim_switch(backup_slot, wan_wait=45, make_default=False, reason="quota_failover")
                                if sw_r.get("ok"):
                                    dispatch_comm_push(
                                        f"[流量超限自动切卡] SIM {slot_idx + 1} -> SIM {backup_slot}",
                                        f"SIM {slot_idx + 1} ({prof.get('nickname') or mask_iccid(cur_iccid)}) 已达设定流量上限，正在自动热切换至备用卡 SIM {backup_slot}。",
                                        category="quota_switch",
                                    )
            except Exception as e:
                print(f"[CommWatcher] {e}")

    threading.Thread(target=_watcher_loop, daemon=True).start()

def handle_login_request(client_ip, req_json):
    now = time.time()
    with STATE_LOCK:
        fail_rec = dict(LOGIN_FAILURES.get(client_ip, {"count": 0, "until": 0}))
    if fail_rec["until"] > now:
        wait_s = max(1, int(fail_rec["until"] - now))
        return 429, {"ok": 0, "error": f"密码错误次数过多，请等待 {wait_s} 秒后再试"}, None

    username = str(req_json.get("username", "")).strip()
    password = str(req_json.get("password", ""))
    remember = bool(req_json.get("remember", True))

    if verify_system_password(username, password):
        token = secrets.token_hex(32)
        duration = 30 * 86400 if remember else 86400
        with STATE_LOCK:
            LOGIN_FAILURES.pop(client_ip, None)
            SESSIONS[token] = {
                "user": username,
                "expire": time.time() + duration
            }
        save_sessions()
        cookie = f"u30_session={token}; Path=/; Max-Age={duration}; SameSite=Lax; HttpOnly"
        print(f"[Auth] 用户 {username} 登录成功")
        return 200, {"ok": 1, "username": username, "token": token}, cookie

    with STATE_LOCK:
        cur = LOGIN_FAILURES.get(client_ip, {"count": 0, "until": 0})
        cnt = int(cur.get("count", 0)) + 1
        LOGIN_FAILURES[client_ip] = {
            "count": cnt,
            "until": (now + 60) if cnt >= 5 else 0
        }
    return 401, {"ok": 0, "error": "用户名或密码错误"}, None

def handle_logout_request(token):
    removed = False
    if token:
        with STATE_LOCK:
            if token in SESSIONS:
                del SESSIONS[token]
                removed = True
    if removed:
        save_sessions()
    return "u30_session=; Path=/; Max-Age=0; SameSite=Lax; HttpOnly"

def handle_password_change_request(client_ip, current_token, username, req_json):
    now = time.time()
    with STATE_LOCK:
        fail_rec = dict(LOGIN_FAILURES.get(client_ip, {"count": 0, "until": 0}))
    if fail_rec["until"] > now:
        wait_s = max(1, int(fail_rec["until"] - now))
        return 429, {"ok": 0, "error": f"密码错误次数过多，请等待 {wait_s} 秒后再试"}, None

    if not username or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(username)):
        return 400, {"ok": 0, "error": "当前登录账号无效"}, None

    old_password = str(req_json.get("old_password", ""))
    new_password = str(req_json.get("new_password", ""))
    confirm_password = str(req_json.get("confirm_password", new_password))

    if not old_password:
        return 400, {"ok": 0, "error": "请输入当前密码"}, None
    if len(new_password) < 6:
        return 400, {"ok": 0, "error": "新密码长度至少需要 6 位"}, None
    if len(new_password) > 128:
        return 400, {"ok": 0, "error": "新密码长度不能超过 128 位"}, None
    if any(c in new_password for c in ("\n", "\r", "\x00")):
        return 400, {"ok": 0, "error": "新密码不能包含换行或控制字符"}, None
    if new_password != confirm_password:
        return 400, {"ok": 0, "error": "两次输入的新密码不一致"}, None
    if new_password == old_password:
        return 400, {"ok": 0, "error": "新密码不能与当前密码相同"}, None

    if not verify_system_password(username, old_password):
        with STATE_LOCK:
            cur = LOGIN_FAILURES.get(client_ip, {"count": 0, "until": 0})
            cnt = int(cur.get("count", 0)) + 1
            LOGIN_FAILURES[client_ip] = {
                "count": cnt,
                "until": (now + 60) if cnt >= 5 else 0
            }
        return 403, {"ok": 0, "error": "当前密码不正确"}, None

    ok, msg = update_system_password(username, new_password)
    if not ok:
        return 500, {"ok": 0, "error": msg}, None

    new_token = secrets.token_hex(32)
    revoked_others = 0
    duration = 30 * 86400
    with STATE_LOCK:
        LOGIN_FAILURES.pop(client_ip, None)
        cur_sess = SESSIONS.get(current_token) if current_token else None
        if cur_sess:
            rem = int(cur_sess.get("expire", now + 86400) - now)
            duration = max(86400, min(30 * 86400, rem))
        stale_tokens = [k for k, v in SESSIONS.items() if v.get("user") == username]
        for k in stale_tokens:
            if k != current_token:
                revoked_others += 1
            SESSIONS.pop(k, None)
        SESSIONS[new_token] = {
            "user": username,
            "expire": time.time() + duration
        }
    save_sessions()
    cookie = f"u30_session={new_token}; Path=/; Max-Age={duration}; SameSite=Lax; HttpOnly"
    print(f"[Auth] 用户 {username} 已修改系统与面板密码 (吊销其他会话 {revoked_others} 个)")
    return 200, {
        "ok": 1,
        "username": username,
        "token": new_token,
        "revoked_sessions": revoked_others,
        "msg": f"系统与面板密码已同步更新（已清理其他登录会话 {revoked_others} 个）"
    }, cookie

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

class WebUIHandler(BaseHTTPRequestHandler):
    def get_token_from_request(self):
        # 1. 尝试从 Authorization: Bearer <token> 获取
        auth_hdr = self.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            return auth_hdr[7:].strip()
        # 2. 尝试从 Cookie: u30_session=<token> 获取
        cookie_hdr = self.headers.get("Cookie")
        if cookie_hdr:
            try:
                cookie = SimpleCookie()
                cookie.load(cookie_hdr)
                if "u30_session" in cookie:
                    return cookie["u30_session"].value
            except Exception:
                pass
        return None

    def is_authenticated(self):
        token = self.get_token_from_request()
        if not token:
            return False, None
        expired = False
        user = None
        with STATE_LOCK:
            session = SESSIONS.get(token)
            if not session:
                return False, None
            if session.get("expire", 0) < time.time():
                del SESSIONS[token]
                expired = True
            else:
                user = session.get("user")
        if expired:
            save_sessions()
            return False, None
        return True, user

    def send_json(self, data, code=200, set_cookie=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        accept_enc = self.headers.get("Accept-Encoding", "") if hasattr(self, "headers") and self.headers else ""
        use_gzip = len(body) >= 1024 and ("gzip" in accept_enc)
        if use_gzip:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        if getattr(self, "command", "GET") != "HEAD":
            self.wfile.write(body)

    def _serve_static_file(self, file_path):
        st = os.stat(file_path)
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        fsize = st.st_size
        with STATE_LOCK:
            entry = STATIC_ASSET_CACHE.get(file_path)
        if not entry or entry.get("mtime_ns") != mtime_ns or entry.get("size") != fsize:
            ctype, _ = mimetypes.guess_type(file_path)
            if not ctype:
                ctype = "text/html" if file_path.endswith(".html") else "application/octet-stream"
            with open(file_path, "rb") as f:
                raw_bytes = f.read()
            etag = '"' + hashlib.sha1(raw_bytes).hexdigest()[:16] + '"'
            compressible = any(file_path.endswith(ext) for ext in (".html", ".js", ".css", ".json", ".svg", ".txt"))
            gz_bytes = gzip.compress(raw_bytes, compresslevel=6) if (compressible and len(raw_bytes) >= 512) else None
            entry = {
                "mtime_ns": mtime_ns,
                "size": fsize,
                "ctype": ctype,
                "etag": etag,
                "raw": raw_bytes,
                "gzip": gz_bytes,
            }
            with STATE_LOCK:
                if len(STATIC_ASSET_CACHE) > 16:
                    STATIC_ASSET_CACHE.clear()
                STATIC_ASSET_CACHE[file_path] = entry

        inm = self.headers.get("If-None-Match", "") if hasattr(self, "headers") and self.headers else ""
        cache_ctl = "public, max-age=604800" if file_path.endswith("tailwindcss.js") else "no-cache"
        if inm and entry["etag"] in [x.strip() for x in inm.split(",")]:
            self.send_response(304)
            self.send_header("ETag", entry["etag"])
            self.send_header("Cache-Control", cache_ctl)
            self.end_headers()
            return

        accept_enc = self.headers.get("Accept-Encoding", "") if hasattr(self, "headers") and self.headers else ""
        use_gzip = entry["gzip"] is not None and ("gzip" in accept_enc)
        payload = entry["gzip"] if use_gzip else entry["raw"]
        ctype_hdr = f"{entry['ctype']}; charset=utf-8" if entry["ctype"].startswith(("text/", "application/javascript", "application/json")) else entry["ctype"]
        self.send_response(200)
        self.send_header("Content-Type", ctype_hdr)
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("ETag", entry["etag"])
        self.send_header("Cache-Control", cache_ctl)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if getattr(self, "command", "GET") != "HEAD":
            self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/ping":
            self.send_json({"ok": 1, "service": "u30air-webui", "status": "online"})
            return

        if path == "/api/auth/status":
            auth_ok, username = self.is_authenticated()
            self.send_json({"ok": 1, "authenticated": auth_ok, "username": username})
            return

        if path == "/api/cockpit/status":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            proc = subprocess.run(["systemctl", "is-active", "cockpit.socket"], capture_output=True, text=True)
            active = proc.stdout.strip() == "active"
            self.send_json({"ok": 1, "running": active})
            return

        if path == "/api/bluetooth/status":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            self.send_json(get_bluetooth_status())
            return

        if path == "/api/logs/status":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            self.send_json(get_storage_and_logs_status())
            return

        if path == "/api/backup/download":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            res = run_mu300_api("backup.export")
            bfile = res.get("file", "") if isinstance(res, dict) else ""
            bname = res.get("name", "mu300-backup.tar.gz") if isinstance(res, dict) else "mu300-backup.tar.gz"
            if bfile and os.path.exists(bfile):
                with open(bfile, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition", f'attachment; filename="{bname}"')
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            self.send_json({"ok": 0, "error": "backup_failed"}, code=500)
            return

        # 保护的 API 端点
        if path == "/api/realtime_net":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            self.send_json(get_realtime_traffic())
            return

        if path == "/api/overview":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized", "detail": "需要登录"}, code=401)
                return
            self.send_json(get_overview_data())
            return

        if path in ["/api/cellular/ip", "/api/cellular_ip"]:
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            self.send_json({"ok": 1, "data": get_cellular_ip_info()})
            return

        if path == "/api/cellular_qos":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            self.send_json({"ok": 1, "data": get_cellular_qos()})
            return

        if path == "/api/battery":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            self.send_json({"ok": 1, "data": get_battery_info()})
            return

        if path == "/api/wifi/clients":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            force_refresh = ("force=1" in self.path) or ("force=true" in self.path)
            clients = get_connected_stations(force=force_refresh)
            bl = load_blocklist()
            subnet_prefix, gw_ip = get_lan_subnet()
            self.send_json({
                "ok": 1,
                "clients": clients,
                "blocklist": bl,
                "count": len(clients),
                "policies": load_client_policies(),
                "lan_subnet": subnet_prefix,
                "gateway": gw_ip
            })
            return

        if path == "/api/lan/policies":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            subnet_prefix, gw_ip = get_lan_subnet()
            self.send_json({
                "ok": 1,
                "policies": load_client_policies(),
                "clients": get_connected_stations(force=False),
                "lan_subnet": subnet_prefix,
                "gateway": gw_ip
            })
            return

        # 静态文件服务
        if path in ("/", "/index.html"):
            file_path = os.path.join(WEB_DIR, "index.html")
        else:
            rel_path = path.lstrip("/")
            file_path = os.path.join(WEB_DIR, rel_path)

        if not os.path.exists(file_path) or os.path.isdir(file_path):
            file_path = os.path.join(WEB_DIR, "index.html")

        if os.path.exists(file_path) and os.path.isfile(file_path):
            self._serve_static_file(file_path)
        else:
            self.send_error(404, "File not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        req_json = json.loads(body) if body else {}

        # 1. 登录端点 (含防暴力破解保护)
        if path == "/api/login":
            client_ip = self.client_address[0] if self.client_address else "unknown"
            code, resp_obj, cookie = handle_login_request(client_ip, req_json)
            self.send_json(resp_obj, code=code, set_cookie=cookie)
            return

        # 2. 登出端点
        if path == "/api/logout":
            token = self.get_token_from_request()
            cookie = handle_logout_request(token)
            self.send_json({"ok": 1}, set_cookie=cookie)
            return

        # 2.1 修改系统与面板密码端点 (同步更新 Linux / SSH 密码并吊销旧会话)
        if path == "/api/auth/password":
            auth_ok, username = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized", "detail": "登录状态已失效，请重新登录"}, code=401)
                return
            client_ip = self.client_address[0] if self.client_address else "unknown"
            token = self.get_token_from_request()
            code, resp_obj, cookie = handle_password_change_request(client_ip, token, username, req_json)
            self.send_json(resp_obj, code=code, set_cookie=cookie)
            return

        # 3. Cockpit 启停切换端点
        if path == "/api/cockpit/toggle":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            # Backward-compatible endpoint: the retired panel can only be stopped.
            subprocess.run(["systemctl", "disable", "--now", "cockpit.socket"],
                           capture_output=True, timeout=15)
            subprocess.run(["systemctl", "stop", "cockpit.service"],
                           capture_output=True, timeout=15)
            proc = subprocess.run(["systemctl", "is-active", "cockpit.socket"],
                                  capture_output=True, text=True, timeout=5)
            stopped = proc.stdout.strip() != "active"
            self.send_json({"ok": 1 if stopped else 0, "running": not stopped,
                            "msg": "旧面板保持停用，使用当前 8080 面板" if stopped else "旧面板未能停止"})
            return


        # 3.1 蓝牙服务启停切换端点
        if path == "/api/bluetooth/toggle":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            desired = req_json.get("enable", None)
            self.send_json(toggle_bluetooth_service(desired))
            return

        # 3.2 存储与日志清理及策略配置端点
        if path == "/api/logs/clean":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            target = req_json.get("target", "all")
            res = run_log_cleanup(target=target, force_truncate=True)
            res["status"] = get_storage_and_logs_status()
            self.send_json(res)
            return

        if path == "/api/logs/policy":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            pol = load_log_policy()
            if "auto_clean" in req_json:
                pol["auto_clean"] = bool(req_json["auto_clean"])
            if "max_journal_mb" in req_json:
                pol["max_journal_mb"] = max(2, min(64, int(req_json["max_journal_mb"])))
            if "retention_days" in req_json:
                pol["retention_days"] = max(1, min(30, int(req_json["retention_days"])))
            if "max_logcat_kb" in req_json:
                pol["max_logcat_kb"] = max(128, min(4096, int(req_json["max_logcat_kb"])))
            if "mihomo_log_level" in req_json and req_json["mihomo_log_level"] in ("silent", "error", "warning", "info"):
                pol["mihomo_log_level"] = req_json["mihomo_log_level"]
            save_log_policy(pol)
            apply_journald_policy(pol["max_journal_mb"], pol["retention_days"])
            apply_mihomo_log_level(pol["mihomo_log_level"])
            run_log_cleanup(target="all", force_truncate=False)
            self.send_json({"ok": 1, "msg": "日志保留与自动清理策略已生效", "status": get_storage_and_logs_status()})
            return

        # 4. 终端管理相关端点 (踢下线、拉黑、解封)
        if path == "/api/wifi/kick":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            mac = req_json.get("mac", "")
            ok, msg = kick_station(mac)
            self.send_json({"ok": 1 if ok else 0, "msg": msg})
            return

        if path == "/api/wifi/block":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            mac = req_json.get("mac", "")
            name = req_json.get("name", "")
            ip = req_json.get("ip", "")
            ok, msg = block_station(mac, name, ip)
            self.send_json({"ok": 1 if ok else 0, "msg": msg})
            return

        if path == "/api/wifi/unblock":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            mac = req_json.get("mac", "")
            ok, msg = unblock_station(mac)
            self.send_json({"ok": 1 if ok else 0, "msg": msg})
            return

        # 4.1 局域网固定 IP 分配与 VPN 策略组分流端点
        if path == "/api/lan/bind":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            self.send_json(bind_client_policy(req_json))
            return

        if path == "/api/lan/unbind":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized"}, code=401)
                return
            self.send_json(unbind_client_policy(req_json))
            return

        # 5. 核心 API 端点校验权限
        if path == "/api/call":
            auth_ok, _ = self.is_authenticated()
            if not auth_ok:
                self.send_json({"ok": 0, "error": "unauthorized", "detail": "需要登录"}, code=401)
                return

            method = req_json.get("method")
            req_data = req_json.get("req", {})
            if not method:
                self.send_json({"ok": 0, "error": "missing_method"}, code=400)
                return

            # 原生拦截与处理 VPN 节点与策略分流相关请求
            if method == "vpn.nodes":
                prof_id = req_data.get("id", "mihomo")
                self.send_json(get_vpn_nodes(prof_id))
                return

            if method == "vpn.subs":
                self.send_json(list_vpn_subscriptions())
                return

            if method == "vpn.sub_add":
                self.send_json(add_vpn_subscription(req_data))
                return

            if method == "vpn.sub_remove":
                self.send_json(remove_vpn_subscription(req_data))
                return

            if method == "tailscale.status":
                self.send_json(get_tailscale_status())
                return

            if method == "vpn.node_select":
                prof_id = req_data.get("id", "mihomo")
                group = req_data.get("group", "")
                node = req_data.get("node", "")
                self.send_json(select_vpn_node(prof_id, group, node))
                return

            if method == "vpn.node_delay":
                prof_id = req_data.get("id", "mihomo")
                group = req_data.get("group", "")
                node = req_data.get("node", "")
                self.send_json(test_node_latency(prof_id, group, node))
                return

            if method in ("vpn.sub_refresh", "vpn.refresh_sub", "vpn.subscription.refresh"):
                sub_id = req_data.get("id", "all")
                self.send_json(refresh_vpn_subscription(sub_id))
                return

            if method == "vpn.merge":
                self.send_json(build_merged_vpn_config(apply_live=True))
                return

            if method == "vpn.group_exit_ips":
                self.send_json(probe_vpn_group_exit_ips())
                return

            if method == "system.drop_caches":
                self.send_json(drop_memory_caches())
                return

            if method == "lan.bind":
                self.send_json(bind_client_policy(req_data))
                return

            if method == "lan.unbind":
                self.send_json(unbind_client_policy(req_data))
                return

            if method == "lan.policies":
                subnet_prefix, gw_ip = get_lan_subnet()
                self.send_json({
                    "ok": 1,
                    "policies": load_client_policies(),
                    "clients": get_connected_stations(force=False),
                    "lan_subnet": subnet_prefix,
                    "gateway": gw_ip
                })
                return

            if method == "overview":
                self.send_json(get_overview_data(req_data))
                return

            if method in ("power.get", "power.set", "power.control"):
                pwr_resp = handle_power_api(method, req_data)
                if pwr_resp is not None:
                    self.send_json(pwr_resp)
                    return

            sim_comm_resp = handle_sim_comm_api(method, req_data)
            if sim_comm_resp is not None:
                self.send_json(sim_comm_resp)
                return

            nfc_resp = handle_nfc_api(method, req_data)
            if nfc_resp is not None:
                self.send_json(nfc_resp)
                return

            resp = run_mu300_api(method, req_data)
            if method == "wifi.set" and isinstance(resp, dict) and resp.get("ok"):
                trigger_nfc_wifi_sync_async()
            if method.startswith("power.") or method.startswith("wifi.") or method.startswith("vpn."):
                BATTERY_INFO_CACHE["time"] = 0
                OVERVIEW_API_CACHE["time"] = 0
            if method in ["data.get", "data"] and isinstance(resp, dict):
                cell_ip = get_cellular_ip_info()
                resp["cellular_ip"] = cell_ip
                if not resp.get("address") and cell_ip.get("ipv4"):
                    resp["address"] = cell_ip["ipv4"]
                resp["ipv4"] = cell_ip.get("ipv4", "")
                resp["public_ipv4"] = cell_ip.get("public_ipv4", "")
                resp["ipv6"] = cell_ip.get("ipv6", "")
            self.send_json(resp)
            return

        self.send_error(404, "Unknown API")

    def log_message(self, format, *args):
        pass

# =========================================================================
# NFC 标签 (FM11NT08 / I2C-2 0x57) 状态读取、射频开关与 Wi-Fi 自动同步
# =========================================================================
NFC_BIN = "/opt/mu300/bin/mu300-nfc"
HOTSPOT_CONF_PATH = "/etc/mu300/hotspot.conf"
I2C_MODULES_LOAD_CONF = "/etc/modules-load.d/mu300-i2c.conf"
NFC_LOCK = threading.Lock()
NFC_STATUS_CACHE = {"time": 0, "data": None}
_NFC_SUPPORTED_CACHE = None

def _ensure_i2c_dev_ready():
    """确保 Linux i2c-dev 模块已加载且开机自动加载，避免 5.4 内核缺失 /dev/i2c-2"""
    if not sys.platform.startswith("linux"):
        return
    try:
        if os.path.isdir("/etc/modules-load.d") and not os.path.exists(I2C_MODULES_LOAD_CONF):
            _atomic_write_text(I2C_MODULES_LOAD_CONF, "i2c-dev\n", mode=0o644)
    except Exception:
        pass
    if not os.path.exists("/dev/i2c-2"):
        for modprobe_bin in ("/sbin/modprobe", "/usr/sbin/modprobe", "modprobe"):
            try:
                subprocess.run([modprobe_bin, "i2c-dev"], capture_output=True, timeout=5)
                if os.path.exists("/dev/i2c-2"):
                    break
            except Exception:
                continue

def _read_current_hotspot_ssid():
    try:
        if os.path.exists(HOTSPOT_CONF_PATH):
            with open(HOTSPOT_CONF_PATH, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("SSID="):
                        return line.split("=", 1)[1].rstrip("\r\n")
    except Exception:
        pass
    return ""

def _parse_nfc_status_output(stdout_text, hotspot_ssid=""):
    lines = [ln.strip() for ln in (stdout_text or "").splitlines() if ln.strip()]
    first = lines[0] if lines else "empty"
    rf_on = not any("NFC is off" in ln for ln in lines)
    mode = "other"
    wifi_ssid = ""
    wifi_encrypted = False
    value = ""
    summary = first

    if first.startswith("wifi: "):
        mode = "wifi"
        rest = first[6:]
        if rest.endswith(" (with its password)"):
            wifi_ssid = rest[:-20]
            wifi_encrypted = True
            summary = f"Wi-Fi 一碰连：{wifi_ssid}（含密码）"
        elif rest.endswith(" (open)"):
            wifi_ssid = rest[:-7]
            wifi_encrypted = False
            summary = f"Wi-Fi 一碰连：{wifi_ssid}（开放免密）"
        else:
            wifi_ssid = rest
            summary = f"Wi-Fi 一碰连：{wifi_ssid}"
    elif first.startswith("url: "):
        mode = "url"
        value = first[5:]
        summary = f"网址链接：{value}"
    elif first.startswith("text: "):
        mode = "text"
        value = first[6:]
        summary = f"纯文本：{value}"
    elif first in ("empty", "no NDEF message"):
        mode = "empty"
        summary = "空白标签（尚未写入数据）"
    elif first.startswith("other: "):
        mode = "other"
        value = first[7:]
        summary = f"自定义记录（{value}）"

    in_sync = bool(mode == "wifi" and hotspot_ssid and wifi_ssid == hotspot_ssid)
    return {
        "ok": 1,
        "supported": True,
        "enabled": rf_on,
        "mode": mode,
        "raw": first,
        "summary": summary,
        "wifi_ssid": wifi_ssid,
        "wifi_encrypted": wifi_encrypted,
        "hotspot_ssid": hotspot_ssid,
        "in_sync": in_sync,
        "value": value,
    }

def _is_nfc_supported():
    global _NFC_SUPPORTED_CACHE
    if _NFC_SUPPORTED_CACHE is not None:
        return _NFC_SUPPORTED_CACHE
    if not os.path.exists(NFC_BIN):
        _NFC_SUPPORTED_CACHE = False
        return False
    try:
        proc = subprocess.run([NFC_BIN, "supported"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        _NFC_SUPPORTED_CACHE = (proc.returncode == 0)
    except Exception:
        _NFC_SUPPORTED_CACHE = False
    return _NFC_SUPPORTED_CACHE

def get_nfc_status(force=False):
    now = time.time()
    if not force and NFC_STATUS_CACHE["data"] and (now - NFC_STATUS_CACHE["time"] < 120):
        cached = dict(NFC_STATUS_CACHE["data"])
        cur_ssid = _read_current_hotspot_ssid()
        cached["hotspot_ssid"] = cur_ssid
        cached["in_sync"] = bool(cached.get("mode") == "wifi" and cur_ssid and cached.get("wifi_ssid") == cur_ssid)
        return cached

    if not _is_nfc_supported():
        return {
            "ok": 1,
            "supported": False,
            "enabled": False,
            "mode": "unsupported",
            "summary": "当前设备未检测到可编程 NFC 标签芯片",
            "hotspot_ssid": _read_current_hotspot_ssid(),
            "in_sync": False,
            "value": "",
        }

    if not NFC_LOCK.acquire(timeout=6.0):
        if NFC_STATUS_CACHE["data"]:
            return dict(NFC_STATUS_CACHE["data"])
        return {"ok": 0, "error": "NFC 芯片正在读写中，请稍后重试"}

    try:
        _ensure_i2c_dev_ready()
        proc = subprocess.run([NFC_BIN, "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return {"ok": 0, "supported": True, "error": f"读取 NFC 芯片失败: {err or f'exit {proc.returncode}'}"}
        parsed = _parse_nfc_status_output(proc.stdout, _read_current_hotspot_ssid())
        NFC_STATUS_CACHE["data"] = dict(parsed)
        NFC_STATUS_CACHE["time"] = time.time()
        return parsed
    except Exception as e:
        return {"ok": 0, "supported": True, "error": f"读取 NFC 异常: {e}"}
    finally:
        NFC_LOCK.release()

def set_nfc_rf_power(enable):
    if not _is_nfc_supported():
        return {"ok": 0, "error": "当前设备不支持可编程 NFC"}
    target = "on" if bool(enable) else "off"
    if not NFC_LOCK.acquire(timeout=6.0):
        return {"ok": 0, "error": "NFC 芯片忙，请稍后重试"}
    parsed = None
    try:
        _ensure_i2c_dev_ready()
        proc = subprocess.run([NFC_BIN, target], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return {"ok": 0, "error": f"切换 NFC 射频失败: {err or f'exit {proc.returncode}'}"}
        proc_st = subprocess.run([NFC_BIN, "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        if proc_st.returncode == 0:
            parsed = _parse_nfc_status_output(proc_st.stdout, _read_current_hotspot_ssid())
            NFC_STATUS_CACHE["data"] = dict(parsed)
            NFC_STATUS_CACHE["time"] = time.time()
        else:
            NFC_STATUS_CACHE["time"] = 0
    except Exception as e:
        return {"ok": 0, "error": f"切换 NFC 异常: {e}"}
    finally:
        NFC_LOCK.release()

    msg = "NFC 射频已开启（手机贴近机身即可感应）" if target == "on" else "NFC 射频已关闭（防他人贴近蹭网）"
    return {"ok": 1, "msg": msg, "status": parsed or get_nfc_status(force=True)}

def write_nfc_payload(mode, value=""):
    if not _is_nfc_supported():
        return {"ok": 0, "error": "当前设备不支持可编程 NFC"}
    mode = str(mode or "").strip().lower()
    val = str(value or "").strip()

    if mode in ("wifi", "sync"):
        cmd = [NFC_BIN, mode]
        ok_msg = "已将当前 Wi-Fi 名称与密码同步写入 NFC 芯片"
    elif mode == "clear":
        cmd = [NFC_BIN, "clear"]
        ok_msg = "NFC 标签内容已清空"
    elif mode == "url":
        if not val or not re.match(r"^(https?://|tel:|mailto:)", val, re.I):
            return {"ok": 0, "error": "请输入以 https://、http://、tel: 或 mailto: 开头的有效链接"}
        if len(val) > 512 or any(c in val for c in ("\r", "\n", "\x00")):
            return {"ok": 0, "error": "链接长度超限或包含非法控制字符"}
        cmd = [NFC_BIN, "url", val]
        ok_msg = "已将自定义网址写入 NFC 芯片（安卓与 iPhone 均可一碰打开）"
    elif mode == "text":
        if not val:
            return {"ok": 0, "error": "请输入要写入标签的文本内容"}
        if len(val.encode("utf-8")) > 512 or "\x00" in val:
            return {"ok": 0, "error": "文本长度超过 NFC 标签容量上限"}
        cmd = [NFC_BIN, "text", val]
        ok_msg = "已将纯文本内容写入 NFC 芯片"
    else:
        return {"ok": 0, "error": f"不支持的 NFC 写入模式: {mode}"}

    if not NFC_LOCK.acquire(timeout=8.0):
        return {"ok": 0, "error": "NFC 芯片忙，请稍后重试"}
    parsed = None
    try:
        _ensure_i2c_dev_ready()
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=12)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return {"ok": 0, "error": f"NFC 写入失败: {err or f'exit {proc.returncode}'}"}
        proc_st = subprocess.run([NFC_BIN, "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        if proc_st.returncode == 0:
            parsed = _parse_nfc_status_output(proc_st.stdout, _read_current_hotspot_ssid())
            NFC_STATUS_CACHE["data"] = dict(parsed)
            NFC_STATUS_CACHE["time"] = time.time()
        else:
            NFC_STATUS_CACHE["time"] = 0
    except Exception as e:
        return {"ok": 0, "error": f"NFC 写入异常: {e}"}
    finally:
        NFC_LOCK.release()

    return {"ok": 1, "msg": ok_msg, "status": parsed or get_nfc_status(force=True)}

def trigger_nfc_wifi_sync_async(delay_sec=3.0):
    """热点配置更新后延迟兜底同步 NFC 标签，与 hotspot-start 形成双保险且用 NFC_LOCK 防并发竞争"""
    NFC_STATUS_CACHE["time"] = 0
    def _worker():
        time.sleep(max(0.5, float(delay_sec)))
        if not _is_nfc_supported():
            return
        if not NFC_LOCK.acquire(timeout=8.0):
            return
        try:
            _ensure_i2c_dev_ready()
            subprocess.run([NFC_BIN, "sync"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=12)
            proc_st = subprocess.run([NFC_BIN, "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
            if proc_st.returncode == 0:
                NFC_STATUS_CACHE["data"] = _parse_nfc_status_output(proc_st.stdout, _read_current_hotspot_ssid())
                NFC_STATUS_CACHE["time"] = time.time()
        except Exception as e:
            print(f"[NFC Sync] {e}")
        finally:
            NFC_LOCK.release()
    threading.Thread(target=_worker, daemon=True).start()

def handle_nfc_api(method, req_data):
    req_data = req_data if isinstance(req_data, dict) else {}
    if method == "nfc.status":
        return get_nfc_status(force=bool(req_data.get("force", False)))
    if method == "nfc.toggle":
        if "enable" in req_data:
            desired = bool(req_data["enable"])
        else:
            cur = get_nfc_status(force=False)
            desired = not bool(cur.get("enabled", True))
        return set_nfc_rf_power(desired)
    if method == "nfc.write":
        return write_nfc_payload(req_data.get("mode", "wifi"), req_data.get("value", ""))
    return None

def main():
    print(f"[*] 启动 U30 Air 极轻量 Web 控制台服务 (带身份鉴权与日志守护)...")
    print(f"[*] 静态文件根目录: {WEB_DIR}")
    print(f"[*] 监听端口: {PORT}")
    _ensure_i2c_dev_ready()
    update_public_ip_async()
    start_log_guardian()
    start_comm_and_quota_watcher()
    server = ThreadedHTTPServer(("0.0.0.0", PORT), WebUIHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 正在关闭服务...")
        server.server_close()

if __name__ == "__main__":
    main()
