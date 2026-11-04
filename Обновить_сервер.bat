@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "SCP=C:\Program Files\Git\usr\bin\scp.exe"
set "SSH=C:\Program Files\Git\usr\bin\ssh.exe"
set "VPS=root@89.22.228.22"

echo ===================================================
echo [1/4] Building store (optimize.py)...
echo ===================================================
python optimize.py
if errorlevel 1 goto :fail

echo.
echo ===================================================
echo [2/4] Checking required VPS files...
echo ===================================================
for %%F in (bot.py auth_api.py auth_store.py auth_email.py telegram_auth.py auth_schema.sql) do (
    if not exist "%%F" (
        echo ERROR: Missing local file %%F
        goto :fail
    )
)

if not exist "%SCP%" (
    echo ERROR: SCP not found: %SCP%
    goto :fail
)
if not exist "%SSH%" (
    echo ERROR: SSH not found: %SSH%
    goto :fail
)

echo.
echo ===================================================
echo [3/4] Uploading bot and authentication modules...
echo ===================================================
"%SCP%" -P 22 bot.py auth_api.py auth_store.py auth_email.py telegram_auth.py auth_schema.sql %VPS%:/root/
if errorlevel 1 goto :fail

echo.
echo ===================================================
echo [4/4] Verifying files and restarting forestbot...
echo ===================================================
"%SSH%" -p 22 %VPS% "test -s /root/bot.py && test -s /root/auth_api.py && test -s /root/auth_store.py && test -s /root/auth_email.py && test -s /root/telegram_auth.py && test -s /root/auth_schema.sql && systemctl restart forestbot && sleep 2 && systemctl is-active --quiet forestbot"
if errorlevel 1 goto :fail

echo.
echo ===================================================
echo SUCCESS: Files uploaded; forestbot is active.
echo ===================================================
echo NOTE: auth.sqlite3 is created automatically by the bot.
echo NOTE: Website files are deployed separately from the VPS.
goto :end

:fail
echo.
echo ===================================================
echo ERROR: Update stopped. Check the message above.
echo The service was not restarted if upload/verification failed.
echo ===================================================

:end
pause
