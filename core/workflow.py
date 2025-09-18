# core/workflow.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
import uuid
import time as _time
import yaml

# Rich (не обязателен для работы; без него будет простой текстовый вывод)
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    RICH = True
except Exception:
    RICH = False

import os
import re
from pathlib import Path

console = Console() if RICH else None

# -------------------- шаблоны, утилиты --------------------

def _parse_duration(s: str) -> float:
    """'250ms' -> 0.25; '2s' -> 2; '1m' -> 60. По умолчанию — секунды."""
    if s is None:
        return 0.0
    s = str(s).strip().lower()
    m = re.fullmatch(r'(\d+(?:\.\d+)?)(ms|s|m)?', s)
    if not m:
        try:
            return float(s)
        except Exception:
            return 0.0
    val, unit = m.groups()
    val = float(val)
    if unit == 'ms':
        return val / 1000.0
    if unit == 'm':
        return val * 60.0
    return val  # 's' или None

_TMPL_RE = re.compile(r"\$\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")

def _render_templates(value, ctx: dict):
    """Рекурсивно подставляем ${{ a.b.c }} в str/list/dict."""
    def _lookup(path: str):
        cur = ctx
        for part in path.split('.'):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return ""
        return cur if isinstance(cur, str) else str(cur)

    def _render_str(s: str):
        return _TMPL_RE.sub(lambda m: _lookup(m.group(1)), s)

    if isinstance(value, str):
        return _render_str(value)
    if isinstance(value, list):
        return [_render_templates(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _render_templates(v, ctx) for k, v in value.items()}
    return value

def _deep_merge_dicts(a: dict, b: dict) -> dict:
    out = dict(a or {})
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dicts(out[k], v)
        else:
            out[k] = v
    return out

def _load_secrets(path: str | None) -> dict:
    if not path:
        return {}
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return {}
    try:
        with p.open('r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                return {}
            return data
    except Exception:
        return {}

def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s_total = int(round(seconds))
    m, s = divmod(s_total, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

def _tail_file(path: str | None, n: int = 12) -> str:
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        text = p.read_text(errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-n:]) if lines else ""
    except Exception:
        return ""

def _read_text(path: str | None, max_bytes: int | None = None) -> str:
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        if max_bytes is None:
            return p.read_text(errors="replace")
        data = p.read_bytes()
        if max_bytes and len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode(errors="replace")
    except Exception:
        return ""

# -------------------- mask helpers --------------------

def _collect_masks(runtime: "WorkflowRuntime", extra: List[str] | None) -> List[str]:
    masks: List[str] = []
    # из secrets
    try:
        for v in (runtime.ctx.get("secrets") or {}).values():
            if isinstance(v, str) and len(v) >= 3:
                masks.append(v)
    except Exception:
        pass
    # явные маски из шага
    for v in (extra or []):
        if isinstance(v, str) and len(v) >= 3:
            masks.append(v)
    # уникальные, длинные вперёд
    masks = sorted(set(masks), key=len, reverse=True)
    return masks

def _mask_text(s: str, masks: List[str]) -> str:
    if not s or not masks:
        return s or ""
    out = s
    for m in masks:
        out = out.replace(m, "***")
    return out

# -------------------- модели --------------------

class Target(str, Enum):
    AUTO = "auto"
    HOST = "host"
    DOCKER = "docker"
    SSH = "ssh"

@dataclass
class StepSpec:
    name: str
    run: str
    target: Target = Target.AUTO
    timeout: Optional[int] = None
    env: Dict[str, str] = field(default_factory=dict)
    needs: List[str] = field(default_factory=list)
    if_expr: Optional[str] = None
    retries: Dict[str, object] = field(default_factory=dict)
    continue_on_error: bool = False
    capture: Dict[str, object] = field(default_factory=dict)
    cwd: Optional[str] = None
    mask: List[str] = field(default_factory=list)
    ssh: Optional[str] = None  # алиас из target: ssh:<alias>

@dataclass
class WorkflowSpec:
    name: str
    steps: List[StepSpec]
    env: Dict[str, str] = field(default_factory=dict)
    secrets_from: Optional[str] = None
    source_path: Optional[str] = None
    source_sha256: Optional[str] = None

@dataclass
class StepRunResult:
    step: StepSpec
    ok: bool
    exit_code: int
    duration_sec: float
    target_used: Target
    stdout_path: str | None = None
    stderr_path: str | None = None
    meta: dict = field(default_factory=dict)
    error: str | None = None

@dataclass
class WorkflowRunResult:
    workflow: WorkflowSpec
    run_id: str
    started_at: float
    finished_at: float | None
    steps: List[StepRunResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps) if self.steps else False

@dataclass
class WorkflowContext:
    run_id: str
    workflow_name: str
    step_index: int
    step_total: int

# -------------------- runtime ctx --------------------

class WorkflowRuntime:
    """
    Контекст выполнения workflow:
      - env: глобальные ENV (root YAML)
      - secrets: из файла secrets_from
      - steps: результаты шагов (status/outputs)
    """
    def __init__(self, wf_name: str, wf_env: dict, secrets: dict):
        self.ctx = {
            "workflow": {"name": wf_name},
            "env": dict(wf_env or {}),
            "secrets": dict(secrets or {}),
            "steps": {},
        }

    def register_step_result(self, step_name: str, status: str, outputs: dict | None = None):
        self.ctx["steps"][step_name] = {
            "status": status,
            "outputs": outputs or {},
        }

    def build_env_for_step(self, step_env: dict | None) -> dict:
        base_env = dict(self.ctx["env"])
        merged = _deep_merge_dicts(base_env, step_env or {})
        merged = _render_templates(merged, self.ctx)
        return merged

    def lookup(self, path: str):
        cur = self.ctx
        for part in path.split('.'):
            if isinstance(cur, dict):
                cur = cur.get(part, {})
            else:
                return {}
        return cur

# -------------------- capture --------------------

def _capture_outputs(stdout: str, capture_spec: dict | None) -> dict:
    """
    capture:
      KEY:
        regex: '... (group 1) ...'
    """
    if not stdout or not capture_spec:
        return {}
    out = {}
    for key, rule in (capture_spec or {}).items():
        if not isinstance(rule, dict):
            continue
        rx = rule.get("regex")
        if not rx:
            continue
        m = re.search(rx, stdout, flags=re.MULTILINE)
        if m and m.groups():
            out[key] = m.group(1)
    return out

# -------------------- загрузка и превью --------------------

def load_workflow(path: str) -> WorkflowSpec:
    from hashlib import sha256
    with open(path, "r", encoding="utf-8") as f:
        raw_text = f.read()
    data = yaml.safe_load(raw_text)
    _sha = sha256(raw_text.encode("utf-8")).hexdigest()

    if not isinstance(data, dict):
        raise ValueError("Workflow YAML должен быть объектом верхнего уровня.")

    name = str(data.get("name") or "unnamed")

    root_env = data.get("env") or {}
    if not isinstance(root_env, dict):
        raise ValueError("Корневой 'env' должен быть объектом (dict).")

    secrets_from = data.get("secrets_from")
    if secrets_from is not None and not isinstance(secrets_from, str):
        raise ValueError("'secrets_from' должен быть строкой с путём к YAML-файлу секретов.")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("В workflow должен быть непустой список steps.")

    steps: List[StepSpec] = []
    seen = set()

    for i, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Шаг #{i} должен быть объектом (dict).")

        sname = str(item.get("name") or f"step_{i}")
        if sname in seen:
            raise ValueError(f"Дублируется имя шага: {sname}")
        seen.add(sname)

        run = item.get("run")
        if not run or not isinstance(run, str):
            raise ValueError(f"У шага {sname} обязательное поле 'run' (строка).")

        target_field = item.get("target", "auto")
        target_raw = str(target_field).lower()

        ssh_alias: Optional[str] = None
        if target_raw.startswith("ssh:"):
            # поддержка target: ssh:<alias>
            target = Target.SSH
            ssh_alias = str(target_field)[4:].strip() or None
        else:
            try:
                target = Target(target_raw)
            except Exception:
                raise ValueError(
                    f"Неверный target='{target_raw}' у шага {sname} (auto|host|docker|ssh:<name>)."
                )

        timeout: Optional[int] = None
        timeout_raw = item.get("timeout")
        if timeout_raw is not None:
            if isinstance(timeout_raw, int) and timeout_raw > 0:
                timeout = timeout_raw
            elif isinstance(timeout_raw, str):
                sec = _parse_duration(timeout_raw)
                timeout = int(sec) if sec > 0 else None
            else:
                raise ValueError(f"timeout у шага {sname} должен быть положительным int или строкой '10s/250ms/1m'.")

        env = item.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError(f"env у шага {sname} должен быть объектом (dict).")

        needs = item.get("needs") or []
        if not isinstance(needs, list):
            raise ValueError(f"needs у шага {sname} должен быть списком.")
        needs = [str(x) for x in needs]

        if_expr = item.get("if")

        retries = item.get("retries") or {}
        if not isinstance(retries, dict):
            raise ValueError(f"retries у шага {sname} должен быть объектом (dict).")
        r_max = int(retries.get("max", 0) or 0)
        r_delay = retries.get("delay", 0)
        if isinstance(r_delay, (int, float)):
            delay_sec = float(r_delay)
        else:
            delay_sec = _parse_duration(str(r_delay))
        r_backoff = float(retries.get("backoff", 1.0) or 1.0)
        retries_norm = {"max": r_max, "delay": delay_sec, "backoff": r_backoff}

        continue_on_error = bool(item.get("continue_on_error", False))

        capture = item.get("capture") or {}
        if not isinstance(capture, dict):
            raise ValueError(f"capture у шага {sname} должен быть объектом (dict).")

        # >>> cwd парсим отдельно
        cwd = item.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError(f"cwd у шага {sname} должен быть строкой (путь).")

        mask = item.get("mask") or []
        if not isinstance(mask, list):
            raise ValueError(f"mask у шага {sname} должен быть списком строк.")
        mask = [str(x) for x in mask]

        steps.append(StepSpec(
            name=sname, run=run, target=target, timeout=timeout, env=env, needs=needs,
            if_expr=if_expr, retries=retries_norm, continue_on_error=continue_on_error,
            capture=capture, cwd=cwd, mask=mask, ssh=ssh_alias,
        ))

    # проверка ссылок в needs
    names = {s.name for s in steps}
    for s in steps:
        for dep in s.needs:
            if dep not in names:
                raise ValueError(f"Шаг '{s.name}' зависит от неизвестного шага '{dep}'.")

    return WorkflowSpec(
        name=name,
        steps=steps,
        env=root_env,
        secrets_from=secrets_from,
        source_path=path,
        source_sha256=_sha,
    )


def preview_workflow(wf: WorkflowSpec) -> None:
    def _fmt_retries(r: dict | None) -> str:
        if not r:
            return ""
        m = int((r.get("max") or 0))
        if m <= 0:
            return ""
        d = float(r.get("delay") or 0.0)
        b = float(r.get("backoff") or 1.0)
        d_str = f"{int(d)}" if abs(d - int(d)) < 1e-9 else f"{d:g}"
        b_str = f"{b:.1f}" if (b % 1) else f"{int(b)}"
        return f"{m}/{d_str}/{b_str}"

    title = f"Workflow: {wf.name} • {len(wf.steps)} шаг"
    if not RICH:
        print(f"[workflow] {title}")
        print("#  step        run                         target  needs        if                        retries   COE")
        for i, s in enumerate(wf.steps, start=1):
            needs = ", ".join(s.needs) if s.needs else ""
            retries = _fmt_retries(s.retries)
            coe = "✓" if s.continue_on_error else ""
            first_line = (s.run or "").splitlines()[0]
            # >>> target_str для plain-таблицы
            target_str = f"ssh:{s.ssh}" if getattr(s, "target", None) == Target.SSH and getattr(s, "ssh", None) else getattr(s, "target", None).value
            print(f"{i:<2} {s.name:<11} {first_line:<27} {target_str:<6} {needs:<12} {str(s.if_expr or ''):<24} {retries:<8} {coe}")
        print("\nЭто превью. Выполнение добавим на шаге 2.")
        return

    console.print(Panel.fit(f"[bold]{title}[/bold]", border_style="cyan"))

    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("#", justify="right", style="bold")
    table.add_column("step", style="bold")
    table.add_column("run")
    table.add_column("target", justify="center")
    table.add_column("needs")
    table.add_column("if")
    table.add_column("retries", justify="center")
    table.add_column("COE", justify="center")

    for i, s in enumerate(wf.steps, start=1):
        needs = ", ".join(s.needs) if s.needs else ""
        retries = _fmt_retries(s.retries)
        coe = "✓" if s.continue_on_error else ""
        first_line = (s.run or "").splitlines()[0]
        # >>> target_str для Rich-таблицы
        target_str = f"ssh:{s.ssh}" if getattr(s, "target", None) == Target.SSH and getattr(s, "ssh", None) else getattr(s, "target", None).value
        table.add_row(
            str(i), s.name, first_line, target_str,
            needs, s.if_expr or "", retries, coe
        )

    console.print(table)
    console.print(Panel.fit("[dim]Это превью. Выполнение добавим на шаге 2.[/dim]", border_style="grey50"))

# -------------------- выполнение --------------------

def _confirm_run(wf: WorkflowSpec) -> bool:
    if not RICH:
        ans = input(f"[workflow] Запустить '{wf.name}' на {len(wf.steps)} шаг(ов)? [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    console.print(Panel.fit(
        f"[bold]Запустить workflow:[/bold] {wf.name}\n[dim]{len(wf.steps)} шаг(ов)[/dim]",
        border_style="yellow"
    ))
    ans = input("Proceed? [y/N]: ").strip().lower()
    return ans in ("y", "yes")

def _print_summary(run: WorkflowRunResult) -> None:
    total = len(run.steps)
    skipped = sum(1 for s in run.steps if s.meta.get("skipped"))
    soft = sum(1 for s in run.steps if s.meta.get("soft_fail"))
    ok = sum(1 for s in run.steps if s.ok and not s.meta.get("soft_fail"))
    fail = sum(1 for s in run.steps if (not s.ok) and not s.meta.get("skipped"))

    longest_name = "-"
    longest_dur = -1.0
    for s in run.steps:
        d = s.duration_sec or 0.0
        if d > longest_dur:
            longest_dur = d
            longest_name = s.step.name

    total_time = None
    if run.finished_at is not None and run.started_at is not None:
        total_time = run.finished_at - run.started_at

    if not RICH:
        print(f"\n    Workflow: {run.workflow.name}    ")
        print("┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━┓")
        print(f"┃ Metric       ┃      Value ┃")
        print("┡━━━━━━━━━━━━━━╇━━━━━━━━━━━━┩")
        print(f"│ Total steps  │ {total:>10} │")
        print(f"│ OK           │ {ok:>10} │")
        print(f"│ SOFT_FAIL    │ {soft:>10} │")
        print(f"│ FAIL         │ {fail:>10} │")
        print(f"│ SKIPPED      │ {skipped:>10} │")
        print(f"│ Duration     │ {_fmt_duration(total_time):>10} │")
        if longest_dur >= 0:
            print(f"│ Longest step │ {longest_name} ({int(round(longest_dur))}s) │")
        print("└──────────────┴────────────┘")
        return

    table = Table(title=f"[bold]Workflow: {run.workflow.name}[/bold]")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Total steps", str(total))
    table.add_row("OK", str(ok))
    table.add_row("SOFT_FAIL", str(soft))
    table.add_row("FAIL", str(fail))
    table.add_row("SKIPPED", str(skipped))
    table.add_row("Duration", _fmt_duration(total_time))
    if longest_dur >= 0:
        table.add_row("Longest step", f"{longest_name} ({int(round(longest_dur))}s)")
    console.print()
    console.print(table)


def _step_fingerprint(step: StepSpec) -> str:
    """
    Делает хэш шага по ключевым полям YAML — для сравнения изменений между запусками.
    """
    from hashlib import sha256 as _sha256
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


def _save_last_run(res: WorkflowRunResult) -> None:
    """
    Сохраняет сводку последнего прогона в .ghostcmd/last_run.json
    """
    try:
        from pathlib import Path
        import json as _json
        out_dir = Path(".ghostcmd")
        out_dir.mkdir(exist_ok=True)
        wf = res.workflow
        steps = []
        for s in res.steps:
            status = "ok" if s.ok else "failed"
            if s.meta.get("skipped"):
                status = "skipped"
            if s.meta.get("soft_fail") and s.ok:
                status = "soft_ok"
            steps.append({
                "name": s.step.name,
                "status": status,
                "ok": bool(s.ok),
                "exit_code": int(s.exit_code),
                "duration_sec": float(s.duration_sec),
                "target": getattr(s.target_used, "value", str(s.target_used)),
                "stdout_path": s.stdout_path,
                "stderr_path": s.stderr_path,
                "fingerprint": _step_fingerprint(s.step),
                "meta": s.meta or {},
            })
        payload = {
            "run_id": res.run_id,
            "workflow_name": wf.name,
            "file_path": getattr(wf, "source_path", None),
            "yaml_sha256": getattr(wf, "source_sha256", None),
            "started_at": float(res.started_at),
            "finished_at": float(res.finished_at) if res.finished_at is not None else None,
            "ok": bool(res.ok),
            "steps": steps,
            "step_fingerprints": {s["name"]: s["fingerprint"] for s in steps},
        }
        (out_dir / "last_run.json").write_text(
            _json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        try:
            print(f"[workflow] Не удалось сохранить last_run.json: {e}")
        except Exception:
            pass

def run_workflow(
    wf: WorkflowSpec,
    execute_step_cb,   # (StepSpec, WorkflowContext) -> StepRunResult
    ask_confirm: bool = True,
) -> WorkflowRunResult:
    run_id = str(uuid.uuid4())
    res = WorkflowRunResult(workflow=wf, run_id=run_id, started_at=_time.time(), finished_at=None)

    if ask_confirm:
        preview_workflow(wf)
        if not _confirm_run(wf):
            if RICH:
                console.print("[dim]Отменено пользователем.[/dim]")
            else:
                print("[workflow] Отменено пользователем.")
            res.finished_at = _time.time()
            return res

    # runtime: env + secrets
    wf_env = getattr(wf, "env", {}) or {}
    wf_secrets = _load_secrets(getattr(wf, "secrets_from", None))
    runtime = WorkflowRuntime(wf.name, wf_env, wf_secrets)

    total_steps = len(wf.steps)

    for idx, step in enumerate(wf.steps, start=1):
        header = f"┌ Step {idx}/{total_steps}: {step.name} ┐"
        console.print(f"[bold]{header}[/bold]") if RICH else print(header)

        # env для шага (merge root env + step env + secrets)
        env_for_step = runtime.build_env_for_step(step.env or {})
        for k, v in (runtime.ctx.get("secrets") or {}).items():
            if isinstance(v, str) and k not in env_for_step:
                env_for_step[k] = v

        # needs: пропуск, если зависимость провалена
        if step.needs:
            failed = [
                dep for dep in step.needs
                if any(r.step.name == dep and not r.ok for r in res.steps)
            ]
            if failed:
                sres = StepRunResult(
                    step=step, ok=False, exit_code=200, duration_sec=0.0,
                    target_used=step.target,
                    meta={"skipped": True, "reason": f"failed needs: {', '.join(failed)}"},
                    error="skipped due to failed dependencies",
                )
                runtime.register_step_result(step.name, "SKIPPED", {})
                res.steps.append(sres)
                msg = "⏭️  SKIPPED • failed dependencies"
                console.print(msg) if RICH else print(msg)
                continue

        # if: условный пропуск
        if step.if_expr:
            import subprocess
            rc = 1
            try:
                if_expr = _render_templates(step.if_expr, runtime.ctx)
                env_for_if = os.environ.copy()
                env_for_if.update(env_for_step)
                rc = subprocess.call(["bash", "-lc", if_expr], env=env_for_if)
            except Exception:
                rc = 1
            if rc != 0:
                sres = StepRunResult(
                    step=step, ok=False, exit_code=201, duration_sec=0.0,
                    target_used=step.target,
                    meta={"skipped": True, "reason": "if failed"},
                    error="skipped by condition",
                )
                runtime.register_step_result(step.name, "SKIPPED", {})
                res.steps.append(sres)
                msg = "⏭️  SKIPPED • condition false"
                console.print(msg) if RICH else print(msg)
                continue

        # retries/backoff
        attempts_meta = []
        attempt = 0
        last_sres: Optional[StepRunResult] = None
        ok = False

        r_cfg = step.retries or {}
        max_retries = int(r_cfg.get("max", 0) or 0)
        base_delay = float(r_cfg.get("delay", 0.0) or 0.0)
        backoff = float(r_cfg.get("backoff", 1.0) or 1.0)
        total_attempts = max_retries + 1

        while True:
            attempt += 1
            started = _time.time()
            try:
                ctx = WorkflowContext(
                    run_id=run_id,
                    workflow_name=wf.name,
                    step_index=idx,
                    step_total=total_steps,
                )

                # resolve cwd (всегда ставим)
                if getattr(step, "cwd", None):
                    _cwd_rendered = _render_templates(step.cwd, runtime.ctx)
                    cwd_resolved = os.path.abspath(os.path.expanduser(str(_cwd_rendered)))
                else:
                    cwd_resolved = os.getcwd()

                # копия шага с подставленными шаблонами и объединённым env
                exec_step = StepSpec(
                    name=step.name,
                    run=_render_templates(step.run, runtime.ctx),
                    target=step.target,
                    timeout=step.timeout,
                    env=env_for_step,
                    needs=step.needs,
                    if_expr=step.if_expr,
                    retries=step.retries,
                    continue_on_error=step.continue_on_error,
                    capture=step.capture,
                    cwd=cwd_resolved,
                    mask=step.mask,
                    ssh=step.ssh,
                )

                # === ВЫПОЛНЕНИЕ ШАГА ===
                sres: StepRunResult = execute_step_cb(exec_step, ctx)

            except Exception as e:
                # >>> ВАЖНО: печатаем причину, чтобы не было «немого 999»
                try:
                    print(f"{type(e).__name__}: {e}")
                except Exception:
                    pass
                sres = StepRunResult(
                    step=step, ok=False, exit_code=999,
                    duration_sec=_time.time() - started,
                    target_used=step.target,
                    error=str(e), meta={"exception": True},
                )
                import traceback
                sres.meta["trace"] = traceback.format_exc()

            attempts_meta.append({
                "attempt": attempt,
                "exit_code": sres.exit_code,
                "duration": sres.duration_sec,
                "ok": sres.ok,
                "error": sres.error,
            })
            last_sres = sres

            if sres.ok:
                ok = True
                break

            fail_line = f"❌ FAIL • exit={sres.exit_code} • {sres.duration_sec:.1f}s • target={sres.target_used.value} • attempt {attempt}/{total_attempts}"
            console.print(fail_line) if RICH else print(fail_line)

            if attempt > max_retries:
                break

            delay = base_delay * (backoff ** (attempt - 1))
            if delay > 0:
                retry_line = f"⟳ retry in {delay:.2f}s (attempt {attempt+1}/{total_attempts})"
                console.print(retry_line) if RICH else print(retry_line)
                _time.sleep(delay)

        # continue_on_error -> мягкий фейл
        soft_fail = False
        final_sres = last_sres
        if not ok and step.continue_on_error:
            soft_fail = True
            final_sres = StepRunResult(
                step=last_sres.step, ok=True,
                exit_code=last_sres.exit_code,
                duration_sec=last_sres.duration_sec,
                target_used=last_sres.target_used,
                stdout_path=last_sres.stdout_path,
                stderr_path=last_sres.stderr_path,
                meta={**(last_sres.meta or {}), "soft_fail": True, "attempts": attempts_meta},
                error=last_sres.error,
            )
        else:
            final_sres.meta = {**(final_sres.meta or {}), "attempts": attempts_meta}

        # статусная строка
        if final_sres.ok and not final_sres.meta.get("soft_fail"):
            ok_line = f"✅ OK • exit={final_sres.exit_code} • {final_sres.duration_sec:.1f}s • target={final_sres.target_used.value}"
            if attempt > 1:
                ok_line += f" • attempt {attempt}/{total_attempts}"
            console.print(ok_line) if RICH else print(ok_line)
        elif final_sres.meta.get("soft_fail"):
            soft_line = f"❌ SOFT_FAIL • exit={final_sres.exit_code} • {final_sres.duration_sec:.1f}s • target={final_sres.target_used.value}"
            console.print(soft_line) if RICH else print(soft_line)
        else:
            fail_line = f"❌ FAIL • exit={final_sres.exit_code} • {final_sres.duration_sec:.1f}s • target={final_sres.target_used.value}"
            console.print(fail_line) if RICH else print(fail_line)

        # хвосты stdout/stderr (короткие)
        tail_out = _tail_file(final_sres.stdout_path, n=5)
        if tail_out:
            block = "stdout (last 5):\n  " + "\n  ".join(tail_out.splitlines())
            console.print(block) if RICH else print(block)

        tail_err = _tail_file(final_sres.stderr_path, n=5)
        if tail_err:
            block = "stderr (last 5):\n  " + "\n  ".join(tail_err.splitlines())
            console.print(block) if RICH else print(block)

        # захват outputs + регистрация статуса для шаблонов последующих шагов
        stdout_full = _read_text(final_sres.stdout_path, max_bytes=512_000)
        outputs = _capture_outputs(stdout_full, step.capture)

        status_str = "OK" if (final_sres.ok and not final_sres.meta.get("soft_fail")) else \
                     ("SOFT_FAIL" if final_sres.meta.get("soft_fail") else "FAIL")
        runtime.register_step_result(step.name, status_str, outputs)

        # маски (секреты + шаговые mask)
        masks = _collect_masks(runtime, step.mask)

        # печать outputs с маскированием
        if outputs:
            masked_lines = [
                f"  steps.{step.name}.outputs.{k} = {_mask_text(str(v), masks)}"
                for k, v in outputs.items()
            ]
            block = "(outputs)\n" + "\n".join(masked_lines)
            if RICH:
                try:
                    console.print(Panel.fit(block, border_style="cyan"))
                except Exception:
                    console.print(block)
            else:
                print(block)

        # сохраняем результат
        res.steps.append(final_sres)

        # длинные хвосты с маскированием (для наглядности)
        long_tail_out = _mask_text(_tail_file(final_sres.stdout_path, n=12), masks)
        long_tail_err = _mask_text(_tail_file(final_sres.stderr_path, n=12), masks)

        if long_tail_out:
            if RICH:
                console.print(Panel.fit(long_tail_out, title="[dim]stdout (last 12)[/dim]", border_style="grey50"))
            else:
                print("\n[stdout tail]\n" + long_tail_out)

        if long_tail_err:
            if RICH:
                console.print(Panel.fit(long_tail_err, title="[dim]stderr (last 12)[/dim]", border_style="grey50"))
            else:
                print("\n[stderr tail]\n" + long_tail_err)

        # стоп при реальном фейле
        if not final_sres.ok and not final_sres.meta.get("skipped"):
            stop_msg = "⛔ Останов: шаг завершился ошибкой."
            console.print(stop_msg) if RICH else print(stop_msg)
            break

    res.finished_at = _time.time()

    # краткий итог
    total = len(res.steps)
    ok_count = sum(1 for s in res.steps if s.ok and not s.meta.get("soft_fail") and not s.meta.get("skipped"))
    skipped_count = sum(1 for s in res.steps if s.meta.get("skipped"))
    fail_count = sum(1 for s in res.steps if not s.ok and not s.meta.get("skipped"))

    if fail_count == 0 and skipped_count > 0:
        msg = f"⏭️  Завершено: {ok_count}/{total} ok, {skipped_count} skipped."
    elif fail_count == 0:
        msg = f"✅ Готово: {ok_count}/{total} шаг(ов) успешно."
    else:
        msg = f"⛔ Останов: {ok_count}/{total} ok, {fail_count} fail, {skipped_count} skipped."
    console.print(msg) if RICH else print(msg)

    _print_summary(res)
    res.finished_at = _time.time()
    _save_last_run(res)
    return res
