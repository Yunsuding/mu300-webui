#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cross-platform (Windows / macOS / Linux) per-device NV, IMEI, RF calibration & bootchain backup tool.
Python equivalent of upstream tools/backup-device.sh — MUST be run while the U30 Air is booted in rooted Android!

Saves verified partition dumps into device-backups/nv-backups/<serial>-<timestamp>/ (excluded from Git).
"""

import argparse
import datetime
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

CRITICAL_PARTITIONS = [
    "prodnv", "miscdata", "persist", "ztecfg", "ztepersist", "calinv", "isedata",
    "teecfg_a", "teecfg_b",
    "nr_fixnv1_a", "nr_fixnv1_b", "nr_fixnv2_a", "nr_fixnv2_b",
    "nr_runtimenv1", "nr_runtimenv2",
    "nr_deltanv_a", "nr_deltanv_b", "nr_phy_a", "nr_phy_b",
]

BOOTCHAIN_PARTITIONS = [
    "sml_a", "sml_b", "uboot_a", "uboot_b", "trustos_a", "trustos_b", "vbmeta_a", "vbmeta_b", "misc",
]

STOCK_BOOT_IMAGES = [
    "boot_a", "boot_b", "vendor_boot_a", "vendor_boot_b",
    "init_boot_a", "init_boot_b", "dtb_a", "dtb_b", "dtbo_a", "dtbo_b",
]


def find_adb() -> str:
    adb = shutil.which("adb")
    if adb:
        return adb
    exe = "adb.exe" if os.name == "nt" else "adb"
    candidates = [
        ROOT / "platform-tools" / exe,
        ROOT.parent / "platform-tools" / exe,
        Path.home() / "Downloads" / "platform-tools" / exe,
        Path.home() / "Desktop" / "platform-tools" / exe,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    raise RuntimeError("找不到 adb！请安装 Android platform-tools 或将 platform-tools 文件夹放在项目目录旁。")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


class AdbAndroid:
    def __init__(self, adb_bin: str, serial: str = ""):
        self.adb = adb_bin
        self.serial = serial

    def _base(self):
        return [self.adb] + (["-s", self.serial] if self.serial else [])

    def run(self, *args, check=True) -> str:
        p = subprocess.run(self._base() + list(args), capture_output=True, text=True, timeout=120)
        if check and p.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)} 失败: {p.stderr.strip() or p.stdout.strip()}")
        return p.stdout.replace("\r\n", "\n").strip()

    def su(self, cmd: str, check=True) -> str:
        return self.run("shell", f"su -c '{cmd}'", check=check)

    def pull_via_tmp(self, remote_expr: str, local_dest: Path):
        tmp_remote = "/data/local/tmp/mu300-nv-pull.bin"
        try:
            self.su(f"cat {remote_expr} > {tmp_remote}")
            p = subprocess.run(self._base() + ["pull", tmp_remote, str(local_dest)], capture_output=True, text=True, timeout=300)
            if p.returncode != 0:
                raise RuntimeError(f"adb pull 失败: {p.stderr.strip()}")
        finally:
            self.su(f"rm -f {tmp_remote}", check=False)


def main():
    parser = argparse.ArgumentParser(description="备份新 ZTE U30 Air 不可替代的基带校准/IMEI 与引导链分区（需在已 Root 的 Android 下运行）")
    parser.add_argument("--serial", default="", help="指定 adb 设备序列号（单设备连接时自动识别）")
    parser.add_argument("--include-boot-images", action="store_true", help="同时备份原厂 boot_a/boot_b 等大体积启动镜像")
    parser.add_argument("--out", default="", help="自定义保存目录（默认: device-backups/nv-backups/<序列号>-<时间>）")
    args = parser.parse_args()

    adb_bin = find_adb()
    adb = AdbAndroid(adb_bin, args.serial)

    state = adb.run("get-state", check=False)
    if state != "device":
        raise SystemExit("错误：未检测到已连接的 Android ADB 设备！请确认设备已开机进入原厂 Android、已开启 USB 调试并在弹窗中允许授权。")

    serial = adb.run("get-serialno")
    if serial.startswith("MU300LINUX"):
        raise SystemExit(
            f"错误：当前设备 ({serial}) 正在运行 MU300 Linux，而非原厂 Android！\n"
            "若需备份原厂 Android 分区表与 NV 校准数据，请先切回 Android (sudo mu300-next-boot android && sudo reboot)。"
        )

    uid = adb.su("id -u", check=False)
    if uid != "0":
        raise SystemExit("错误：adb shell su 无法获取 root 权限（返回非 0）！请打开 Magisk 授予 Shell root 权限后重试。")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_serial = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in serial)
    out_dir = Path(args.out).resolve() if args.out else (ROOT / "device-backups" / "nv-backups" / f"{safe_serial}-{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    parts = list(CRITICAL_PARTITIONS) + list(BOOTCHAIN_PARTITIONS)
    if args.include_boot_images:
        parts.extend(STOCK_BOOT_IMAGES)

    print(f"==> 开始备份设备 [{serial}] 的专属不可替代分区 -> {out_dir}")
    total_bytes = 0
    sums_lines = []

    for name in parts:
        dev = adb.su(f"readlink -f /dev/block/by-name/{name} 2>/dev/null", check=False).strip()
        if not dev:
            print(f"  [跳过] {name:16s} (当前机型不存在此分区)")
            continue
        dest = out_dir / f"{name}.img"
        adb.pull_via_tmp(dev, dest)
        sz = dest.stat().st_size if dest.exists() else 0
        if sz == 0:
            dest.unlink(missing_ok=True)
            print(f"  [跳过] {name:16s} (空分区)")
            continue
        want = adb.su(f"sha256sum {dev}").split()[0]
        have = sha256_file(dest)
        if want != have:
            raise RuntimeError(f"致命错误：分区 {name} 导出校验不一致！设备={want} 本地={have}")
        sums_lines.append(f"{have}  {name}.img")
        total_bytes += sz
        print(f"  [ OK ] {name:16s} {sz / 1048576:7.2f} MiB  (sha256={have[:12]}...)")

    (out_dir / "sha256sums.txt").write_text("\n".join(sums_lines) + "\n", encoding="utf-8")

    # Save GPT layout & header
    gpt_layout = adb.su('for p in /sys/block/mmcblk0/mmcblk0p*; do echo "${p##*/} $(cat $p/start) $(cat $p/size)"; done')
    (out_dir / "gpt-layout.txt").write_text(gpt_layout + "\n", encoding="utf-8")

    tmp_gpt = "/data/local/tmp/mu300-gpt.bin"
    try:
        adb.su(f"dd if=/dev/block/mmcblk0 bs=512 count=34 2>/dev/null > {tmp_gpt}")
        subprocess.run(adb._base() + ["pull", tmp_gpt, str(out_dir / "gpt-header.bin")], check=True, capture_output=True)
    finally:
        adb.su(f"rm -f {tmp_gpt}", check=False)

    by_name = adb.su("ls -l /dev/block/by-name")
    (out_dir / "by-name.txt").write_text(by_name + "\n", encoding="utf-8")

    print(f"\n==> 备份圆满完成！共导出 {total_bytes / 1048576:.2f} MiB 至：{out_dir}")
    print("重要提示：此文件夹包含本台机器独有的 IMEI 与射频校准参数，请永久妥善保存在电脑/网盘加密备份，切勿上传公开仓库，也切勿刷入另一台机器！")


if __name__ == "__main__":
    main()
