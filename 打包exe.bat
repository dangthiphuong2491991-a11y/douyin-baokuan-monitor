@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在打包，请稍候（首次约几分钟）...
python -m PyInstaller build.spec --noconfirm --clean
echo.
echo 打包完成！exe 在 dist\ 目录里：dist\爆款监控.exe
echo 把整个 dist 文件夹（或里面的 exe）发给别人即可运行。
pause
