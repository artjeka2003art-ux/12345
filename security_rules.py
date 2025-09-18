# security_rules.py

import re

# Метки риска для красивого вывода
RISK_LABEL = {
    "read_only": "read-only",
    "mutating": "mutating",
    "dangerous": "dangerous",
    "blocked_interactive": "interactive (blocked)",
}

# --- Паттерны ---

# Интерактивные — блокируем (не подходят для автоматического запуска)
INTERACTIVE_PATTERNS = [
    r"(^|\s)(top|htop|less|more|man|vim|vi|nano|ssh|mysql|psql|redis-cli|mongo)\b",
]

# Опасные — идут в песочницу / требуют спец-обращения
DANGEROUS_PATTERNS = [
    # тотальный вайп
    r"(^|\s)rm\s+-rf\s+/(?:\s|$)",
    r"(^|\s)rm\s+-rf\s+/\*",
    # форк-бомба
    r":\(\)\s*{\s*:\s*\|\s*:\s*;\s*}\s*;\s*:?\s*$",
    # выключение/перезагрузка
    r"(^|\s)(shutdown|reboot|halt|poweroff)\b",
    # отключение сети (macOS, Linux)
    r"(^|\s)networksetup\s+-setnetworkserviceenabled\b",
    r"(^|\s)ip\s+link\s+set\s+\S+\s+down\b",
    r"(^|\s)ifconfig\s+\S+\s+down\b",
    # системные политики/серьёзные твики
    r"(^|\s)spctl\s+--master-disable\b",
]

# Мутации — изменение ФС/ПО/прав/процессов (не столь разрушительные)
MUTATING_PATTERNS = [
    # запись/создание файлов
    r">>",              # append
    r"(?<!2)>(?!/dev/null)",  # перехват основной stdout в файл (исключим 2> и >/dev/null частично)
    r"\btee\b",
    r"\btouch\b",
    r"\btruncate\b",
    r"\bmkdir\b",
    r"\brmdir\b",
    r"\bmv\b",
    r"\bcp\b",
    r"\brm\b",  # обычный rm (не rm -rf /)
    # правка in-place
    r"\bsed\b.*\s-i\b",
    # права/владельцы/ссылки
    r"\bchmod\b",
    r"\bchown\b",
    r"\bln\b",
    # пакетные менеджеры / сервисы
    r"\b(apt|apt-get|yum|dnf|apk|pacman|brew|pip|pip3)\b",
    r"\b(systemctl|launchctl|service)\b",
    # архивы и загрузки, меняющие ФС
    r"\btar\b",
    r"\bunzip\b",
    r"\bzip\b",
    r"\bwget\b.*-O\b",   # wget с выводом в файл
    r"\bcurl\b.*-o\b",   # curl с выводом в файл
]

# Read-only — если ничего не сработало выше
READ_ONLY_HINTS = [
    r"\b(df|whoami|pwd|ls|cat|echo|ps|uname|hostname|date|uptime|id|env|printenv)\b",
]

def _match_any(patterns, cmd: str) -> bool:
    for pat in patterns:
        if re.search(pat, cmd, flags=re.IGNORECASE):
            return True
    return False

def assess_risk(command: str) -> str:
    """
    Возвращает одну из: 'read_only' | 'mutating' | 'dangerous' | 'blocked_interactive'
    Порядок важен: сначала интерактивные, потом опасные, потом мутации.
    """
    cmd = (command or "").strip()

    # 1) Интерактивные — блок
    if _match_any(INTERACTIVE_PATTERNS, cmd):
        return "blocked_interactive"

    # 2) Опасные
    if _match_any(DANGEROUS_PATTERNS, cmd):
        return "dangerous"

    # 3) Мутации (в т.ч. редиректы > >>, tee, touch, mkdir и т.д.)
    if _match_any(MUTATING_PATTERNS, cmd):
        return "mutating"

    # 4) По умолчанию — read-only
    return "read_only"
