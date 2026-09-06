"""Module-aware entrypoint for the existing local daily workflow.

No cloud analysis stage exists here. Only new stage admission is controlled;
settings never interrupt a running subprocess or remove its archived output.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dashboard.modules import ModuleDisabledError, ModuleRegistry


STEPS = (
    ('sync', 'messages', 'sync_all_wechat.py', ('--sync', '--scheduled')),
    ('media', 'media', 'archive_wechat_local.py', ('--archive',)),
    ('voice', 'voice', 'archive_wechat_voice.py', ('--archive',)),
    ('voice', 'transcripts', 'transcribe_wechat_voice.py', ('--max-seconds', '900')),
    ('sync', 'classification', 'classify_wechat_local.py', ()),
    (None, 'library', 'refresh_library.py', ()),
    ('backup', 'backup', 'backup_wechat_local.py', ('--backup',)),
)


def run_pipeline(project_root=REPO, *, registry=None, runner=subprocess.run):
    root = Path(project_root).resolve()
    results = []
    # The backup/sync source allowlists are project-relative. A separate data
    # override would split module settings from those sources; refuse rather
    # than silently use another DB's default-enabled policy.
    configured = Path(os.environ.get('SHE_LOVE_ME_DATA_DIR', root / 'data')).resolve()
    if configured != root / 'data':
        return {'status':'error','error_code':'DATA_ROOT_MISMATCH','steps':[]}
    try:
        if registry is None:
            from dashboard.library import LibraryRepository
            registry = ModuleRegistry(LibraryRepository(root / 'data'))
        # Validate settings before admitting even the core projection refresh.
        try:
            registry.require('sync')
        except ModuleDisabledError:
            pass
        for module_id, step_id, script, args in STEPS:
            try:
                if module_id:
                    registry.require(module_id)
            except ModuleDisabledError:
                results.append({'id':step_id,'state':'skipped','reason':'module_disabled'})
                continue
            command = [sys.executable, '-X', 'utf8', str(root / 'scripts' / script), *args]
            try:
                outcome = runner(command, cwd=root, capture_output=True)
                results.append({'id':step_id,'state':'completed' if outcome.returncode == 0 else 'failed',
                                'exit_code':outcome.returncode})
            except OSError:
                results.append({'id':step_id,'state':'failed','error_code':'PROCESS_UNAVAILABLE'})
        return {'status':'error' if any(item['state']=='failed' for item in results) else 'ok','steps':results}
    except (OSError, RuntimeError, ValueError):
        return {'status':'error','error_code':'MODULE_SETTINGS_UNAVAILABLE','steps':results}


def main():
    if REPO.drive.casefold() == 'c:':
        print(json.dumps({'status':'error','error_code':'INACTIVE_PROJECT'}))
        return 1
    result = run_pipeline()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status']=='ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
