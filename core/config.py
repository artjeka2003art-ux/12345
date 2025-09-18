# core/config.py
from __future__ import annotations

from typing import Dict, Optional
import os
import yaml

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.ghostcmd/config.yml")


def _read_yaml(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except FileNotFoundError:
        return {}
    except Exception:
        # не валим выполнение — вернём пустой конфиг
        return {}


def load_user_config(path: Optional[str] = None) -> dict:
    return _read_yaml(path or DEFAULT_CONFIG_PATH)


def load_ssh_hosts(path: Optional[str] = None) -> Dict[str, dict]:
    """
    Возвращает словарь алиас -> конфиг, например:
    {
      "prod": {"host": "1.2.3.4", "user": "ubuntu", "port": 22, "identity_file": "~/.ssh/id_rsa",
               "strict_host_key_checking": "accept-new"}  # accept-new|yes|no
    }
    """
    cfg = load_user_config(path)
    ssh = cfg.get("ssh") or {}
    if not isinstance(ssh, dict):
        return {}
    out: Dict[str, dict] = {}
    for name, item in ssh.items():
        if isinstance(item, dict) and ("host" in item):
            out[name] = dict(item)
    return out

    