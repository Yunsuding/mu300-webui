@echo off
chcp 65001 >nul
title U30 Air 第二段：一键离线注入满血自研系统 (Ubuntu 24.04 启动后运行)
cd /d "%~dp0\.."
echo ============================================================================
echo   ZTE U30 Air 二阶段全离线满血系统注入工具 (改版 VPN+Mihomo + 0mA旁路 + 8080面板)
echo   请确认新设备已完成第一段 Ubuntu 24.04 (k5.4) 离线刷机，并通过 USB 连上电脑 (192.168.78.1)
echo ============================================================================
echo.
set /p TARGET_IP="请输入设备 IP 地址 (直接回车默认 192.168.78.1): "
if "%TARGET_IP%"=="" set TARGET_IP=192.168.78.1
echo.
python standalone\provision_device.py --host %TARGET_IP% --mode clean
echo.
pause
