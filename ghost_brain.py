import os
import platform
import json
import re
from dotenv import load_dotenv, find_dotenv
from openai import OpenAI
from rich.panel import Panel

import difflib

# Популярные команды, для которых будем править опечатки
COMMON_COMMANDS = [
    "git", "brew", "python", "pip", "npm", "node", "ls", "cd", "pwd",
    "docker", "kubectl", "ssh", "top", "ps", "kill", "htop", "man", "grep", "find",
    # 🆕 добавил популярные утилиты
    "wget", "curl", "make", "gcc"
]


def _correct_command(word: str) -> str | None:
    """
    Если слово похоже на популярную команду — вернёт исправление.
    Например: 'gitt' -> 'git'
    """
    matches = difflib.get_close_matches(word, COMMON_COMMANDS, n=1, cutoff=0.75)
    return matches[0] if matches else None

# ищем .env в текущем каталоге проекта
load_dotenv(find_dotenv(usecwd=True))

api_key = (os.getenv("OPENAI_API_KEY") or "").strip().strip('"').strip("'")
if "\n" in api_key:
    api_key = api_key.splitlines()[0].strip()

# простая валидация: ASCII и начинается с sk-
if not (api_key.startswith("sk-") and api_key.isascii()):
    raise RuntimeError(
        "OPENAI_API_KEY некорректен. Проверь .env: строка должна быть вида "
        "OPENAI_API_KEY=sk-... (одна строка, без кавычек и лишних символов)."
    )

client = OpenAI(api_key=api_key)


def _extract_json(text: str) -> str:
    """
    Достаём JSON из ответа модели:
    - если завернула в ```json ... ``` — берём внутренний блок
    - если есть хоть одна фигурная скобка — берём от первой { до последней }
    - иначе возвращаем как есть
    """
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.S)
    if m:
        return m.group(1)
    if "{" in text and "}" in text:
        return text[text.find("{"): text.rfind("}") + 1]
    return text


def _fallback_parse_legacy(raw: str) -> dict:
    """
    Фоллбэк для старого формата "Команда: ... / Пояснение: ...",
    чтобы GhostCMD не ломался, если модель вдруг не вернула JSON.
    """
    bash_cmd = ""
    explanation = ""
    for line in (raw or "").splitlines():
        low = line.lower()
        if low.startswith("команда:"):
            bash_cmd = line.split(":", 1)[1].strip()
        elif low.startswith("пояснение:"):
            explanation = line.split(":", 1)[1].strip()
    if not bash_cmd:
        bash_cmd = "echo Не удалось определить команду"
    if not explanation:
        explanation = "Нет пояснения"
    return {
        "mode": "single",
        "bash_command": bash_cmd,
        "explanation": explanation,
    }


def process_prompt(user_input: str) -> dict:
    """
    Определяет, нужна ли одиночная команда или workflow (несколько шагов).
    Возвращает dict со структурой под GhostCMD.
    """
    os_type = platform.system()
    if os_type == "Darwin":
        os_label = "macOS"
    elif os_type == "Linux":
        os_label = "Linux"
    elif os_type == "Windows":
        os_label = "Windows"
    else:
        os_label = "неизвестная ОС"

    system_prompt = f"""
Ты — терминальный ИИ-инженер на {os_label}.
Определи, нужна ли одна команда или последовательность из нескольких шагов.
Всегда учитывай ОС: команды должны работать на {os_label}.
Никогда не предлагай интерактивные команды (top/htop/less/vi/nano и т.п.).
Не придумывай несуществующие файлы/пути.
Отвечай СТРОГО одним JSON без пояснений вокруг.

Если ОДНА команда:
{{
  "mode": "single",
  "single": {{
    "command": "<однострочная команда>",
    "explanation": "<краткое объяснение>"
  }}
}}

Если НЕСКОЛЬКО шагов:
{{
  "mode": "workflow",
  "workflow": {{
    "name": "auto_nlu_plan",
    "env": {{}},
    "steps": [
      {{
        "name": "step_1",
        "run": "<однострочная команда>",
        "target": "auto",
        "cwd": null,
        "timeout": null,
        "env": {{}}
      }}
    ]
  }}
}}
""".strip()

    # Запрос к модели
    resp = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input},
    ],
    temperature=0.1,
    max_tokens=1400,
    response_format={"type": "json_object"},  # <— ключевое
)
    raw = (resp.choices[0].message.content or "").strip()
    raw_json = _extract_json(raw)

    # Парсим JSON-ответ
    try:
        data = json.loads(raw_json)
    except Exception:
        # --- УМНЫЙ ФОЛЛБЭК НА КЛЮЧЕВЫЕ ФРАЗЫ (русский) ---
        ui = (user_input or "").lower()

        steps = []

        # показать файлы (ls)
        if any(k in ui for k in ["покажи файлы", "покажи список файлов", "список файлов", "ls "]):
            steps.append({"name": "step_ls", "run": "ls", "target": "auto"})

        # установить wget через brew (macOS)
        if "wget" in ui and ("brew" in ui or "через brew" in ui):
            steps.append({"name": "step_brew_wget", "run": "brew install wget", "target": "auto"})

        # создать /tmp/testfolder
        if any(k in ui for k in ["создай /tmp/testfolder", "создай папку /tmp/testfolder", "mkdir /tmp/testfolder"]):
            steps.append({"name": "step_mkdir", "run": "mkdir -p /tmp/testfolder", "target": "auto"})

        # обновить систему (sudo softwareupdate)
        if "обнови систему" in ui or "softwareupdate" in ui:
            steps.append({"name": "step_update", "run": "sudo softwareupdate --install --all", "target": "auto"})

        # перезагрузка (sudo reboot)
        if "перезагрузи" in ui or "reboot" in ui:
            steps.append({"name": "step_reboot", "run": "sudo reboot", "target": "auto"})

        # docker run hello-world
        if "docker run hello-world" in ui or ("docker" in ui and "hello-world" in ui):
            steps.append({"name": "step_docker_hello", "run": "docker run hello-world", "target": "auto"})

        # apt-get update (для Linux — у нас это будет помечено и пропущено на macOS в твоей логике)
        if "apt-get update" in ui or "обнови apt-get" in ui:
            steps.append({"name": "step_apt_update", "run": "apt-get update", "target": "auto"})

        if steps:
            return {
                "mode": "workflow",
                "bash_command": f"echo План из {len(steps)} шагов (см. превью)",
                "explanation": "Будет выполнен как workflow (фоллбэк по ключевым фразам)",
                "workflow": {
                    "name": "auto_nlu_plan",
                    "env": {},
                    "steps": steps
                }
            }

        # --- если ничего не распознали — старый фоллбэк ---
        return _fallback_parse_legacy(raw)

        # вторая попытка — подчищаем
        try:
            raw_json2 = raw_json.replace("'", '"')
            data = json.loads(raw_json2)
        except Exception:
            return _fallback_parse_legacy(raw)

    mode = (data.get("mode") or "").lower().strip()

    # ---- WORKFLOW ----
    if mode == "workflow":
        wf = data.get("workflow") or {}
        steps = wf.get("steps") or []

                # Минимальная валидация и нормализация шагов
        norm_steps = []
        skipped = []  # сюда будем собирать пропущенные шаги

        for i, s in enumerate(steps, start=1):
            if not isinstance(s, dict):
                continue
            name = (s.get("name") or f"step_{i}").strip()
            run  = (s.get("run") or "").strip()
            if not run:
                continue

            # --- фильтрация по ОС ---
            if os_label == "macOS" and run.startswith("apt-get"):
                skipped.append((name, run, "несовместимо с macOS (работает только в Linux)"))
                continue
            if os_label == "Linux" and run.startswith("brew"):
                skipped.append((name, run, "несовместимо с Linux (работает только в macOS)"))
                continue

            # --- фильтрация опасных ---
            if ":(){ :|:& };:" in run or "fork" in run.lower():
                skipped.append((name, run, "заблокировано как опасное"))
                continue

            target = (s.get("target") or "auto").lower().strip()
            if target not in ("auto", "host", "docker"):
                target = "auto"

            entry = {
                "name": name,
                "run": run,
                "target": target,
            }

            # если команда с sudo → помечаем
            if run.startswith("sudo "):
                entry["needs_sudo"] = True

            # пробрасываем опциональные поля
            if "cwd" in s: entry["cwd"] = str(s["cwd"])
            if "timeout" in s and s["timeout"] is not None:
                entry["timeout"] = int(s["timeout"])
            if "env" in s and isinstance(s["env"], dict):
                entry["env"] = dict(s["env"])
            if "if" in s: entry["if"] = str(s["if"])
            if "continue_on_error" in s: entry["continue_on_error"] = bool(s["continue_on_error"])
            if "retries" in s: entry["retries"] = dict(s["retries"])

            norm_steps.append(entry)

        # --- если были пропуски, покажем ---
        if skipped:
            msg = "Пропущено {} шаг(ов):\n".format(len(skipped))
            for name, run, reason in skipped:
                msg += f"• {run} — {reason}\n"
            try:
                from rich.panel import Panel
                print(Panel.fit(msg.strip(), border_style="red"))
            except Exception:
                print("\n" + msg.strip() + "\n")

        if norm_steps:
            wf_name = (wf.get("name") or "auto_nlu_plan").strip() or "auto_nlu_plan"
            return {
                "mode": "workflow",
                "bash_command": f"echo План из {len(norm_steps)} шагов (см. превью)",
                "explanation": "Будет выполнен как workflow",
                "workflow": {
                    "name": wf_name,
                    "env": wf.get("env") or {},
                    "steps": norm_steps,
                },
            }

    # ---- SINGLE ----
    single = (data.get("single") or {})
    cmd = (single.get("command") or "").strip()
    expl = (single.get("explanation") or "").strip()
    if not cmd:
        cmd = "echo Не удалось определить команду"
    if not expl:
        expl = "Нет пояснения"
    return {"mode": "single", "bash_command": cmd, "explanation": expl}



def suggest_overlay(query: str, context: dict | None = None) -> dict:
    """
    Генерирует совет для HUD Overlay:
      - короткий заголовок (title)
      - однострочную команду (command)
      - краткое пояснение (explain)
    Возвращает dict с указанными ключами.
    """
    os_type = platform.system()
    if os_type == "Darwin":
        os_label = "macOS"
    elif os_type == "Linux":
        os_label = "Linux"
    elif os_type == "Windows":
        os_label = "Windows"
    else:
        os_label = "неизвестная ОС"

    ctx = context or {}
    tokens = query.strip().split()
    if tokens:
        correction = _correct_command(tokens[0])
        if correction and correction != tokens[0]:
            tokens[0] = correction
            query = " ".join(tokens)
    cwd = ctx.get("cwd") or os.getcwd()
    last_cmd = (ctx.get("last_cmd") or "").strip()
    exit_code = int(ctx.get("exit_code") or 0)
    stderr = (ctx.get("stderr") or "").strip()

    system_prompt = f"""
Ты — Ghost Brain: ИИ-помощник для терминала на {os_label}.
Твоя задача — предложить ОДНУ понятную команду shell (строго одна строка, без комментариев и переноса \n),
и коротко объяснить её смысл простыми словами на русском. Также придумай короткий заголовок.

Правила:
- Команда должна быть исполнимой в реальном терминале для {os_label}.
- Не используй псевдокод и не добавляй пояснения в самой команде.
- Если видишь, что пользователь сделал опечатку в известной команде (например 'gitt' вместо 'git'), обязательно исправь.
- Никогда не предлагай 'brew install <что-то>', если это не популярный пакет. Если команда реально не существует — верни безопасный вариант: 'man <слово>' или '<слово> --help'.


Верни JSON строго такого вида:
{{
  "title": "Короткий заголовок",
  "command": "однострочная команда",
  "explain": "краткое пояснение"
}}
""".strip()

    user_msg = (
        f"Запрос пользователя: {query}\n\n"
        f"Контекст:\n- cwd: {cwd}\n- last_cmd: {last_cmd}\n- exit_code: {exit_code}\n- stderr: {stderr[:400]}"
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=600,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw_json = _extract_json(raw)
        data = json.loads(raw_json)
        title = (data.get("title") or "").strip() or "Совет от ИИ"
        command = (data.get("command") or "").strip() or "echo Не удалось определить команду"
        explain = (data.get("explain") or "").strip() or "Нет пояснения"
        command = " ".join(command.splitlines()).strip()  # строго одна строка

        # --- Жёсткая пост-обработка ---
        # если модель сгенерила brew install <неизвестное>, заменяем на безопасный fallback
                # --- Жёсткая пост-обработка ---
        if command.startswith("brew install "):
            pkg = command.replace("brew install", "").strip()
            if pkg and pkg not in COMMON_COMMANDS:
                print(f"[GhostBrain] ⚠️ Перехват brew install {pkg} → заменено на help")
                title = f"Команда «{pkg}» не найдена"
                command = f"man {pkg} || {pkg} --help"
                explain = f"Такой команды нет. Лучше посмотреть справку по «{pkg}»."


        return {"title": title, "command": command, "explain": explain}

    except Exception:
        safe = (query or "").strip() or "help"
        return {"title": "Открой помощь", "command": f"man {safe} || {safe} --help", "explain": "Безопасно посмотрим справку по запросу."}

import difflib

# Список популярных бинарей для авто-исправлений
COMMON_BINARIES = [
    "git", "ls", "python", "pip", "brew", "npm", "node", "cargo", "make",
    "docker", "kubectl", "ssh", "top", "ps", "kill", "htop", "man", "grep", "find"
]

def analyze_error(command: str, exit_code: int, stderr: str, cwd: str | None = None) -> dict:
    """
    Анализирует ошибку последней команды и предлагает исправление.
    Возвращает dict: { "title": ..., "command": ..., "explain": ... }
    """
    os_type = platform.system()
    if os_type == "Darwin":
        os_label = "macOS"
    elif os_type == "Linux":
        os_label = "Linux"
    elif os_type == "Windows":
        os_label = "Windows"
    else:
        os_label = "неизвестная ОС"

    stderr = (stderr or "").strip()
    cmd = (command or "").strip()

    # 🆕 1. Проверка: "command not found"
    if "command not found" in stderr:
        wrong = stderr.split(":")[-1].replace("command not found", "").strip()
        if wrong:
            match = difflib.get_close_matches(wrong, COMMON_BINARIES, n=1, cutoff=0.7)
            if match:
                fixed = match[0]
                fixed_cmd = cmd.replace(wrong, fixed, 1)
                return {
                    "title": f"Опечатка? Похоже, ты имел в виду «{fixed}»",
                    "command": fixed_cmd,
                    "explain": f"Команда «{wrong}» не найдена. Исправлено на «{fixed}»."
                }
            else:
                return {
                    "title": f"Команда «{wrong}» не найдена",
                    "command": f"man {wrong} || {wrong} --help",
                    "explain": f"Такой команды нет. Попробуй проверить установку или справку."
                }

    # 🧠 2. Если это не "command not found", пробуем ИИ-анализ
    system_prompt = f"""
Ты — Ghost Brain: помощник в терминале на {os_label}.
Тебе дают команду, её код выхода и stderr.
Нужно предложить одну исправляющую команду и коротко объяснить решение.
Формат ответа — JSON:
{{
  "title": "Короткий заголовок (например 'Прими лицензию Xcode')",
  "command": "команда для исправления",
  "explain": "пояснение простыми словами"
}}
Если ошибка не критична или решения нет — предложи посмотреть справку (--help).
""".strip()

    user_msg = (
        f"Команда: {cmd}\n"
        f"Код выхода: {exit_code}\n"
        f"stderr:\n{stderr[:600]}"
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw_json = _extract_json(raw)
        data = json.loads(raw_json)
        return {
            "title": (data.get("title") or "Совет по ошибке").strip(),
            "command": (data.get("command") or "echo 'см. --help'").strip(),
            "explain": (data.get("explain") or "Нет пояснения.").strip(),
        }
    except Exception:
        return {
            "title": "Не удалось проанализировать",
            "command": f"echo '{cmd}' failed, см. stderr",
            "explain": "Ghost Brain не смог предложить решение.",
        }

