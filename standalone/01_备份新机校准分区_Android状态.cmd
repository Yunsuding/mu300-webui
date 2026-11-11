@echo off
chcp 65001 >nul
title U30 Air 第0步：备份新机校准与IMEI分区 (需处于已Root的Android系统)
cd /d "%~dp0\.."
echo ============================================================================
echo   ZTE U30 Air 刷机前必做：备份单机独有的 IMEI / 5G 射频校准 / 引导链分区
echo   请确认设备当前处于【已 Root 的原厂 Android 系统】且已连接 USB 并开启 USB 调试
echo ============================================================================
echo.
python standalone\backup_nv_windows.py
echo.
pause
