"""Install a pinned CPU speech reader in this project's existing isolated venv.

Only public package/model downloads occur here. No WeChat data is opened.
The actual transcription command has no downloader or cloud client.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / '.runtime/voice'
MODEL = RUNTIME / 'sensevoice-2024-07-17'
MODEL_URL = 'https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/assets/288366523'
MODEL_SHA = '7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e'
MODEL_BYTES = 163002883
MODEL_EXPANDED_LIMIT = 384 * 1024**2
WHEELS = (
    {
        'package': 'sherpa-onnx',
        'version': '1.13.7',
        'filename': 'sherpa_onnx-1.13.7-cp312-cp312-win_amd64.whl',
        'bytes': 2282452,
        'sha256': 'd6b8adb339e0e89cf9914de610c85d2d5e4715b09f566eaf796a92eb72c014d1',
        'url': 'https://files.pythonhosted.org/packages/46/3d/7b5cdf6a1c01080b6c579134fba082c4fc35b13bc6e35d85285ab8fc9380/sherpa_onnx-1.13.7-cp312-cp312-win_amd64.whl',
    },
    {
        'package': 'sherpa-onnx-core',
        'version': '1.13.7',
        'filename': 'sherpa_onnx_core-1.13.7-py3-none-win_amd64.whl',
        'bytes': 16526006,
        'sha256': 'cbcb78ef8a3bb74acf0b9d0a8715e9b857491be8e2b1e8f589f09ecf3d2f2a0b',
        'url': 'https://files.pythonhosted.org/packages/20/87/2ac78c15d4b6bc8ba663b4fef8a9d01ef32a09c336b828d27af3e4962116/sherpa_onnx_core-1.13.7-py3-none-win_amd64.whl',
    },
    {
        'package': 'silk-python',
        'version': '0.2.8',
        'filename': 'silk_python-0.2.8-cp312-cp312-win_amd64.whl',
        'bytes': 336992,
        'sha256': 'b9bb030589150e0d91f8148971eebf6f9211e6839af64dd39b26b9802be242b0',
        'url': 'https://files.pythonhosted.org/packages/58/56/67d9a94be90df5d0d80e1bbc6da0fc142e01777fb7c1e7c2c27047a90ee0/silk_python-0.2.8-cp312-cp312-win_amd64.whl',
    },
    {
        'package': 'numpy',
        'version': '2.2.6',
        'filename': 'numpy-2.2.6-cp312-cp312-win_amd64.whl',
        'bytes': 12614190,
        'sha256': 'c1f9540be57940698ed329904db803cf7a402f3fc200bfe599334c9bd84a40b2',
        'url': 'https://files.pythonhosted.org/packages/36/fa/8c9210162ca1b88529ab76b41ba02d433fd54fecaf6feb70ef9f124683f1/numpy-2.2.6-cp312-cp312-win_amd64.whl',
    },
    {
        'package': 'cffi',
        'version': '2.0.0',
        'filename': 'cffi-2.0.0-cp312-cp312-win_amd64.whl',
        'bytes': 183557,
        'sha256': 'da68248800ad6320861f129cd9c1bf96ca849a2771a59e0344e88681905916f5',
        'url': 'https://files.pythonhosted.org/packages/f8/ed/13bd4418627013bec4ed6e54283b1959cf6db888048c7cf4b4c3b5b36002/cffi-2.0.0-cp312-cp312-win_amd64.whl',
    },
    {
        'package': 'pycparser',
        'version': '2.23',
        'filename': 'pycparser-2.23-py3-none-any.whl',
        'bytes': 118140,
        'sha256': 'e5c6e8d3fbad53479cab09ac03729e0a9faf2bee3db8208a550daf5af81a5934',
        'url': 'https://files.pythonhosted.org/packages/a0/e3/59cd50310fc9b59512193629e1984c1f95e5c8ae6e5d8c69532ccc65a7fe/pycparser-2.23-py3-none-any.whl',
    },
)
PACKAGES = {item['package']: item['version'] for item in WHEELS}
IMPORTS = ('sherpa_onnx', 'pysilk', 'numpy', 'cffi', 'pycparser')
CURRENT_STAGE = 'idle'


def set_stage(stage):
    global CURRENT_STAGE
    CURRENT_STAGE = stage


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        while block := f.read(2 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def runtime_status(verify=True):
    packages = True
    for package, version in PACKAGES.items():
        try:
            packages &= importlib.metadata.version(package) == version
        except importlib.metadata.PackageNotFoundError:
            packages = False
    if packages:
        try:
            for module in IMPORTS:
                importlib.import_module(module)
        except Exception:
            packages = False
    try:
        manifest = json.loads((MODEL / 'verified.json').read_text('utf-8'))
        model = manifest['archive_sha256'] == MODEL_SHA
        for name in ('model.int8.onnx', 'tokens.txt'):
            path = MODEL / name
            model &= path.is_file() and not path.is_symlink()
            if verify and model:
                model &= digest(path) == manifest['files'][name]
    except (OSError, ValueError, KeyError, TypeError):
        model = False
    return {'ready': bool(packages and model), 'packages_ready': bool(packages),
            'model_ready': bool(model), 'engine': 'sherpa-onnx',
            'model': 'SenseVoice int8 2024-07-17', 'device': 'cpu'}


def download(url, target, sha, limit, expected_size=None):
    if (target.exists() and
            (expected_size is None or target.stat().st_size == expected_size) and
            digest(target) == sha):
        return
    part = target.with_suffix(target.suffix + '.part')
    if (part.exists() and expected_size is not None and
            part.stat().st_size == expected_size and digest(part) == sha):
        os.replace(part, target)
        return
    offset = part.stat().st_size if part.exists() else 0
    if expected_size is not None and offset >= expected_size:
        part.unlink(missing_ok=True)
        offset = 0
    headers = {'User-Agent': 'SheLoveMe-LocalVoiceSetup/1'}
    if url.startswith('https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/assets/'):
        headers['Accept'] = 'application/octet-stream'
    if expected_size is not None:
        # Explicit ranges also avoid stalled full-object CDN responses on
        # some local networks; the final size and SHA remain mandatory.
        headers['Range'] = f'bytes={offset}-{expected_size - 1}'
    elif offset:
        headers['Range'] = f'bytes={offset}-'
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=45) as response:
            if not response.geturl().startswith('https://'):
                raise ValueError('download_redirect_invalid')
            status = getattr(response, 'status', None)
            content_range = response.headers.get('Content-Range', '')
            resumed = bool(
                offset and status == 206 and
                content_range.startswith(f'bytes {offset}-')
            )
            if offset and status not in (200, 206):
                raise ValueError('download_resume_rejected')
            if offset and status == 206 and not resumed:
                raise ValueError('download_content_range_invalid')
            mode = 'ab' if resumed else 'wb'
            total = offset if resumed else 0
            with part.open(mode) as f:
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > limit:
                        raise ValueError('download_size_limit')
                    if expected_size is not None and total > expected_size:
                        raise ValueError('download_size_mismatch')
                    f.write(block)
        if expected_size is not None and total != expected_size:
            raise ValueError('download_size_mismatch')
        if digest(part) != sha:
            raise ValueError('download_checksum_mismatch')
        os.replace(part, target)
    except ValueError:
        part.unlink(missing_ok=True)
        raise


def extract_member(tar, member, target):
    source = tar.extractfile(member)
    if source is None:
        raise ValueError('model_archive_member_invalid')
    part = target.with_suffix(target.suffix + '.part')
    h = hashlib.sha256()
    total = 0
    try:
        with source, part.open('wb') as output:
            while block := source.read(1024 * 1024):
                total += len(block)
                if total > member.size or total > MODEL_EXPANDED_LIMIT:
                    raise ValueError('model_archive_member_too_large')
                h.update(block)
                output.write(block)
        if total != member.size:
            raise ValueError('model_archive_member_size_mismatch')
        os.replace(part, target)
        return h.hexdigest()
    finally:
        part.unlink(missing_ok=True)


def model_smoke():
    code = '''
import sherpa_onnx
import sys
sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=sys.argv[1], tokens=sys.argv[2], num_threads=1,
    sample_rate=16000, feature_dim=80, provider="cpu",
    language="zh", use_itn=True,
)
'''
    result = subprocess.run(
        [sys.executable, '-c', code, str(MODEL / 'model.int8.onnx'),
         str(MODEL / 'tokens.txt')],
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        raise ValueError('model_smoke_failed')


def install():
    set_stage('checking_environment')
    if REPO.drive.upper() == 'C:' or Path(sys.prefix).resolve() != (REPO / '.venv').resolve():
        raise ValueError('project_venv_on_non_c_drive_required')
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12):
        raise ValueError('pinned_runtime_requires_windows_python312')
    set_stage('preparing_runtime')
    RUNTIME.mkdir(parents=True, exist_ok=True)
    wheels = RUNTIME / 'wheels'
    wheels.mkdir(exist_ok=True)
    locked, wheel_paths = [], []
    for item in WHEELS:
        set_stage(f'downloading_wheel:{item["package"]}')
        if (Path(item['filename']).name != item['filename'] or
                not item['url'].startswith('https://files.pythonhosted.org/')):
            raise ValueError('package_host_invalid')
        target = wheels / item['filename']
        download(item['url'], target, item['sha256'], 180 * 1024**2,
                 expected_size=item['bytes'])
        wheel_paths.append(str(target))
        locked.append({key: item[key] for key in
                       ('package', 'version', 'filename', 'bytes', 'sha256')})
    (RUNTIME / 'wheels-lock.json').write_text(json.dumps(locked, indent=2), encoding='utf-8')
    print(json.dumps({'state': 'installing_pinned_packages', 'packages': len(locked)}), flush=True)
    set_stage('installing_packages')
    env = dict(os.environ, TEMP=str(RUNTIME), TMP=str(RUNTIME), PIP_NO_CACHE_DIR='1')
    result = subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'install', '--no-index',
                             '--no-deps', '--no-cache-dir', *wheel_paths],
                            env=env, capture_output=True, timeout=300)
    if result.returncode:
        raise ValueError('package_install_failed')
    print(json.dumps({'state': 'downloading_model', 'approx_megabytes': 163}), flush=True)
    set_stage('downloading_model')
    archive = RUNTIME / 'sensevoice-int8-2024-07-17.tar.bz2'
    download(MODEL_URL, archive, MODEL_SHA, 170 * 1024**2,
             expected_size=MODEL_BYTES)
    MODEL.mkdir(exist_ok=True)
    verified = MODEL / 'verified.json'
    verified.unlink(missing_ok=True)
    set_stage('extracting_model')
    names = {'model.int8.onnx', 'tokens.txt'}
    hashes = {}
    with tarfile.open(archive, 'r:bz2') as tar:
        members = [m for m in tar.getmembers() if Path(m.name).name in names]
        if len(members) != 2 or {Path(m.name).name for m in members} != names:
            raise ValueError('model_archive_layout_invalid')
        if sum(member.size for member in members) > MODEL_EXPANDED_LIMIT:
            raise ValueError('model_archive_too_large')
        for member in members:
            if not member.isfile() or member.size > 300 * 1024**2:
                raise ValueError('model_archive_member_invalid')
            name = Path(member.name).name
            target = MODEL / name
            hashes[name] = extract_member(tar, member, target)
    set_stage('loading_model')
    model_smoke()
    verified.write_text(json.dumps({'archive_sha256': MODEL_SHA, 'files': hashes}), 'utf-8')
    set_stage('verifying_runtime')
    return runtime_status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    try:
        set_stage('installing' if args.install else 'checking_status')
        result = install() if args.install else runtime_status()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['ready'] else 1
    except Exception as exc:
        print(json.dumps({'ready': False, 'code': 'voice_runtime_setup_failed',
                          'stage': CURRENT_STAGE,
                          'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
