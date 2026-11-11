# ZTE U30 Air (`mu300`) 从原厂 Android 到满血 Ubuntu 24.04 全离线刷机与维护指南

> **适用机型**：中兴 ZTE U30 Air 5G 随身路由器（紫光展锐 T7510 / UDX710 平台，代号 `mu300`）  
> **锁定黄金固件基线**：`Ubuntu 24.04.5 LTS (ARM64)` + 展锐原厂内核 `Linux 5.4.254-gb50db5b6224c`（`v2026.10.27.2`）

由于上游 `mu300-linux` 项目重心已转向 OpenWrt，本仓库将 **Ubuntu 24.04 LTS 完整运行底座、跨平台离线刷机工具、深度优化的 Mihomo 透明代理框架、SQC 0mA 零循环旁路充电保护以及 `:8080` 极轻量 Web 控制台** 整合为一套可完全离线部署的独立发行版。

---

## 一、跨设备刷机安全第一铁律（必读）

1. **严禁跨设备直接 `dd` 全盘镜像！**
   - 每一台 ZTE U30 Air 的 `prodnv`、`nr_fixnv1_a/b`、`nr_fixnv2_a/b`、`calinv`、`miscdata`、`persist` 分区均存有**该台设备独一无二的 IMEI 串号与 5G 射频校准参数**。直接将 A 设备的整盘镜像写入 B 设备会导致串号冲突或无 5G 信号。
2. **标准「两段式全离线刷机法」（同型号任意机器 100% 安全通用）**：
   - **第 0 步（保命备份）**：在已 Root 的原厂 Android 下运行 `01_备份新机校准分区_Android状态.cmd`，导出该机独有的 29 个关键校准与引导分区并完成 SHA256 强校验。
   - **第 1 步（干净底座刷入）**：运行 `mu300-magisk-v2026.10.27.2-ubuntu-24.04-k5.4.zip` 离线安装包。安装包**不含任何中兴私有固件，也不触碰校准分区**，而是在安装时自动从本机 Android 分区提取驱动并合成纯净 Ubuntu 24.04 + 5.4 系统。
   - **第 2 步（一键满血注入）**：进入 Ubuntu 后运行 `02_一键注入满血系统_Ubuntu启动后.cmd`，约 30 秒离线装入**改版 VPN + Mihomo 核心 + 双栈分流 + Cortex-A76 大核绑定 + SQC 0mA 硬件旁路限充 + `:8080` 极轻量控制台**（彻底摒弃占内存 110MB+ 的旧版 9090 Cockpit 面板）。

---

## 二、准备离线刷机包（无需登录 / 无需 Token）

在电脑上安装 Python 3.10+ 并执行：

```bash
git clone https://github.com/Yunsuding/mu300-webui.git
cd mu300-webui
pip install -r requirements.txt

# 自动从本仓库 GitHub Release (baseline-v2026.10.27.2) 下载全部 9 个离线包并逐字节校验 SHA256
python standalone/fetch_offline_bundles.py
```

> 如仅需核心二合一离线刷机包 + 改版 VPN/Mihomo 包（共约 231 MB），可加参数 `python standalone/fetch_offline_bundles.py --essential-only`。  
> 离线查看原厂 Android Root 与按键图文教程：直接用浏览器打开本地文件 `standalone/upstream-installer/site/zh/install/index.html`。

---

## 三、三步手工刷入新机实操流程

### 第 0 步：备份本机 IMEI 与 5G 校准分区（原厂 Root Android 状态）
1. 确认 U30 Air 处于原厂 Android 系统（电量 ≥ 50%），已安装 Magisk 获取 Root 权限并开启 USB 调试。
2. USB 连接电脑（在设备 Magisk 弹窗点允许 Root 授权），Windows 用户直接双击 `standalone/01_备份新机校准分区_Android状态.cmd`（macOS/Linux 执行 `python3 standalone/backup_nv_windows.py`）。
3. 看到 `[SUCCESS] 全部 29 个关键分区已导出并通过 SHA256 逐字节校验！` 后，将 `device-backups/nv-backups/<序列号>-<时间>/` 文件夹额外拷贝一份到 U 盘永久保存。

### 第 1 步：离线刷入干净的 Ubuntu 24.04 + 5.4 底座
1. 解压 `device-backups/offline-bundles/mu300-magisk-v2026.10.27.2-ubuntu-24.04-k5.4.zip` 到纯英文无空格目录（如 `D:\u30air_flash\`）。
2. 双击运行解压目录内的 `start.cmd`（macOS/Linux 运行 `./start.sh`，也可复制 `standalone/u30air-ubuntu2404-k54.answers.example` 预设密码免交互运行）：
   - 系统选择 **`Ubuntu 24.04`**，内核选择 **`5.4`**；
   - 额外组件 `Panel`（旧版 Cockpit）与 `VPN`（原版未打补丁 VPN）均选 **`No`**；
   - 设置你的 `ubuntu` 登录密码，最后输入大写 `INSTALL` 确认，等待约 3~5 分钟自动重启进入 Ubuntu。
   - *(备选免电脑方式：也可直接在安卓端打开 Magisk App → 模块 → 从本地安装该 `.zip` 包并重启)*。

### 第 2 步：一键离线注入改版 VPN、0mA 限充与 `:8080` 面板
1. 设备重启进入 Ubuntu 后，通过 USB RNDIS 或连接随身 Wi-Fi 访问默认网关 `192.168.78.1`。
2. Windows 双击 `standalone/02_一键注入满血系统_Ubuntu启动后.cmd`（或命令行执行 `python standalone/provision_device.py --host 192.168.78.1 --mode clean`）。
3. 输入第 1 步设置的 `ubuntu` 密码，等待约 30 秒看到 `WEBUI_HEALTH=200_OK` 与 `[SUCCESS]`。
4. 打开浏览器访问 **`http://192.168.78.1:8080/`**，使用 `ubuntu` 账号登录即可开始配置 Wi-Fi、5G 频段锁定与 Clash/Mihomo 订阅！

---

## 四、无损回退原厂 Android 与日常维护守则

- **切回原厂 Android**：SSH 登录执行 `sudo /opt/mu300/bin/mu300-boot android && sudo reboot`（或在电脑执行 `powershell -ExecutionPolicy Bypass -File standalone\upstream-installer\install.ps1 -Uninstall`）。
- **日常更新面板与底层脚本**：本地拉取最新代码后运行 `python deploy.py --with-runtime` 即可平滑热更新，无需重刷系统。
- **维护安全守则**：
  1. **请保持 `Linux 5.4` 原厂内核**：主线 `6.6+` 内核不支持展锐 SIPA 硬件转发与 SQC 充电 IC 0mA 硬件旁路寄存器。
  2. **保持 `hostapd` 与 `kmod` 锁定（`apt-mark hold`）**：注入脚本已自动设置，请勿手动解锁以免破坏原厂 Wi-Fi 驱动兼容。
  3. **需要 Tailscale 异地组网时**：在每台设备上各自执行 `sudo tailscale up` 获取独立节点身份，切勿跨设备直接拷贝 `/var/lib/tailscale`。
