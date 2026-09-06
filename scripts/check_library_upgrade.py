"""Local upgrade invariant check; only numeric/digest aggregates reach stdout.

Snapshots source file identity and hashes message/AI-cache bytes. Media are not
rehash-read during a UI migration; their stat/inode baseline is compared and the
independent backup verifier remains the content-integrity authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from dashboard.library.repository import _guard, _mkdir

PROTECTED = ('data/contacts','data/raw','data/private/wechat-archive',
             'data/private/wechat-voice','data/exports','data/private/scoped-ai')
BASELINE = REPO / 'data/private/library-upgrade/baseline.json'


def inventory():
    result = {}
    for relative in PROTECTED:
        root = REPO / relative
        if not root.exists():
            continue
        _guard(root, directory=True)
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name not in ('tmp','.tmp','temp')]
            for name in files:
                if name.endswith(('.lock','-shm','-wal','-journal')):
                    continue
                path = Path(folder) / name
                _guard(path)
                before = path.stat()
                row = [before.st_size,before.st_mtime_ns,before.st_ino]
                if name == 'messages.json' or relative == 'data/private/scoped-ai':
                    with path.open('rb') as handle:
                        digest = hashlib.sha256()
                        while chunk := handle.read(2 * 1024 * 1024):
                            digest.update(chunk)
                        row.append(digest.hexdigest())
                    after = path.stat()
                    if (before.st_size,before.st_mtime_ns,before.st_ino) != (after.st_size,after.st_mtime_ns,after.st_ino):
                        raise RuntimeError('source changed during check')
                result[path.relative_to(REPO).as_posix()] = row
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture',action='store_true')
    args=parser.parse_args()
    try:
        current=inventory()
        _mkdir(BASELINE.parent)
        _guard(BASELINE,missing=True)
        if args.capture:
            # Exclusive creation protects the original before-upgrade evidence.
            with BASELINE.open('x',encoding='utf-8') as handle:
                json.dump(current,handle,ensure_ascii=False)
            result={'status':'captured','files':len(current),'hashed_files':sum(len(row)==4 for row in current.values())}
        else:
            baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
            changed=sum(key in current and current[key]!=row for key,row in baseline.items())
            added=len(set(current)-set(baseline))
            removed=len(set(baseline)-set(current))
            result={'status':'ok' if not (changed or added or removed) else 'changed',
                    'files':len(current),'changed':changed,'added':added,'removed':removed,
                    'hashed_files':sum(len(row)==4 for row in current.values())}
        print(json.dumps(result))
        return 0 if result['status'] in ('captured','ok') else 1
    except (OSError,RuntimeError,ValueError):
        print(json.dumps({'status':'error','error_code':'UPGRADE_CHECK_UNAVAILABLE'}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
