# core/workflow_edit.py
from __future__ import annotations
from ruamel.yaml.comments import CommentedMap

import re
from typing import Any, Dict, List, Tuple, Optional



# Ядро редактирования YAML с сохранением форматирования делаем поверх ruamel'овских структур,
# но код работает и с обычными dict/list (fallback).
def _is_mapping(x):  # CommentedMap или dict
    try:
        from ruamel.yaml.comments import CommentedMap  # type: ignore
        return isinstance(x, (dict, CommentedMap))
    except Exception:
        return isinstance(x, dict)

def _is_seq(x):  # CommentedSeq или list
    try:
        from ruamel.yaml.comments import CommentedSeq  # type: ignore
        return isinstance(x, (list, CommentedSeq))
    except Exception:
        return isinstance(x, list)

def _ensure_steps(data: Any) -> Tuple[List[dict], List[str]]:
    """
    Возвращает ИМЕННО исходный список data['steps'], а не его копию.
    Мы только проверяем элементы и пишем варнинги, но не создаём новый список.
    Это критично для операций вставки/удаления, чтобы изменения сохранились в data.
    """
    msgs: List[str] = []
    if not _is_mapping(data):
        raise ValueError("YAML root должен быть объектом.")
    steps = data.get("steps")
    if not _is_seq(steps):
        raise ValueError("В YAML отсутствует корректный список steps.")

    # sanity-check: предупредим, но не фильтруем
    for i, s in enumerate(list(steps), start=1):
        if not _is_mapping(s):
            msgs.append(f"Шаг #{i} не является объектом — операции над ним будут пропущены.")

    # ВАЖНО: возвращаем ОРИГИНАЛЬНЫЙ список, чтобы insert/pop реально меняли data
    return steps, msgs


def _parse_duration_to_string(v: str) -> str:
    # принимаем "60", "60s", "2m", "250ms"
    v = (v or "").strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)?", v)
    if not m:
        # если мусор, вернём как есть — валидатор core/workflow сам отловит
        return v
    num, unit = m.groups()
    return num + (unit or "s")

def _find_step_by_index(steps: List[dict], idx: int) -> dict:
    if not (1 <= idx <= len(steps)):
        raise IndexError(f"Нет шага с индексом {idx} (всего {len(steps)}).")
    return steps[idx - 1]

def _find_step_index_by_name(steps: List[dict], name: str) -> int:
    for i, s in enumerate(steps, start=1):
        if str(s.get("name")) == name:
            return i
    raise ValueError(f"Шаг с именем '{name}' не найден.")

def _coerce_env(x: Any) -> Dict[str, str]:
    if _is_mapping(x):
        return {str(k): str(v) for k, v in x.items()}
    # парсим "KEY=VAL KEY2=VAL2"
    s = str(x or "")
    out: Dict[str, str] = {}
    for token in re.findall(r'(\w+)=(".*?"|\'.*?\'|[^ \t]+)', s):
        k, v = token
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k] = v
    return out

# -------------------------------
# OPS: список операций редактирования yaml workflow
# каждая операция — dict с ключом "op"
# Поддерживаем:
#  set_run, set_target, set_timeout, set_if, set_cwd,
#  set_env, unset_env, set_retries, set_needs, add_needs, del_needs,
#  rename_step, insert_after, insert_before, delete_step, move_before, move_after,
#  set_mask, clear_mask, set_root_env, unset_root_env
# Для адресации шага используем либо {"index": N}, либо {"name": "step_name"}.
# -------------------------------

def _resolve_step_ref(steps: list[dict], step_ref: dict | str) -> tuple[dict, int]:
    """
    Находит шаг по имени или индексу.
    Возвращает (step_dict, 1-based index).
    """
    if isinstance(step_ref, dict):
        if "index" in step_ref:
            idx = int(step_ref["index"])
            if 1 <= idx <= len(steps):
                return steps[idx - 1], idx
            raise ValueError(f"Шаг с index={idx} не найден")

        if "name" in step_ref:
            target_name = str(step_ref["name"]).strip().lower()
            for i, s in enumerate(steps, 1):
                if str(s.get("name", "")).strip().lower() == target_name:
                    return s, i
            raise ValueError(f"Шаг с name='{step_ref['name']}' не найден")

    elif isinstance(step_ref, str):
        target_name = step_ref.strip().lower()
        for i, s in enumerate(steps, 1):
            if str(s.get("name", "")).strip().lower() == target_name:
                return s, i
        raise ValueError(f"Шаг с name='{step_ref}' не найден")

    raise ValueError(f"Неподдерживаемый step_ref: {step_ref}")


# --- алиасы шагов ---
ALIASES = {
    "run tests": "test",
    "tests": "test",
    "pytest": "test",
    "unit tests": "test",
    "lint check": "lint",
    "black": "lint",
    "format": "lint",
    "coverage report": "coverage",
    "cov": "coverage",
}

def _normalize_step_name(name: str | None) -> str:
    if not name:
        return ""
    return ALIASES.get(name.lower().strip(), name.strip())


def apply_ops(data: Any, ops: List[Dict[str, Any]]) -> List[str]:
    """
    Применяет операции к YAML-объекту workflow.
    Возвращает список сообщений/предупреждений для пользователя.
    """
    msgs: List[str] = []
    jobs = data.get("jobs", {})
    job = None

    # выбираем job
    target_job = None
    for op in ops or []:
        if "job" in op:
            target_job = op["job"]
            break

    if target_job and target_job in jobs:
        job = jobs[target_job]
    else:
        for _, candidate in jobs.items():
            if isinstance(candidate, dict) and "steps" in candidate:
                job = candidate
                break

    if not job or "steps" not in job:
        return ["В YAML отсутствует корректный список steps."]

    steps = job["steps"]

    def _coerce_step_ref(step_ref):
        if isinstance(step_ref, str):
            return {"name": step_ref}
        return step_ref

    def _resolve_step_ref(steps, step_ref):
        if not step_ref:
            raise ValueError("step_ref пуст")

        if "index" in step_ref:
            idx = step_ref["index"]
            if isinstance(idx, int) and 0 <= idx < len(steps):
                return steps[idx], idx
            raise ValueError(f"Индекс шага вне диапазона: {idx}")

        if "name" in step_ref:
            target = _normalize_step_name(step_ref["name"])
            for i, st in enumerate(steps):
                nm = _normalize_step_name(st.get("name"))
                if nm == target:
                    return st, i
            raise ValueError(f"Шаг с именем '{step_ref['name']}' не найден")

        raise ValueError("step_ref не содержит index/name")

    # --- применяем операции ---
    for op in ops or []:
        try:
            kind = str(op.get("op") or "").strip()
            if not kind:
                msgs.append("пропущена операция без поля 'op'")
                continue

            step_ref = _coerce_step_ref(op.get("step"))
            step = None
            step_idx = None
            if step_ref is not None:
                try:
                    step, step_idx = _resolve_step_ref(steps, step_ref)
                except Exception as e:
                    step, step_idx = None, None
                    msgs.append(f"ошибка операции {op}: {e}")

            # === операции ===
            if kind == "set_run":
                if step is None:
                    raise ValueError("нет index/name для set_run")
                step["run"] = str(op.get("value"))

            elif kind == "insert_before":
                payload = op.get("value", {})
                new_step = {"name": op.get("name") or "step_new"}
                new_step["run"] = payload if isinstance(payload, str) else payload.get("run", "echo (пустой шаг)")
                if step_idx is None:
                    steps.insert(0, new_step)
                    msgs.append(f"{kind}: anchor не найден — '{new_step['name']}' в начало.")
                else:
                    steps.insert(step_idx, new_step)
                    msgs.append(f"{kind}: вставлен '{new_step['name']}' перед '{step_ref}'")

            elif kind == "insert_after":
                payload = op.get("value", {})
                new_step = {"name": op.get("name") or "step_new"}
                new_step["run"] = payload if isinstance(payload, str) else payload.get("run", "echo (пустой шаг)")
                if step_idx is None:
                    steps.append(new_step)
                    msgs.append(f"{kind}: anchor не найден — '{new_step['name']}' в конец.")
                else:
                    steps.insert(step_idx + 1, new_step)
                    msgs.append(f"{kind}: вставлен '{new_step['name']}' после '{step_ref}'")

            elif kind == "delete_step":
                if step_idx is None:
                    raise ValueError("нет index/name для delete_step")
                steps.pop(step_idx)

            else:
                msgs.append(f"неизвестная операция '{kind}' — пропущена")

        except Exception as e:
            msgs.append(f"ошибка операции {op}: {e}")

    return msgs

