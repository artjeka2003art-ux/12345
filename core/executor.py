# core/executor.py
from __future__ import annotations

from typing import Optional, Dict, List
import os
import shlex
import re
import time
from pathlib import Path
from getpass import getpass

from .config import load_ssh_hosts
from .workflow import StepSpec, WorkflowContext, StepRunResult, Target
from core.limits import ResourceLimits, load_limits_for_risk
from core.exec_limits import run_on_host_with_limits

# Можно переопределить через переменную окружения GHOSTCMD_SANDBOX_IMAGE
SANDBOX_IMAGE = os.environ.get("GHOSTCMD_SANDBOX_IMAGE", "ghost-sandbox:latest")


# ----------------------------- SSH helpers ---------------------------------

def _classify_ssh_error(stderr: str) -> tuple[str, str]:
    s = (stderr or "").lower()
    if "permission denied" in s:
        return ("Permission denied", "Проверь user/identity_file (chmod 600) и доступ на сервере.")
    if "no route to host" in s or "network is unreachable" in s or "could not resolve hostname" in s:
        return ("Хост недоступен", "Проверь host/ip, DNS, порт и firewall.")
    if "connection timed out" in s or "operation timed out" in s or "timed out" in s:
        return ("Таймаут подключения", "Проверь порт, firewall и что sshd слушает.")
    if "connection refused" in s:
        return ("Подключение отклонено", "На порту нет sshd или подключение блокируется.")
    if "host key verification failed" in s or "man-in-the-middle" in s:
        return ("Проверка ключа хоста", "Поставь strict_host_key_checking: accept-new или добавь ключ в known_hosts.")
    if "kex_exchange_identification" in s:
        return ("KEX ошибка", "Часто бан/лимит. Проверь fail2ban/sshd_config/лимиты подключений.")
    return ("", "")

def _resolve_ssh_host(alias: str) -> dict:
    """
    Возвращает словарь параметров SSH-хоста по алиасу.
    Формат: {host, user, port, identity_file, strict_host_key_checking}
    """
    hosts = load_ssh_hosts()
    cfg = hosts.get(alias)
    if not cfg:
        raise RuntimeError(
            f"SSH-алиас '{alias}' не найден в ~/.ghostcmd/config.yml (секция 'ssh')."
        )
    host = cfg.get("host")
    if not host:
        raise RuntimeError(f"SSH-алиас '{alias}' задан без поля 'host'.")

    user = cfg.get("user", os.getenv("USER", ""))
    port = int(cfg.get("port", 22))
    identity_file = cfg.get("identity_file")  # может быть None
    shk = cfg.get("strict_host_key_checking", "accept-new")  # yes|no|accept-new

    return {
        "host": host,
        "user": user,
        "port": port,
        "identity_file": identity_file,
        "strict_host_key_checking": shk,
    }

def _build_ssh_cmd(host_cfg: dict, run_script: str, env: dict | None, cwd: str | None) -> list[str]:
    """
    Собирает список аргументов для системной команды ssh, включая:
    - порт, ключ, StrictHostKeyChecking + fail-fast параметры
    - user@host
    - удалённую команду: export VAR=...; cd ...; bash -lc '<script>'
    """
    user = host_cfg["user"]
    host = host_cfg["host"]
    login = f"{user}@{host}" if user else host

    ssh_args = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",      # быстрый таймаут коннекта
        "-o", "ServerAliveInterval=5", # keepalive
        "-o", "ServerAliveCountMax=1", # одна попытка
        "-p", str(host_cfg["port"]),
    ]

    shk = host_cfg.get("strict_host_key_checking")
    if shk:
        ssh_args += ["-o", f"StrictHostKeyChecking={shk}"]

    identity = host_cfg.get("identity_file")
    if identity:
        ssh_args += ["-i", os.path.expanduser(identity)]

    # формируем удалённую команду
    exports = ""
    if env:
        parts = []
        for k, v in env.items():
            if v is None:
                continue
            parts.append(f"export {k}={shlex.quote(str(v))}")
        if parts:
            exports = "; ".join(parts) + "; "

    cd_part = f"cd {shlex.quote(cwd)} && " if cwd else ""
    remote_shell = f"{exports}{cd_part}bash -lc {shlex.quote(run_script)}"

    return ssh_args + [login, "--", remote_shell]


# ----------------------------- common helpers ------------------------------

def _sh_quote(s: str) -> str:
    return shlex.quote(s)

# Эвристика "пишущих/изменяющих" команд (для оценки риска и read-only docker fs)
_WRITE_LIKE_PATTERNS = [
    r">>\s*",
    r">\s*(?!/?dev/null)",
    r"\btee\b",
    r"\btouch\b",
    r"\btruncate\b",
    r"\bmkdir\b",
    r"\brmdir\b",
    r"\bmv\b",
    r"\bcp\b",
    r"\bsed\b.*\s-i\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bln\b",
    r"\bwget\b.*\s-(O|output-document)\b",
    r"\bcurl\b.*\s-(o|O)\b",
    r"\bapt(-get)?\b",
    r"\bdpkg\b",
    r"\byum\b",
    r"\bdnf\b",
    r"\bpip3?\b",
    r"\bdd\b",
    r"\bmkfs\b",
    r"\bmount\b",
    r"\bumount\b",
    r"\brm\b",
    r"^\s*sudo\b",
    r"\bsoftwareupdate\b",
    r"\breboot\b",
]

def _is_write_like(cmd: str) -> bool:
    c = (cmd or "").strip()
    for pat in _WRITE_LIKE_PATTERNS:
        if re.search(pat, c, flags=re.IGNORECASE):
            return True
    return False


# ----------------------- docker command builder ----------------------------

def build_docker_run_cmd(
    inner_cmd: str,
    limits: ResourceLimits,
    image: str = SANDBOX_IMAGE,
    tmpfs_mb: int = 64,
) -> str:
    flags: List[str] = ["--rm"]
    flags += ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    flags += ["-e", "LANG=C.UTF-8"]

    if limits.pids:
        flags += ["--pids-limit", str(limits.pids)]
    if limits.memory_mb:
        flags += ["--memory", f"{limits.memory_mb}m", "--memory-swap", f"{limits.memory_mb}m"]
    if limits.cpus:
        flags += ["--cpus", str(limits.cpus)]

    flags += ["--network", "none" if limits.no_network else "bridge"]
    flags += ["--tmpfs", f"/tmp:rw,noexec,nosuid,size={tmpfs_mb}m"]

    # Для read-only команд делаем rootfs readonly
    if not _is_write_like(inner_cmd):
        flags += ["--read-only"]

    docker_cmd = (
        "docker run "
        + " ".join(flags)
        + f" {image} /bin/bash -lc "
        + _sh_quote(inner_cmd)
    )
    return docker_cmd


# ---------------------------- low-level API --------------------------------

class ExecTarget:
    HOST = "host"
    DOCKER = "docker"

def choose_default_target(risk: str) -> str:
    # По умолчанию опасное — в докере, остальное — на хосте
    return ExecTarget.DOCKER if risk == "dangerous" else ExecTarget.HOST

def execute_with_limits(
    command: str,
    *,
    risk: str,
    target: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    docker_image: str = SANDBOX_IMAGE,
    host_mem_watch_mb: int = 1024,
):
    limits = load_limits_for_risk(risk)
    tgt = target or choose_default_target(risk)

    if tgt == ExecTarget.DOCKER:
        docker_cmd = build_docker_run_cmd(command, limits, image=docker_image)
        return run_on_host_with_limits(
            docker_cmd,
            timeout_sec=limits.timeout_sec,
            grace_kill_sec=limits.grace_kill_sec,
            cwd=cwd,
            env=env,
            mem_watch_mb=None,
        )

    # HOST
    return run_on_host_with_limits(
        command,
        timeout_sec=limits.timeout_sec,
        grace_kill_sec=limits.grace_kill_sec,
        cwd=cwd,
        env=env,
        mem_watch_mb=host_mem_watch_mb,
    )


# ----------------------------- Workflow adapter ----------------------------

def _risk_of_script(script_body: str) -> str:
    """
    Грубая классификация риска:
      - 'rm -rf /', 'reboot', 'softwareupdate', 'sudo' → dangerous
      - write_like → mutating
      - иначе → read_only
    """
    body = (script_body or "").strip()
    lowered = body.lower()

    dangerous_markers = [
        r"\brm\s+-rf\s+/\b",
        r"\breboot\b",
        r"\bshutdown\b",
        r"\bmkfs\b",
        r"\bsoftwareupdate\b",
    ]
    for pat in dangerous_markers:
        if re.search(pat, lowered):
            return "dangerous"

    if re.search(r"^\s*sudo\b", body, flags=re.IGNORECASE | re.MULTILINE):
        return "dangerous"

    if _is_write_like(body):
        return "mutating"

    return "read_only"


def execute_step_cb(step: StepSpec, ctx: WorkflowContext) -> StepRunResult:
    """
    HOST: создаём файл-скрипт и исполняем его.
    DOCKER: собираем docker run и запускаем.
    SSH: запускаем ssh-клиент на локальном хосте с теми же правилами логирования/таймаутов.
    """
    env = os.environ.copy()
    env.update(step.env or {})

    ts = int(time.time() * 1000)
    base = f".ghostcmd_{ctx.run_id}_{ctx.step_index}_{ts}"
    stdout_path = f"/tmp/{base}.out"
    stderr_path = f"/tmp/{base}.err"

    # Нормализуем переносы строк + финальный '\n'
    script_body = (step.run or "").replace("\r\n", "\n").replace("\r", "\n")
    if not script_body.endswith("\n"):
        script_body += "\n"

    risk = _risk_of_script(script_body)

    # used_target будем заполнять в ветках
    used_target: Target = Target.HOST
    rr = None  # результат раннера

    # Определяем фактическую цель запуска
    if step.target == Target.DOCKER:
        tgt = ExecTarget.DOCKER
    elif step.target == Target.HOST:
        tgt = ExecTarget.HOST
    elif step.target == Target.SSH:
        # === SSH ветка ===
        alias = getattr(step, "ssh", None)
        if not alias:
            raise RuntimeError(
                f"Шаг '{step.name}' помечен как SSH, но алиас не задан. Ожидалось: target: ssh:<alias>"
            )

        # 1) Резолвим алиас из ~/.ghostcmd/config.yml
        try:
            host_cfg = _resolve_ssh_host(alias)
        except Exception as e:
            raise RuntimeError(
                f"SSH-алиас '{alias}' не найден или задан некорректно в ~/.ghostcmd/config.yml (секция 'ssh'). "
                f"Нужно минимум: host, а опционально user/port/identity_file. Исходная ошибка: {e}"
            ) from e

        # 2) Берём env/cwd/команду
        run_text = step.run
        effective_env = step.env or {}
        effective_cwd = step.cwd

        # 3) Собираем ssh-команду (list -> str)
        try:
            ssh_cmd = _build_ssh_cmd(
                host_cfg=host_cfg,
                run_script=run_text,
                env=effective_env,
                cwd=effective_cwd,
            )
        except Exception as e:
            raise RuntimeError(f"Не удалось собрать SSH-команду: {e}") from e

        ssh_cmd_str = " ".join(shlex.quote(x) for x in ssh_cmd)

        # 4) Запускаем через общий раннер (на хосте)
        rr = execute_with_limits(
            ssh_cmd_str,
            risk="read_only",               # локально запускаем ssh-клиент — он не меняет систему
            target=ExecTarget.HOST,         # именно на хосте
            cwd=os.getcwd(),
            env=env,
            docker_image=SANDBOX_IMAGE,
        )

        # 5) Дружелюбная расшифровка типовых SSH-ошибок
        stderr_text = (getattr(rr, "stderr", "") or "")
        exit_code_dbg = int(getattr(rr, "code", getattr(rr, "exit_code", 999)))
        if exit_code_dbg != 0:
            label, hint = _classify_ssh_error(stderr_text)
            if label:
                print(f"[SSH] {label}. {hint}")

        used_target = Target.SSH

    else:
        tgt = choose_default_target(risk)

    # === HOST / DOCKER выполнение ===
    if rr is None and (step.target != Target.SSH):
        docker_image = os.environ.get("GHOSTCMD_SANDBOX_IMAGE", SANDBOX_IMAGE)
        debug_script = os.environ.get("GHOSTCMD_DEBUG_SCRIPT", "0") == "1"

        if tgt == ExecTarget.HOST:
            # --- готовим bash-скрипт на хосте ---
            script_host_path = Path("/tmp") / f"{base}.sh"
            header = "#!/usr/bin/env bash\n" + ("set -euxo pipefail\n" if debug_script else "set -eo pipefail\n")
            script_text = header + script_body
            try:
                script_host_path.write_text(script_text, encoding="utf-8", errors="replace")
                script_host_path.chmod(0o700)
                if debug_script:
                    (Path("/tmp") / f"{base}.debug.sh").write_text(script_text, encoding="utf-8", errors="replace")
            except Exception as e:
                return StepRunResult(
                    step=step, ok=False, exit_code=997, duration_sec=0.0,
                    target_used=step.target, stdout_path=None, stderr_path=None,
                    meta={"exception": True}, error=f"cannot write host script: {e}"
                )

            # Если есть sudo-команды — спросим пароль и завернём выполнение
            needs_sudo = any(re.search(r"^\s*sudo\b", line, flags=re.IGNORECASE) for line in script_body.splitlines())
            if needs_sudo:
                try:
                    print("🔐 Этот шаг требует прав sudo на хосте.")
                    sudo_pw = getpass("Пароль sudo (не сохраняется): ")
                    wrapped = f'printf "%s\\n" {_sh_quote(sudo_pw)} | sudo -S -p "" /usr/bin/env bash {_sh_quote(str(script_host_path))}'
                    command = wrapped
                except Exception as e:
                    return StepRunResult(
                        step=step, ok=False, exit_code=996, duration_sec=0.0,
                        target_used=step.target, stdout_path=None, stderr_path=None,
                        meta={"exception": True}, error=f"sudo prompt error: {e}"
                    )
            else:
                command = _sh_quote(str(script_host_path))

            rr = execute_with_limits(
                command,
                risk=risk,
                target=tgt,
                cwd=os.getcwd(),
                env=env,
                docker_image=docker_image,
            )
            used_target = Target.HOST

            # Неверный пароль sudo → считаем шаг "пропущенным", а не просто FAIL
            stderr_lower = (getattr(rr, "stderr", "") or "").lower()
            exit_code_tmp = int(getattr(rr, "code", getattr(rr, "exit_code", 999)))
            if needs_sudo and (exit_code_tmp != 0) and (
                "incorrect password" in stderr_lower
                or "a password is required" in stderr_lower
                or "password is required" in stderr_lower
            ):
                return StepRunResult(
                    step=step,
                    ok=False,
                    exit_code=exit_code_tmp,
                    duration_sec=float(getattr(rr, "duration_sec", 0.0)),
                    target_used=Target.HOST,
                    stdout_path=None,
                    stderr_path=None,
                    meta={"skipped": True, "reason": "неверный пароль sudo"},
                    error="sudo password incorrect",
                )

        else:
            # --- docker: отдаём сырой скрипт во внутренний bash ---
            rr = execute_with_limits(
                script_body,
                risk=risk,
                target=tgt,
                cwd=os.getcwd(),
                env=env,
                docker_image=docker_image,
            )
            used_target = Target.DOCKER

    # Артефакты (stdout/stderr в файлы)
    out_path, err_path = None, None
    try:
        if getattr(rr, "stdout", None):
            with open(stdout_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(rr.stdout)
            out_path = stdout_path
        if getattr(rr, "stderr", None):
            with open(stderr_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(rr.stderr)
            err_path = stderr_path
    except Exception:
        pass

    duration = float(getattr(rr, "duration_sec", 0.0))
    exit_code = int(getattr(rr, "code", getattr(rr, "exit_code", 999)))
    ok = (exit_code == 0)

    return StepRunResult(
        step=step,
        ok=ok,
        exit_code=exit_code,
        duration_sec=duration,
        target_used=used_target,
        stdout_path=out_path,
        stderr_path=err_path,
        meta={"limits": True, "risk": risk, "target": (ExecTarget.DOCKER if used_target == Target.DOCKER else ExecTarget.HOST if used_target == Target.HOST else "ssh")},
        error=None if ok else "non-zero exit",
    )
