# core/ghost_logging.py
from __future__ import annotations
import json, re, os, io
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict

# Папка ~/.ghostcmd/logs
def _logs_dir() -> Path:
    base = Path(os.path.expanduser("~")) / ".ghostcmd" / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),               # OpenAI-подобные ключи
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),              # GitHub PAT
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*([^\s\"']+)"),
    re.compile(r"(?i)authorization:\s*bearer\s+[^\s]+"),
]

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _mask_string(s: str) -> str:
    masked = s
    for pat in _SECRET_PATTERNS:
        masked = pat.sub("***", masked)
    return masked

def _mask_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_obj(v) for v in obj]
    if isinstance(obj, (str, bytes)):
        if isinstance(obj, bytes):
            try:
                obj = obj.decode("utf-8", "replace")
            except Exception:
                obj = repr(obj)
        return _mask_string(obj)
    return obj

def preview_bytes(b: bytes, limit: int = 4096) -> str:
    """Обрезает stdout/stderr до разумного размера, чтобы не раздувать лог."""
    if b is None:
        return ""
    if len(b) > limit:
        return (b[:limit].decode("utf-8", "replace")) + f"\n...[truncated {len(b)-limit} bytes]"
    return b.decode("utf-8", "replace")

class JsonlLogger:
    """Запись событий в logs/YYYY-MM-DD.jsonl с маскированием секретов."""
    def __init__(self, dirpath: Path | None = None):
        self.dir = dirpath or _logs_dir()

    def write(self, event: Dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("ts", now_utc_iso())
        safe = _mask_obj(event)
        date = event["ts"][:10]  # YYYY-MM-DD
        path = self.dir / f"{date}.jsonl"
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(safe, ensure_ascii=False) + "\n")

# Удобный синглтон
logger = JsonlLogger()
