@echo off
chcp 65001 >nul
cd /d "%~dp0electron"
echo 正在启动爆款监控（Electron 外壳，内嵌视频号后台）...
call ".\node_modules\.bin\electron.cmd" .
