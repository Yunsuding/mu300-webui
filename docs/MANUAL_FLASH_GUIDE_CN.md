# ZTE U30 Air (mu300) 刷机前校准分区冷备与 Ubuntu 24.04 独立维护指南

> **适用版本**：Ubuntu 24.04 LTS (ARM64) + 展锐原厂内核 `Linux 5.4.254`（推荐稳定基线 `v2026.10.27.2`）

---

## 一、跨设备刷机安全铁律

每一台 ZTE U30 Air 的 `prodnv`、`nr_fixnv1/2`、`calinv`、`miscdata`、`persist` 分区均保存着**该台设备独一无二的 IMEI 串号与 5G 射频校准参数**：
- **严禁**将 A 设备的全盘 `dd` 镜像写入 B 设备！
- **正确做法**：每台新机先在已 Root 的原厂 Android 下运行 `tools/backup_nv_windows.py` 导出并校验本机 29 个关键分区，再使用 `mu300-magisk-v2026.10.27.2-ubuntu-24.04-k5.4.zip` 离线安装包自动提取本机驱动并合成干净 Ubuntu 24.04 系统。

---

## 二、手工零 AI 三步刷机流程

### 第 0 步：备份新机独有 IMEI 与校准分区（原厂 Root Android 状态）
1. 设备进入原厂 Android（已安装 Magisk 并开启 USB 调试），通过 USB 连接电脑。
2. 在电脑运行：
   ```bash
   python tools/backup_nv_windows.py
   ```
3. 确认终端提示全部 29 个分区导出且 `SHA256` 逐字节校验通过后，将生成的 `device-backups/nv-backups/<序列号>-<时间>/` 目录妥善保存至 U 盘或冷备硬盘。

### 第 1 步：离线刷入 Ubuntu 24.04 + 5.4 内核底座
1. 解压 `mu300-magisk-v2026.10.27.2-ubuntu-24.04-k5.4.zip` 到纯英文路径。
2. 参考 `tools/u30air-ubuntu2404-k54.answers.example` 选择：
   - 系统版本：`Ubuntu 24.04`
   - 内核版本：`5.4`（支持 SIPA 硬件转发与 SQC 0mA 硬件旁路限充）
   - 旧版 Cockpit 面板（`Panel`）：选 **`No`**（节省 110MB+ 内存，后续由本仓库 `:8080` 轻量面板完全接管）
3. 运行 `start.cmd`（Windows）或 `start.sh`（macOS/Linux），输入 `INSTALL` 完成刷入并自动重启进入 Ubuntu。

### 第 2 步：一键安装 `:8080` 极轻量控制台与硬件优化
设备重启进入 Ubuntu（默认网关 `192.168.78.1`）后，在电脑执行：
```bash
pip install -r requirements.txt
cp .env.example .env   # 填入刚才设置的 ubuntu 登录密码
python deploy.py
```
或在设备上直接运行 `sudo bash install.sh --all -y`，随后浏览器访问 `http://192.168.78.1:8080/` 即可。

---

## 三、长期独立维护建议

1. **保持 `Linux 5.4` 原厂内核与 `Ubuntu 24.04 LTS`**：切勿切至主线 `6.6` 内核，否则将失去展锐硬件加速与 SQC 充电 IC 旁路控制能力。
2. **锁定无线与模块关键包**：建议在设备上执行 `sudo apt-mark hold hostapd kmod`，避免系统常规 apt 升级覆盖原厂 Wi-Fi 配套组件。
3. **停用上游更新轮询**：若无需跟随上游测试分支变动，可执行 `sudo systemctl disable --now mu300-update-notice.timer`。
