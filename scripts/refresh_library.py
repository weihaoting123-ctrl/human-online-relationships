"""Idempotent local metadata projection refresh; never edits source messages."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def main():
    try:
        from dashboard import app
        # Follow the same configured data root as the local dashboard.
        snapshot = app.library_service().snapshot()
        print(json.dumps({'status':'ok','health':snapshot['health']}, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError):
        print(json.dumps({'status':'error','error_code':'LIBRARY_REFRESH_FAILED'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
