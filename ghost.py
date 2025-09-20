# ghost.py — верх файла (импорты + история/логи + исправленный print_logs)
# ВСТАВЛЯЙ САМЫМ ВЕРХОМ ДО МАРКЕРА "CLI helpers"

import os
import sys
sys.path.append(os.path.dirname(__file__))

import platform
import re
import shlex
import time
import json
import unicodedata
from pathlib import Path
from datetime import datetime, timezone
import time as _time

from rich.panel import Panel
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.syntax import Syntax

import time as _time
import json as _json
from rich.panel import Panel as Panel
from rich.table import Table as _Table


try:
    from ghostcoach.daemon import RUN_QUEUE
except ImportError:
    RUN_QUEUE = None


# --- safe error printing (добавить после импортов) ---
def _safe_print_error(msg: str):
    """
    Печатает ошибку красивой рамкой через rich.Panel, а если rich/Panel
    по какой-то причине недоступен — просто через print.
    """
    try:
        from rich import print as rprint
        rprint(Panel.fit(msg, border_style="red"))
    except Exception:
        print(msg)


import yaml  # для автосохранения workflow
from rich import print
from rich.prompt import Prompt, Confirm
from rich.panel import Panel
from rich.table import Table

# workflow API
from core.workflow import (
    load_workflow,
    preview_workflow,
    run_workflow,
    StepRunResult,
    Target,
    WorkflowContext,
    WorkflowSpec,
    StepSpec,
)

# нормальный executor для шагов workflow (ВАЖНО!)
from core.executor import execute_step_cb, execute_with_limits, ExecTarget
# YAML diff/save helpers (будем использовать на следующем шаге)
from core.yaml_edit import (
    load_yaml_preserve,
    dump_yaml_preserve,
    make_unified_diff,
    atomic_write_with_backup,
    preview_and_write_yaml,
    build_ops_from_nl,
)

from core.workflow_edit import apply_ops
from ghost_brain import process_prompt
from security_rules import assess_risk, RISK_LABEL

# лимиты/раннеры
from core.limits import load_limits_for_risk
from core.exec_limits import run_on_host_with_limits   # для тестового CLI режима

from core.ci_github import gh_exists, gh_version, gh_is_authenticated, install_hint
from core.ci_init import init_ci
from core.ci_manage import ci_list, ci_run, ci_logs_last
from core.ci_init import init_ci, TEMPLATES



# =========================
# Импорты истории/логов
# =========================
try:
    from core.history import (
        init_db,
        create_command_event,
        finalize_command_event,
        add_artifact,
        recent,
        get_command,
        artifacts_for_command,
    )
except ImportError:
    # (оставляем тот же импорт на случай иной схемы пакета)
    from core.history import (
        init_db,
        create_command_event,
        finalize_command_event,
        add_artifact,
        recent,
        get_command,
        artifacts_for_command,
    )

# =====================================================
# ЛОГ-ФАЙЛЫ (JSONL) — helpers
# =====================================================
def _logs_dir() -> Path:
    p = Path(os.path.expanduser("~")) / ".ghostcmd" / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _today_log_path_utc() -> Path:
    # Используем UTC, чтобы дата не «скакала» при смене TZ
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _logs_dir() / f"{today}.jsonl"

def _tail_lines(path: Path, n: int = 20) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [line.rstrip("\n") for line in lines[-n:]]
    except Exception as e:
        return [f"❌ Не удалось прочитать {path}: {e}"]

def print_logs(count: str | None = None):
    """
    Печатает «хвост» логов за сегодня. Никакого кода ВНЕ функции!
    """
    n = 20
    if count:
        try:
            n = max(1, min(1000, int(count)))
        except ValueError:
            pass

    path = _today_log_path_utc()
    last = _tail_lines(path, n)

    if not last:
        print(Panel.fit(
            f"Лог за сегодня пуст или ещё не создан:\n{path}\n\nВыполни любую команду — записи появятся.",
            border_style="yellow",
            padding=(1, 2),
        ))
        return

    body = "\n".join(last)
    print(Panel.fit(
        f"Последние {len(last)} строк из:\n{str(path)}\n\n{body}",
        border_style="cyan",
        padding=(1, 2),
    ))
    

# ============== CLI helpers (help/history/logs/show/replay) ==============

# =====================================================
# Конфиг GhostCMD (host-only правила per-OS)
# ~/.ghostcmd/config.yml (опционально)
# =====================================================
from pathlib import Path as _Path

_DEFAULT_CONFIG = {
    "host_only_patterns": {
        # macOS утилиты, которых нет в нашей Ubuntu-песочнице
        "Darwin": ["brew", "networksetup", "systemsetup", "launchctl", "scutil", "pmset", "osascript", "open "],
        # Здесь специально по умолчанию пусто: большинство Linux-команд можно запускать в контейнере
        "Linux": [],
        # Windows PowerShell/Chocolatey — явно не работают в Linux-контейнере
        "Windows": ["choco", "Get-NetIPAddress", "Get-Process", "ipconfig", "powershell", "winget"],
    }
}

_CONFIG_CACHE: dict | None = None

def _config_dir() -> _Path:
    p = _Path(_Path.home()) / ".ghostcmd"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _config_path() -> _Path:
    return _config_dir() / "config.yml"

def load_ghost_config() -> dict:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    cfg_path = _config_path()
    try:
        if cfg_path.exists():
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}
    except Exception:
        data = {}
    # поверх накатываем дефолты (дефолты → потом пользовательские перекрывают)
    out = dict(_DEFAULT_CONFIG)
    try:
        # глубокий merge только для host_only_patterns
        hop = dict(_DEFAULT_CONFIG.get("host_only_patterns", {}))
        hop.update((data.get("host_only_patterns") or {}))
        out["host_only_patterns"] = hop
    except Exception:
        pass
    _CONFIG_CACHE = out
    return out

def host_only_markers_for_current_os() -> list[str]:
    cfg = load_ghost_config()
    os_label = platform.system()  # Darwin | Linux | Windows
    return list(cfg.get("host_only_patterns", {}).get(os_label, []))
# =====================================================
# ХЭНДЛЕРЫ PREVIEW/RUN WORKFLOW
# =====================================================
def handle_flow_preview(cmdline: str) -> bool:
    """
    Обрабатывает команды вида:
      flow <path.yml>
      workflow <path.yml>
    Возвращает True, если команда обработана.
    """
    text = cmdline.strip()
    if not (text.startswith("flow ") or text.startswith("workflow ")):
        return False

    try:
        _, path = text.split(" ", 1)
    except ValueError:
        print("[workflow] Использование: flow <path/to/file.yml>")
        return True

    path = path.strip().strip('"').strip("'")
    if not path:
        print("[workflow] Укажи путь к .yml файлу. Пример: flow flows/hello.yml")
        return True

    try:
        wf = load_workflow(path)
        global LAST_AUTOGEN_PATH
        LAST_AUTOGEN_PATH = path
        preview_workflow(wf)
    except FileNotFoundError:
        print(f"[workflow] Файл не найден: {path}")
    except Exception as e:
        print(f"[workflow] Ошибка: {e}")
    return True


def handle_flow_run(cmdline: str) -> bool:
    """
    runflow <path.yml> [--from N] [--yes|-y] [--dry-run]
    """
    text = cmdline.strip()
    if not text.startswith("runflow "):
        return False

    try:
        _, rest = text.split(" ", 1)
    except ValueError:
        print("[workflow] Использование: runflow <path/to/file.yml> [--from N] [--yes] [--dry-run]")
        return True

    parts = rest.split()
    if not parts:
        print("[workflow] Укажи путь к .yml файлу. Пример: runflow flows/hello.yml")
        return True

    path = parts[0]
    start_from = 1
    auto_yes = False
    dry_run = False

    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok == "--from" and i + 1 < len(parts):
            try:
                start_from = max(1, int(parts[i+1]))
            except ValueError:
                print("[workflow] --from N: N должно быть целым числом ≥ 1")
                return True
            i += 2
            continue
        if tok in ("--yes", "-y"):
            auto_yes = True
            i += 1
            continue
        if tok == "--dry-run":
            dry_run = True
            i += 1
            continue
        i += 1

    try:
        wf = load_workflow(path)
        global LAST_AUTOGEN_PATH
        LAST_AUTOGEN_PATH = path
        if dry_run:
            preview_workflow(wf)
            try:
                from core.workflow_lint import lint_workflow, print_lint_report, has_errors
                issues = lint_workflow(wf)
                print_lint_report(issues)
            # не блокируем запуск, просто предупреждаем; можно сделать auto-cancel при ERROR если захочешь
            except Exception as _e:
            # линтер не должен ломать запуск
                pass
            return True

        # Сдвиг старта по шагам
        if start_from > 1:
            total = len(wf.steps)
            if start_from > total:
                print(f"[workflow] В workflow всего {total} шаг(ов); нельзя начать с {start_from}.")
                return True
            wf = WorkflowSpec(
                name=f"{wf.name} (from {start_from})",
                steps=wf.steps[start_from-1:],
                env=getattr(wf, "env", {}),
                secrets_from=getattr(wf, "secrets_from", None),
                source_path=getattr(wf, "source_path", None),
                source_sha256=getattr(wf, "source_sha256", None),
)

        # НОРМАЛЬНЫЙ executor (не адаптер!)
        result = run_workflow(
            wf,
            execute_step_cb=execute_step_cb,
            ask_confirm=not auto_yes,
        )

        total = len(result.steps)
        ok_count = sum(1 for s in result.steps if s.ok)
        skipped_count = sum(1 for s in result.steps if s.meta.get("skipped"))
        failed_count = total - ok_count - skipped_count

        if failed_count == 0 and skipped_count > 0:
            print(f"[workflow] ⏭️ Завершено: {ok_count}/{total} ok, {skipped_count} skipped.")
        elif failed_count == 0:
            print(f"[workflow] ✅ Готово: {ok_count}/{total} шаг(ов) успешно.")
        else:
            print(f"[workflow] ❌ Останов: {ok_count}/{total} ok, {failed_count} fail, {skipped_count} skipped.")

    except FileNotFoundError:
        print(f"[workflow] Файл не найден: {path}")
    except Exception as e:
        print(f"[workflow] Ошибка: {e}")

    return True

def handle_rerun_failed(cmdline: str) -> bool:
    """
    перезапусти упавшие [--include-soft] [--yes] | rerun failed [--include-soft] [--yes]
    """
    text_raw = (cmdline or "").strip()
    text = text_raw.lower()

    # Триггеры
    triggers = (
        text.startswith("перезапусти упавшие")
        or text.startswith("перезапусти неуспешные")
        or text.startswith("rerun failed")
        or text == "перезапусти упавшие шаги"
    )
    if not triggers:
        return False

    include_soft = ("--include-soft" in text) or ("—include-soft" in text)
    auto_yes = ("--yes" in text) or ("-y" in text)

    from pathlib import Path
    import json

    state_path = Path(".ghostcmd/last_run.json")
    if not state_path.exists():
        print("[workflow] Нет .ghostcmd/last_run.json — сначала запусти workflow.")
        return True

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[workflow] Не удалось прочитать last_run.json: {e}")
        return True

    steps = data.get("steps") or []
    last_sha = data.get("yaml_sha256")

    def _is_failed(entry: dict) -> bool:
        # «жёстко» упавшие: ok=False и не skipped
        return (not entry.get("ok")) and (entry.get("status") != "skipped")

    def _is_soft(entry: dict) -> bool:
        # мягкий провал: meta.soft_fail == True
        meta = entry.get("meta") or {}
        return bool(meta.get("soft_fail"))

    # Базовый набор — жёсткие фейлы; при флаге добавляем soft_fail
    to_rerun = []
    soft_set = set()
    for s in steps:
        if _is_failed(s) or (include_soft and _is_soft(s)):
            to_rerun.append(s["name"])
        if _is_soft(s):
            soft_set.add(s["name"])

    if not to_rerun:
        if include_soft:
            print("[workflow] Нет ни упавших, ни soft-fail шагов — всё зелёное.")
        else:
            print("[workflow] Нет упавших шагов — всё зелёное. (Добавь --include-soft, чтобы захватить soft-fail.)")
        return True

    yaml_path = data.get("file_path") or LAST_AUTOGEN_PATH
    if not yaml_path:
        print("[workflow] Не знаю, какой YAML запускать. Укажи файл: runflow <file.yml> или сгенерируй план.")
        return True

    try:
        wf = load_workflow(yaml_path)
    except Exception as e:
        print(f"[workflow] Не удалось загрузить YAML '{yaml_path}': {e}")
        return True

    # Предупреждение, если YAML изменился с момента последнего прогона
    try:
        cur_sha = getattr(wf, "source_sha256", None)
        if last_sha and cur_sha and last_sha != cur_sha:
            print(Panel.fit(
                "Внимание: YAML изменился с момента последнего прогона (SHA отличается).\n"
                "Перезапуск упавших может вести себя иначе.",
                border_style="yellow"
            ))
    except Exception:
        pass

    # Оставляем только выбранные шаги (сохраняем порядок)
    names = set(to_rerun)
    filtered_steps = [s for s in wf.steps if s.name in names]
    if not filtered_steps:
        print("[workflow] Эти шаги не найдены в текущем YAML (возможно, переименованы).")
        return True

    # Превью
    try:
        from rich.table import Table
        t = Table(title="Перезапуск упавших шагов", show_lines=False)
        t.add_column("step")
        t.add_column("reason")
        for s in filtered_steps:
            reason = "soft_fail" if s.name in soft_set and include_soft else "failed"
            t.add_row(s.name, reason)
        print(t)
    except Exception:
        print("[workflow] К перезапуску:", ", ".join(to_rerun))

    res = run_workflow(
        WorkflowSpec(
            name=f"{wf.name} (rerun failed{' +soft' if include_soft else ''})",
            steps=filtered_steps,
            env=getattr(wf, "env", {}),
            secrets_from=getattr(wf, "secrets_from", None),
            source_path=getattr(wf, "source_path", None),
            source_sha256=getattr(wf, "source_sha256", None),
        ),
        execute_step_cb=execute_step_cb,
        ask_confirm=not auto_yes,
    )

    # Итоговая таблица (как в 'изменённых')
    try:
        from rich.table import Table
        t2 = Table(title="Результаты перезапуска (failed)", show_lines=False)
        t2.add_column("step")
        t2.add_column("status")
        t2.add_column("duration")
        for s in res.steps:
            dur = f"{int(round(s.duration_sec))}s"
            status = "OK" if s.ok and not s.meta.get("soft_fail") else ("SOFT_FAIL" if s.meta.get("soft_fail") else "FAIL" if not s.ok else "OK")
            t2.add_row(s.step.name, status, dur)
        print(t2)
    except Exception:
        pass

    return True




def handle_rerun_changed(cmdline: str) -> bool:
    """
    перезапусти изменённые [--with-deps]
    Сравнивает текущий YAML с последним прогоном (.ghostcmd/last_run.json) и перезапускает только изменённые шаги.
    """
    text = (cmdline or "").strip().lower()
    if not text.startswith("перезапусти измен") and not text.startswith("rerun changed"):
        return False

    include_deps = ("--with-deps" in text) or ("—with-deps" in text)

    from pathlib import Path
    import json
    from hashlib import sha256 as _sha256

    # 1) Загружаем last_run.json
    state_path = Path(".ghostcmd/last_run.json")
    if not state_path.exists():
        print("[workflow] Нет .ghostcmd/last_run.json — сначала запусти workflow.")
        return True

    try:
        last = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[workflow] Не удалось прочитать last_run.json: {e}")
        return True

    yaml_path = last.get("file_path") or LAST_AUTOGEN_PATH
    if not yaml_path:
        print("[workflow] Не знаю, какой YAML сравнивать. Укажи файл или сначала выполни workflow.")
        return True

    # 2) Грузим текущий YAML
    try:
        wf = load_workflow(yaml_path)
    except Exception as e:
        print(f"[workflow] Не удалось загрузить YAML '{yaml_path}': {e}")
        return True

    # 3) Пересчитываем отпечатки по тем же полям
    def _fp(step) -> str:
        import json as _json
        data = {
            "name": step.name,
            "run": step.run,
            "target": getattr(step.target, "value", str(step.target)),
            "timeout": step.timeout,
            "env": dict(step.env or {}),
            "needs": list(step.needs or []),
            "if": step.if_expr,
            "retries": dict(step.retries or {}),
            "continue_on_error": bool(step.continue_on_error),
            "capture": dict(step.capture or {}),
            "cwd": step.cwd,
            "mask": list(step.mask or []),
        }
        s = _json.dumps(data, ensure_ascii=False, sort_keys=True)
        return _sha256(s.encode("utf-8")).hexdigest()

    prev_map = last.get("step_fingerprints") or {}
    changed_names = []
    for s in wf.steps:
        cur = _fp(s)
        prev = prev_map.get(s.name)
        if prev is None or prev != cur:
            changed_names.append(s.name)

    if not changed_names:
        print("[workflow] Изменённых шагов нет (сравнение по отпечаткам).")
        return True

    # 4) Если нужны зависимости — добавим все needs для изменённых
    names_set = set(changed_names)
    if include_deps:
        name_to_step = {s.name: s for s in wf.steps}
        def add_deps(nm):
            st = name_to_step.get(nm)
            if not st:
                return
            for dep in (st.needs or []):
                if dep not in names_set:
                    names_set.add(dep)
                    add_deps(dep)
        for nm in list(changed_names):
            add_deps(nm)

    # 5) Фильтруем шаги в исходном порядке
    filtered = [s for s in wf.steps if s.name in names_set]

    # Превью только изменённых/с зависимостями
    try:
        from rich.table import Table
        t = Table(title="Перезапуск изменённых шагов", show_lines=False)
        t.add_column("step")
        t.add_column("reason")
        for s in wf.steps:
            if s.name in names_set:
                reason = "changed" if s.name in changed_names else "dep"
                t.add_row(s.name, reason)
        print(t)
    except Exception:
        print("[workflow] Изменённые шаги:", ", ".join(n for n in [s.name for s in filtered]))

    wf2 = WorkflowSpec(
        name=f"{wf.name} (rerun changed{' +deps' if include_deps else ''})",
        steps=filtered,
        env=getattr(wf, "env", {}),
        secrets_from=getattr(wf, "secrets_from", None),
        source_path=getattr(wf, "source_path", None),
        source_sha256=getattr(wf, "source_sha256", None),
    )

    res = run_workflow(wf2, execute_step_cb=execute_step_cb, ask_confirm=True)

    # 6) Итоговая таблица: step | status | duration
    try:
        from rich.table import Table
        t2 = Table(title="Результаты перезапуска", show_lines=False)
        t2.add_column("step")
        t2.add_column("status")
        t2.add_column("duration")
        for s in res.steps:
            dur = f"{int(round(s.duration_sec))}s"
            status = "OK" if s.ok and not s.meta.get("soft_fail") else ("SOFT_FAIL" if s.meta.get("soft_fail") else "FAIL" if not s.ok else "OK")
            t2.add_row(s.step.name, status, dur)
        print(t2)
    except Exception:
        pass

    return True


def handle_rerun_from_name(cmdline: str) -> bool:
    """
    перезапусти с шага <name> | restart from <name> | run from <name>
    Пример: перезапусти с шага flaky_test
    """
    import re
    text = (cmdline or "").strip()

    # Рус/англ с кавычками/без
    m = re.search(r'(?:перезапусти|запусти|restart|run)\s+с\s+шага\s+"?([^"]+)"?$', text, flags=re.I)
    if not m:
        return False

    step_name = m.group(1).strip()
    if not step_name:
        print("[workflow] Укажи имя шага, например: перезапусти с шага build")
        return True

    # 1) Определяем YAML: из last_run.json, иначе из последнего автосохранённого
    from pathlib import Path
    import json
    yaml_path = None
    state_path = Path(".ghostcmd/last_run.json")
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            yaml_path = data.get("file_path") or None
        except Exception as e:
            print(f"[workflow] Не удалось прочитать last_run.json: {e}")

    if not yaml_path:
        yaml_path = LAST_AUTOGEN_PATH

    if not yaml_path:
        print("[workflow] Не знаю, какой YAML запускать. Либо сначала запусти workflow, либо укажи файл явно: runflow <file>.yml")
        return True

    # 2) Загружаем workflow и ищем индекс шага
    try:
        wf = load_workflow(yaml_path)
    except Exception as e:
        print(f"[workflow] Не удалось загрузить YAML '{yaml_path}': {e}")
        return True

    names = [s.name for s in wf.steps]
    try:
        start_idx = names.index(step_name)  # 0-based
    except ValueError:
        # полезная подсказка с доступными именами
        if names:
            print("[workflow] Шаг не найден. Доступные имена:", ", ".join(names))
        else:
            print("[workflow] В YAML нет шагов.")
        return True

    # 3) Строим срез с сохранением env/secrets/меты
    wf2 = WorkflowSpec(
        name=f"{wf.name} (from {step_name})",
        steps=wf.steps[start_idx:],
        env=getattr(wf, "env", {}),
        secrets_from=getattr(wf, "secrets_from", None),
        source_path=getattr(wf, "source_path", None),
        source_sha256=getattr(wf, "source_sha256", None),
    )

    print(f"[workflow] Перезапуск с шага: {step_name} (позиция {start_idx+1})")
    _ = run_workflow(wf2, execute_step_cb=execute_step_cb, ask_confirm=True)
    return True

def handle_lintflow(cmdline: str) -> bool:
    """
    lintflow <path.yml>  |  проверь workflow <path.yml>
    """
    text = (cmdline or "").strip()
    low = text.lower()

    # рус/англ триггеры
    is_lint = low.startswith("lintflow ") or low.startswith("проверь workflow ")
    if not is_lint:
        return False

    try:
        _, rest = text.split(" ", 1)
    except ValueError:
        print("[lint] Использование: lintflow <path.yml>")
        return True

    path = rest.strip()
    if not path:
        print("[lint] Укажи путь к YAML. Пример: lintflow flows/hello_v2.yml")
        return True

    try:
        from core.workflow import load_workflow
        from core.workflow_lint import lint_workflow, print_lint_report
        wf = load_workflow(path)
        issues = lint_workflow(wf)
        print_lint_report(issues)
    except Exception as e:
        print(f"[lint] Ошибка: {e}")
    return True

def handle_ci_auth(cmdline: str) -> bool:
    """
    Команды:
      - ci auth
      - ci status
      - ghost ci auth  (пропускаем префикс "ghost" на всякий случай)
    """
    raw = cmdline or ""
    text = _norm_text(raw)
    if not (text.startswith("ci auth") or text.startswith("ci status") or text.startswith("ghost ci auth")):
        return False

    if not gh_exists():
        msg = "[ci] [red]GitHub CLI (gh) не установлен.[/red]\n" + install_hint()
        print(Panel.fit(msg, border_style="red", padding=(1,2)))
        return True

    ver = gh_version() or "gh (версию не удалось определить)"
    ok, detail = gh_is_authenticated()
    if ok:
        body = f"[ci] [green]OK[/green]: {ver}\nАвторизация активна.\n\n[dim]{detail}[/dim]"
        print(Panel.fit(body, border_style="green", padding=(1,2)))
    else:
        body = (
            f"[ci] [yellow]Требуется авторизация[/yellow]: {ver}\n"
            "Выполни логин: [bold]gh auth login[/bold]\n\n"
            f"[dim]{detail}[/dim]"
        )
        print(Panel.fit(body, border_style="yellow", padding=(1,2)))
    return True

from core.ci_init import init_ci, TEMPLATES
from core.ci_ai import ci_edit, ci_fix_last
def handle_ci_init(cmdline: str) -> bool:
    """
    Обработка команды:
      - ci init
      - ci init python
      - ci init rust --force
      - ci init node --as ci_node.yml
      - ci init go --auto
      - ci init java --push
    """
    raw = cmdline or ""
    text = _norm_text(raw)
    if not text.startswith("ci init"):
        return False

    parts = raw.strip().split()
    target = "python"
    force = False
    outfile = None
    autopush = False  # <-- только один флаг

    i = 2
    while i < len(parts):
        tok = parts[i]
        low = tok.lower()
        if low in TEMPLATES:
            target = low
        elif tok in ("--force", "-f"):
            force = True
        elif tok in ("--as", "--file"):
            if i + 1 < len(parts):
                outfile = parts[i + 1]
                i += 1
        elif tok in ("--auto", "--push"):  # <-- оба включают autopush
            autopush = True
        i += 1

    # если указали --auto, но не указали файл → назначаем дефолтное имя
    if autopush and not outfile:
        outfile = f"ci_{target}.yml"

    print(f"[debug] handle_ci_init: target={target}, force={force}, outfile={outfile}, autopush={autopush}, parts={parts}")

    try:
        init_ci(target=target, force=force, outfile=outfile, autopush=autopush)
    except Exception as e:
        print(Panel.fit(f"[red]Ошибка: {e}[/red]", border_style="red"))

    return True





def handle_ci_manage(cmdline: str) -> bool:
    raw = cmdline or ""
    text = _norm_text(raw)

    if text.startswith("ci list"):
        ci_list()
        return True
    if text.startswith("ci run"):
        parts = raw.strip().split()
        workflow = None
        if len(parts) > 2:
            workflow = parts[2]
        else:
            # если имя не указано — пробуем взять .github/workflows/ci.yml
            import os, glob
            candidates = glob.glob(".github/workflows/*.yml")
            if candidates:
                workflow = os.path.basename(candidates[0])
        ci_run(workflow)
        return True

    if text.startswith("ci logs last"):
        ci_logs_last()
        return True
    if text.startswith("ci edit"):
        try:
            import shlex as _shlex
            tokens = _shlex.split(raw)
            # tokens: ['ci','edit','<features>', ...]
            features = tokens[2] if len(tokens) > 2 else ""
            filename = None
            auto_yes = ("--yes" in tokens)
            autopush = not ("--no-push" in tokens)
            if "--file" in tokens:
                idx = tokens.index("--file")
                filename = tokens[idx+1] if idx + 1 < len(tokens) else None
            ci_edit(features, filename, auto_yes=auto_yes, autopush=autopush)
        except Exception as e:
            print(Panel.fit(f"[red]ci edit: {e}[/red]", border_style="red"))
        return True

    if text.startswith("ci fix last"):
        try:
            import shlex as _shlex
            tokens = _shlex.split(raw)
            filename = None
            auto_yes = ("--yes" in tokens)
            autopush = not ("--no-push" in tokens)
            if "--file" in tokens:
                idx = tokens.index("--file")
                filename = tokens[idx+1] if idx + 1 < len(tokens) else None
            ci_fix_last(filename, auto_yes=auto_yes, autopush=autopush)
        except Exception as e:
            print(Panel.fit(f"[red]ci fix last: {e}[/red]", border_style="red"))
        return True

    return False







# =====================================================
# МИНИ-ИСТОРИЯ (SQLite → табличка)
# =====================================================
def print_history(limit: int = 10):
    rows = recent(limit)
    if not rows:
        print("[yellow]История пуста.[/yellow]")
        return
    t = Table(title=f"Последние {limit} команд", show_lines=False)
    t.add_column("ID", justify="right")
    t.add_column("Время (UTC)")
    t.add_column("Риск")
    t.add_column("Таргет")
    t.add_column("Код")
    t.add_column("Запрос")
    t.add_column("Команда")
    for r in rows:
        t.add_row(
            str(r["id"]), r["ts_utc"], r["risk"], r["exec_target"],
            str(r["exit_code"]) if r["exit_code"] is not None else "-",
            (r["user_input"] or "")[:40],
            (r["plan_cmd"] or "")[:40],
        )
    print(t)

# =====================================================
# SHOW <id> — подробный просмотр одной команды
# =====================================================
def print_show(cmd_id_str: str):
    try:
        cmd_id = int(cmd_id_str)
    except (TypeError, ValueError):
        print("[red]Укажи корректный ID: show <id>[/red]")
        return

    row = get_command(cmd_id)
    if not row:
        print(f"[yellow]Запись #{cmd_id} не найдена.[/yellow]")
        return

    # Шапка
    meta_lines = []
    meta_lines.append(f"[bold]ID:[/bold] {row['id']}   [bold]UTC:[/bold] {row['ts_utc']}")
    meta_lines.append(f"[bold]Риск:[/bold] {row['risk']}   [bold]Таргет:[/bold] {row['exec_target']}   [bold]Код:[/bold] {row['exit_code']}")
    meta_lines.append(f"[bold]Длительность:[/bold] {row['duration_ms']} ms   [bold]Sandbox:[/bold] {bool(row['sandbox'])}")
    if row.get("workflow_id"):
        meta_lines.append(f"[bold]Workflow:[/bold] {row['workflow_id']}")
    if row.get("host_alias"):
        meta_lines.append(f"[bold]Host:[/bold] {row['host_alias']}")

    user_input = row.get("user_input") or ""
    plan_cmd = row.get("plan_cmd") or ""
    explanation = row.get("explanation") or ""

    header = "\n".join(meta_lines)
    body = f"[bold]Запрос:[/bold] {user_input}\n[bold]Команда:[/bold] [yellow]{plan_cmd}[/yellow]\n[bold]Пояснение:[/bold] {explanation}"

    print(Panel.fit(header + "\n\n" + body, border_style="blue", title=f"Команда #{cmd_id}", padding=(1,2)))

    # Артефакты
    arts = artifacts_for_command(cmd_id)
    if not arts:
        print("[dim]Артефактов нет.[/dim]")
        return

    for a in arts:
        kind = a.get("kind") or "artifact"
        path = a.get("path") or ""
        preview = (a.get("preview") or "").strip()
        title = f"{kind.upper()}" + (f" — {path}" if path else "")
        if preview and len(preview) > 4000:
            preview = preview[:4000] + "\n... [preview trimmed]"
        print(Panel.fit(preview or "[пусто]", title=title, border_style="white", padding=(1,2)))

# =====================================================
# HELP (список встроенных команд)
# =====================================================
def print_help():
    t = Table(title="📖 Встроенные команды GhostCMD", show_lines=False)
    t.add_column("Команда", style="bold")
    t.add_column("Описание")
    t.add_row("help, ?", "Показать это окно со списком встроенных команд")
    t.add_row("history [N], h", "Показать последние N команд из истории (по умолчанию 10)")
    t.add_row("logs [N]", "Показать хвост логов JSONL за сегодня (по умолчанию 20 строк)")
    t.add_row("show <id>", "Подробно показать одну команду из истории с её артефактами")
    t.add_row("replay <id>", "Повторить выполнение команды из истории по ID")
    t.add_row("!!", "Повторить последнюю команду")
    t.add_row("!<id>", "Повторить команду по ID")
    t.add_row("plan", "Показать путь последнего автоген-плана и подсказки")
    t.add_row("config", "Показать путь и активные host-only маркеры для текущей ОС")
    t.add_row("перезапусти упавшие", "Запустить только упавшие шаги из последнего прогона")
    t.add_row("перезапусти изменённые [--with-deps]", "Запустить только изменённые шаги (и опционально их зависимости)")
    t.add_row("перезапусти с шага <имя>", "Запустить workflow, начиная с шага по имени (алиас к --from)")
    t.add_row("ci auth", "Проверить GitHub CLI и авторизацию для GitHub Actions")
    t.add_row("ci init [python|node|go|docker]", "Создать .github/workflows/ci.yml из шаблона (по умолчанию python)")
    t.add_row("перезапусти упавшие [--include-soft]", "Запустить только упавшие шаги (с флагом — включая soft-fail)")
    t.add_row("перезапусти изменённые [--with-deps] [--yes]", "Перезапустить только изменённые шаги (и опционально их зависимости; --yes без подтверждения)")
    t.add_row("lintflow <file>", "Проверить workflow и показать отчёт по проблемам")
    t.add_row("проверь workflow <file>", "Алиас к lintflow")
    t.add_row("overlay", "Включить/выключить GhostOverlay (HUD работает независимо от GhostCMD)")
    t.add_row("ci list", "Список workflows в репозитории")
    t.add_row("ci run [имя.yml]", "Запустить workflow (по умолчанию первый)")
    t.add_row('ci edit "..." [--file <ci.yml>] [--yes] [--no-push]', "Правки YAML естественным языком (diff-превью, автопуш)")
    t.add_row("", "Если .github/workflows содержит несколько файлов — появится меню выбора.")
    t.add_row("ci fix last [--yes] [--no-push]", "ИИ-анализ логов последнего запуска и патч YAML")
    t.add_row("ci logs last", "Показать логи последнего запуска workflow")
    t.add_row("ci init <tpl> [--auto|--as <file.yml>] [--force]", "Создать workflow (по умолчанию .github/workflows/ci.yml)")
    t.add_row("  tpl ∈ " + ", ".join(sorted(TEMPLATES.keys())), "Доступные шаблоны")
    t.add_row("  --push", "Автоматически git add/commit/push после генерации CI")








    print(t)

def print_plan_status():
    path = LAST_AUTOGEN_PATH
    if not path:
        print(Panel.fit(
            "План ещё не сохранён.\n\n"
            "Как создать:\n"
            "• просто опиши действия на естественном языке (я сгенерирую и сохраню YAML),\n"
            "• или запусти готовый: flow flows/hello.yml / runflow flows/hello.yml\n\n"
            "После сохранения будут доступны короткие команды:\n"
            "• запусти с шага N — старт последнего плана с шага N\n"
            "• измени шаг N на: <команда> — правка шага в последнем плане\n"
            "• runflow <file> [--from N] — запустить сохранённый YAML вручную",
            border_style="yellow", padding=(1,2)))
        return

    msg = (
        f"[bold]Последний автоген-план:[/bold] {path}\n\n"
        "Доступные команды:\n"
        f"• runflow {path} — запустить целиком\n"
        f"• runflow {path} --from 4 — запустить с шага 4\n"
        "• запусти с шага 4 — то же, но берём последний план автоматически\n"
        "• измени шаг 3 на: pytest -q — отредактировать шаг в YAML\n\n"
        "[dim]Примечание: пока выполняется workflow, ввод недоступен; "
        "эти команды можно ввести после завершения текущего запуска.[/dim]"
    )
    print(Panel.fit(msg, border_style="grey50", padding=(1,2)))

def print_config_status():
    cfg_path = _config_path()
    os_label = platform.system()
    marks = host_only_markers_for_current_os()
    body = (
        f"[bold]OS:[/bold] {os_label}\n"
        f"[bold]Config path:[/bold] {cfg_path}\n\n"
        f"[bold]host_only_patterns для {os_label}:[/bold]\n"
        + ("\n".join(f" • {m}" for m in marks) if marks else "[dim](нет маркеров — все dangerous шаги можно отправлять в Docker)[/dim]")
        + "\n\nПример overrides в config.yml:\n"
        "host_only_patterns:\n"
        f"  {os_label}:\n"
        "    - brew\n"
        "    - networksetup\n"
    )
    print(Panel.fit(body, border_style="blue", padding=(1,2)))


# =====================================================
# Перехват естественных фраз (алиасы к встроенным командам)
# =====================================================
def _norm_text(s: str) -> str:
    s = (s or "").lower().strip()
    s = s.replace("ё", "е")
    s = "".join(ch for ch in s if not unicodedata.category(ch).startswith("P"))
    s = " ".join(s.split())
    return s

def _extract_int(s: str) -> int | None:
    m = re.search(r"\d+", s)
    return int(m.group(0)) if m else None

def intercept_builtin_intent(user_input: str):
    raw = user_input or ""
    text = _norm_text(raw)
    # NEW: разбиение на подкоманды по ; или ,
    # Например: "измени шаг 3 на: pytest -q; поставь target docker шагу 2"
    # → две подстроки для парсинга
    subcommands = []
    if ";" in raw:
        import re as _re2
        # делим только по ;, и то только если не внутри кавычек
        parts = _re2.split(r';(?=(?:[^"]*"[^"]*")*[^"]*$)', raw)
        subcommands = [p.strip() for p in parts if p.strip()]
    else:
        subcommands = [raw]

    # Нормализация русских порядковых числительных → цифры (1..10)
    num_words = {
        "перв": 1, "втор": 2, "трет": 3, "четв": 4, "пят": 5,
        "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10
    }
    import re as _re
    def _word_to_num(s: str) -> str:
        # заменяем шаблоны вида "с третьего шага" → "с 3 шага", "шаг пятый" → "шаг 5"
        def repl(m):
            stem = m.group(1)
            n = num_words.get(stem, None)
            return f" {n} " if n else m.group(0)
        # разные формы ("третьего", "третий", "третьем" и т.п.) сводим к основе
        s = _re.sub(r"\b(перв\w*)\b", " 1 ", s)
        for stem, n in num_words.items():
            s = _re.sub(rf"\b({stem}\w*)\b", f" {n} ", s)
        # схлопываем лишние пробелы
        return " ".join(s.split())
    text = _word_to_num(text)
    raw  = _word_to_num(raw)

    # HISTORY
    hist_kw = ("история", "history", "журнал", "прошлые", "открой историю", "покажи историю")
    if any(k in text for k in hist_kw):
        limit = _extract_int(text) or 10
        return ("history", {"limit": max(1, min(200, limit))})

    # LOGS - ищем строго по словам, а не по подстрокам
    import re as _re
    logs_patterns = [
        r"\bлоги?\b",
        r"\blogs?\b",
        r"\bжурнал логов\b",
        r"\bпоследние логи\b",
        r"\bоткрой логи\b",
        r"\bпокажи логи\b",
    ]
    if any(_re.search(pat, text) for pat in logs_patterns):
        n = _extract_int(text) or 20
        return ("logs", {"count": max(1, min(1000, n))})

    # SHOW
    show_kw = ("подробно", "детали", "details", "show", "покажи команду", "открой команду")
    if any(k in text for k in show_kw):
        cid = _extract_int(text)
        if cid is not None:
            return ("show", {"id": cid})

    # REPLAY
    replay_kw = ("повтори", "replay", "запусти снова", "еще раз", "ещё раз", "снова выполнить")
    if any(k in text for k in replay_kw):
        cid = _extract_int(text)
        if cid is not None:
            return ("replay", {"id": cid})
        if "последн" in text or "предыдущ" in text:
            return ("replay", {"last": True})

    # RUNFLOW FROM N
    runfrom_kw = ("запусти с шага", "запусти с", "начни с шага", "начни с", "стартуй с", "от шага", "run from", "start from")
    if any(k in text for k in runfrom_kw):
        n = _extract_int(text)
        if n is not None:
            return ("runflow_from", {"start": max(1, n)})
    # вариант "с 4 шага"
    m_from = re.search(r"\bс\s+(\d+)\s+шага\b", text)
    if m_from:
        return ("runflow_from", {"start": max(1, int(m_from.group(1)))})

    # EDIT STEP по номеру: "измени шаг 3 на: pytest -q" или без двоеточия
    m = re.search(r"(измени|поменяй|редактируй|change|edit|update)\s+(?:шаг|step)\s+(\d+)\s+на(?::)?\s*(.+)$", raw, flags=re.IGNORECASE)
    if m:
        return ("edit_step", {"index": int(m.group(2)), "cmd": m.group(3).strip()})

    # EDIT STEP по имени: "измени шаг step_3 на: pytest -q"
    m2 = re.search(r"(измени|поменяй|редактируй|change|edit|update)\s+(?:шаг|step)\s+([a-zA-Z0-9_.-]+)\s+на(?::)?\s*(.+)$", raw, flags=re.IGNORECASE)
    if m2:
        return ("edit_step_by_name", {"name": m2.group(2).strip(), "cmd": m2.group(3).strip()})

     # COACH (overlay): "coach", "start coach", "open coach", "открой коуч"
    if re.search(r"\b(coach|коуч)\b", raw, flags=re.IGNORECASE):
        if re.search(r"\b(status|статус)\b", raw, flags=re.IGNORECASE):
            return ("coach", {"action": "status"})
        if re.search(r"\b(stop|стоп|останови|kill)\b", raw, flags=re.IGNORECASE):
            return ("coach", {"action": "stop"})
        if re.search(r"\b(open|открой|show|ui)\b", raw, flags=re.IGNORECASE):
            return ("coach", {"action": "open"})
        if re.search(r"\b(start|запусти|run|launch)\b", raw, flags=re.IGNORECASE):
            return ("coach", {"action": "start"})
        return ("coach", {"action": "start"})



    # PLAN STATUS
    plan_kw = ("план", "покажи план", "где план", "где файл плана", "plan")
    if any(k == text or k in text for k in plan_kw):
        return ("plan_status", {})

    # RUN LAST PLAN
    runplan_kw = ("запусти план", "повтори план", "run plan", "run last plan", "запусти workflow", "запусти последний план")
    if any(k in text for k in runplan_kw):
        return ("runflow_last", {})

        # ===== Natural-language edits → ops =====


    edit_ops: list[dict] = []

    for sub in subcommands:
        sub_lower = sub.lower()

    # set run by index: "измени шаг 3 на: pytest -q"
    m = _re.search(r"(измени|поменяй|редактируй|change|edit|update)\s+(?:шаг|step)\s+(\d+)\s+на(?::)?\s*(.+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_run", "step": {"index": int(m.group(2))}, "value": m.group(3).strip()})

    # set run by name: "измени шаг build на: npm ci"
    m = _re.search(r"(измени|поменяй|редактируй|change|edit|update)\s+(?:шаг|step)\s+([a-zA-Z0-9_.-]+)\s+на(?::)?\s*(.+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_run", "step": {"name": m.group(2).strip()}, "value": m.group(3).strip()})

    # set target: "поставь target docker шагу 3", "target host шагу build"
    m = _re.search(r"(поставь|set)\s+target\s+(auto|host|docker)\s+(?:шагу|for\s+step)\s+(\d+)", text, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_target", "step": {"index": int(m.group(3))}, "value": m.group(2).lower()})
    m = _re.search(r"(поставь|set)\s+target\s+(auto|host|docker)\s+(?:шагу|for\s+step)\s+([a-zA-Z0-9_.-]+)", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_target", "step": {"name": m.group(3)}, "value": m.group(2).lower()})

    # timeout: "поставь timeout 60s шагу 2"
    m = _re.search(r"(поставь|set)\s+timeout\s+([0-9a-zA-Z.]+)\s+(?:шагу|for\s+step)\s+(\d+)", text, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_timeout", "step": {"index": int(m.group(3))}, "value": m.group(2)})
    m = _re.search(r"(поставь|set)\s+timeout\s+([0-9a-zA-Z.]+)\s+(?:шагу|for\s+step)\s+([a-zA-Z0-9_.-]+)", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_timeout", "step": {"name": m.group(3)}, "value": m.group(2)})

    # if-condition: "поставь if '$[[ ... ]]' шагу test"
    m = _re.search(r"(поставь|set)\s+if\s+(.+?)\s+(?:шагу|for\s+step)\s+(\d+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_if", "step": {"index": int(m.group(3))}, "value": m.group(2).strip()})
    m = _re.search(r"(поставь|set)\s+if\s+(.+?)\s+(?:шагу|for\s+step)\s+([a-zA-Z0-9_.-]+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_if", "step": {"name": m.group(3)}, "value": m.group(2).strip()})

    # cwd: "поставь cwd ./app шагу 3"
    m = _re.search(r"(поставь|set)\s+cwd\s+(\S+)\s+(?:шагу|for\s+step)\s+(\d+)", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_cwd", "step": {"index": int(m.group(3))}, "value": m.group(2)})
    m = _re.search(r"(поставь|set)\s+cwd\s+(\S+)\s+(?:шагу|for\s+step)\s+([a-zA-Z0-9_.-]+)", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_cwd", "step": {"name": m.group(3)}, "value": m.group(2)})

    # env add: "добавь env FOO=bar BAR=baz шагу 2"
    m = _re.search(r"(добавь|add)\s+env\s+(.+?)\s+(?:шагу|to\s+step)\s+(\d+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_env", "step": {"index": int(m.group(3))}, "value": m.group(2)})
    m = _re.search(r"(добавь|add)\s+env\s+(.+?)\s+(?:шагу|to\s+step)\s+([a-zA-Z0-9_.-]+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_env", "step": {"name": m.group(3)}, "value": m.group(2)})

    # env del: "удали env FOO у шага build"
    m = _re.search(r"(удали|remove|unset)\s+env\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:у\s+шага|from\s+step)\s+(\d+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "unset_env", "step": {"index": int(m.group(3))}, "key": m.group(2)})
    m = _re.search(r"(удали|remove|unset)\s+env\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:у\s+шага|from\s+step)\s+([a-zA-Z0-9_.-]+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "unset_env", "step": {"name": m.group(3)}, "key": m.group(2)})

    # retries: "retries max=3 delay=2s backoff=1.5 шагу 2"
    m = _re.search(r"retries\s+((?:max=\d+\s*)?(?:delay=[0-9a-zA-Z.]+\s*)?(?:backoff=[0-9.]+\s*)?)\s+(?:шагу|for\s+step)\s+(\d+)", raw, flags=_re.IGNORECASE)
    if m:
        kv = dict(_re.findall(r'(\w+)=([^\s]+)', m.group(1)))
        op = {"op": "set_retries", "step": {"index": int(m.group(2))}}
        op.update(kv)
        edit_ops.append(op)
    m = _re.search(r"retries\s+((?:max=\d+\s*)?(?:delay=[0-9a-zA-Z.]+\s*)?(?:backoff=[0-9.]+\s*)?)\s+(?:шагу|for\s+step)\s+([a-zA-Z0-9_.-]+)", raw, flags=_re.IGNORECASE)
    if m:
        kv = dict(_re.findall(r'(\w+)=([^\s]+)', m.group(1)))
        op = {"op": "set_retries", "step": {"name": m.group(2)}}
        op.update(kv)
        edit_ops.append(op)

    # needs: "поставь needs build,lint шагу 3" / "добавь needs ..." / "удали из needs ..."
    m = _re.search(r"(поставь|set)\s+needs\s+(.+?)\s+(?:шагу|for\s+step)\s+(\d+)$", raw, flags=_re.IGNORECASE)
    if m:
        vals = [x.strip() for x in m.group(2).replace(",", " ").split() if x.strip()]
        edit_ops.append({"op": "set_needs", "step": {"index": int(m.group(3))}, "value": vals})
    m = _re.search(r"(поставь|set)\s+needs\s+(.+?)\s+(?:шагу|for\s+step)\s+([a-zA-Z0-9_.-]+)$", raw, flags=_re.IGNORECASE)
    if m:
        vals = [x.strip() for x in m.group(2).replace(",", " ").split() if x.strip()]
        edit_ops.append({"op": "set_needs", "step": {"name": m.group(3)}, "value": vals})

    m = _re.search(r"(добавь|add)\s+needs\s+(.+?)\s+(?:шагу|to\s+step)\s+(\d+)$", raw, flags=_re.IGNORECASE)
    if m:
        vals = [x.strip() for x in m.group(2).replace(",", " ").split() if x.strip()]
        edit_ops.append({"op": "add_needs", "step": {"index": int(m.group(3))}, "value": vals})
    m = _re.search(r"(добавь|add)\s+needs\s+(.+?)\s+(?:шагу|to\s+step)\s+([a-zA-Z0-9_.-]+)$", raw, flags=_re.IGNORECASE)
    if m:
        vals = [x.strip() for x in m.group(2).replace(",", " ").split() if x.strip()]
        edit_ops.append({"op": "add_needs", "step": {"name": m.group(3)}, "value": vals})

    m = _re.search(r"(удали|remove|del)\s+из\s+needs\s+(.+?)\s+(?:у\s+шага|from\s+step)\s+(\d+)$", raw, flags=_re.IGNORECASE)
    if m:
        vals = [x.strip() for x in m.group(2).replace(",", " ").split() if x.strip()]
        edit_ops.append({"op": "del_needs", "step": {"index": int(m.group(3))}, "value": vals})
    m = _re.search(r"(удали|remove|del)\s+из\s+needs\s+(.+?)\s+(?:у\s+шага|from\s+step)\s+([a-zA-Z0-9_.-]+)$", raw, flags=_re.IGNORECASE)
    if m:
        vals = [x.strip() for x in m.group(2).replace(",", " ").split() if x.strip()]
        edit_ops.append({"op": "del_needs", "step": {"name": m.group(3)}, "value": vals})

    # mask: "добавь mask SECRET шагу 2" / "очисти mask у шага build"
    m = _re.search(r"(добавь|add)\s+mask\s+(.+?)\s+(?:шагу|to\s+step)\s+(\d+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_mask", "step": {"index": int(m.group(3))}, "value": [x for x in m.group(2).split()]})
    m = _re.search(r"(очисти|clear)\s+mask\s+(?:у\s+шага|of\s+step)\s+(\d+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "clear_mask", "step": {"index": int(m.group(1))}})

    # root env: "добавь root env FOO=1 BAR=2", "удали root env FOO"
    m = _re.search(r"(добавь|add)\s+root\s+env\s+(.+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "set_root_env", "value": m.group(2)})
    m = _re.search(r"(удали|remove|unset)\s+root\s+env\s+([A-Za-z_][A-Za-z0-9_]*)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "unset_root_env", "key": m.group(2)})

    # rename: "переименуй шаг 3 в build", "переименуй шаг test в unit"
    m = _re.search(r"(переименуй|rename)\s+(?:шаг|step)\s+(\d+)\s+в\s+([A-Za-z0-9_.-]+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "rename_step", "step": {"index": int(m.group(2))}, "new_name": m.group(3)})
    m = _re.search(r"(переименуй|rename)\s+(?:шаг|step)\s+([A-Za-z0-9_.-]+)\s+в\s+([A-Za-z0-9_.-]+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "rename_step", "step": {"name": m.group(2)}, "new_name": m.group(3)})

    # insert: "вставь шаг после 3: npm ci", "вставь шаг перед build: {name: lint, run: eslint .}"
    m = _re.search(r"(вставь|insert)\s+(?:шаг|step)\s+после\s+(\d+)\s*:\s*(.+)$", raw, flags=_re.IGNORECASE|_re.S)
    if m:
        val = m.group(2).strip()
        edit_ops.append({"op": "insert_after", "step": {"index": int(m.group(2))}, "value": m.group(3).strip()})
    m = _re.search(r"(вставь|insert)\s+(?:шаг|step)\s+перед\s+(\d+)\s*:\s*(.+)$", raw, flags=_re.IGNORECASE|_re.S)
    if m:
        edit_ops.append({"op": "insert_before", "step": {"index": int(m.group(2))}, "value": m.group(3).strip()})

    m = _re.search(r"(вставь|insert)\s+(?:шаг|step)\s+после\s+([A-Za-z0-9_.-]+)\s*:\s*(.+)$", raw, flags=_re.IGNORECASE|_re.S)
    if m:
        edit_ops.append({"op": "insert_after", "step": {"name": m.group(2)}, "value": m.group(3).strip()})
    m = _re.search(r"(вставь|insert)\s+(?:шаг|step)\s+перед\s+([A-Za-z0-9_.-]+)\s*:\s*(.+)$", raw, flags=_re.IGNORECASE|_re.S)
    if m:
        edit_ops.append({"op": "insert_before", "step": {"name": m.group(2)}, "value": m.group(3).strip()})

    # delete: "удали шаг 3" / "delete step build"
    m = _re.search(r"(удали|delete|remove)\s+(?:шаг|step)\s+(\d+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "delete_step", "step": {"index": int(m.group(2))}})
    m = _re.search(r"(удали|delete|remove)\s+(?:шаг|step)\s+([A-Za-z0-9_.-]+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "delete_step", "step": {"name": m.group(2)}})

    # move: "перемести шаг 5 перед 2" / "move step build after test"
    m = _re.search(r"(перемести|move)\s+(?:шаг|step)\s+(\d+)\s+перед\s+(\d+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "move_before", "step": {"index": int(m.group(2))}, "anchor": {"index": int(m.group(3))}})
    m = _re.search(r"(перемести|move)\s+(?:шаг|step)\s+(\d+)\s+после\s+(\d+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "move_after", "step": {"index": int(m.group(2))}, "anchor": {"index": int(m.group(3))}})

    m = _re.search(r"(перемести|move)\s+(?:шаг|step)\s+([A-Za-z0-9_.-]+)\s+перед\s+([A-Za-z0-9_.-]+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "move_before", "step": {"name": m.group(2)}, "anchor": {"name": m.group(3)}})
    m = _re.search(r"(перемести|move)\s+(?:шаг|step)\s+([A-Za-z0-9_.-]+)\s+после\s+([A-Za-z0-9_.-]+)$", raw, flags=_re.IGNORECASE)
    if m:
        edit_ops.append({"op": "move_after", "step": {"name": m.group(2)}, "anchor": {"name": m.group(3)}})

    if edit_ops:
        return ("nl_edit_ops", {"ops": edit_ops})
        # ===== CI генерация =====
        # ... другие блоки intercept_builtin_intent ...

    # ===== CI генерация из естественного описания (Шаг 4) =====
    # Примеры:
    #  - сделай ci для python с black и coverage
    #  - generate workflow for node with eslint and docker push
    m = _re.search(
        r"^(сгенерируй|создай|сделай|generate|make)\s+(?:ci|workflow)\s+(?:для|for)\s+"
        r"(python|node|docker|докер|go|java|rust|dotnet|generic)"
        r"(?:\s+(?:с|with)\s+(.+))?$",
        raw.strip(),
        flags=_re.IGNORECASE
    )
    if m:
        kind = m.group(2).lower()
        if kind == "докер":
            kind = "docker"
        features = (m.group(3) or "").strip()
        if features:
            return ("gen_ci_from_nl", {"kind": kind, "features": features})
        else:
            return ("gen_ci", {"kind": kind})


    # ===== CI генерация =====
    m = _re.search(
    r"(сгенерируй|создай|generate|make)\s+ci\s+(?:для|for)\s+(python|node|docker|докер|go|java|rust|dotnet|generic)",
    raw,
    flags=_re.IGNORECASE
    )
    if m:
        kind = m.group(2).lower()
        if kind == "докер":
            kind = "docker"
        return ("gen_ci", {"kind": kind})


    # ===== Docker автоген =====
    m = _re.search(r"(собери|построй|build)\s+(докер|docker)(?:[- ]образ| image)?", raw, flags=_re.IGNORECASE)
    if m:
        return ("gen_docker_workflow", {"action": "build"})

    m = _re.search(r"(запусти|run)\s+(докер|docker)(?:[- ]контейнер| container)?", raw, flags=_re.IGNORECASE)
    if m:
        return ("gen_docker_workflow", {"action": "run"})

    return None


# =====================================================
# Утилиты риска и эвристики записи в ФС
# =====================================================
def _risk_to_db(r: str) -> str:
    r = (r or "").lower()
    if r in ("read_only", "read-only", "green", "safe", "readonly"):
        return "green"
    if r in ("mutating", "yellow"):
        return "yellow"
    if r in ("dangerous", "red"):
        return "red"
    if r in ("blocked_interactive", "blocked"):
        return "blocked"
    return "green"

import re as _re
_WRITE_LIKE_PATTERNS = [
    r">>\s*", r">\s*(?!/?dev/null)", r"\btee\b", r"\btouch\b", r"\btruncate\b",
    r"\bmkdir\b", r"\brmdir\b", r"\bmv\b", r"\bcp\b",
    r"\bsed\b.*\s-i\b", r"\bchmod\b", r"\bchown\b", r"\bln\b",
    r"\bwget\b.*\s-(O|output-document)\b", r"\bcurl\b.*\s-(o|O)\b",
]
def is_write_like(cmd: str) -> bool:
    c = (cmd or "").strip()
    for pat in _WRITE_LIKE_PATTERNS:
        if _re.search(pat, c, flags=_re.IGNORECASE):
            return True
    return False

# =====================================================
# OS-специфичная коррекция
# =====================================================
def correct_command_for_os(command: str) -> str:
    os_type = platform.system()

    fixes = {
        "top_memory": {
            "Linux": "ps aux --sort=-%mem | head -n 10",
            "Darwin": "ps aux | sort -nrk 4 | head -n 10",
            "Windows": "Get-Process | Sort-Object WorkingSet -Descending | Select-Object -First 10",
        },
        "top_cpu": {
            "Linux": "ps -eo pid,comm,%cpu --sort=-%cpu | head -n 10",
            "Darwin": "ps aux | sort -nrk 3 | head -n 10",
            "Windows": "Get-Process | Sort-Object CPU -Descending | Select-Object -First 10",
        },
        "disk_usage": {
            "Linux": "df -h",
            "Darwin": "df -h",
            "Windows": "Get-PSDrive C | Select-Object Used,Free",
        },
        "ip_address": {
            "Linux": "hostname -I | awk '{print $1}'",
            "Darwin": "ipconfig getifaddr en0",
            "Windows": "Get-NetIPAddress | findstr IPv4",
        },
        "python_version": {
            "Linux": "python3 --version",
            "Darwin": "python3 --version",
            "Windows": "python --version",
        },
    }

    cmd = (command or "").strip()
    if "%mem" in cmd:
        return fixes["top_memory"].get(os_type, cmd)
    if "%cpu" in cmd:
        return fixes["top_cpu"].get(os_type, cmd)
    if "df -h" in cmd:
        return fixes["disk_usage"].get(os_type, cmd)
    if "ipconfig" in cmd or "hostname -I" in cmd or "Get-NetIPAddress" in cmd:
        return fixes["ip_address"].get(os_type, cmd)
    if "python" in cmd and "--version" in cmd:
        return fixes["python_version"].get(os_type, cmd)
    return cmd

def _unwrap_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```") and s.endswith("```"):
        return s[3:-3].strip()
    return s

def _unwrap_wrapping_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"', "`"):
        return s[1:-1]
    return s

def clean_command(command: str) -> str:
    s = (command or "").strip()
    s = _unwrap_code_fence(s)
    s = _unwrap_wrapping_quotes(s)
    return s

# =====================================================
# Быстрые красные флаги и запреты на хосте
# =====================================================
FORK_BOMB_PATTERNS = [
    r":\(\)\s*{\s*:\s*\|\s*:\s*;?\s*&?\s*}\s*;?\s*:?\s*$",
    r"\b([a-zA-Z_]\w*)\s*\(\)\s*{\s*\1\s*\|\s*\1\s*&?\s*;?\s*}\s*;?\s*\1\b",
]

QUICK_DANGERS = [
    *FORK_BOMB_PATTERNS,
    r"(^|\s)rm\s+-rf\s+/(?:\s|$)",
    r"(^|\s)rm\s+-rf\s+/\*",
    r"(^|\s)shutdown(\s|$)",
    r"(^|\s)reboot(\s|$)",
]

def is_quick_danger(cmd: str) -> bool:
    for pat in QUICK_DANGERS:
        if re.search(pat, cmd, flags=re.IGNORECASE):
            return True
    return False

DESTRUCTIVE_DENY_PATTERNS = [
    *FORK_BOMB_PATTERNS,
    r"(^|\s)rm\s+-rf\s+/(?:\s|$)",
    r"(^|\s)rm\s+-rf\s+/\*",
    r"\bdd\b\s+if=/dev/zero\b",
    r"\bdd\b\s+of=/dev/(sd|vd|nvme|disk)\w",
    r"\bdiskutil\b\s+erase",
    r"\bmkfs(\.| )",
    r"\bparted\b", r"\bfdisk\b",
    r"\bmount\b\s+-o\s+remount,ro\s+/",
    r"\bswapoff\b\s+-a",
]

def is_destructive_on_host(cmd: str) -> bool:
    for pat in DESTRUCTIVE_DENY_PATTERNS:
        if re.search(pat, cmd, flags=re.IGNORECASE):
            return True
    return False

# =====================================================
# Предоценка риска шагов workflow + сводка
# =====================================================
_RISK_ICON = {"read_only": "⚪", "mutating": "🟠", "dangerous": "🔴"}
_RISK_LABEL_SHORT = {"read_only": "read-only", "mutating": "mutating", "dangerous": "danger"}

def _looks_host_only(cmd: str) -> bool:
    """Проверка по маркерам из конфига для текущей ОС (substring, case-insensitive)."""
    low = (cmd or "").lower()
    for mark in host_only_markers_for_current_os():
        m = (mark or "").strip()
        if not m:
            continue
        if m.lower() in low:
            return True
    return False


def _classify_step_risk(run_cmd: str) -> tuple[str, str]:
    cmd = run_cmd or ""
    base_risk = assess_risk(cmd)

    if base_risk == "read_only" and is_write_like(cmd):
        base_risk = "mutating"

    note = None
    if _looks_host_only(cmd):
        base_risk = "dangerous"
        # найдём конкретный маркер, который сработал
        for mark in host_only_markers_for_current_os():
            if mark.lower() in cmd.lower():
                note = f"host-only: {mark}"
                break

    # эвристика таргета
    if base_risk == "dangerous":
        sugg = "host" if _looks_host_only(cmd) else "docker"
    else:
        sugg = "host"  # по умолчанию

    # добавим пояснение к риску
    risk_label = base_risk
    if note:
        risk_label += f" ({note})"

    return risk_label, sugg


def _print_risk_summary(wf_name: str, steps_for_summary: list):
    """
    steps_for_summary: список dict с ключами:
      - name
      - risk (например "dangerous" или "dangerous (host-only: brew)")
      - target_suggest
    """
    from rich.table import Table
    from rich import box

    def _risk_base(label: str) -> str:
        l = (label or "").lower()
        if l.startswith("dangerous"):
            return "dangerous"
        if l.startswith("mutating"):
            return "mutating"
        return "read_only"

    cnt = {"read_only": 0, "mutating": 0, "dangerous": 0}

    table = Table(title=f"План: {wf_name}\n           • сводка рисков            ",
                  box=box.SIMPLE_HEAVY)
    table.add_column("#", justify="right", style="bold")
    table.add_column("step")
    table.add_column("risk", justify="center")
    table.add_column("target", justify="center")

    for i, s in enumerate(steps_for_summary, start=1):
        r = s.get("risk", "read_only")
        t = s.get("target_suggest", "auto")
        rb = _risk_base(r)
        cnt[rb] += 1
        icon = _RISK_ICON.get(rb, "•")
        short = _RISK_LABEL_SHORT.get(rb, rb)
        table.add_row(str(i), s.get("name", f"step_{i}"), f"{icon} {r}", t)

    print(table)
    print(Panel.fit(
        f"Итого: {cnt['dangerous']} dangerous, {cnt['mutating']} mutating, {cnt['read_only']} read-only",
        border_style="grey50", padding=(1,2)
    ))

    return cnt  # <= ВОТ ЭТО важно



# =====================================================
# Docker-песочница (часть старого кода для совместимости build/translate)
# =====================================================
SANDBOX_IMAGE = "ghost-sandbox:latest"
LIMIT_MEMORY = "512m"
LIMIT_CPUS = "1.0"
LIMIT_PIDS = "256"
ULIMIT_NOFILE = "1024:1024"
ULIMIT_NPROC = "256:256"
TIMEOUT_HOST = 60
TIMEOUT_SANDBOX = 90
TRIM_OUTPUT = 20000
LAST_AUTOGEN_PATH: str | None = None
def ensure_sandbox_image() -> bool:
    check = subprocess.run(
        ["docker", "image", "inspect", SANDBOX_IMAGE],
        check=False, capture_output=True, text=True
    )
    return check.returncode == 0

def build_sandbox_image() -> bool:
    print("🔧 Собираю образ песочницы…")
    build = subprocess.run(
        ["docker", "build", "-f", "Dockerfile.sandbox", "-t", SANDBOX_IMAGE, "."],
        check=False, text=True
    )
    return build.returncode == 0

def ensure_or_build_sandbox() -> bool:
    if ensure_sandbox_image():
        return True
    return build_sandbox_image()

def needs_network_access(cmd: str) -> bool:
    low = (cmd or "").lower()
    keywords = [
        "curl ", "wget ", "apt ", "apt-get ", "pip ", "pip3 ", "git clone ",
        "ping ", "traceroute ", "nc ", "ncat ", "telnet ",
        "dig ", "nslookup ", "host ",
    ]
    return any(k in low for k in keywords)

def needs_net_admin(cmd: str) -> bool:
    low = (cmd or "").lower()
    keywords = [
        "ip link", "ip addr", "ip route", "ifconfig", "route ",
        "networksetup", "nmcli", "ethtool", "sysctl net.", "iptables", "tc "
    ]
    return any(k in low for k in keywords)

def needs_fs_write(cmd: str) -> bool:
    low = (cmd or "").lower()
    patterns = [
        "rm ", "mv ", "cp ", "touch ", "mkdir ", "rmdir ",
        "chmod ", "chown ", "ln ", "tee ", "echo >",
        "apt ", "apt-get ", "dpkg ", "pip ", "pip3 ",
        "sed -i", "truncate ", "dd ", "mkfs", "mount ", "umount "
    ]
    return any(p in low for p in patterns)

def translate_for_sandbox(cmd: str) -> str:
    c = (cmd or "").strip()
    if c.startswith("sudo "):
        c = c[5:].lstrip()
    low = c.lower()

    wipe_root = (
        re.search(r"(^|\s)rm\s+-rf\s+/(?:\s|$)", low) or
        re.search(r"(^|\s)rm\s+-rf\s+/\*", low) or
        re.search(r"--no-preserve-root(\s|$)", low)
    )
    if wipe_root:
        return ("count=$(find / -xdev -mindepth 1 | wc -l); "
                "find / -xdev -mindepth 1 -delete 2>/dev/null; "
                "echo \"🗑️ Удалено $count объектов в контейнере (rootfs очищен)\"")

    if low.startswith("networksetup "):
        if " -setnetworkserviceenabled " in low:
            if low.strip().endswith(" off"):
                return "ip link set eth0 down || true"
            if low.strip().endswith(" on"):
                return "ip link set eth0 up || true"
        return "echo 'networksetup недоступен в Ubuntu-среде'; ip a"

    if low.startswith("scutil ") and " --set " in low and "hostname" in low:
        parts = shlex.split(c)
        try:
            idx = parts.index("--set")
            if idx + 2 <= len(parts):
                key = parts[idx+1].lower()
                val = parts[idx+2]
                if key == "hostname":
                    return f"hostname {val!r} && echo 'hostname set to {val}' || true"
        except ValueError:
            pass
        return "echo 'scutil недоступен; используйте hostname'; hostname || true"

    if low.startswith("say "):
        return f"echo {c[4:].strip()}"

    if low.startswith("open "):
        arg = c[5:].strip()
        if arg.startswith("http://") or arg.startswith("https://"):
            return f"echo 'Cannot open GUI in sandbox. URL: {arg}'"
        return f"ls -la {arg} || echo 'GUI open недоступен; показал ls'"

    if low.startswith("pbcopy") or low.startswith("pbpaste"):
        return "echo 'pbcopy/pbpaste недоступны в Ubuntu-контейнере'"

    for mac_only in ("pmset", "systemsetup", "launchctl"):
        if low == mac_only or low.startswith(mac_only + " "):
            return f"echo 'Команда недоступна в Ubuntu-контейнере: {c}'"

    return c

def build_docker_cmd(cmd_for_container: str) -> list[str]:
    allow_net = needs_network_access(cmd_for_container)
    need_admin = needs_net_admin(cmd_for_container)
    write_fs = needs_fs_write(cmd_for_container)

    args = [
        "docker", "run", "--rm",
        "--pids-limit", str(LIMIT_PIDS),
        "--memory", LIMIT_MEMORY,
        "--cpus", LIMIT_CPUS,
        "--ulimit", f"nofile={ULIMIT_NOFILE}",
        "--ulimit", f"nproc={ULIMIT_NPROC}",
        "--security-opt", "no-new-privileges",
        "-e", "LANG=C.UTF-8",
    ]

    if allow_net:
        args += ["--network", "bridge"]
    else:
        args += ["--network", "none"]

    args += ["--cap-drop", "ALL"]
    if need_admin:
        args += ["--cap-add", "NET_ADMIN"]

    if not write_fs:
        args += ["--read-only", "--tmpfs", "/tmp", "--tmpfs", "/run"]

    args += [SANDBOX_IMAGE, "bash", "-lc", cmd_for_container]
    return args

def trim(s: str, limit: int = TRIM_OUTPUT) -> str:
    if s is None:
        return ""
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n... [output trimmed to {limit} chars]"

# старые хелперы run_host/run_in_sandbox остаются только для совместимости тестового CLI
def run_host(cmd: str, timeout_sec: int = TIMEOUT_HOST) -> tuple[int, str]:
    try:
        res = subprocess.run(
            cmd, shell=True, check=False, text=True,
            capture_output=True, timeout=timeout_sec,
        )
        out = (res.stdout or "") + (res.stderr or "")
        return res.returncode, trim(out)
    except subprocess.TimeoutExpired:
        return 124, "⏱️ Timeout on host"

def run_in_sandbox(cmd: str, timeout_sec: int = TIMEOUT_SANDBOX) -> tuple[int, str]:
    docker_cmd = build_docker_cmd(cmd)
    try:
        res = subprocess.run(
            docker_cmd, check=False, text=True,
            capture_output=True, timeout=timeout_sec,
        )
        out = (res.stdout or "") + (res.stderr or "")
        return res.returncode, trim(out)
    except FileNotFoundError:
        return 127, "❌ Docker не найден. Установи и запусти Docker Desktop."
    except subprocess.TimeoutExpired:
        return 124, "⏱️ Timeout в песочнице"

# =====================================================
# Риск и исполнение одиночных команд
# =====================================================
def normalize_risk(r: str) -> str:
    r = (r or "").lower()
    if r in ("read_only", "read-only", "green", "safe", "readonly"):
        return "green"
    if r in ("mutating", "yellow"):
        return "yellow"
    if r in ("dangerous", "red"):
        return "red"
    if r in ("blocked_interactive", "blocked"):
        return "blocked"
    return r

def is_destructive_on_host(cmd: str) -> bool:
    for pat in DESTRUCTIVE_DENY_PATTERNS:
        if re.search(pat, cmd, flags=re.IGNORECASE):
            return True
    return False

def execute_command(corrected_cmd: str, risk_level: str):
    """
    Возвращает (exit_code, output_text, actual_target, meta),
    где actual_target ∈ {'host','docker','dry'},
    meta = {'kill_reason','duration_sec','limits','target'}.
    """
    rl = normalize_risk(risk_level)
    risk_map_for_limits = {"green": "read_only", "yellow": "mutating", "red": "dangerous"}

    if rl == "blocked":
        return 1, "⛔ Интерактивные/заблокированные команды не поддерживаются.", "dry", {
            "kill_reason": "none", "duration_sec": 0.0, "limits": {}, "target": "dry",
        }

    target: str | None = None
    cmd_to_run = corrected_cmd

    if rl == "green":
        if not Confirm.ask("ℹ️ Команда read-only. Выполнить?", default=True):
            return 1, "❌ Отменено пользователем.", "dry", {
                "kill_reason": "manual_cancel", "duration_sec": 0.0, "limits": {}, "target": "dry",
            }
        target = "host"

    elif rl == "yellow":
        if not Confirm.ask("⚠️ Команда изменит систему на ХОСТЕ. Выполнить?", default=False):
            return 1, "❌ Отменено пользователем.", "dry", {
                "kill_reason": "manual_cancel", "duration_sec": 0.0, "limits": {}, "target": "dry",
            }
        target = "host"

    elif rl == "red":
        # НОРМАЛИЗАТОР ВЫБОРА ТАРГЕТА (англ/рус)
        def _normalize_where(s: str) -> str | None:
            s = (s or "").strip().lower()
            mapping = {
                "d": "d", "docker": "d", "д": "d", "докер": "d",
                "h": "h", "host": "h", "х": "h", "хост": "h",
                "c": "c", "cancel": "c", "с": "c", "стоп": "c", "отмена": "c",
            }
            return mapping.get(s)

        while True:
            where_raw = Prompt.ask(
                "Где выполнить? ([bold]d/д[/bold]=Docker, [bold]h/х[/bold]=Хост, [bold]c/с[/bold]=Отмена)",
                default="d"
            )
            where = _normalize_where(where_raw)
            if where:
                break
            print("[yellow]Не понял выбор. Введи d/д, h/х или c/с.[/yellow]")

        if where == "c":
            return 1, "❌ Отменено пользователем.", "dry", {
                "kill_reason": "manual_cancel",
                "duration_sec": 0.0,
                "limits": {},
                "target": "dry",
            }

        if where == "h":
            if is_destructive_on_host(corrected_cmd):
                return 1, "⛔ Эта команда слишком разрушительна и заблокирована для хоста. Выполни в песочнице.", "dry", {
                    "kill_reason": "manual_cancel",
                    "duration_sec": 0.0,
                    "limits": {},
                    "target": "dry",
                }
            if not Confirm.ask("⚠️ Опасная команда будет выполнена на ХОСТЕ. Продолжить?", default=False):
                return 1, "❌ Отменено пользователем.", "dry", {
                    "kill_reason": "manual_cancel",
                    "duration_sec": 0.0,
                    "limits": {},
                    "target": "dry",
                }
            target = "host"
        else:
            if not ensure_or_build_sandbox():
                return 1, f"Не удалось собрать {SANDBOX_IMAGE}. Проверь Dockerfile.sandbox и Docker Desktop.", "dry", {
                    "kill_reason": "manual_cancel",
                    "duration_sec": 0.0,
                    "limits": {},
                    "target": "dry",
                }
            cmd_to_run = translate_for_sandbox(corrected_cmd)
            print(Panel.fit("🧪 Запуск в [bold magenta]Docker-песочнице[/bold magenta]", border_style="magenta", padding=(1,2)))
            target = "docker"

    # Запуск через движок лимитов
    risk_for_limits = risk_map_for_limits.get(rl, "read_only")
    res = execute_with_limits(cmd_to_run, risk=risk_for_limits, target=target, cwd=None, env=None)

    # Снимок лимитов
    L = load_limits_for_risk(risk_for_limits)
    limits_snapshot = {
        "timeout_sec": L.timeout_sec, "grace_kill_sec": L.grace_kill_sec,
        "cpus": L.cpus, "memory_mb": L.memory_mb, "pids": L.pids, "no_network": L.no_network,
    }
    target_eff = target or ("docker" if rl == "red" else "host")

    # Объединяем вывод
    combined_out = (res.stdout or "")
    if res.stderr:
        combined_out += ("\n" if combined_out else "") + res.stderr
    if res.killed:
        reason = "тайм-аут" if res.kill_reason == "timeout" else \
                 "превышение памяти" if res.kill_reason == "memory_exceeded" else res.kill_reason
        combined_out = (combined_out + ("\n" if combined_out else "")) + f"🧯 Прервано по причине: {reason}"

    meta = {
        "kill_reason": res.kill_reason,
        "duration_sec": res.duration_sec,
        "limits": limits_snapshot,
        "target": target_eff,
    }
    return res.code, combined_out, target_eff, meta

# =====================================================
# Повтор запуска из истории (replay)
# =====================================================
def replay_command(id_token: str | None):
    # !! — последняя
    if id_token in (None, "", "!!"):
        rows = recent(1)
        if not rows:
            print("[yellow]История пуста — нечего повторять.[/yellow]")
            return
        parent_id = rows[0]["id"]
    else:
        tok = id_token.strip()
        if tok.startswith("!"):
            tok = tok[1:]
        try:
            parent_id = int(tok)
        except ValueError:
            print("[red]Укажи корректный ID: replay <id> или !<id> или !![/red]")
            return

    row = get_command(parent_id)
    if not row:
        print(f"[yellow]Запись #{parent_id} не найдена.[/yellow]")
        return

    original_user = row.get("user_input") or ""
    original_cmd = row.get("plan_cmd") or ""
    original_expl = row.get("explanation") or ""

    if not original_cmd.strip():
        print(f"[yellow]У записи #{parent_id} нет сохранённой команды — нечего повторять.[/yellow]")
        return

    corrected_cmd = clean_command(correct_command_for_os(original_cmd))

    risk = assess_risk(corrected_cmd)
    if risk != "dangerous" and is_quick_danger(corrected_cmd):
        risk = "dangerous"
    if risk == "read_only" and is_write_like(corrected_cmd):
        risk = "mutating"

    print(Panel.fit(
        f"🔁 Повтор команды из истории #{parent_id}\n\n"
        f"[bold]Оригинальный запрос:[/bold] {original_user}\n"
        f"[bold]Команда:[/bold] [yellow]{corrected_cmd}[/yellow]\n"
        f"[bold]Пояснение:[/bold] {original_expl or '(нет)'}\n"
        f"[bold]Риск сейчас:[/bold] {RISK_LABEL[risk]}",
        border_style="blue", padding=(1,2))
    )

    planned_target = "host" if risk in ("read_only", "mutating") else ("dry" if risk == "dangerous" else "host")
    command_id = create_command_event(
        user_input=f"[replay #{parent_id}] {original_user}",
        plan_cmd=corrected_cmd,
        explanation=f"(REPLAY of #{parent_id}) {original_expl}",
        risk=_risk_to_db(risk),
        exec_target=planned_target,
        timeout_sec=None,
        workflow_id=f"replay:{parent_id}",
        host_alias=None,
        sandbox=(planned_target == "docker"),
    )

    t0 = time.perf_counter()
    code, out, actual_target, meta = execute_command(corrected_cmd, risk)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    try:
        preview = (out or "")[:4096]
        add_artifact(command_id, "stdout", preview=preview)
    except Exception:
        pass

    try:
        add_artifact(command_id, "meta", preview=json.dumps(meta, ensure_ascii=False, indent=2)[:4000])
    except Exception:
        pass

    finalize_command_event(
        command_id=command_id,
        exit_code=code,
        bytes_stdout=len((out or "").encode("utf-8")),
        bytes_stderr=0,
        duration_ms=duration_ms,
        error=None if code == 0 else "nonzero or cancelled",
        exec_target_final=actual_target,
    )

    if code == 0:
        body = (out or "").strip()
        if not body and is_write_like(corrected_cmd):
            body = ("✅ Команда выполнена. Похоже, был вывод в файл/изменение ФС.\n"
                    "Например, попробуй: [bold]ls -la[/bold]")
        print(Panel.fit(f"📤 Результат:\n\n{body}", border_style="green", padding=(1,2)))
    else:
        print(Panel.fit(f"⚠️ Ошибка/сообщение:\n\n{(out or '').strip()}", border_style="red", padding=(1,2)))

# =====================================================
# GhostCoach helpers
# =====================================================
def _coach_is_alive(port: int = 8765) -> bool:
    try:
        import urllib.request, json
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as r:
            if r.status == 200:
                data = json.loads(r.read().decode("utf-8"))
                return bool(data.get("ok"))
    except Exception:
        return False
    return False

def start_ghostcoach(open_ui: bool = True, port: int = 8765):
    """Стартует демон ghostcoach, если он не запущен. По желанию открывает UI."""
    if _coach_is_alive(port):
        print("[dim]GhostCoach уже запущен на http://127.0.0.1:%d[/dim]" % port)
    else:
        try:
            import subprocess, sys, webbrowser, time as _t
            # Запускаем в фоне: python -m ghostcoach.daemon
            p = subprocess.Popen([sys.executable, "-m", "ghostcoach.daemon"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Немного подождём и проверим healthz
            for _ in range(12):
                if _coach_is_alive(port):
                    break
                _t.sleep(0.25)
            if not _coach_is_alive(port):
                print("[red]Не удалось запустить GhostCoach (демон не ответил).[/red]")
                return
            print("[green]GhostCoach запущен.[/green]  → http://127.0.0.1:%d/ui.html" % port)
        except Exception as e:
            print(f"[red]Ошибка запуска GhostCoach: {e}[/red]")
            return
    if open_ui:
        try:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}/ui.html", new=2)
        except Exception:
            pass

def coach_status(port: int = 8765):
    alive = _coach_is_alive(port)
    print("[green]RUNNING[/green]" if alive else "[red]STOPPED[/red]", f"http://127.0.0.1:{port}/ui.html")

def stop_ghostcoach(port: int = 8765):
    # Пытаемся найти процесс, слушающий порт, и завершить.
    try:
        import socket
        s = socket.socket()
        try:
            s.connect(("127.0.0.1", port))
            s.close()
        except Exception:
            pass
    except Exception:
        pass
    # Лучший кросс-платформенный способ без зависимостей — через lsof (macOS) / fuser (linux).
    import subprocess, sys, os
    try:
        # macOS/bsd: lsof -t -iTCP:8765 -sTCP:LISTEN
        pid = subprocess.check_output(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], text=True).strip()
        if pid:
            os.kill(int(pid.splitlines()[0]), 15)
            print("[yellow]GhostCoach остановлен[/yellow]")
            return
    except Exception:
        pass
    print("[dim]Не нашёл запущенный GhostCoach на порту %d[/dim]" % port)


# =====================================================
# Главный цикл
# =====================================================
def main():
    global LAST_AUTOGEN_PATH
    init_db()
    print("[bold green]👻 GhostCMD запущен. Жду команду...[/bold green]")
    print("[dim]Подсказка: набери [bold]help[/bold] для списка встроенных команд (history, logs, show, replay)[/dim]")

    while True:
        if RUN_QUEUE:
                try:
                    while not RUN_QUEUE.empty():
                        auto_cmd = RUN_QUEUE.get_nowait()
                        print(f"\n[GhostCoach → RUN] {auto_cmd}")
                        user_input = auto_cmd
                        break  # выполняем одну команду за раз
                except Exception:
                    pass
        user_input = Prompt.ask("[bold]>[/bold]")
        if not user_input.strip():
            continue

        # workflow preview/run
        if handle_flow_preview(user_input):
            continue
        if handle_rerun_failed(user_input):
            continue
        if handle_rerun_from_name(user_input):
            continue
        if handle_rerun_changed(user_input):
            continue
        if handle_flow_run(user_input):
            continue
        if handle_lintflow(user_input):
            continue
        if handle_ci_auth(user_input):
            continue
        if handle_ci_init(user_input):
            continue
        if handle_ci_manage(user_input):
            continue

        # --- Перехват естественных фраз (алиасы) ---
        intent = intercept_builtin_intent(user_input)
        if intent:
            name, params = intent

            if name == "history":
                print_history(params.get("limit", 10))
                continue

            if name == "logs":
                print_logs(str(params.get("count", 20)))
                continue

            if name == "show":
                print_show(str(params["id"]))
                continue

            if name == "replay":
                replay_command("!!" if params.get("last") else str(params["id"]))
                continue
            if name == "coach":
                action = (params.get("action") or "start")
                if action == "status":
                    coach_status()
                elif action == "stop":
                    stop_ghostcoach()
                else:
                    start_ghostcoach(open_ui=(action in ("start", "open")))
                continue


            if name == "plan_status":
                print_plan_status()
                continue
            if name == "runflow_last":
                if not LAST_AUTOGEN_PATH:
                    print(Panel.fit(
                        "План ещё не сохранён в этом сеансе.\n"
                        "Сначала опиши действия (я сгенерирую план) или укажи файл: runflow flows/<file>.yml",
                        border_style="red"))
                    continue
                try:
                    wf = load_workflow(LAST_AUTOGEN_PATH)
                    _ = run_workflow(wf, execute_step_cb=execute_step_cb, ask_confirm=True)
                    print_plan_status()
                except Exception as e:
                    print(Panel.fit(f"Не удалось запустить workflow: {e}", border_style="red"))
                continue

            if name == "runflow_from":
                start = int(params.get("start", 1))
                if not LAST_AUTOGEN_PATH:
                    print(Panel.fit(
                        "План ещё не сохранён в этом сеансе.\n\n"
                        "Сначала сгенерируй его (например: 'Скачай пакеты, ...') "
                        "или укажи файл явно: runflow flows/<file>.yml [--from N]",
                        border_style="red"))
                    continue
                try:
                    wf = load_workflow(LAST_AUTOGEN_PATH)
                    total = len(wf.steps)
                    if start > total:
                        print(f"[workflow] В workflow всего {total} шаг(ов); нельзя начать с {start}.")
                        continue
                    wf = WorkflowSpec(
                        name=f"{wf.name} (from {start})",
                        steps=wf.steps[start-1:],
                        env=getattr(wf, "env", {}),
                        secrets_from=getattr(wf, "secrets_from", None),
                        source_path=getattr(wf, "source_path", None),
                        source_sha256=getattr(wf, "source_sha256", None),
                        )
                    _ = run_workflow(wf, execute_step_cb=execute_step_cb, ask_confirm=True)
                    print_plan_status()
                except Exception as e:
                    print(Panel.fit(f"Не удалось запустить workflow: {e}", border_style="red"))
                continue

            if name == "nl_edit_ops":
                ops = params.get("ops") or []
                if not LAST_AUTOGEN_PATH:
                    print(Panel.fit(
                        "Нет автоген-плана для редактирования.\n"
                        "Сначала сгенерируй план или укажи файл и измени его вручную.",
                        border_style="red"))
                    continue
                try:
                    from pathlib import Path
                    p = Path(LAST_AUTOGEN_PATH)

                    # грузим YAML с сохранением форматирования/комментов
                    data, _old_text = load_yaml_preserve(str(p))

                    # применяем операции (set_run/target/timeout/if/env/needs/insert/delete/rename/move и т.д.)
                    msgs = apply_ops(data, ops)
                    if msgs:
                        print(Panel.fit(
                            "Сообщения редактора:\n" + "\n".join(f"• {m}" for m in msgs),
                            border_style="grey50", padding=(1,2)
                        ))

                    # показываем diff, спрашиваем подтверждение и сохраняем атомарно с бэкапом
                    saved, backup = preview_and_write_yaml(str(p), data)
                    if saved:
                        print(Panel.fit(
                            f"✅ Обновлён: {p.name}\n" + (f"[dim]backup: {backup}[/dim]" if backup else ""),
                            border_style="green"))
                        LAST_AUTOGEN_PATH = str(p)
                except Exception as e:
                    print(Panel.fit(f"Ошибка редактирования: {e}", border_style="red"))
                continue

            if name == "gen_ci_from_nl":
                kind = (params.get("kind") or "").strip().lower()
                features = params.get("features") or ""
                if kind == "докер":
                    kind = "docker"

                try:
                    import pathlib, time
                    tmpl_path = pathlib.Path(f"core/templates/ci_{kind}.yml")
                    if not tmpl_path.exists():
                        print(Panel.fit(f"❌ Нет шаблона для {kind}", border_style="red"))
                        continue

                    # Загружаем шаблон
                    data, _tmpl_text = load_yaml_preserve(str(tmpl_path))

                    # Строим ops из фич и применяем
                    ops = build_ops_from_nl(kind, features)
                    try:
                        from rich.console import Console
                        import json
                        console = Console()
                        # Покажем список шагов в текущем файле
                        step_names = []
                        if isinstance(data, dict):
                            for s in (data.get("steps") or []):
                                try:
                                    step_names.append(str(s.get("name")))
                                except Exception:
                                    pass
                        console.print(Panel.fit(
                            "DEBUG\n"
                            f"steps: {step_names}\n"
                            f"ops:\n{json.dumps(ops, ensure_ascii=False, indent=2)}",
                            border_style="magenta"
                        ))
                    except Exception:
                        pass
                    if ops:
                        msgs = apply_ops(data, ops)
                        if msgs:
                            print(Panel.fit(
                                "Сообщения редактора:\n" + "\n".join(f"• {m}" for m in msgs),
                                border_style="grey50", padding=(1,2)
                            ))

                    # Сохраняем в новый autogen-файл
                    out_name = f"flows/autogen_ci_{kind}_{time.strftime('%Y%m%d_%H%M%S')}.yml"
                    saved, backup = preview_and_write_yaml(out_name, data)
                    if saved:
                        LAST_AUTOGEN_PATH = out_name
                        print(Panel.fit(
                            f"✅ Сохранено: {out_name}\n"
                            + (f"[dim]backup: {backup}[/dim]\n" if backup else "")
                            + "\nТеперь вы можете:\n"
                            f"• Изменять этот план естественными командами (например: измени шаг 2 на: pytest -q)\n"
                            f"• Вернуться к редактированию позже: flow {out_name}\n"
                            f"• Запустить план: runflow {out_name}",
                            border_style="green", padding=(1,2)
                        ))
                except Exception as e:
                    _safe_print_error(f"Ошибка генерации CI: {e}")
                continue
            # 🔎 Отладка: печатаем какой интент распознан
            try:
                print(Panel.fit(f"DEBUG INTENT: {name} | params={params}", border_style="magenta"))
            except Exception:
                pass

            if name == "gen_ci":
                kind = params.get("kind")
                import pathlib, shutil, time

                tmpl_path = pathlib.Path(f"core/templates/ci_{kind}.yml")
                if not tmpl_path.exists():
                    print(Panel.fit(f"❌ Нет шаблона для {kind}", border_style="red"))
                    continue

                out_name = f"flows/autogen_ci_{kind}_{time.strftime('%Y%m%d_%H%M%S')}.yml"
                out_path = pathlib.Path(out_name)

                # читаем шаблон
                tmpl_text = tmpl_path.read_text()

                # если файл уже был — diff покажет изменения
                old_text = "" if not out_path.exists() else out_path.read_text()

                # diff-превью
                import difflib
                diff = "\n".join(difflib.unified_diff(
                    old_text.splitlines(), tmpl_text.splitlines(),
                    fromfile=str(out_path),
                    tofile=str(out_path) + " (new)",
                    lineterm=""
                ))

                if diff.strip():
                    print(Panel(diff, title=f"DIFF • {out_path.name}", border_style="cyan", padding=(1,2)))
                else:
                    print(Panel.fit("⚠️ Изменений нет (файл уже совпадает с шаблоном)", border_style="yellow"))

                # подтверждение
                if Confirm.ask("Сохранить шаблон?", default=True):
                    out_path.write_text(tmpl_text)
                    print(Panel.fit(f"✅ Сохранено: {out_path}", border_style="green"))
                    LAST_AUTOGEN_PATH = str(out_path)

                    # NEW: подсказка пользователю
                    print(Panel.fit(
                        f"Теперь вы можете:\n"
                        f"• Изменять этот шаблон естественными командами (например: измени шаг 2 на: pytest -q)\n"
                        f"• Вернуться к редактированию позже: flow {out_path}\n"
                        f"• Запустить план: runflow {out_path}\n",
                        border_style="cyan", padding=(1,2)
                    ))
                continue

            if name == "gen_ci_from_nl":
                kind = params.get("kind")
                features = (params.get("features") or "").strip()
                import pathlib, time, difflib

                tmpl_path = pathlib.Path(f"core/templates/ci_{kind}.yml")
                if not tmpl_path.exists():
                    print(Panel.fit(f"❌ Нет шаблона для {kind}", border_style="red"))
                    continue

                out_name = f"flows/autogen_ci_{kind}_{time.strftime('%Y%m%d_%H%M%S')}.yml"
                out_path = pathlib.Path(out_name)

                # 1) Сохраняем базовый шаблон (как в gen_ci)
                tmpl_text = tmpl_path.read_text()
                old_text = "" if not out_path.exists() else out_path.read_text()
                diff = "\n".join(difflib.unified_diff(
                    old_text.splitlines(), tmpl_text.splitlines(),
                    fromfile=str(out_path), tofile=str(out_path) + " (new)", lineterm=""
                ))
                print(Panel(diff or "⚠️ Изменений нет (файл уже совпадает с шаблоном)",
                            title=f"DIFF • {out_path.name}", border_style="cyan", padding=(1,2)))
                if not Confirm.ask("Сохранить шаблон?", default=True):
                    print(Panel.fit("❌ Отменено. Файл не изменён.", border_style="red"))
                    continue
                out_path.write_text(tmpl_text)
                print(Panel.fit(f"✅ Сохранено: {out_path}", border_style="green"))
                LAST_AUTOGEN_PATH = str(out_path)

                # 2) Строим ops из фич и применяем
                ops = build_ops_from_nl(kind, features)
                try:
                    from rich.syntax import Syntax
                    ops_json = json.dumps(ops, ensure_ascii=False, indent=2)
                    print(Panel(Syntax(ops_json, "json"), title="DEBUG • ops", border_style="green"))
                except Exception:
                    print(Panel.fit(f"DEBUG ops: {ops}", border_style="green"))

                data, used_yaml = load_yaml_preserve(out_path)
                msgs = apply_ops(data, ops)
                new_text = dump_yaml_preserve(data, used_yaml)

                diff2 = "\n".join(difflib.unified_diff(
                    tmpl_text.splitlines(), new_text.splitlines(),
                    fromfile=str(out_path) + " (before ops)", tofile=str(out_path) + " (after ops)", lineterm=""
                ))
                print(Panel(diff2 or "⚠️ Изменений нет после ops",
                            title=f"DIFF after ops • {out_path.name}", border_style="cyan", padding=(1,2)))
                if msgs:
                    print(Panel("\n".join(msgs), title="apply_ops messages", border_style="yellow"))

                if Confirm.ask("Сохранить изменения после ops?", default=True):
                    out_path.write_text(new_text)
                    print(Panel.fit(f"✅ Изменения сохранены: {out_path}", border_style="green"))
                else:
                    print(Panel.fit("❌ Отменено. Файл остался как в шаблоне.", border_style="red"))

                print(Panel.fit(
                    f"Теперь можно:\n"
                    f"• Естественным языком править: измени шаг 2 на: pytest -q\n"
                    f"• Открыть снова: flow {out_path}\n"
                    f"• Запустить: runflow {out_path}\n",
                    border_style="cyan", padding=(1,2)
                ))
                continue



            if name == "edit_step":
                idx = int(params["index"]); cmd = str(params["cmd"])
                if not LAST_AUTOGEN_PATH:
                    print(Panel.fit(
                        "Нет автоген-плана для редактирования.\n"
                        "Сначала сгенерируй план или укажи файл и измени его вручную.",
                        border_style="red"))
                    continue
                try:
                    from pathlib import Path
                    p = Path(LAST_AUTOGEN_PATH)

                    # 1) грузим YAML с сохранением форматирования/комментов
                    data, _old_text = load_yaml_preserve(str(p))
                    steps = (data.get("steps") or []) if isinstance(data, dict) else []
                    if not (isinstance(steps, list) and steps):
                        print(Panel.fit(f"В файле {p.name} отсутствует корректный список steps.", border_style="red"))
                        continue

                    if not (1 <= idx <= len(steps)):
                        print(Panel.fit(f"В файле {p.name} нет шага #{idx}. Всего шагов: {len(steps)}", border_style="red"))
                        continue

                    # 2) меняем только run у нужного шага
                    step_map = steps[idx-1]
                    try:
                        # ruamel: CommentedMap поддерживает обычную индексацию
                        step_map["run"] = cmd
                    except Exception as e:
                        print(Panel.fit(f"Не удалось обновить поле run у шага #{idx}: {e}", border_style="red"))
                        continue

                    # 3) показываем diff и сохраняем атомарно с бэкапом
                    saved, backup = preview_and_write_yaml(str(p), data)
                    if saved:
                        print(Panel.fit(
                            f"✅ Шаг #{idx} обновлён в {p.name}\n"
                            + (f"[dim]backup: {backup}[/dim]" if backup else ""),
                            border_style="green"))
                        LAST_AUTOGEN_PATH = str(p)
                except Exception as e:
                    print(Panel.fit(f"Ошибка редактирования: {e}", border_style="red"))
                continue
                try:
                    from pathlib import Path
                    p = Path(LAST_AUTOGEN_PATH)
                    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                    steps = data.get("steps") or []
                    if not (1 <= idx <= len(steps)):
                        print(Panel.fit(f"В файле {p.name} нет шага #{idx}. Всего шагов: {len(steps)}", border_style="red"))
                        continue
                    steps[idx-1]["run"] = cmd
                    data["steps"] = steps
                    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
                    print(Panel.fit(f"✅ Шаг #{idx} обновлён в {p}", border_style="green"))
                    LAST_AUTOGEN_PATH = str(p)
                except Exception as e:
                    print(Panel.fit(f"Ошибка редактирования: {e}", border_style="red"))
                continue

            if name == "edit_step_by_name":
                step_name = str(params["name"]); cmd = str(params["cmd"])
                if not LAST_AUTOGEN_PATH:
                    print(Panel.fit("Нет автоген-плана для редактирования.", border_style="red"))
                    continue
                try:
                    from pathlib import Path
                    p = Path(LAST_AUTOGEN_PATH)

                    data, _old_text = load_yaml_preserve(str(p))
                    if not isinstance(data, dict):
                        print(Panel.fit(f"{p.name} не является корректным YAML-объектом.", border_style="red"))
                        continue

                    steps = data.get("steps") or []
                    if not isinstance(steps, list) or not steps:
                        print(Panel.fit(f"В файле {p.name} отсутствует корректный список steps.", border_style="red"))
                        continue

                    found = False
                    for s in steps:
                        try:
                            if str(s.get("name")) == step_name:
                                s["run"] = cmd
                                found = True
                                break
                        except Exception:
                            # если шаг не dict/CommentedMap — просто пропустим
                            pass

                    if not found:
                        print(Panel.fit(f"В файле {p.name} нет шага с именем '{step_name}'.", border_style="red"))
                        continue

                    saved, backup = preview_and_write_yaml(str(p), data)
                    if saved:
                        print(Panel.fit(
                            f"✅ Шаг '{step_name}' обновлён в {p.name}\n"
                            + (f"[dim]backup: {backup}[/dim]" if backup else ""),
                            border_style="green"))
                        LAST_AUTOGEN_PATH = str(p)
                except Exception as e:
                    print(Panel.fit(f"Ошибка редактирования: {e}", border_style="red"))
                continue

        # --- Встроенные короткие команды ---
        low = user_input.strip().lower()
        parts = user_input.strip().split()

        if low in ("help", "?"):
            print_help()
            continue

        if parts[0].lower() in ("history", "h"):
            lim = 10
            if len(parts) > 1:
                try:
                    lim = max(1, min(200, int(parts[1])))
                except ValueError:
                    pass
            print_history(lim)
            continue

        if parts[0].lower() == "logs":
            count = parts[1] if len(parts) > 1 else None
            print_logs(count)
            continue

        if parts[0].lower() == "show" and len(parts) > 1:
            print_show(parts[1])
            continue

        if parts[0].lower() == "replay":
            replay_command(parts[1] if len(parts) > 1 else None)
            continue

        if parts[0].lower() == "plan":
            print_plan_status()
            continue
        if parts[0].lower() == "config":
            print_config_status()
            continue
        if parts[0].lower() == "overlay":
            import subprocess, os, signal

            pid_file = os.path.expanduser("~/.ghostcmd/overlay.pid")
            os.makedirs(os.path.dirname(pid_file), exist_ok=True)

            def is_process_alive(pid: int) -> bool:
                try:
                    os.kill(pid, 0)  # не убивает, просто проверяет
                    return True
                except OSError:
                    return False

            if os.path.exists(pid_file):
                try:
                    with open(pid_file) as f:
                        pid = int(f.read().strip())
                except Exception:
                    pid = None

                if pid and is_process_alive(pid):
                    # 🔻 Overlay работает → выключаем
                    try:
                        os.kill(pid, signal.SIGTERM)
                        print("👻 GhostOverlay остановлен")
                    except Exception as e:
                        print(f"⚠️ Не удалось остановить Overlay: {e}")
                else:
                    print("ℹ️ GhostOverlay уже не работает, перезапускаю...")

                try:
                    os.remove(pid_file)
                except FileNotFoundError:
                    pass

                # 🔺 Запускаем новый процесс
                try:
                    proc = subprocess.Popen(
                        ["ghost-overlay"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        preexec_fn=os.setpgrp  # отвязать процесс от GhostCMD
                    )
                    with open(pid_file, "w") as f:
                        f.write(str(proc.pid))
                    print("👻 GhostOverlay запущен")
                except Exception as e:
                    print(f"⚠️ Ошибка запуска Overlay: {e}")

            else:
                # 🔺 Overlay не запущен → включаем
                try:
                    proc = subprocess.Popen(
                        ["ghost-overlay"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        preexec_fn=os.setpgrp
                    )
                    with open(pid_file, "w") as f:
                        f.write(str(proc.pid))
                    print("👻 GhostOverlay запущен (закрыть: повтори 'overlay' или крестик в HUD)")
                except Exception as e:
                    print(f"⚠️ Ошибка запуска Overlay: {e}")
            continue


        if low.startswith("!"):  # !<id> или !!
            replay_command(low)
            continue

        

        # === 1) NLU → bash ===
        result = process_prompt(user_input)
                # --- NLU может вернуть многошаговый план ---
        if (result.get("mode") == "workflow") and (result.get("workflow") or {}).get("steps"):
            wf_data = result["workflow"]
            wf_name = wf_data.get("name") or f"NLU plan"
            wf_env  = wf_data.get("env") or {}
            steps_in = wf_data.get("steps") or []

            step_specs = []
            for i, s in enumerate(steps_in, start=1):
                name    = s.get("name") or f"step_{i}"
                run     = s.get("run") or "echo (пустой шаг)"
                target  = (s.get("target") or "auto").lower().strip()
                target  = target if target in ("auto","host","docker") else "auto"
                timeout = s.get("timeout") if isinstance(s.get("timeout"), int) else None
                env     = s.get("env") or {}
                cwd     = s.get("cwd")

                step_specs.append(StepSpec(
                    name=name,
                    run=run,
                    target=Target(target),
                    timeout=timeout,
                    env=env,
                    cwd=cwd or None,
                    if_expr=s.get("if") or None,
                    continue_on_error=bool(s.get("continue_on_error", False)),
                    retries=s.get("retries") or {},
                ))
                        # --- Автоподстановка Dockerfile при отсутствии ---
            adjusted_steps = []
            for original_s, s_in in zip(step_specs, steps_in):
                adjusted_steps.append(original_s)

                run_lower = (s_in.get("run") or "").lower()
                has_docker_build = ("docker build" in run_lower) and (" -f " not in run_lower)
                if not has_docker_build:
                    continue

                # Определяем целевой каталог для этого шага (где будет искаться Dockerfile)
                import os as _os
                cwd_for_step = original_s.cwd or _os.getcwd()
                dockerfile_path = _os.path.join(cwd_for_step, "Dockerfile")

                if _os.path.exists(dockerfile_path):
                    continue  # Dockerfile уже есть — ничего не делаем

                # Вставляем шаг ensure_dockerfile ПЕРЕД сборкой
                dockerfile_cmd = r"""if [ ! -f Dockerfile ]; then
cat > Dockerfile <<'EOF'
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir Flask
COPY . .
CMD ["python","-c","import flask,sys; sys.stdout.write('flask ok\\n')"]
EOF
echo "✅ Dockerfile создан"
else
echo "ℹ️ Dockerfile уже существует — пропускаю создание"
fi
"""
                ensure_step = StepSpec(
                    name=f"ensure_dockerfile_for_{original_s.name}",
                    run=dockerfile_cmd,
                    target=Target.HOST,
                    timeout=60,
                    env={},
                    cwd=cwd_for_step,
                    continue_on_error=True
                )
                # Вставляем перед сборкой:
                adjusted_steps[-1] = ensure_step
                adjusted_steps.append(original_s)

            step_specs = adjusted_steps


            wf_spec = WorkflowSpec(name=wf_name, steps=step_specs, env=wf_env, secrets_from=None)
                        # --- Автосохранение плана в flows/autogen_<timestamp>.yml ---
            # --- Автосохранение плана в flows/autogen_<timestamp>.yml ---
            from pathlib import Path
            import time as _t
            import os as _os

            flows_dir = Path("flows")
            flows_dir.mkdir(parents=True, exist_ok=True)
            ts = _t.strftime("%Y%m%d_%H%M%S")
            autopath = flows_dir / f"autogen_{ts}.yml"
            tmp_path = autopath.with_suffix(".yml.tmp")

            def _step_to_yaml_dict(s: StepSpec) -> dict:
                d = {
                    "name": s.name,
                    "run": s.run,
                    "target": s.target.value,
                }
                if s.cwd: d["cwd"] = s.cwd
                if s.timeout: d["timeout"] = s.timeout
                if s.env: d["env"] = s.env
                if getattr(s, "if_expr", None): d["if"] = s.if_expr
                if getattr(s, "continue_on_error", False): d["continue_on_error"] = True
                if getattr(s, "retries", None):
                    r = s.retries or {}
                    if r: d["retries"] = r
                return d

            yaml_obj = {
                "name": wf_spec.name,
                "env": wf_spec.env or {},
                "steps": [_step_to_yaml_dict(s) for s in step_specs],
            }

            # Пишем во временный файл, затем атомарно переименовываем → не оставим пустышку при падении
            with tmp_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(yaml_obj, f, sort_keys=False, allow_unicode=True)
            _os.replace(tmp_path, autopath)  # атомарная замена на большинстве ОС

            LAST_AUTOGEN_PATH = str(autopath)
            print(Panel.fit(f"📝 План сохранён: {autopath}", border_style="grey50", padding=(1,2)))
            print_plan_status()



            print(Panel.fit("🧠 Обнаружен план из нескольких шагов — запускаю workflow",
                            border_style="cyan", padding=(1,2)))

            # 1) Покажем превью шагов (раньше это делал run_workflow с ask_confirm=True)
            try:
                preview_workflow(wf_spec)
            except Exception:
                pass

            # 2) Предоценка рисков + сводка
            summary = []
            for s in step_specs:
                risk, target_suggest = _classify_step_risk(s.run)
                summary.append({
                    "name": s.name,
                    "risk": risk,
                    "target_suggest": "host" if (s.target.value == "host") else ("docker" if (s.target.value == "docker") else target_suggest)
                })
            cnt = _print_risk_summary(wf_spec.name, summary)

            # 3) Если есть dangerous — спросим, где их запускать
            danger_indices = [i for i, s in enumerate(summary) if s["risk"].startswith("dangerous")]
            if danger_indices:
                for i in danger_indices:
                    s = step_specs[i]
                    must_host = _looks_host_only(s.run)

                    def _normalize_choice(x: str) -> str | None:
                        x = (x or "").strip().lower()
                        mapping = {
                            "d": "docker", "docker": "docker", "д": "docker", "докер": "docker",
                            "h": "host", "host": "host", "х": "host", "хост": "host",
                            "s": "skip", "skip": "skip", "пропусти": "skip", "пропустить": "skip",
                            "c": "cancel", "cancel": "cancel", "с": "cancel", "стоп": "cancel", "отмена": "cancel",
                        }
                        return mapping.get(x)

                    default = "h" if must_host else "d"
                    choice = Prompt.ask(
                        f"Шаг {i+1} '{s.name}' опасный. Где выполнить? "
                        "([bold]d[/bold]=Docker, [bold]h[/bold]=Host, [bold]s[/bold]=Пропустить, [bold]c[/bold]=Отмена всего)",
                        default=default
                    )
                    where = _normalize_choice(choice)

                    if where == "cancel":
                        print(Panel.fit("❌ Отменено пользователем.", border_style="red"))
                        return  # выходим из main-loop → workflow не пойдёт

                    if where == "skip":
                        # Пропускаем шаг — ставим continue_on_error и заменяем run на echo
                        s.run = f"echo '⏭️ Шаг {s.name} пропущен пользователем'"
                        s.continue_on_error = True
                        s.target = Target.HOST  # без разницы
                        continue

                    if where == "host":
                        if is_destructive_on_host(s.run):
                            print(Panel.fit("⛔ Команда слишком разрушительна для хоста. Автоматически переведена в Docker.", border_style="red"))
                            s.target = Target.DOCKER
                        else:
                            s.target = Target.HOST
                    else:
                        s.target = Target.DOCKER

            # 4) Финальное подтверждение одного кликом
            proceed = Confirm.ask(
                f"Обнаружены: {cnt['dangerous']} dangerous, {cnt['mutating']} mutating. Выполнить все шаги?",
                default=(cnt["dangerous"] == 0)  # по умолчанию y только если нет dangerous
            )
            if not proceed:
                print(Panel.fit("❌ Отменено пользователем.", border_style="red"))
                continue

            for s in wf_spec.steps:
                s.continue_on_error = True

            # 5) Запуск без повторного вопроса (мы уже спросили)
            _ = run_workflow(
                wf_spec,
                execute_step_cb=execute_step_cb,
                ask_confirm=False,
            )
            print_plan_status()
            continue


            # Покажем превью и спросим подтверждение внутри run_workflow (ask_confirm=True)
            _ = run_workflow(
                wf_spec,
                execute_step_cb=execute_step_cb,
                ask_confirm=True,
            )
            print_plan_status()
            # После выполнения workflow возвращаемся в цикл, одиночную команду уже не исполняем
            continue

        bash_cmd = result["bash_command"]
        explanation = result["explanation"]

        # === 2) OS-правки + очистка ===
        corrected_cmd = clean_command(correct_command_for_os(bash_cmd))

        # === 3) Оценка риска + страховки ===
        risk = assess_risk(corrected_cmd)
        if risk != "dangerous" and is_quick_danger(corrected_cmd):
            risk = "dangerous"
        if risk == "read_only" and is_write_like(corrected_cmd):
            risk = "mutating"

        # === 4) Вывод превью ===
        print(f"\n[bold cyan]🧠 Предложенная команда:[/bold cyan] [yellow]{corrected_cmd}[/yellow]")
        print(f"[bold cyan]📘 Объяснение:[/bold cyan] {explanation}")
        print(f"[bold magenta]🔒 Уровень риска:[/bold magenta] {RISK_LABEL[risk]}\n")

        # === 5) История: черновик записи ===
        planned_target = "host" if risk in ("read_only", "mutating") else ("dry" if risk == "dangerous" else "host")
        command_id = create_command_event(
            user_input=user_input,
            plan_cmd=corrected_cmd,
            explanation=explanation,
            risk=_risk_to_db(risk),
            exec_target=planned_target,
            timeout_sec=None,
            workflow_id=None,
            host_alias=None,
            sandbox=(planned_target == "docker"),
        )

        # === 6) Исполнение ===
        t0 = _time.perf_counter()
        code, out, actual_target, meta = execute_command(corrected_cmd, risk)
        duration_ms = int((_time.perf_counter() - t0) * 1000)

        # 7) Артефакты + финализация
        try:
            add_artifact(command_id, "stdout", preview=(out or "")[:4096])
        except Exception as e:
            print(Panel.fit(f"⚠️ Не удалось сохранить STDOUT-артефакт: {e}", border_style="red"))

        try:
            add_artifact(
                command_id,
                "json",
                path="meta.json",
                preview=_json.dumps(meta, ensure_ascii=False, indent=2)[:4000],
            )
        except Exception as e:
            print(Panel.fit(f"⚠️ Не удалось сохранить META-артефакт: {e}", border_style="red"))

        finalize_command_event(
            command_id=command_id,
            exit_code=code,
            bytes_stdout=len((out or "").encode("utf-8")),
            bytes_stderr=0,  # stdout+stderr склеены
            duration_ms=duration_ms,
            error=None if code == 0 else "nonzero or cancelled",
            exec_target_final=actual_target,
        )

        # 7.4 Показать META в консоли ТОЛЬКО если сработали лимиты
        try:
            if str(meta.get("kill_reason")) in ("timeout", "memory_exceeded"):
                meta_preview = json.dumps(meta, ensure_ascii=False, indent=2)[:1000]
                print(Panel.fit(meta_preview, title="META", border_style="white", padding=(1,2)))
        except Exception:
            pass

        # === 8) Итог пользователю ===
        if code == 0:
            body = (out or "").strip()
            if not body and is_write_like(corrected_cmd):
                body = ("✅ Команда выполнена. Похоже, был вывод в файл/изменение ФС.\n"
                        "Например, попробуй: [bold]ls -la[/bold]")
            print(Panel.fit(f"📤 Результат:\n\n{body}", border_style="green", padding=(1,2)))
        else:
            print(Panel.fit(f"⚠️ Ошибка/сообщение:\n\n{(out or '').strip()}", border_style="red", padding=(1,2)))




def cli_entry():
    import sys, os, subprocess, signal

    if len(sys.argv) > 1 and sys.argv[1] == "overlay":
        # --- CLI Overlay toggle ---
        pid_file = os.path.expanduser("~/.ghostcmd/overlay.pid")
        if os.path.exists(pid_file):
            try:
                with open(pid_file) as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGTERM)
                os.remove(pid_file)
                print("👻 GhostOverlay остановлен (через ghost overlay)")
            except Exception:
                print("⚠️ Не удалось остановить Overlay, пробую заново...")
                try: os.remove(pid_file)
                except: pass
        else:
            try:
                os.makedirs(os.path.dirname(pid_file), exist_ok=True)
                proc = subprocess.Popen(
                    ["ghost-overlay"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setpgrp
                )
                with open(pid_file, "w") as f:
                    f.write(str(proc.pid))
                print("👻 GhostOverlay запущен (через ghost overlay)")
            except Exception as e:
                print(f"⚠️ Ошибка запуска Overlay: {e}")
        return

    elif len(sys.argv) > 1 and sys.argv[1] == "limits":
        try:
            for risk in ("read_only", "mutating", "dangerous"):
                lim = load_limits_for_risk(risk)
                print(f"[{risk}] timeout={lim.timeout_sec}s, grace={lim.grace_kill_sec}s, "
                      f"cpus={lim.cpus}, mem={lim.memory_mb}MB, pids={lim.pids}, net_off={lim.no_network}")
        except Exception as e:
            print("Ошибка чтения лимитов:", e)
        return

    elif len(sys.argv) > 1 and sys.argv[1] == "run":
        if len(sys.argv) < 4:
            print("Использование: ghost run <read_only|mutating|dangerous> <команда>")
            return
        risk = sys.argv[2]
        cmd = " ".join(sys.argv[3:])
        L = load_limits_for_risk(risk)
        res = run_on_host_with_limits(
            cmd,
            timeout_sec=L.timeout_sec,
            grace_kill_sec=L.grace_kill_sec,
            mem_watch_mb=1024,
        )
        print(f"📤 Код выхода: {res.code}")
        print(f"⏱ Длительность: {res.duration_sec}s")
        if res.killed:
            print(f"🧯 Прервано по причине: {res.kill_reason}")
        if res.stdout.strip():
            print("—— STDOUT ——")
            print(res.stdout)
        if res.stderr.strip():
            print("—— STDERR ——")
            print(res.stderr)
        return

    else:
        main()


if __name__ == "__main__":
    cli_entry()

    

