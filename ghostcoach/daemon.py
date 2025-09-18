# ghostcoach/daemon.py
#!/usr/bin/env python3
"""
ghostcoach/daemon.py — лёгкий локальный демон GhostCoach (MVP overlay).

Функции:
- POST /update        — клиент (shell hook) присылает JSON {cwd, last_cmd, exit_code, stderr}
- GET  /latest        — вернуть последнюю подсказку и сырой апдейт
- GET  /stream        — Server-Sent Events поток с подсказками для UI
- GET  /healthz       — healthcheck

Запуск:
    python -m ghostcoach.daemon
или:
    python ghostcoach/daemon.py

Без внешних зависимостей. Работает на localhost, порт по умолчанию 8765.
"""

from __future__ import annotations
import json, os, re, sys, time, queue, signal, threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
# --- fix import root ---
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


from multiprocessing import Queue

import subprocess, shlex

from ghost_brain import suggest_overlay, analyze_error



RUN_QUEUE = Queue()

HOST = "127.0.0.1"
PORT = int(os.environ.get("GHOSTCOACH_PORT", "8765"))

STATE_LOCK = threading.RLock()
LAST_UPDATE = None   # dict | None
LAST_TIP = None      # dict | None
CLIENTS = []         # list[queue.Queue] для /stream подписчиков

def _now_iso():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat()

def _detect_venv(cwd: str) -> str | None:
    for name in ("venv", ".venv"):
        cand = os.path.join(cwd, name)
        if os.path.isdir(cand) and os.path.exists(os.path.join(cand, "bin", "activate")):
            return cand
    return None

def _has_file(cwd: str, name: str) -> bool:
    return os.path.exists(os.path.join(cwd, name))

def _which(cmd: str) -> bool:
    from shutil import which as _w
    return _w(cmd) is not None



def _missing_command(stderr: str) -> str | None:
    pats = [
        r"command not found: ([a-zA-Z0-9_.-]+)",
        r"^([a-zA-Z0-9_.-]+): command not found",
        r"Unknown command: ([a-zA-Z0-9_.-]+)",
    ]
    for p in pats:
        m = re.search(p, stderr, re.MULTILINE)
        if m: return m.group(1)
    return None

def _missing_module(stderr: str) -> str | None:
    pats = [
        r"ModuleNotFoundError: No module named ['\"]([a-zA-Z0-9_\.]+)['\"]",
        r"ImportError: No module named ['\"]?([a-zA-Z0-9_\.]+)['\"]?",
        r"No module named ['\"]?([a-zA-Z0-9_\.]+)['\"]?",
    ]
    for p in pats:
        m = re.search(p, stderr)
        if m: return m.group(1)
    return None

def ghost_coach_suggest(update: dict) -> dict:
    """
    Эвристики для подсказки.
    Возвращает {title, command, explain, explain_long?}.
    """
    cwd = update.get("cwd") or os.getcwd()
    last_cmd = (update.get("last_cmd") or "").strip()
    exit_code = int(update.get("exit_code") or 0)
    stderr = update.get("stderr") or ""

    def tip(title, command, explain, explain_long=None):
        out = {"title": title, "command": command, "explain": explain}
        if explain_long: out["explain_long"] = explain_long
        return out

    def has(path): return os.path.exists(os.path.join(cwd, path))
    in_src = os.path.basename(cwd) == "src"
    venv_active = bool(os.environ.get("VIRTUAL_ENV"))
    venv_path = _detect_venv(cwd)

    # 1) Есть src/, но мы не в ней
    if os.path.isdir(os.path.join(cwd, "src")) and not in_src:
        return tip(
            "Перейти в директорию src/",
            "cd src",
            "Многие команды ожидают запуск из src/.",
            "Структура layout с src/ помогает избегать теневых импортов и упорядочивает пакет."
        )

    # 2) Нашли venv, но не активирован
    if not venv_active and venv_path:
        return tip(
            "Активируй виртуальное окружение",
            f"source {venv_path}/bin/activate",
            "Перед установкой пакетов включи окружение.",
            "Виртуальное окружение изолирует зависимости проекта и предотвращает конфликты версий в системе."
        )

    # 3) Git: не репозиторий
    if "fatal: not a git repository" in stderr:
        return tip(
            "Инициализируй Git-репозиторий",
            'git init && git add . && git commit -m "init"',
            "Команда git выполнена вне репозитория.",
            "Создай репозиторий в корне проекта, чтобы отслеживать изменения, делать ветки и откаты. Если репо уже есть выше — перейди в корень."
        )

    # 4) Команда не найдена (общая) + частные случаи
    missing = _missing_command(stderr) if exit_code != 0 and stderr else None
    if missing:
        if missing in ("pytest",):
            return tip("Установи pytest", "pip install pytest", "pytest не найден.",
                       "Pytest — популярный тест-раннер. После установки запусти `pytest -q` для лаконичного вывода.")
        if missing in ("pip", "pip3"):
            return tip("Установи pip", "python3 -m ensurepip --upgrade", "pip не найден.",
                       "ensurepip разворачивает pip, затем обнови: `python3 -m pip install -U pip`.")
        if missing in ("docker",):
            return tip("Установи Docker", "brew install --cask docker", "docker не найден.",
                       "Установи Docker Desktop, затем перезайди. Проверь `docker version`.")
        if missing in ("npm", "node", "pnpm", "yarn"):
            return tip(f"Пакетный менеджер «{missing}» не найден",
                       "brew install node",  # для macOS по умолчанию
                       "Node.js/менеджер пакетов не установлен.",
                       "Официальный инсталлятор ставит node и npm. Альтернатива — nvm.")
        # Общий случай: даём исполнимую команду для macOS/homebrew, иначе — which
        if sys.platform == "darwin" and _which("brew"):
            return tip(
                f"Команда «{missing}» не найдена",
                f"brew install {missing}",
                "Команда недоступна: попробуй установить через Homebrew.",
                "Если пакет не найден в Homebrew — проверь альтернативные источники или PATH."
            )
        else:
            return tip(
                f"Команда «{missing}» не найдена",
                f"which {missing} || echo 'Утилита {missing} не установлена'",
                "Команда недоступна в PATH.",
                "Установи через пакетный менеджер системы (apt/pacman/choco/winget) и добавь в PATH."
            )


    # 5) Python: отсутствует модуль
    mod = _missing_module(stderr) if exit_code != 0 and stderr else None
    if mod:
        return tip(
            f"Не найден модуль Python «{mod}»",
            f"pip install {mod}",
            "Установи недостающий модуль.",
            "Если используешь pyproject.toml/poetry — добавляй зависимость соответствующей командой (`poetry add`)."
        )

    # 6) Node.js: Cannot find module 'X'
    m = re.search(r"Cannot find module ['\"]([@a-zA-Z0-9_\-/\.]+)['\"]", stderr)
    if m:
        pkg = m.group(1)
        return tip(
            f"Не найден модуль Node.js «{pkg}»",
            f"npm install {pkg}",
            "Установи пакет в проект.",
            "Если используешь pnpm или yarn — поставь той же командой (`pnpm add` / `yarn add`)."
        )

    # 7) Если есть package.json — поставить зависимости
    if has("package.json"):
        return tip(
            "Установить зависимости Node.js",
            "npm install",
            "Нашёл package.json.",
            "Это скачает declared dependencies из package.json; затем сможешь запускать npm-скрипты."
        )

    # 8) Python-зависимости
    if has("requirements.txt"):
        return tip(
            "Установить зависимости проекта",
            "pip install -r requirements.txt",
            "Нашёл requirements.txt.",
            "После установки зависимости кэшируются в venv; фиксируй версии для воспроизводимости."
        )
    if has("pyproject.toml"):
        try:
            txt = open(os.path.join(cwd, "pyproject.toml"), "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            txt = ""
        if "tool.poetry" in txt:
            return tip(
                "Poetry-зависимости",
                "poetry install",
                "Проект управляется Poetry.",
                "Poetry читает pyproject.toml, ставит зависимости и создаёт изолированное окружение."
            )

    # 9) Фоллбек
    return tip(
        "Нужна помощь?",
        "ghost help",
        "Не удалось вывести конкретную подсказку.",
        "Сформулируй цель: «запусти тесты», «собери docker-образ», «инициализируй репозиторий» — и я подскажу следующий шаг."
    )



def _broadcast_tip(tip: dict, update: dict):
    payload = {
        "ts": _now_iso(),
        "tip": tip,
        "update": {k: update.get(k) for k in ("cwd","last_cmd","exit_code","stderr")}
    }
    data = f"event: tip\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
    for q in list(CLIENTS):
        try: q.put_nowait(data)
        except Exception: pass
    print(f"[GhostCoach] 🔊 Broadcast: {payload}")

def _shell_join_cd_and_cmd(cwd: str, cmd: str) -> str:
    # Безопасно формируем строку: cd <cwd> && <cmd>
    cwd_q = shlex.quote(cwd or os.getcwd())
    return f"cd {cwd_q} && {cmd}"

def _pbcopy(text: str) -> None:
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(input=text.encode("utf-8"))

def _osascript(script: str) -> int:
    try:
        r = subprocess.run(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return r.returncode
    except Exception:
        return 1

def _app_running(name: str) -> bool:
    # Проверяем через System Events, запущен ли процесс приложения
    osa = f'tell application "System Events" to exists (process "{name}")'
    try:
        r = subprocess.run(["osascript", "-e", osa], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return (r.stdout or "").strip().lower() == "true"
    except Exception:
        return False


def _send_to_vscode_like(app_name: str, line: str) -> bool:
    """Cursor / Visual Studio Code: через меню Terminal → New Terminal, потом вставка."""
    safe = line.replace('"', '\\"')

    # Попытка 1: New Terminal
    osa_new = f'''
    tell application "{app_name}" to activate
    tell application "System Events"
      tell process "{app_name}"
        try
          click menu item "New Terminal" of menu "Terminal" of menu bar 1
        on error
          click menu item "Create New Terminal" of menu "Terminal" of menu bar 1
        end try
        delay 0.10
        set the clipboard to "{safe}"
        keystroke "v" using command down
        key code 36
      end tell
    end tell
    '''
    if _osascript(osa_new) == 0:
        return True

    # Попытка 2: Focus Terminal (если есть такой пункт)
    osa_focus = f'''
    tell application "{app_name}" to activate
    tell application "System Events"
      tell process "{app_name}"
        try
          click menu item "Focus Terminal" of menu "Terminal" of menu bar 1
          delay 0.08
          set the clipboard to "{safe}"
          keystroke "v" using command down
          key code 36
          return
        on error
          -- fallthrough
        end try
      end tell
    end tell
    '''
    if _osascript(osa_focus) == 0:
        return True

    # Попытка 3: шорткат Ctrl+` как резерв
    osa_key = f'''
    tell application "{app_name}" to activate
    tell application "System Events"
      tell process "{app_name}"
        keystroke "`" using control down
        delay 0.10
        set the clipboard to "{safe}"
        keystroke "v" using command down
        key code 36
      end tell
    end tell
    '''
    return _osascript(osa_key) == 0


def _send_to_jetbrains(app_name: str, line: str) -> bool:
    """JetBrains IDEs: через меню View → Tool Windows → Terminal, потом вставка."""
    safe = line.replace('"', '\\"')

    osa_menu = f'''
    tell application "{app_name}" to activate
    tell application "System Events"
      tell process "{app_name}"
        try
          click menu item "Terminal" of menu "Tool Windows" of menu "View" of menu bar 1
        on error
          key code 111 using option down -- Alt+F12 резерв
        end try
        delay 0.10
        set the clipboard to "{safe}"
        keystroke "v" using command down
        key code 36
      end tell
    end tell
    '''
    return _osascript(osa_menu) == 0



def run_in_front_app(cmd: str, cwd: str | None) -> tuple[str, int | None, str | None]:
    """
    Отправляет команду в GUI/IDE/терминал И параллельно выполняет её в фоне,
    чтобы поймать exit_code + stderr для анализа.
    Возвращает tuple: (backend, exit_code, stderr)
    """
    shline = _shell_join_cd_and_cmd(cwd or os.getcwd(), cmd)

    backend = None

    # 1) Cursor
    if _app_running("Cursor"):
        if _send_to_vscode_like("Cursor", shline):
            backend = "cursor"

    # 2) VS Code
    if not backend and _app_running("Visual Studio Code"):
        if _send_to_vscode_like("Visual Studio Code", shline):
            backend = "vscode"

    # 3) JetBrains IDE
    if not backend:
        for jb in ("PyCharm", "IntelliJ IDEA", "WebStorm", "PhpStorm", "CLion", "GoLand", "RubyMine", "Rider", "DataGrip"):
            if _app_running(jb):
                if _send_to_jetbrains(jb, shline):
                    backend = "jetbrains"
                    break

    # 4) iTerm2
    if not backend and _app_running("iTerm2"):
        safe = shline.replace('"', '\\"')
        osa_iterm = f"""
        tell application "iTerm2"
          activate
          try
            tell current session of current window to write text "{safe}"
          on error
            create window with default profile
            tell current session of current window to write text "{safe}"
          end try
        end tell
        """
        if _osascript(osa_iterm) == 0:
            backend = "iterm2"

    # 5) Terminal.app
    if not backend and _app_running("Terminal"):
        safe = shline.replace('"', '\\"')
        osa_term = f"""
        tell application "Terminal"
          activate
          if not (exists window 1) then
            do script "{safe}"
          else
            do script "{safe}" in window 1
          end if
        end tell
        """
        if _osascript(osa_term) == 0:
            backend = "terminal"

    # 6) Paste
    if not backend:
        try:
            _pbcopy(shline)
            osa_paste = r'''
            tell application "System Events"
              keystroke "v" using {command down}
              key code 36
            end tell
            '''
            if _osascript(osa_paste) == 0:
                backend = "paste"
        except Exception:
            pass

    # Если ничего не подошло — считаем "background"
    if not backend:
        backend = "background"

    # Но сначала проверим на "опасные" команды
    dangerous_patterns = [
        "rm -rf", "shutdown", "reboot", "halt",
        "mkfs", "dd ", ">:",
        "kill -9", "pkill", "reboot",
    ]
    lower_cmd = cmd.lower()
    if any(pat in lower_cmd for pat in dangerous_patterns):
        # ⚠️ Опасные команды НЕ дублируем в фоне
        return backend, None, None

    # --- Гибрид: всегда запускаем копию в фоне ---
    try:
        proc = subprocess.run(
            shline, shell=True, cwd=cwd or os.getcwd(),
            env=os.environ.copy(), capture_output=True, text=True
        )
        return backend, proc.returncode, proc.stderr
    except Exception as e:
        return backend, 1, str(e)





class Handler(BaseHTTPRequestHandler):
    server_version = "GhostCoach/0.1"

    def _set_cors(self):
        self.send_header("Access-Control-Allow-Origin", f"http://{HOST}:{PORT}")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._set_cors()
        self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            return self._resp_json({"ok": True, "ts": _now_iso()})
        if parsed.path == "/latest":
            with STATE_LOCK:
                return self._resp_json({"ts": _now_iso(), "tip": LAST_TIP, "update": LAST_UPDATE})
        if parsed.path == "/stream":
            return self._sse_stream()
        if parsed.path in ("/", "/ui.html"):
            return self._serve_ui()
        self.send_error(HTTPStatus.NOT_FOUND, "No such endpoint")

    def do_POST(self):
        global LAST_TIP, LAST_UPDATE
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        

        if parsed.path == "/update":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except Exception:
                length = 0
            body = self.rfile.read(length) if length else self.rfile.read()

            try:
                raw = body.decode("utf-8") if body else "{}"
                data = json.loads(raw)
            except Exception as e:
                return self._resp_json({"ok": False, "error": f"bad json: {e}"}, status=HTTPStatus.BAD_REQUEST)

            # поддержка форматов
            upd = data.get("update")
            if not upd and any(k in data for k in ("cwd", "last_cmd", "exit_code", "stderr")):
                upd = {
                    "cwd": data.get("cwd"),
                    "last_cmd": data.get("last_cmd"),
                    "exit_code": data.get("exit_code"),
                    "stderr": data.get("stderr"),
                }

            tip = data.get("tip")

            with STATE_LOCK:
                if "tip" in data and data["tip"]:   # 🆕 только если реально пришёл tip
                    LAST_TIP = tip
                if upd:
                    LAST_UPDATE = upd
                

                # формируем финальный payload
                current_tip = LAST_TIP if LAST_TIP else {
                    "title": "Жду подсказку…",
                    "command": "-",
                    "explain": "-"
                }
                current_update = LAST_UPDATE if LAST_UPDATE else {
                    "cwd": os.getcwd(),
                    "last_cmd": "",
                    "exit_code": 0,
                    "stderr": ""
                }

                print(f"[GhostCoach] 🔊 FIXED Broadcast: tip={current_tip}, update={current_update}")
                _broadcast_tip(current_tip, current_update)

            return self._resp_json({"ok": True})




        elif parsed.path == "/brain":
            # читаем тело
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except Exception:
                length = 0
            body = self.rfile.read(length) if length else self.rfile.read()

            # парсим JSON
            try:
                raw = body.decode("utf-8") if body else "{}"
                data = json.loads(raw)
            except Exception as e:
                return self._resp_json({"ok": False, "error": f"bad json: {e}"}, status=HTTPStatus.BAD_REQUEST)

            query = (data.get("query") or "").strip()
            context = data.get("context") or (LAST_UPDATE or {})
            if not query:
                return self._resp_json({"ok": False, "error": "empty query"}, status=HTTPStatus.BAD_REQUEST)

            try:
                tip = suggest_overlay(query, context)
            except Exception as e:
                return self._resp_json({"ok": False, "error": str(e)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

            with STATE_LOCK:
                upd = context or (LAST_UPDATE or {})
                if upd:
                    upd = {
                        "cwd": upd.get("cwd") or os.getcwd(),
                        "last_cmd": (upd.get("last_cmd") or "").strip(),
                        "exit_code": int(upd.get("exit_code") or 0),
                        "stderr": upd.get("stderr") or "",
                    }
                else:
                    upd = {"cwd": os.getcwd(), "last_cmd": "", "exit_code": 0, "stderr": ""}

                LAST_TIP = tip
                _broadcast_tip(LAST_TIP, upd)

            return self._resp_json({"ok": True, "tip": tip})

        elif parsed.path == "/analyze":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except Exception:
                length = 0
            body = self.rfile.read(length) if length else self.rfile.read()

            try:
                raw = body.decode("utf-8") if body else "{}"
                data = json.loads(raw)
            except Exception as e:
                return self._resp_json({"ok": False, "error": f"bad json: {e}"}, status=HTTPStatus.BAD_REQUEST)

            cmd = (data.get("command") or "").strip()
            exit_code = int(data.get("exit_code") or 0)
            stderr = (data.get("stderr") or "").strip()
            cwd = data.get("cwd") or os.getcwd()

            if not cmd and not stderr:
                return self._resp_json({"ok": False, "error": "empty input"}, status=HTTPStatus.BAD_REQUEST)

            tip = analyze_error(cmd, exit_code, stderr, cwd)

            with STATE_LOCK:
                upd = {"cwd": cwd, "last_cmd": cmd, "exit_code": exit_code, "stderr": stderr}
                LAST_TIP = tip
                _broadcast_tip(LAST_TIP, upd)

            return self._resp_json({"ok": True, "tip": tip})



        elif parsed.path == "/run":
            # читаем тело (надёжно, даже если нет Content-Length)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except Exception:
                length = 0
            body = self.rfile.read(length) if length else self.rfile.read()

            # парсим JSON и запускаем команду в активном приложении (Cursor/VSCode/JetBrains/iTerm/Terminal)
            try:
                raw = body.decode("utf-8") if body else "{}"
                data = json.loads(raw)
                cmd = (data.get("command") or "").strip()
                if not cmd:
                    return self._resp_json({"ok": False, "error": "no command"})

                cwd = (LAST_UPDATE or {}).get("cwd") or os.getcwd()
                backend, exit_code, stderr = run_in_front_app(cmd, cwd)  # <--- поправь helper, чтобы он возвращал (backend, exit_code, stderr)

                upd = {
                    "cwd": cwd,
                    "last_cmd": cmd,
                    "exit_code": exit_code,
                    "stderr": stderr.strip() if stderr else "",
                }
                with STATE_LOCK:
                    LAST_UPDATE = upd

                if exit_code not in (0, None):
                    # Если команда реально вернула ошибку
                    tip = analyze_error(cmd, exit_code, stderr or "", cwd)
                    with STATE_LOCK:
                        LAST_TIP = tip
                    _broadcast_tip(LAST_TIP, upd)
                    return self._resp_json({"ok": False, "update": upd, "tip": tip, "backend": backend})

                elif exit_code is None and stderr:
                    # Если мы не знаем exit_code (GUI-терминал), но stderr есть
                    tip = analyze_error(cmd, 1, stderr, cwd)
                    with STATE_LOCK:
                        LAST_TIP = tip
                    _broadcast_tip(LAST_TIP, upd)
                    return self._resp_json({"ok": False, "update": upd, "tip": tip, "backend": backend})

                # Успешный запуск
                _broadcast_tip(LAST_TIP, upd)
                return self._resp_json({"ok": True, "update": upd, "backend": backend})

            except Exception as e:
                return self._resp_json({"ok": False, "error": str(e)})


        # если ни один эндпоинт не совпал
        self.send_error(HTTPStatus.NOT_FOUND, "No such endpoint")

    def _resp_json(self, obj, status=HTTPStatus.OK):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._set_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_stream(self):
        q = queue.Queue(maxsize=100)
        CLIENTS.append(q)
        try:
            self.send_response(HTTPStatus.OK)
            self._set_cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            # сразу отправляем последнее состояние, если есть
            with STATE_LOCK:
                if LAST_TIP is not None and LAST_UPDATE is not None:
                    payload = {"ts": _now_iso(), "tip": LAST_TIP, "update": LAST_UPDATE}
                    self.wfile.write(
                        f"event: tip\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()

            # основной цикл SSE
            while True:
                try:
                    data = q.get(timeout=60)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                CLIENTS.remove(q)
            except ValueError:
                pass

    def _serve_ui(self):
        # Пробуем отдать файл ghostcoach/ui.html с диска, иначе — встроенный UI.
        disk_path = os.path.join(os.path.dirname(__file__), "ui.html")
        html = None
        if os.path.exists(disk_path):
            try:
                with open(disk_path, "r", encoding="utf-8") as f:
                    html = f.read()
            except Exception:
                html = None
        if html is None:
            html = UI_HTML

        html = html.replace("{{PORT}}", str(PORT))
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._set_cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)



def run_server():
    # --- Автоосвобождение порта, если он занят ---
    import subprocess, sys, os, time
    try:
        # macOS/BSD: lsof
        pid = subprocess.check_output(
            ["lsof", "-t", f"-iTCP:{PORT}", "-sTCP:LISTEN"],
            text=True
        ).strip()
        if pid:
            os.kill(int(pid.splitlines()[0]), 9)
            print(f"[GhostCoach] ⚠️  Убил старый процесс на порту {PORT} (PID {pid})")
            time.sleep(1)  # 🆕 ждём освобождения порта
    except subprocess.CalledProcessError:
        # ничего не слушает
        pass
    except Exception as e:
        print(f"[GhostCoach] Не удалось освободить порт {PORT}: {e}")

    # 🆕 разрешаем повторное использование адреса
    from http.server import ThreadingHTTPServer
    ThreadingHTTPServer.allow_reuse_address = True

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)

    def _graceful_shutdown(signum, frame):
        try:
            httpd.shutdown()
        except Exception:
            pass
        os._exit(0)

    import signal as _sig
    for sig in (_sig.SIGINT, _sig.SIGTERM):
        _sig.signal(sig, _graceful_shutdown)

    print(f"[GhostCoach] listening on http://{HOST}:{PORT}  (GET /ui.html, /latest, /stream; POST /update)")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass


UI_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <title>GhostCoach — overlay</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { font-family: -apple-system, system-ui, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 24px; background: #0b0b0f; color: #e5e7eb; }
    .card { background: #111318; border: 1px solid #1f2430; border-radius: 16px; padding: 20px; max-width: 760px; box-shadow: 0 10px 30px rgba(0,0,0,0.35); }
    .title { font-size: 18px; font-weight: 700; margin-bottom: 8px; }
    .cmd { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; background: #0a0c12; border: 1px solid #1a1f2b; border-radius: 12px; padding: 12px; margin: 12px 0; overflow-x: auto; }
    .row { display: flex; gap: 8px; margin-top: 8px; }
    button { padding: 10px 14px; border-radius: 10px; border: 1px solid #2a3243; background: #161a22; color: #e5e7eb; cursor: pointer; }
    button:hover { background: #1b2130; }
    .muted { color: #9aa4b2; font-size: 13px; }
    .small { font-size: 12px; }
  </style>
</head>
<body>
  <div class="card">
    <div class="title" id="title">Жду обновления из терминала…</div>
    <div class="cmd" id="command">—</div>
    <div class="muted" id="explain">Открой подсказки GhostCoach: отправь POST на /update или запусти shell hook.</div>
    <div class="row">
      <button id="copy">Copy</button>
      <button id="explainBtn">Explain</button>
    </div>
    <div class="muted small" id="meta" style="margin-top:12px;">Нет данных</div>
  </div>

  <script>
    const port = {{PORT}};
    const titleEl = document.getElementById('title');
    const cmdEl = document.getElementById('command');
    const explainEl = document.getElementById('explain');
    const metaEl = document.getElementById('meta');

    document.getElementById('copy').onclick = async () => {
      const text = cmdEl.textContent;
      await navigator.clipboard.writeText(text);
      alert('Скопировано: ' + text);
    };

    document.getElementById('explainBtn').onclick = async () => {
      alert(explainEl.textContent);
    };

    function updateTip(payload) {
      const tip = payload.tip || {};
      titleEl.textContent = tip.title || 'Подсказка';
      cmdEl.textContent = tip.command || '—';
      explainEl.textContent = tip.explain || '';
      const u = payload.update || {};
      metaEl.textContent = `[${payload.ts}] ${u.cwd || ''}  $ ${u.last_cmd || ''}  (exit=${u.exit_code || 0})`;
    }

    const ev = new EventSource(`http://127.0.0.1:${port}/stream`);
    ev.addEventListener('tip', (e) => {
      try { updateTip(JSON.parse(e.data)); } catch (err) { console.error(err); }
    });
    ev.onerror = (e) => console.error('SSE error', e);

    fetch(`http://127.0.0.1:${port}/latest`).then(r => r.json()).then((data) => {
      if (data && data.tip) updateTip(data);
    }).catch(console.error);
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    run_server()
    main()

