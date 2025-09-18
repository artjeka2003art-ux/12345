# core/limits.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from pathlib import Path
import yaml

@dataclass
class ResourceLimits:
    timeout_sec: int
    grace_kill_sec: int
    cpus: Optional[float] = None       # для Docker
    memory_mb: Optional[int] = None    # для Docker
    pids: Optional[int] = None         # для Docker
    no_network: bool = False           # для Docker

def _config_path() -> Path:
    # .../GHOSTCMD/core/limits.py -> подняться в корень и найти config/limits.yml
    return Path(__file__).resolve().parents[1] / "config" / "limits.yml"

def load_limits_for_risk(risk: str) -> ResourceLimits:
    """
    Прочитать config/limits.yml и вернуть лимиты для 'read_only' | 'mutating' | 'dangerous'
    """
    path = _config_path()
    data = yaml.safe_load(path.read_text())
    if "limits" not in data or risk not in data["limits"]:
        raise KeyError(f"В {path} нет секции limits.{risk}")
    d = data["limits"][risk]
    return ResourceLimits(
        timeout_sec=int(d.get("timeout_sec", 60)),
        grace_kill_sec=int(d.get("grace_kill_sec", 3)),
        cpus=float(d["cpus"]) if d.get("cpus") is not None else None,
        memory_mb=int(d["memory_mb"]) if d.get("memory_mb") is not None else None,
        pids=int(d["pids"]) if d.get("pids") is not None else None,
        no_network=bool(d.get("no_network", False)),
    )
    