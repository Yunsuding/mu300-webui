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
NET_PREV = {"time": time.time(), "rx": 0, "tx": 0, "rx_rate": 0, "tx_rate": 0}

def get_realtime_traffic():
    """实时读取 /proc/net/dev 获取微秒级精确上下行网速"""
    global NET_PREV
    now = time.time()
    dt = max(0.2, now - NET_PREV["time"])
    try:
        with open("/proc/net/dev", "r") as f:
            lines = f.readlines()
        for line in lines[2:]:
            parts = line.split(":")
            if len(parts) == 2 and parts[0].strip() == "sipa_eth0":
                fields = parts[1].split()
                rx = int(fields[0])
                tx = int(fields[8])
                if NET_PREV["rx"] > 0:
                    rx_rate = max(0.0, (rx - NET_PREV["rx"]) / dt)
                    tx_rate = max(0.0, (tx - NET_PREV["tx"]) / dt)
                    NET_PREV["rx_rate"] = rx_rate
                    NET_PREV["tx_rate"] = tx_rate
                NET_PREV["rx"] = rx
                NET_PREV["tx"] = tx
                NET_PREV["time"] = now
                break
    except Exception:
        pass
    return {
        "ok": 1,
        "rx_rate": NET_PREV["rx_rate"],
        "tx_rate": NET_PREV["tx_rate"],
        "rx_bytes": NET_PREV["rx"],
        "tx_bytes": NET_PREV["tx"]
    }

CELLULAR_CACHE = {
    "time": 0,
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

def get_cellular_qos():
    """实时读取蜂窝射频与 QoS 签约状态 (QCI, 签约上下行限速, 5G频段, SINR/RSRP/RSRQ)"""
    global CELLULAR_CACHE
    now = time.time()
    if now - CELLULAR_CACHE["time"] < 10 and CELLULAR_CACHE["data"]:
        return CELLULAR_CACHE["data"]
    
    data = dict(CELLULAR_CACHE["data"])
    try:
        at_bin = "/opt/mu300/bin/mu300-at"
        if os.path.exists(at_bin):
            # 1. 信号质量 (CESQ: ss_rsrq, ss_rsrp, ss_sinr)
            proc = subprocess.run([at_bin, "AT+CESQ"], capture_output=True, text=True, timeout=3)
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

            # 2. 签约 QoS 状态 (C5GQOSRDP / CGEQOSRDP: QCI 与 AMBR 签约速率)
            proc = subprocess.run([at_bin, "AT+C5GQOSRDP=1"], capture_output=True, text=True, timeout=3)
            out = proc.stdout
            if "+C5GQOSRDP:" not in out:
                proc = subprocess.run([at_bin, "AT+CGEQOSRDP=1"], capture_output=True, text=True, timeout=3)
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

            # 3. 运营商与制式 (COPS)
            proc = subprocess.run([at_bin, "AT+COPS?"], capture_output=True, text=True, timeout=3)
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
                
                # 读取锁定配置或实际驻留频段
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

                if "5G" in data["rat"]:
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
        CELLULAR_CACHE["data"] = data
    except Exception as e:
        print(f"[Cellular QoS Error] {e}")

    return data

CELLULAR_IP_CACHE = {
    "time": 0,
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
    """获取移动蜂窝网络真实公网出口 IPv4 (直接绑定 sipa_eth0 网卡，防止被本地代理干扰)"""
    # 方式 1: 优先直接绑定 sipa_eth0 接口执行 curl 获取真实蜂窝出口
    try:
        proc = subprocess.run(
            ["curl", "--interface", "sipa_eth0", "-4", "-s", "--connect-timeout", "2", "http://myip.ipip.net"],
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
    """获取移动蜂窝网络的内网 IPv4、公网出口 IPv4 与 IPv6 地址 (毫秒级响应)"""
    global CELLULAR_IP_CACHE
    now = time.time()
    if now - CELLULAR_IP_CACHE["time"] < 3 and CELLULAR_IP_CACHE["data"].get("ipv4"):
        # 补充异步更新公网 IP
        update_public_ip_async()
        ret = dict(CELLULAR_IP_CACHE["data"])
        if PUBLIC_IP_CACHE["ip"]:
            ret["public_ipv4"] = PUBLIC_IP_CACHE["ip"]
        return ret

    info = {
        "ipv4": "",
        "public_ipv4": PUBLIC_IP_CACHE["ip"],
        "ipv6": "",
        "iface": "sipa_eth0"
    }

    # 1. 获取蜂窝接口内网 IPv4 (优先通过 socket ioctl 获取 sipa_eth0 地址，零子进程)
    target_ifaces = ["sipa_eth0", "sipa_eth1"]
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

    # 2. 如果 ioctl 未获取到，解析 ip -4 addr show sipa_eth0
    if not info["ipv4"]:
        try:
            proc = subprocess.run(["ip", "-4", "addr", "show", "sipa_eth0"], capture_output=True, text=True, timeout=1.5)
            m = re.search(r'inet\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)', proc.stdout)
            if m:
                info["ipv4"] = m.group(1)
                info["iface"] = "sipa_eth0"
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
    CELLULAR_IP_CACHE["data"] = info
    return info

BATTERY_INFO_CACHE = {"time": 0, "data": None}

def get_battery_info(force=False):
    """Read policy and hardware independently; an attached cable is not charging."""
    try:
        import time as _time
        now = _time.time()
        cache = globals().get("BATTERY_INFO_CACHE")
        if not force and cache and cache.get("data") is not None and (now - cache.get("time", 0) < 4.5):
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

    supply = "/sys/class/power_supply/"
    inputs = [read(supply + p + "/online") for p in ("ac", "usb", "bq2560x_charger")]
    plugged = True if "1" in inputs else (False if "0" in inputs else None)
    cap = number(read(supply + "battery/capacity"))
    volts = number(read(supply + "battery/voltage_now"), 1000000)
    raw = read(supply + "bq2560x_charger/status") or "Unknown"
    policy = {}
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

    # The helper rejects unknown kernels/chips. Also compare its HAL read with
    # the actual BQ CHG_CONFIG bit, rather than trusting the policy marker.
    enabled = None
    verified = False
    try:
        result = subprocess.run(["/opt/mu300/bin/mu300-sqc-charge", "get"],
                                text=True, capture_output=True, timeout=2)
        regs = read(supply + "bq2560x_charger/device/registers") or ""
        primary = regs.split("SLAVE Reg:")[0]
        match = re.search(r"^Reg\[01\] = 0x([0-9a-fA-F]{2})$", primary, re.M)
        if result.returncode == 0 and result.stdout.strip() in ("0", "1") and match:
            hal = result.stdout.strip() == "1"
            if hal == bool(int(match[1], 16) & 0x10):
                enabled, verified = hal, True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass

    reason = policy.get("charge_off")
    full_once = str(conf.get("FULL_ONCE", 0)) == "1"
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
        elif raw.lower() == "charging" or (number(battery.get("ma")) or 0) > 20:
            charging = True
            state, text = "charging", "充电中（充满一次）" if full_once else "充电中"
        else:
            state, text = "enabled", "充电已允许（待充电）"
    elif plugged is True:
        text = "电源已连接，充电开关未核实"
    limit = number(conf.get("CHARGE_TO"))
    resume = number(conf.get("RESUME_AT"))
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
    if full_once:
        detail += "；充满一次已开启（仍受温度保护）"
    res = {"capacity": cap, "plugged": plugged, "charging": charging,
            "state": state, "status_text": text, "voltage_v": volts,
            "raw_charger": raw, "switch_enabled": enabled, "control_verified": verified,
            "pause_reason": reason, "charge_limit": limit, "resume_below": resume,
            "full_once": full_once, "current_ma": number(battery.get("ma")),
            "temperature_c": number(battery.get("temp"), 10), "detail_text": detail}
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
    """读取当前局域网各 MAC 正在使用的实时 IP (ARP 邻居表 + DHCP 租约 + 变更前缓存，0 子进程开销)"""
    subnet_prefix, gw_ip = get_lan_subnet()
    mac_ips = {}

    def _add(mac_str, ip_str):
        mac_str = str(mac_str or "").lower().strip()
        ip_str = str(ip_str or "").strip()
        if not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac_str) or mac_str == "00:00:00:00:00:00":
            return
        if not ip_str.startswith(subnet_prefix) or ip_str == gw_ip:
            return
        lst = mac_ips.setdefault(mac_str, [])
        if ip_str not in lst:
            lst.append(ip_str)

    # 1. 优先读取 /proc/net/arp 中活跃的局域网邻居 IP
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
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                now = time.time()
                SESSIONS = {k: v for k, v in data.items() if v.get("expire", 0) > now}
        except Exception:
            SESSIONS = {}

def save_sessions():
    try:
        now = time.time()
        valid = {k: v for k, v in SESSIONS.items() if v.get("expire", 0) > now}
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(valid, f)
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
                # 统一标准化为 2 空格缩进 "  - "
                curr_item["lines"].append("  - " + stripped[2:] + "\n")
                after_dash = stripped[2:].strip()
                if after_dash.startswith("name:"):
                    curr_item["name"] = after_dash.split(":", 1)[1].strip().strip("'\"")
            elif curr_item is not None:
                # 保持子属性层级缩进（若原文件顶层列表无缩进则补 2 空格）
                lead_spaces = len(line) - len(line.lstrip(" "))
                norm_line = ("  " + line) if lead_spaces < 4 else line
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
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            target_ips.append(ip)
        if mac and mac in mac_live_ips:
            for lip in mac_live_ips[mac]:
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", lip) and lip not in target_ips:
                    if lip not in bound_ip_owner or bound_ip_owner[lip] == mac:
                        target_ips.append(lip)
        for tip in target_ips:
            rule_s = f"SRC-IP-CIDR,{tip}/32,{grp}"
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
            s = socket.create_connection((server, port), timeout=3.0)
            s.close()
            ms = max(1, round((time.time() - t0) * 1000))
            return {"ok": 1, "delay": ms}
        except Exception:
            pass

    q = urllib.parse.urlencode({"url": "https://www.gstatic.com/generate_204", "timeout": 3500, "expected": "204"})
    code, res = mihomo_controller_request("GET", f"/proxies/{urllib.parse.quote(node_name, safe='')}/delay?{q}", timeout=4.2)
    if code == 200 and isinstance(res, dict) and isinstance(res.get("delay"), int) and res["delay"] > 0:
        return {"ok": 1, "delay": res["delay"]}

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
    """调用底层的 mu300-api"""
    if not os.path.exists(API_BIN):
        return {"ok": 0, "error": f"API binary {API_BIN} not found"}
    try:
        payload = json.dumps(req or {})
        proc = subprocess.Popen(
            [API_BIN, method],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = proc.communicate(input=payload, timeout=25)
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
    if os.path.exists("/sys/class/net/sipa_eth0"):
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
        if not token or token not in SESSIONS:
            return False, None
        session = SESSIONS[token]
        if session.get("expire", 0) < time.time():
            del SESSIONS[token]
            save_sessions()
            return False, None
        return True, session.get("user")

    def send_json(self, data, code=200, set_cookie=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

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
            ctype, _ = mimetypes.guess_type(file_path)
            if not ctype:
                ctype = "text/html" if file_path.endswith(".html") else "application/octet-stream"
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
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
            now = time.time()
            fail_rec = LOGIN_FAILURES.get(client_ip, {"count": 0, "until": 0})
            if fail_rec["until"] > now:
                wait_s = int(fail_rec["until"] - now)
                self.send_json({"ok": 0, "error": f"密码错误次数过多，请等待 {wait_s} 秒后再试"}, code=429)
                return

            username = req_json.get("username", "").strip()
            password = req_json.get("password", "")
            remember = req_json.get("remember", True)

            if verify_system_password(username, password):
                LOGIN_FAILURES.pop(client_ip, None)
                token = secrets.token_hex(32)
                # 记住登录默认 30 天，否则 24 小时
                duration = 30 * 86400 if remember else 86400
                SESSIONS[token] = {
                    "user": username,
                    "expire": time.time() + duration
                }
                save_sessions()
                cookie = f"u30_session={token}; Path=/; Max-Age={duration}; SameSite=Lax; HttpOnly"
                self.send_json({"ok": 1, "username": username, "token": token}, set_cookie=cookie)
                print(f"[Auth] 用户 {username} 登录成功")
            else:
                cnt = fail_rec["count"] + 1
                LOGIN_FAILURES[client_ip] = {
                    "count": cnt,
                    "until": (now + 60) if cnt >= 5 else 0
                }
                self.send_json({"ok": 0, "error": "用户名或密码错误"}, code=401)
            return

        # 2. 登出端点
        if path == "/api/logout":
            token = self.get_token_from_request()
            if token and token in SESSIONS:
                del SESSIONS[token]
                save_sessions()
            cookie = "u30_session=; Path=/; Max-Age=0; SameSite=Lax; HttpOnly"
            self.send_json({"ok": 1}, set_cookie=cookie)
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

            resp = run_mu300_api(method, req_data)
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

def main():
    print(f"[*] 启动 U30 Air 极轻量 Web 控制台服务 (带身份鉴权与日志守护)...")
    print(f"[*] 静态文件根目录: {WEB_DIR}")
    print(f"[*] 监听端口: {PORT}")
    update_public_ip_async()
    start_log_guardian()
    server = ThreadedHTTPServer(("0.0.0.0", PORT), WebUIHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 正在关闭服务...")
        server.server_close()

if __name__ == "__main__":
    main()
