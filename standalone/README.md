# ZTE U30 Air (Ubuntu 24.04 LTS) 独立维护与零 AI 离线刷机套件 (`standalone/`)

由于上游 `mu300-linux` 已转向以 OpenWrt 为主，本目录将 **Ubuntu 24.04.5 ARM64 (`v2026.10.27.2`, 展锐原厂内核 `Linux 5.4.254-gb50db5b6224c`)** 所需的全部设备端原生运行时、**自研改版 VPN + Mihomo 框架**、跨平台离线底包安装器、新机校准分区冷备工具及一键注入脚本完整收编，实现 **100% 脱离上游在线依赖、脱离旧版 9090 Cockpit 面板、无 AI 也能全手工跨设备刷机与自主维护**。

> 📖 **完整傻瓜级手工刷机与日常维护教程**：请直接阅读 [`docs/MANUAL_FLASH_AND_MAINTENANCE.md`](../docs/MANUAL_FLASH_AND_MAINTENANCE.md)。

---

## 一、核心工具与目录结构

### 1. Windows 双击傻瓜向导与跨平台脚本

| 文件 | 作用 |
| :--- | :--- |
| `01_备份新机校准分区_Android状态.cmd` / `backup_nv_windows.py` | **【阶段 0：保命备份】** 在原厂 Root Android 状态下通过 ADB 导出新机独有的 29 个 IMEI / 5G 射频校准与引导分区，并逐个完成 SHA256 强校验 |
| `u30air-ubuntu2404-k54.answers.example` | **【阶段 1：底座应答模版】** 配合离线底包 `start.cmd` 或 `install.ps1` 使用，锁定 `Ubuntu 24.04 + Kernel 5.4`，默认跳过旧版 Cockpit 面板与未打补丁的原版 VPN |
| `02_一键注入满血系统_Ubuntu启动后.cmd` / `provision_device.py` | **【阶段 2：一键满血注入】** 设备进入 Ubuntu 后通过 SSH 全离线装入改版 VPN + Mihomo + SQC 0mA 旁路限充 + 大核绑定 + 安全锁 + `:8080` 轻量新面板（支持 `clean` 干净异机模式与 `restore-backup` 配置恢复模式） |
| `fetch_offline_bundles.py` & `bundle-manifest.json` | 免 Token 自动从 GitHub Release (`baseline-v2026.10.27.2`) 拉取全部 9 个离线底包与改版 VPN 包，并逐字节校验 SHA256 |

### 2. 已纳入 Git 仓库的系统底座与安装器（零敏感凭据）

- `device-runtime/`：从稳定运行的 U30 Air 实机导出的脱敏系统底座代码（共 280 项，附带 `manifest.json` 权限与 SHA256 清单）：
  - `opt-mu300/bin/`：含 `0 mA` 零循环旁路充电控制 `mu300-sqc-charge`、常在线 `mu300-power`、打补丁 `mu300-api`、静态 DHCP `lan-start`、蜂窝就绪门控 `u30air-wait-uplink`、加上独立维护安全锁的 `mu300-update` 与拦截旧面板安装的 `mu300-extra`
  - `opt-mu300/lib/`：公共硬件抽象、AT 指令与短信 PDU 编解码库
  - `extra-vpn/`：**自研改版 VPN 框架**（`mu300-vpn`、`mihomo.sh` 等，配合离线包 `u30air-patched-vpn-mihomo.tar.gz` 部署，彻底不用上游原版未打补丁 VPN）
  - `systemd/`：46 个系统服务与 Mihomo Cortex-A76 大核绑定（`CPUAffinity=4-7`）、6h 双订阅自动融合 Drop-In
  - `system-tuning/`：eMMC 延寿写回参数（`90-u30air-emmc.conf`）、`journald` 8MB 内存日志上限、内核模块与 udev 规则
  - `etc-mu300-templates/`：不含私密密码与订阅 URL 的纯净配置模版
- `upstream-installer/`：`v2026.10.27.2` 跨平台底座安装器（`install.ps1` / `install.cmd` / `install.sh`）及中文离线安装网页（`site/zh/install/index.html`）

---

## 二、常用命令速查

```powershell
# 1. 免登录一键从 GitHub Release 拉取并校验全部离线刷机底包 + 改版 VPN 包（约 433 MB）
python standalone/fetch_offline_bundles.py

# 仅下载核心全合一离线刷机包 + 改版 VPN/Mihomo 包（约 231 MB）
python standalone/fetch_offline_bundles.py --essential-only

# 2. 新机刷机前：备份 Android 下的 IMEI 与 5G 校准分区
python standalone/backup_nv_windows.py

# 3. 新机刷入 Ubuntu 24.04 后：只读预检 / 一键注入满血系统
python standalone/provision_device.py --check --host 192.168.78.1
python standalone/provision_device.py --host 192.168.78.1 --mode clean
```
