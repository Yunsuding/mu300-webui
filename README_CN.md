# mu300-webui — ZTE U30 Air (`mu300-linux`) 极轻量 Web 控制台与硬件优化套件

**[English](./README.md)** | **简体中文**

专为刷入上游 **`mu300-linux` (Ubuntu 24.04 ARM64)** 开源固件的 **中兴 ZTE U30 Air (紫光展锐 T7510 / V510)** 5G 随身路由器打造的**极轻量 Web 控制台与底层硬件优化套件**。

针对设备 **1.4GB RAM + 3.9GB eMMC** 的严苛资源限制，本项目使用 **Python 3 标准库 + 离线单页前端** 替代高内存占用的 Cockpit（常驻内存从 `110MB+` 降至 `~15MB`），并提供开箱即用的 **5G 基带调优、单/多订阅自适应 Mihomo 分流、A76 大核绑定、80% 硬件限充保护与 eMMC 日志防爆守护**。

---

## ✨ 核心特性

### 1. 极轻量全功能 Web 控制台 (`:8080`)
- **零第三方后端依赖**：纯 Python 3 标准库实现的高并发守护进程，常驻内存仅 `~15MB`。
- **纯离线前端渲染**：内嵌 `tailwindcss.js`，在 SIM 卡停机、地下车库无网或弱网环境下依然秒开且样式完整。
- **5G 射频与基带精控**：实时监测 `RSRP` / `SINR` / `RSRQ` / 频段与运营商状态，支持 **5G 频段锁定 (`n41` / `n78` / `n28`)**、射频软重联、数据漫游开关、SIM 卡槽切换、APN 配置与抗运营商限速 **TTL / MTU 伪装**。
- **8 区温控与 CPU 大小核看板**：实时展示展锐 T7510 8 个热敏温区（含 5G 基带、Cortex-A76 大核、电池等）与 CPU 0-3 (A55) / CPU 4-7 (A76) 负载分布。

### 2. 单/多订阅自适应 Mihomo 代理与局域网设备分流
无需手动编写复杂的 YAML 配置，直接在网页端 **「VPN / 代理」 → 「订阅管理」** 中添加或删除任意 Clash / Mihomo 订阅链接：
- **单订阅模式（开箱即用）**：添加 1 个订阅即可直接拉取节点、并发测速、零断流切换节点。
- **多订阅自动融合模式（进阶分流）**：添加多个订阅（例如：主力高速机场 + 住宅家宽专线）并为每个订阅指定策略组名称后，系统会自动将多个订阅合并为统一配置：
  - 每个订阅拥有独立的策略组卡片、节点列表与**实时出口 IP / 国家归属地探测**；
  - 在 **「Wi-Fi 与客户端」** 页面可一键为任意手机/电脑绑定静态 IP，并在多个策略组或 `DIRECT`（直连）之间一键切换分流（基于 `SRC-IP-CIDR` 精确匹配）。
- **私有 Unix Socket 控制 (`/run/mu300-vpn/mihomo-control.sock`)**：不暴露任何 TCP 控制端口，零进程开销秒级热切节点；同时将 Sniffer 嗅探端口严格限制在 `80, 443, 8443`，彻底根治 WebUI 8080 端口 `502 Bad Gateway` 问题。
- **可选 Tailscale 自动兼容**：自动检测设备是否安装了 Tailscale；若已安装，融合配置时会自动置顶 `tailscaled` 进程与 `3478` STUN 端口直连规则，防止开启 TUN 代理后 Tailscale 断联。

### 3. 硬件级续航与性能优化
- **Mihomo 绑定 Cortex-A76 大核 (`CPUAffinity=4-7`)**：将代理加解密线程锁定在 4 颗 A76 大核，把 4 颗 A55 小核留给 `multi-sipa-0` 5G 基带硬中断，实测单线程代理吞吐翻倍。
- **SQC 80% 硬件寄存器限充 (`mu300-sqc-charge`)**：针对原厂 5.4 内核 SQC 充电驱动特性，直接控制充电 IC `REG01 (CHG_CONFIG)` 与 `REG05` 看门狗，实现 **电量达 80% 停充（保持 USB 外部供电不耗电池）、低于 75% 自动恢复充电**，支持长期插电 7×24 小时值守。
- **5G 常在线与防休眠**：配置 `WIFI_IDLE=0` 与 `RADIO_IDLE=keep`，无人连接 Wi-Fi 时蜂窝网络依然保持在线。
- **eMMC 日志防爆守护 (Log Guardian)**：自动配置 `journald` 8MB 上限，且后端每 10 分钟巡检清理 `/var/log` 与 `/tmp`，保护 3.9GB 根分区永不写满。

---

## 📦 目录结构

```text
.
├── u30air-webui/                   # 核心 Web 控制台与设备端优化组件
│   ├── server.py                   # Python 3 标准库后端 API 与系统守护进程 (8080)
│   ├── web/
│   │   ├── index.html              # 响应式单页控制台前端
│   │   └── tailwindcss.js          # 本地离线 Tailwind 引擎
│   ├── bin/
│   │   ├── mu300-mihomo            # Unix Socket 版 Mihomo 控制器脚本
│   │   └── mu300-sqc-charge        # 展锐 SQC 充电 IC 硬件级 80% 限充控制脚本
│   └── service/
│       ├── u30air-webui.service    # WebUI systemd 服务定义
│       ├── mihomo-affinity.conf    # Mihomo A76 大核亲和性 (CPU 4-7) Drop-in
│       └── 10-mu300-journald.conf  # journald 8MB 内存/磁盘上限配置
├── install.sh                      # 设备端一键安装与优化脚本
├── uninstall.sh                    # 设备端一键卸载与还原脚本
├── deploy.py                       # PC 端通过 SFTP 一键远程部署脚本
├── .env.example                    # PC 端 SSH 部署环境变量模板
├── requirements.txt                # PC 端部署脚本依赖 (paramiko)
└── README.md                       # 英文主文档 (English Documentation)
```

---

## 🚀 快速安装指南

**前置要求**：设备已刷入上游 `mu300-linux` (Ubuntu 24.04 ARM64) 固件，且电脑已通过随身 Wi-Fi 或 USB RNDIS 连接设备（默认网关 `192.168.78.1`）。

### 方式一：直接在设备上运行 `install.sh`（推荐）

将本仓库克隆或上传到设备任意目录（如 `/tmp/mu300-webui`），然后执行：

```bash
git clone https://github.com/Yunsuding/mu300-webui.git /tmp/mu300-webui
cd /tmp/mu300-webui
sudo bash install.sh --all -y
```

你也可以按需启用特定优化项：

```bash
# 查看所有可选开关
sudo bash install.sh --help

# 示例：仅安装 WebUI + Mihomo 大核优化 + 80% 电池限充
sudo bash install.sh --optimize-mihomo --enable-battery-limit
```

### 方式二：从电脑端一键 SFTP 远程部署 (`deploy.py`)

1. 在电脑端安装依赖并配置 `.env`：
   ```bash
   pip install -r requirements.txt
   cp .env.example .env
   # 编辑 .env 填入你的 SSH_PASSWORD (默认 IP 为 192.168.78.1，用户为 ubuntu)
   ```
2. 运行一键部署脚本：
   ```bash
   python deploy.py
   ```

安装完成后，在浏览器打开 **`http://192.168.78.1:8080/`** 即可使用！

---

## 🔄 如何配置 VPN / Clash / Mihomo 订阅

1. 打开控制台 **`http://192.168.78.1:8080/`**，切换到 **「VPN / 代理」** 标签页。
2. 点击右上角 **「订阅管理」** 展开抽屉：
   - **单订阅用户**：填写订阅名称（如 `主力节点`）、粘贴 Clash/Mihomo 订阅 HTTPS 链接，点击 **「保存并立即拉取融合」**。
   - **多订阅分流用户**：可依次添加多个订阅，并为每个订阅填写不同的 **「对应策略组名称」**（如第一个填 `日常高速`，第二个填 `住宅家宽`）。
3. 勾选底部的 **「启用开机自启与透明代理」** 并点击 **「应用配置」**。
4. 如需为局域网内特定手机/电脑指定不同订阅出口，前往 **「Wi-Fi 与客户端」** 标签页，直接点击在线设备右侧的策略组徽章即可一键切换分流并固定 IP。

---

## 🧹 卸载与还原

如需卸载本面板并恢复原始状态，可在设备上执行：

```bash
sudo bash uninstall.sh
```

---

## 📄 开源协议

MIT License
