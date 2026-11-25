#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

STAGE="/root/forest-fair-deploy-staging"
BACKUP_ROOT="/root/forest-fair-backups"
APP_ROOT="/root"
SERVICE="forestbot.service"
FILES=(
  bot.py
  auth_api.py
  auth_store.py
  auth_email.py
  telegram_auth.py
  telegram_oidc.py
  auth_schema.sql
)

log() { printf '[deploy] %s\n' "$*"; }
fail() { log "ERROR: $*"; exit 1; }

cd "$STAGE"
for file in "${FILES[@]}"; do
  [[ -s "$file" ]] || fail "Файл отсутствует или пустой в staging: $file"
done

PYTHON="/root/venv/bin/python"
[[ -x "$PYTHON" ]] || fail "Не найден Python виртуального окружения: $PYTHON"

log "Проверка зависимостей без изменения виртуального окружения..."
"$PYTHON" - <<'PY'
import sys
try:
    import cryptography, jwt
except Exception as exc:
    print(f"Не удалось импортировать зависимость: {type(exc).__name__}")
    sys.exit(1)

crypto = tuple(int(x) for x in cryptography.__version__.split(".")[:2])
jwt_version = tuple(int(x) for x in jwt.__version__.split(".")[:2])

if not (crypto >= (50, 0) and crypto < (51, 0)):
    print(f"Неподходящая версия cryptography: {cryptography.__version__}")
    sys.exit(1)
if not (jwt_version >= (2, 10) and jwt_version < (3, 0)):
    print(f"Неподходящая версия PyJWT: {jwt.__version__}")
    sys.exit(1)
print(f"Зависимости OK: cryptography {cryptography.__version__}, PyJWT {jwt.__version__}")
PY

backup_dir="$BACKUP_ROOT/manual-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$backup_dir"
log "Резервное копирование текущих файлов: $backup_dir"
for file in "${FILES[@]}"; do
  if [[ -f "$APP_ROOT/$file" ]]; then
    cp -a "$APP_ROOT/$file" "$backup_dir/"
  else
    : > "$backup_dir/$file.absent"
  fi
done

health_check() {
  systemctl is-active --quiet "$SERVICE" || return 1
  "$PYTHON" - <<'PY'
import sys
import time
import urllib.error
import urllib.request

url = "http://127.0.0.1:8080/api/auth/me"
for attempt in range(10):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception:
        status = None

    # Для /api/auth/me запрос без Bearer-токена должен вернуть 401.
    if status == 401:
        print("HTTP API OK: /api/auth/me -> 401 (ожидаемо без токена)")
        sys.exit(0)
    time.sleep(2)

print(f"HTTP API health check failed; последний статус: {status}")
sys.exit(1)
PY
}

rollback() {
  log "ОШИБКА установки или проверки. Выполняется откат..."
  local restore_failed=0
  for file in "${FILES[@]}"; do
    if [[ -f "$backup_dir/$file" ]]; then
      install -m 600 "$backup_dir/$file" "$APP_ROOT/$file" || restore_failed=1
    elif [[ -f "$backup_dir/$file.absent" ]]; then
      rm -f "$APP_ROOT/$file" || restore_failed=1
    fi
  done

  if ! systemctl restart "$SERVICE"; then
    log "Не удалось перезапустить службу после отката."
    restore_failed=1
  fi
  if ! health_check; then
    log "Проверка службы/API после отката не пройдена."
    restore_failed=1
  fi

  if (( restore_failed )); then
    log "КРИТИЧЕСКИ: откат выполнен не полностью или сервис не прошёл проверку."
    return 1
  fi
  log "Откат завершён; прежние файлы восстановлены, служба и API работают."
  return 0
}

log "Установка новых файлов..."
for file in "${FILES[@]}"; do
  if ! install -m 600 "$STAGE/$file" "$APP_ROOT/$file"; then
    rollback || true
    exit 1
  fi
done

log "Перезапуск $SERVICE..."
if ! systemctl restart "$SERVICE"; then
  rollback || true
  exit 1
fi

log "Проверка службы и HTTP API..."
if ! health_check; then
  rollback || true
  exit 1
fi

log "SUCCESS: backend установлен; systemd и HTTP API прошли проверку."
log "Резервная копия сохранена: $backup_dir"
log "Staging сохранён: $STAGE"
