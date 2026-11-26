@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "SCP=C:\Program Files\Git\usr\bin\scp.exe"
set "SSH=C:\Program Files\Git\usr\bin\ssh.exe"
set "VPS=root@89.22.228.22"
set "SSH_KEY=%~dp0.ssh\id_ed25519"
set "KNOWN_HOSTS=%~dp0.ssh\known_hosts"
set "REMOTE_STAGE=/root/forest-fair-deploy-staging"

echo [1/5] Building local PWA assets...
python optimize.py
if errorlevel 1 goto :fail

echo [2/5] Checking required files...
for %%F in (bot.py auth_api.py auth_store.py auth_email.py telegram_auth.py telegram_oidc.py auth_schema.sql deploy_remote.sh) do (
    if not exist "%%F" (
        echo ERROR: Missing local file %%F
        goto :fail
    )
)
if not exist "%SCP%" goto :fail
if not exist "%SSH%" goto :fail
if not exist "%SSH_KEY%" goto :fail
if not exist "%KNOWN_HOSTS%" goto :fail

echo [3/5] Preparing remote staging directory...
"%SSH%" -i "%SSH_KEY%" -o UserKnownHostsFile="%KNOWN_HOSTS%" -o StrictHostKeyChecking=yes -p 22 %VPS% "mkdir -p /root/forest-fair-deploy-staging && chmod 700 /root/forest-fair-deploy-staging"
if errorlevel 1 goto :fail

echo [4/5] Uploading backend files and deployment script to staging...
"%SCP%" -i "%SSH_KEY%" -o UserKnownHostsFile="%KNOWN_HOSTS%" -o StrictHostKeyChecking=yes -P 22 bot.py auth_api.py auth_store.py auth_email.py telegram_auth.py telegram_oidc.py auth_schema.sql deploy_remote.sh %VPS%:%REMOTE_STAGE%/
if errorlevel 1 goto :fail

echo [5/5] Running remote deployment with backup, health check and rollback...
"%SSH%" -i "%SSH_KEY%" -o UserKnownHostsFile="%KNOWN_HOSTS%" -o StrictHostKeyChecking=yes -p 22 %VPS% "chmod 700 /root/forest-fair-deploy-staging/deploy_remote.sh && /root/forest-fair-deploy-staging/deploy_remote.sh"
if errorlevel 1 goto :fail

echo.
echo SUCCESS: Backend installed; service and HTTP API passed checks.
echo Website deployment remains separate.
goto :end

:fail
echo.
echo ERROR: Update stopped. Check the message above.
echo No further deployment steps will be attempted.

:end
pause
