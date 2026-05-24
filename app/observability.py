import json
import os
import time
from pathlib import Path

LOG_PATH = Path(os.getenv("TRACE_LOG_PATH", "logs/traces.jsonl"))
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def write_trace(event: dict):
    event = dict(event)
    event['ts_epoch_ms'] = int(time.time() * 1000)
    with LOG_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(event, ensure_ascii=True) + '\n')


def read_recent(limit: int = 100):
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text(encoding='utf-8').splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out
