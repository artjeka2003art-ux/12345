# core/exec_limits.py
from __future__ import annotations
import os, signal, time, tempfile, subprocess
from dataclasses import dataclass
from typing import Optional, Dict
import psutil, contextlib

@dataclass
class RunResult:
    code: int
    stdout: str
    stderr: str
    duration_sec: float
    killed: bool
    kill_reason: str  # "none" | "timeout" | "memory_exceeded"

def _collect_tree_rss_bytes(pid: int) -> int:
    """Суммарный RSS процесса и всех детей (в байтах)."""
    try:
        p = psutil.Process(pid)
    except psutil.Error:
        return 0
    total = 0
    try:
        total += p.memory_info().rss
        for ch in p.children(recursive=True):
            try:
                total += ch.memory_info().rss
            except psutil.Error:
                pass
    except psutil.Error:
        pass
    return total
def _terminate_tree_psutil(pid: int, sig: int) -> None:
    """Отправить сигнал процессу и всем потомкам через psutil."""
    with contextlib.suppress(psutil.Error):
        p = psutil.Process(pid)
        # дети сначала
        for ch in p.children(recursive=True):
            with contextlib.suppress(psutil.Error):
                os.kill(ch.pid, sig)
        # потом сам
        with contextlib.suppress(psutil.Error):
            os.kill(p.pid, sig)
def _terminate_group(pid: int, grace_sec: int) -> None:
    """
    Пытаемся убить группу процессов:
    1) killpg(SIGTERM) → подождать → killpg(SIGKILL)
    2) если killpg недоступен (PermissionError) — fallback на psutil-проход по дереву.
    """
    # 1) Путь через killpg
    try:
        pgid = os.getpgid(pid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except PermissionError:
            # нет прав послать группе — падаем в fallback
            raise
        # ждём мягкого завершения
        t0 = time.time()
        while time.time() - t0 < grace_sec:
            try:
                os.killpg(pgid, 0)  # проверка «жив ли кто-то в группе»
            except ProcessLookupError:
                return  # группа умерла
            except PermissionError:
                # если даже проверка запрещена — выйдем на fallback
                break
            time.sleep(0.1)
        # жёстко добиваем группу
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError):
        # 2) Fallback: обрабатываем дерево через psutil
        _terminate_tree_psutil(pid, signal.SIGTERM)
        t0 = time.time()
        while time.time() - t0 < grace_sec:
            with contextlib.suppress(psutil.Error):
                if not psutil.pid_exists(pid):
                    return
            time.sleep(0.1)
        _terminate_tree_psutil(pid, signal.SIGKILL)

def run_on_host_with_limits(
    command: str,
    *,
    timeout_sec: int,
    grace_kill_sec: int,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    mem_watch_mb: Optional[int] = None,  # если None — не следим за памятью
) -> RunResult:
    """
    Запускает команду на хосте с:
      - тайм-аутом по wall-clock времени,
      - корректным завершением (SIGTERM -> ожидание -> SIGKILL),
      - опциональным сторожем памяти по суммарному RSS дерева процессов.
    Stdout/stderr пишем во временные файлы, чтобы не зависнуть на пайпах.
    """
    t0 = time.time()
    killed = False
    kill_reason = "none"

    with tempfile.TemporaryFile() as f_out, tempfile.TemporaryFile() as f_err:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            env=env,
            stdout=f_out,
            stderr=f_err,
            preexec_fn=os.setsid,  # создаём свою группу процессов
            text=False,
        )

        try:
            while True:
                ret = proc.poll()
                if ret is not None:
                    break

                # тайм-аут
                if (time.time() - t0) > timeout_sec:
                    killed = True
                    kill_reason = "timeout"
                    _terminate_group(proc.pid, grace_kill_sec)
                    proc.wait()
                    break

                # сторож памяти
                if mem_watch_mb is not None:
                    rss = _collect_tree_rss_bytes(proc.pid)
                    if rss > mem_watch_mb * 1024 * 1024:
                        killed = True
                        kill_reason = "memory_exceeded"
                        _terminate_group(proc.pid, grace_kill_sec)
                        proc.wait()
                        break

                time.sleep(0.1)
        finally:
            # читаем вывод
            f_out.seek(0)
            f_err.seek(0)
            stdout = f_out.read().decode(errors="replace")
            stderr = f_err.read().decode(errors="replace")

    code = proc.returncode if proc.returncode is not None else -9
    return RunResult(
        code=code,
        stdout=stdout,
        stderr=stderr,
        duration_sec=round(time.time() - t0, 3),
        killed=killed,
        kill_reason=kill_reason,
    )