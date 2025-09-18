# core/history.py
from __future__ import annotations
import os, sqlite3, time
from pathlib import Path
from typing import Optional, Dict, Any
from core.ghost_logging import logger, now_utc_iso
from typing import Optional, Dict, Any
DB_PATH = Path(os.path.expanduser("~")) / ".ghostcmd" / "history.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,                -- когда создана запись (draft)
  user_input TEXT NOT NULL,            -- запрос на человеческом
  plan_cmd TEXT,                       -- предложенная команда (bash)
  explanation TEXT,                    -- пояснение
  risk TEXT CHECK(risk IN ('green','yellow','red','blocked')) NOT NULL,
  exec_target TEXT CHECK(exec_target IN ('host','docker','ssh','dry')) NOT NULL,
  timeout_sec INTEGER,
  exit_code INTEGER,
  bytes_stdout INTEGER,
  bytes_stderr INTEGER,
  duration_ms INTEGER,
  workflow_id TEXT,
  host_alias TEXT,
  sandbox INTEGER DEFAULT 0            -- 0/1
);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  command_id INTEGER NOT NULL,
  kind TEXT CHECK(kind IN ('stdout','stderr','file','json')) NOT NULL,
  path TEXT,
  preview TEXT,
  FOREIGN KEY(command_id) REFERENCES commands(id)
);

CREATE INDEX IF NOT EXISTS idx_cmd_ts ON commands(ts_utc DESC);
"""

def _ensure_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db() -> None:
    with _ensure_db() as con:
        con.executescript(_SCHEMA)

def create_command_event(
    user_input: str,
    plan_cmd: str,
    explanation: str,
    risk: str,
    exec_target: str,
    timeout_sec: Optional[int] = None,
    workflow_id: Optional[str] = None,
    host_alias: Optional[str] = None,
    sandbox: bool = False,
) -> int:
    """Создаёт черновую запись (draft), возвращает id."""
    ts = now_utc_iso()
    with _ensure_db() as con:
        cur = con.execute(
            """
            INSERT INTO commands(ts_utc,user_input,plan_cmd,explanation,risk,exec_target,
                                 timeout_sec,exit_code,bytes_stdout,bytes_stderr,duration_ms,
                                 workflow_id,host_alias,sandbox)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (ts, user_input, plan_cmd, explanation, risk, exec_target,
             timeout_sec, None, None, None, None, workflow_id, host_alias, int(sandbox)),
        )
        cmd_id = cur.lastrowid

    # JSONL лог о планировании
    logger.write({
        "kind": "draft",
        "id": cmd_id,
        "ts": ts,
        "user_input": user_input,
        "plan_cmd": plan_cmd,
        "explanation": explanation,
        "risk": risk,
        "target": exec_target,
        "timeout_sec": timeout_sec,
        "workflow_id": workflow_id,
        "host_alias": host_alias,
        "sandbox": sandbox,
    })
    return cmd_id

def finalize_command_event(
    command_id: int,
    exit_code: int,
    bytes_stdout: int,
    bytes_stderr: int,
    duration_ms: int,
    error: Optional[str] = None,
    exec_target_final: Optional[str] = None,
) -> None:
    """Дописать результаты после выполнения. При желании обновляем exec_target."""
    with _ensure_db() as con:
        if exec_target_final:
            con.execute(
                """
                UPDATE commands
                   SET exit_code=?,
                       bytes_stdout=?,
                       bytes_stderr=?,
                       duration_ms=?,
                       exec_target=?
                 WHERE id=?
                """,
                (exit_code, bytes_stdout, bytes_stderr, duration_ms, exec_target_final, command_id),
            )
        else:
            con.execute(
                """
                UPDATE commands
                   SET exit_code=?, bytes_stdout=?, bytes_stderr=?, duration_ms=?
                 WHERE id=?
                """,
                (exit_code, bytes_stdout, bytes_stderr, duration_ms, command_id),
            )

    logger.write({
        "kind": "final",
        "id": command_id,
        "ts": now_utc_iso(),
        "exit_code": exit_code,
        "bytes_stdout": bytes_stdout,
        "bytes_stderr": bytes_stderr,
        "duration_ms": duration_ms,
        "error": error,
        "target": exec_target_final,
    })

def add_artifact(command_id: int, kind: str, path: Optional[str] = None, preview: Optional[str] = None) -> None:
    with _ensure_db() as con:
        con.execute(
            "INSERT INTO artifacts(command_id,kind,path,preview) VALUES(?,?,?,?)",
            (command_id, kind, path, preview),
        )

def recent(limit: int = 20) -> list[dict]:
    """Вернёт последние записи как словари (для CLI-команды history позже)."""
    with _ensure_db() as con:
        cur = con.execute(
            "SELECT id, ts_utc, user_input, plan_cmd, risk, exec_target, exit_code, duration_ms FROM commands ORDER BY ts_utc DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

def get_command(command_id: int) -> Optional[Dict[str, Any]]:
    """Вернуть одну запись из commands по id, как словарь."""
    with _ensure_db() as con:
        cur = con.execute(
            "SELECT id, ts_utc, user_input, plan_cmd, explanation, risk, exec_target, timeout_sec, exit_code, bytes_stdout, bytes_stderr, duration_ms, workflow_id, host_alias, sandbox FROM commands WHERE id=?",
            (command_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

def artifacts_for_command(command_id: int) -> list[Dict[str, Any]]:
    """Вернуть артефакты (stdout/stderr/file/json) для команды id, в порядке вставки."""
    with _ensure_db() as con:
        cur = con.execute(
            "SELECT id, kind, path, preview FROM artifacts WHERE command_id=? ORDER BY id ASC",
            (command_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]