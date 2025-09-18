# ~/.ghostcoach.zsh — GhostCoach hook for zsh
# Порт демона (по умолчанию 8765)
export GHOSTCOACH_PORT="${GHOSTCOACH_PORT:-8765}"

# Нужен add-zsh-hook
autoload -Uz add-zsh-hook

# Глобальные переменные состояния
typeset -g GC_ERRLOG=""
typeset -g GC_LASTCMD=""
typeset -g GC_ERRFD=""

ghostcoach_preexec() {
  # выключатель на лету
  [[ -n "$GHOSTCOACH_DISABLE" ]] && return

  # $1 — полная строка команды
  GC_LASTCMD="$1"

  # Лог для stderr текущей команды
  GC_ERRLOG=$(mktemp -t ghostcoach_err.XXXXXX)

  # Дублируем текущий stderr в новый FD и перенаправляем 2 -> tee (в лог и обратно)
  exec {GC_ERRFD}>&2
  exec 2> >(tee -a "$GC_ERRLOG" >&$GC_ERRFD)
}

ghostcoach_precmd() {
  # ДОЛЖНО быть первой строкой: берём код возврата предыдущей команды
  local status=$?

  # выключатель
  [[ -n "$GHOSTCOACH_DISABLE" ]] && {
    # Вернём stderr на место, если перенаправляли
    if [[ -n "$GC_ERRFD" ]]; then
      exec 2>&$GC_ERRFD {GC_ERRFD}>&- 2>/dev/null
    fi
    [[ -n "$GC_ERRLOG" ]] && rm -f "$GC_ERRLOG"
    GC_ERRLOG=""; GC_LASTCMD=""
    return
  }

  # Возвращаем stderr как было
  if [[ -n "$GC_ERRFD" ]]; then
    exec 2>&$GC_ERRFD {GC_ERRFD}>&- 2>/dev/null
  fi

  # Требуется интерактивный шелл и наличие curl
  [[ -o interactive ]] || return
  command -v curl >/dev/null 2>&1 || return

  # Формируем JSON надёжно через python (правильные экранирования, обрезаем stderr до 32КБ)
  local json
  json=$(env \
      GC_STATUS="$status" \
      GC_LASTCMD="$GC_LASTCMD" \
      GC_ERRLOG="$GC_ERRLOG" \
      python3 - <<'PY'
import json, os, sys
path = os.environ.get("GC_ERRLOG") or ""
data = b""
try:
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
except Exception:
    data = b""
if len(data) > 32768:
    data = data[-32768:]
stderr = data.decode("utf-8", "replace")
payload = {
    "cwd": os.getcwd(),
    "last_cmd": os.environ.get("GC_LASTCMD",""),
    "exit_code": int(os.environ.get("GC_STATUS") or 0),
    "stderr": stderr
}
print(json.dumps(payload, ensure_ascii=False))
PY
  )

  # Отправляем демону
  printf '%s' "$json" | curl -sS -X POST "http://127.0.0.1:${GHOSTCOACH_PORT}/update" \
    -H 'Content-Type: application/json' \
    --data-binary @- >/dev/null || true

  # Чистим состояние
  [[ -n "$GC_ERRLOG" ]] && rm -f "$GC_ERRLOG"
  GC_ERRLOG=""
  GC_LASTCMD=""
}

add-zsh-hook preexec ghostcoach_preexec
add-zsh-hook precmd  ghostcoach_precmd

