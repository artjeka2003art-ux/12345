# 👻 GhostCMD

**AI-терминал с безопасной песочницей (Docker sandbox).**

GhostCMD позволяет писать команды на человеческом языке, а ИИ переводит их в системные и выполняет:
- 🧠 Перевод естественного языка → bash
- 🔒 Система уровней риска:
  - 🟢 read-only (безопасные команды)
  - 🟡 mutating (изменяют систему, требуют подтверждения)
  - 🔴 dangerous (опасные — только в Docker-песочнице)
  - ⛔ interactive (top/htop/vim/mysql — блокируются)
- 🚫 Stop-лист (rm -rf /, fork-бомбы, выключение сети и т.д.)
- ✅ Автотесты для правил безопасности

## 🔥 Killer-feature

Опасные команды (`rm -rf /`, `networksetup off`, `shutdown`) **не выполняются на хосте**,  
а отправляются в **изолированный Docker-контейнер (Ubuntu 22.04)**:

- Ограниченные ресурсы (CPU, RAM, PIDs)
- Сеть по умолчанию отключена
- Read-only rootfs (запись только когда требуется)
- Автоперевод macOS-команд в Linux-аналоги
- Автоматическая сборка образа при первом запуске

Таким образом, даже самые разрушительные команды можно попробовать без риска для системы.

## 🚀 Установка

1. Установите [Docker Desktop](https://www.docker.com/products/docker-desktop/)
2. Клонируйте проект:
   ```bash
   git clone https://github.com/username/GhostCMD.git
   cd GhostCMD
# Установите зависимости (Bash)
   python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt