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


def apply_ops(data: Any, ops: List[Dict[str, Any]]) -> List[str]:
    """
    Применяет операции к YAML-объекту workflow.
    Возвращает список сообщений/предупреждений для пользователя.

    Улучшения:
    - принимаем step как {"name": "..."} / {"index": N} ИЛИ просто строку "имя_шага"
    - insert_after / insert_before: если якорь не найден — мягкий фоллбек:
        * insert_before  -> вставляем в начало
        * insert_after   -> вставляем в конец
      (и пишем предупреждение в msgs)
    """
    msgs: List[str] = []
    jobs = data.get("jobs", {})
    job = None

    # если операция явно указывает job
    target_job = None
    for op in ops or []:
        if "job" in op:
            target_job = op["job"]
            break

    if target_job and target_job in jobs:
        job = jobs[target_job]
    else:
        # иначе берём первый job, где есть steps
        for name, candidate in jobs.items():
            if isinstance(candidate, dict) and "steps" in candidate:
                job = candidate
                break

    if not job or "steps" not in job:
        return ["В YAML отсутствует корректный список steps."]

    steps = job["steps"]

    def _coerce_step_ref(step_ref):
        """Разрешаем step_ref быть строкой — превращаем в {'name': <str>}."""
        if isinstance(step_ref, str):
            return {"name": step_ref}
        return step_ref

    for op in ops or []:
        try:
            kind = str(op.get("op") or "").strip()
            if not kind:
                msgs.append("пропущена операция без поля 'op'")
                continue

            # ---- адресация шага (если нужна) ----
            step_ref = _coerce_step_ref(op.get("step"))
            step = None
            step_idx = None
            if step_ref is not None:
                try:
                    step, step_idx = _resolve_step_ref(steps, step_ref)
                except Exception as e:
                    # Для операций где адрес шага обязателен — сообщим и продолжим
                    # Для вставок попробуем фоллбек ниже
                    step, step_idx = None, None
                    # Не будем сразу падать: часть операций умеем выполнить и без точного индекса

            # ---- простые сеттеры шага ----
            if kind == "set_run":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                step["run"] = str(op.get("value"))

            elif kind == "set_target":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                tgt = str(op.get("value")).lower().strip()
                if tgt not in ("auto", "host", "docker"):
                    msgs.append(f"неверный target '{tgt}' — оставлен без изменений")
                else:
                    step["target"] = tgt

            elif kind == "set_timeout":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                step["timeout"] = _parse_duration_to_string(str(op.get("value")))

            elif kind == "set_if":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                step["if"] = str(op.get("value"))

            elif kind == "set_cwd":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                step["cwd"] = str(op.get("value"))

            elif kind == "set_env":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                env = step.get("env")
                if not _is_mapping(env):
                    env = {}
                env.update(_coerce_env(op.get("value")))
                step["env"] = env

            elif kind == "unset_env":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                k = str(op.get("key"))
                env = step.get("env")
                if _is_mapping(env) and k in env:
                    env.pop(k, None)

            elif kind == "set_retries":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                r = step.get("retries")
                if not _is_mapping(r):
                    r = {}
                if "max" in op:
                    r["max"] = int(op["max"])
                if "delay" in op:
                    r["delay"] = _parse_duration_to_string(str(op["delay"]))
                if "backoff" in op:
                    r["backoff"] = float(op["backoff"])
                step["retries"] = r

            elif kind == "set_needs":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                needs = _ensure_list_str(op.get("value"))
                step["needs"] = needs

            elif kind == "add_needs":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                extra_vals = _ensure_list_str(op.get("value"))
                base = step.get("needs") or []
                base = list(dict.fromkeys([*base, *extra_vals]))
                step["needs"] = base

            elif kind == "del_needs":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                delv = set(_ensure_list_str(op.get("value")))
                base = [x for x in (step.get("needs") or []) if x not in delv]
                step["needs"] = base

            elif kind == "set_mask":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                vals = _ensure_list_str(op.get("value"))
                step["mask"] = vals

            elif kind == "clear_mask":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                step["mask"] = []

            # ---- root env ----
            elif kind == "set_root_env":
                root_env = data.get("env")
                if not _is_mapping(root_env):
                    root_env = {}
                root_env.update(_coerce_env(op.get("value")))
                data["env"] = root_env

            elif kind == "unset_root_env":
                k = str(op.get("key"))
                root_env = data.get("env")
                if _is_mapping(root_env) and k in root_env:
                    root_env.pop(k, None)

            # ---- операции со списком шагов ----
            elif kind == "rename_step":
                if step is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                new_name = str(op["new_name"])
                step["name"] = new_name

            elif kind in ("insert_after", "insert_before"):
                payload = op.get("value")

                # 1) Строим шаг с гарантированным порядком ключей: name, run, target, ...
                new_step = CommentedMap()

                # name
                if _is_mapping(payload) and payload.get("name"):
                    base_name = str(payload["name"])
                else:
                    base_name = None
                new_step["name"] = op.get("name") or base_name or "step_new"

                # run
                if isinstance(payload, str):
                    new_step["run"] = payload
                elif _is_mapping(payload):
                    new_step["run"] = payload.get("run", "echo (пустой шаг)")
                else:
                    new_step["run"] = "echo (пустой шаг)"

                # target
                if _is_mapping(payload) and "target" in payload:
                    new_step["target"] = payload["target"]
                else:
                    new_step["target"] = "auto"

                # Остальные поля из payload (кроме name/run/target) — в том порядке, как они шли
                if _is_mapping(payload):
                    for k, v in payload.items():
                        if k in ("name", "run", "target"):
                            continue
                        new_step[k] = v

                # 2) Вставка относительно якоря с мягким фоллбеком
                if step_idx is None:
                    if kind == "insert_before":
                        steps.insert(0, new_step)
                        msgs.append(f"{kind}: anchor не найден — шаг '{new_step['name']}' вставлен в начало.")
                    else:
                        steps.append(new_step)
                        msgs.append(f"{kind}: anchor не найден — шаг '{new_step['name']}' вставлен в конец.")
                else:
                    if kind == "insert_before":
                        pos = max(0, step_idx - 1)
                        steps.insert(pos, new_step)
                    else:  # insert_after
                        steps.insert(step_idx, new_step)  # step_idx — 1-базовый, вставляем «после»
                    anchor_label = (step_ref.get("name") if isinstance(step_ref, dict) and "name" in step_ref else step_ref)
                    msgs.append(f"{kind}: вставлен шаг '{new_step['name']}' относительно '{anchor_label}'")



            elif kind == "delete_step":
                if step_idx is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                steps.pop(step_idx - 1)

            elif kind == "move_before":
                if step_idx is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                anchor_ref = _coerce_step_ref(op.get("anchor"))
                if not anchor_ref:
                    raise ValueError("move_before: нет anchor")
                try:
                    _, anchor_idx = _resolve_step_ref(steps, anchor_ref)
                except Exception:
                    # если якорь не найден — перемещаем в начало
                    cur = steps.pop(step_idx - 1)
                    steps.insert(0, cur)
                    msgs.append("move_before: anchor не найден — шаг перемещён в начало.")
                else:
                    cur = steps.pop(step_idx - 1)
                    insert_pos = max(0, anchor_idx - 1)
                    steps.insert(insert_pos, cur)

            elif kind == "move_after":
                if step_idx is None:
                    raise ValueError("Операция не содержит index/name для шага.")
                anchor_ref = _coerce_step_ref(op.get("anchor"))
                if not anchor_ref:
                    raise ValueError("move_after: нет anchor")
                try:
                    _, anchor_idx = _resolve_step_ref(steps, anchor_ref)
                except Exception:
                    # если якорь не найден — перемещаем в конец
                    cur = steps.pop(step_idx - 1)
                    steps.append(cur)
                    msgs.append("move_after: anchor не найден — шаг перемещён в конец.")
                else:
                    cur = steps.pop(step_idx - 1)
                    steps.insert(anchor_idx, cur)

            else:
                msgs.append(f"неизвестная операция '{kind}' — пропущена")

        except Exception as e:
            msgs.append(f"ошибка операции {op}: {e}")

    return msgs
