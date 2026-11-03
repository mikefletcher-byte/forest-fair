@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ===================================================
echo [1/3] Building store (optimize.py)...
echo ===================================================
python optimize.py

echo.
echo ===================================================
echo [2/3] Uploading bot.py to Sweden VPS (Port 22)...
echo ===================================================
"C:\Program Files\Git\usr\bin\scp.exe" -P 22 bot.py root@89.22.228.22:/root/bot.py

echo.
echo ===================================================
echo [3/3] Restarting forestbot service on VPS...
echo ===================================================
"C:\Program Files\Git\usr\bin\ssh.exe" -p 22 root@89.22.228.22 "systemctl restart forestbot && systemctl is-active forestbot"

echo.
echo ===================================================
echo DONE! Server in Sweden is updated and active!
echo ===================================================
pause