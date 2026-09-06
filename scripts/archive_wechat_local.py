"""Offline, content-addressed archive of one local WeChat account.

Original files are opened only for ordinary reads. No network, client control,
database decryption, or source deletion is performed. stdout is aggregate-only.
The private catalog preserves every source mapping and all observed versions.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import stat
import sys
import tempfile
import time
import uuid
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRIVATE = REPO / 'data/private/wechat-archive'
EXPORT = REPO / 'data/exports/wechat-media'
STATUS = REPO / 'data/archive-status.json'
BUCKETS = ('db_storage', 'msg', 'cache', 'FileStorage', 'Msg')
MAX_IMAGE_BYTES = 128 * 1024 * 1024
IMAGE_MAGIC = (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a', b'RIFF', b'BM')
V2_MAGIC = b'\x07\x08V2\x08\x07'
V1_MAGIC = b'\x07\x08V1\x08\x07'


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def account_root():
    import sync_all_wechat as sync
    account, base = sync.discover_unique_account()
    root = base if base.name == account else base / account
    if root.is_symlink() or not root.is_dir() or root.name != account:
        raise ValueError('account_root_invalid')
    if not any((root / name).is_dir() for name in BUCKETS):
        raise ValueError('account_layout_missing')
    return root.resolve()


def is_link(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def inventory(root):
    """Never follow a reparse point, including at a bucket boundary."""
    files, skipped = [], 0
    seen_buckets = set()
    for name in BUCKETS:
        folder = root / name
        if not folder.exists():
            continue
        bucket_identity = os.path.normcase(str(folder.resolve()))
        if bucket_identity in seen_buckets:
            continue
        seen_buckets.add(bucket_identity)
        if is_link(folder):
            skipped += 1
            continue
        for current, dirs, names in os.walk(folder, followlinks=False):
            safe_dirs = []
            for item in dirs:
                p = Path(current) / item
                if is_link(p):
                    skipped += 1
                else:
                    safe_dirs.append(item)
            dirs[:] = safe_dirs
            for item in names:
                p = Path(current) / item
                try:
                    if is_link(p) or not p.is_file() or not p.resolve().is_relative_to(root):
                        skipped += 1
                        continue
                    s = p.stat()
                    files.append((p, s.st_size, name))
                except OSError:
                    skipped += 1
    return files, skipped


@contextmanager
def archive_lock(private):
    private.mkdir(parents=True, exist_ok=True)
    with (private / 'archive.lock').open('a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if sys.platform == 'win32':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == 'win32':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        while block := f.read(2 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def fingerprint(path):
    s = path.stat()
    # Nanosecond timestamps plus file identity detect normal appends/replacements.
    # This is a fast change detector, not a substitute for --verify-existing.
    return s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_dev, s.st_ino


def preserve_file(source, root, private):
    """Copy stable bytes, then verify the content-addressed archive object."""
    temporary_root = private / 'tmp'
    temporary_root.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        temp = None
        try:
            if is_link(source) or not source.resolve().is_relative_to(root):
                raise ValueError('source_boundary_changed')
            before = fingerprint(source)
            if shutil.disk_usage(private).free < before[0] + 2 * 1024**3:
                raise OSError('archive_space_low')
            fd, temp_name = tempfile.mkstemp(prefix='copy-', dir=temporary_root)
            temp = Path(temp_name)
            h = hashlib.sha256()
            with os.fdopen(fd, 'wb') as out, source.open('rb') as inp:
                while block := inp.read(2 * 1024 * 1024):
                    out.write(block)
                    h.update(block)
                out.flush()
                os.fsync(out.fileno())
            sha = h.hexdigest()
            if before != fingerprint(source) or sha != digest_file(source) or before != fingerprint(source):
                continue
            target = private / 'blobs' / sha[:2] / sha
            target.parent.mkdir(parents=True, exist_ok=True)
            duplicate = target.exists()
            if duplicate:
                if target.stat().st_size != before[0] or digest_file(target) != sha:
                    raise ValueError('archive_blob_corrupt')
            else:
                os.replace(temp, target)
            return sha, target, before, duplicate
        except (PermissionError, FileNotFoundError):
            continue
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    raise OSError('source_unstable_or_unreadable')


def image_format(data):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 50_000_000
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                kind, size = picture.format, picture.size
                if kind not in {'JPEG', 'PNG', 'GIF', 'WEBP', 'BMP', 'TIFF', 'AVIF'}:
                    return None
                picture.verify()
            with Image.open(io.BytesIO(data)) as picture:
                picture.load()
        return {'JPEG': 'jpg', 'PNG': 'png', 'GIF': 'gif', 'WEBP': 'webp', 'BMP': 'bmp', 'TIFF': 'tiff', 'AVIF': 'avif'}[kind], size
    except Exception:
        return None


def decode_image(data, image_secrets=None):
    if len(data) > MAX_IMAGE_BYTES:
        return None, None, 'oversized'
    verified = image_format(data)
    if verified:
        return data, verified, 'plain'
    if data.startswith((V1_MAGIC,V2_MAGIC)):
        if image_secrets:
            try:
                from read_wechat_cfg import decode_v2_image
                decoded = decode_v2_image(data, *image_secrets)
                verified = image_format(decoded)
                if verified:
                    return decoded, verified, 'v2'
                if decoded.startswith(b'wxgf'):
                    preview = wxgf_preview(decoded)
                    verified = image_format(preview) if preview else None
                    if verified:
                        return preview, verified, 'wxgf_preview'
            except Exception:
                pass
        return None, None, 'encrypted'
    candidates = set()
    for magic in IMAGE_MAGIC:
        if len(data) >= len(magic):
            key = data[0] ^ magic[0]
            if key and bytes(x ^ key for x in data[:len(magic)]) == magic:
                candidates.add(key)
    for key in candidates:
        decoded = data.translate(bytes(x ^ key for x in range(256)))
        verified = image_format(decoded)
        if verified:
            return decoded, verified, 'xor'
    return None, None, 'unrecognized'


def wxgf_preview(data):
    """A lossless first-frame PNG view; the full original remains archived."""
    exe = shutil.which('ffmpeg')
    start = data.find(b'\x00\x00\x00\x01')
    if not exe or not data.startswith(b'wxgf') or start < 0 or len(data)>MAX_IMAGE_BYTES:
        return None
    try:
        completed = subprocess.run(
            [exe,'-hide_banner','-loglevel','error','-max_alloc','134217728',
             '-protocol_whitelist','pipe','-threads','1','-f','hevc','-i','pipe:0',
             '-frames:v','1','-threads','1','-f','image2pipe','-vcodec','png','pipe:1'],
            input=data[start:],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            timeout=15,check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0,
        )
        if completed.returncode==0 and len(completed.stdout)<=MAX_IMAGE_BYTES:
            return completed.stdout
    except (OSError,subprocess.TimeoutExpired):
        pass
    return None


def classify(path, head, bucket):
    suffix = path.suffix.lower()
    if bucket in {'db_storage', 'Msg'} or suffix in {'.db', '.db-wal', '.db-shm'}:
        return 'database'
    if head.startswith((V1_MAGIC,V2_MAGIC)):
        return 'encrypted_image'
    if suffix == '.dat' or any(head.startswith(m) for m in IMAGE_MAGIC) or suffix in {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.avif'}:
        return 'image_candidate'
    if suffix in {'.mp4', '.mov', '.avi', '.mkv', '.webm'} or head[4:8] == b'ftyp':
        return 'video'
    if suffix in {'.silk', '.mp3', '.wav', '.ogg', '.aac', '.amr'} or b'SILK' in head[:16]:
        return 'audio'
    if suffix in {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.csv'}:
        return 'document'
    return 'other'


def month_and_variant(relative, timestamp):
    match = re.search(r'(20\d\d)[-_/]?(0[1-9]|1[0-2])(?:[/_\-.]|$)', relative)
    month = f'{match[1]}-{match[2]}' if match else datetime.fromtimestamp(timestamp).strftime('%Y-%m')
    lower = relative.lower()
    variant = 'thumbnail' if 'thumb' in lower or '_t.dat' in lower else 'cache' if lower.startswith('cache/') else 'attachment'
    return month, variant


def export_image(data, ext, month, variant, export):
    sha = hashlib.sha256(data).hexdigest()
    rel = Path('images') / month / variant / (sha + '.' + ext)
    target = export / rel
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        try:
            temp.write_bytes(data)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    elif digest_file(target) != sha:
        raise ValueError('export_image_corrupt')
    return rel.as_posix()


def open_catalog(private):
    db = sqlite3.connect(private / 'catalog.sqlite3')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, sha TEXT, size INTEGER, mtime INTEGER,
          bucket TEXT, kind TEXT, month TEXT, variant TEXT, export_rel TEXT, updated_run TEXT);
        CREATE TABLE IF NOT EXISTS versions(path TEXT, sha TEXT, first_run TEXT, PRIMARY KEY(path,sha));
        CREATE TABLE IF NOT EXISTS errors(run TEXT,path TEXT,code TEXT);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
    ''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(files)')}
    if 'source_signature' not in columns:
        # Old catalog rows deliberately remain NULL: a timestamp alone is not a
        # verified baseline. Their first incremental run validates them once.
        db.execute('ALTER TABLE files ADD COLUMN source_signature TEXT')
    db.commit()
    return db


def existing_record(db, relative):
    columns = ('sha', 'size', 'mtime', 'bucket', 'kind', 'month', 'variant',
               'export_rel', 'source_signature')
    row = db.execute('SELECT ' + ','.join(columns) + ' FROM files WHERE path=?',
                     (relative,)).fetchone()
    return dict(zip(columns, row)) if row else None


def archived_blob(record, private):
    if not record or not re.fullmatch('[0-9a-f]{64}', record.get('sha') or ''):
        return None
    blob = private / 'blobs' / record['sha'][:2] / record['sha']
    try:
        if (not is_link(blob) and blob.is_file()
                and blob.resolve().is_relative_to(private.resolve())
                and blob.stat().st_size == record['size']):
            return blob
    except OSError:
        pass
    return None


def exported_image_present(record, export):
    relative = record.get('export_rel') or ''
    if not relative:
        return True
    try:
        path = export / relative
        return (path.resolve().is_relative_to(export.resolve()) and not is_link(path)
                and path.is_file() and path.stat().st_size > 0)
    except OSError:
        return False


def report_present(db, export):
    row = db.execute("SELECT value FROM metadata WHERE key='report_pages_v1'").fetchone()
    try:
        pages = json.loads(row[0]) if row else []
        return bool(pages) and all(
            isinstance(name, str) and Path(name).name == name
            and (export / name).is_file() for name in pages)
    except (OSError, ValueError, TypeError):
        return False


def catalog_totals(db, status):
    status['archived_files'], status['archived_bytes'] = db.execute(
        'SELECT COUNT(*),COALESCE(SUM(size),0) FROM files').fetchone()
    status['categories'] = dict(db.execute('SELECT kind,COUNT(*) FROM files GROUP BY kind'))
    status['unique_payloads'] = db.execute('SELECT COUNT(DISTINCT sha) FROM files').fetchone()[0]
    status['duplicate_payloads'] = status['archived_files'] - status['unique_payloads']
    status['viewable_images'] = db.execute("SELECT COUNT(*) FROM files WHERE export_rel != ''").fetchone()[0]
    status['unique_viewable_images'] = db.execute("SELECT COUNT(DISTINCT export_rel) FROM files WHERE export_rel != ''").fetchone()[0]
    status['encrypted_images'] = db.execute("SELECT COUNT(*) FROM files WHERE kind IN ('encrypted_image','unrecognized_image')").fetchone()[0]


def write_report(db, status, export):
    export.mkdir(parents=True, exist_ok=True)
    counts = dict(db.execute('SELECT kind,COUNT(*) FROM files GROUP BY kind'))
    months = [row[0] for row in db.execute("SELECT DISTINCT month FROM files WHERE export_rel != '' ORDER BY month DESC")]
    links = []
    pages = ['index.html']
    for month in months:
        images = list(db.execute("SELECT DISTINCT export_rel FROM files WHERE month=? AND export_rel != '' ORDER BY export_rel", (month,)))
        for offset in range(0, len(images), 300):
            name = f'album-{month}-{offset//300+1}.html'
            pages.append(name)
            links.append(f'<li><a href="{name}">{month} · 第 {offset//300+1} 页 · {len(images[offset:offset+300])} 张</a></li>')
            figures = ''.join(f'<a href="{html.escape(row[0],quote=True)}"><img loading="lazy" src="{html.escape(row[0],quote=True)}" alt="本地归档图片"></a>' for row in images[offset:offset+300])
            page = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' file:; style-src \'unsafe-inline\'; base-uri \'none\'"><title>本地图片归档</title><style>body{background:#f3f0e9;padding:24px;font:16px system-ui}img{width:220px;height:220px;object-fit:contain;background:white;margin:8px}a{color:#245d51}</style><a href="index.html">返回归档索引</a><h1>'+month+'</h1>'+figures
            (export / name).write_text(page, encoding='utf-8')
    rows = ''.join(f'<tr><td>{html.escape(k)}</td><td>{v:,}</td></tr>' for k,v in counts.items())
    page = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'"><title>微信本地文件与图片归档</title><style>body{max-width:1000px;margin:50px auto;background:#f3f0e9;color:#233c35;font:17px system-ui;line-height:1.8}table{width:100%;background:white;padding:20px}td{padding:5px}a{color:#245d51}</style><h1>微信本地文件与图片归档</h1><p>原始微信文件未修改。以下为本机文件分类；加密数据库的原始备份不等于聊天正文已成功导出。未解码图片已保留原件。</p><p>已归档 '+str(status.get('archived_files',0))+' 个源文件；可查看图片 '+str(status.get('viewable_images',0))+' 个源映射；未解码图片 '+str(status.get('encrypted_images',0))+'。</p><table>'+rows+'</table><h2>按月份查看图片</h2><ul>'+''.join(links)+'</ul>'
    (export / 'index.html').write_text(page, encoding='utf-8')
    db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',
               ('report_pages_v1', json.dumps(pages)))
    db.commit()


def archive(root, private=PRIVATE, export=EXPORT, status_path=STATUS, image_secrets=None,
            verify_existing=False):
    files, skipped = inventory(root)
    run = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    status = dict(state='running', total_files=len(files), total_bytes=sum(r[1] for r in files), archived_files=0, archived_bytes=0, duplicate_payloads=0, viewable_images=0, encrypted_images=0, failed_files=0, skipped_links_or_unreadable=skipped, source_untouched=True, updated_at='', categories={})
    status.update(processed_files=0, new_files=0, changed_files=0,
                  skipped_existing_files=0, baselined_files=0, restored_files=0,
                  verified_existing_files=0, incremental=not verify_existing,
                  report_rebuilt=False)
    with archive_lock(private):
        db = open_catalog(private)
        try:
            # Preserve cumulative counts while a new run is in progress, so
            # the dashboard never implies previously archived data vanished.
            catalog_totals(db, status)
            for index, (source, _, bucket) in enumerate(files):
                relative = source.relative_to(root).as_posix()
                try:
                    record = existing_record(db, relative)
                    stamp = fingerprint(source)
                    signature = json.dumps(stamp, separators=(',', ':'))
                    blob = archived_blob(record, private)
                    unchanged = (record is not None and record['source_signature'] == signature
                                 and not is_link(source)
                                 and source.resolve().is_relative_to(root))
                    if (not verify_existing and unchanged and blob is not None
                            and exported_image_present(record, export)):
                        status['skipped_existing_files'] += 1
                        if index % 100 == 0:
                            status['updated_at'] = datetime.now(timezone.utc).isoformat()
                            atomic_json(status_path, status)
                        continue
                    was_missing = record is not None and blob is None
                    sha, blob, stamp, duplicate = preserve_file(source, root, private)
                    month, variant = month_and_variant(relative, stamp[1]/1e9)
                    # A stat-only change or first-run baseline must not decode
                    # an already verified unchanged payload a second time.
                    if (record and record['sha'] == sha and record['month'] == month
                            and record['variant'] == variant
                            and exported_image_present(record, export)):
                        kind, image_rel = record['kind'], record['export_rel']
                    else:
                        with blob.open('rb') as f:
                            head = f.read(32)
                        kind = classify(source, head, bucket)
                        image_rel = ''
                        if kind in {'image_candidate','encrypted_image'} and stamp[0] <= MAX_IMAGE_BYTES:
                            data, verified, decode_state = decode_image(blob.read_bytes(), image_secrets)
                            if data is not None:
                                image_rel = export_image(data, verified[0], month, variant, export)
                                kind = 'image_preview' if decode_state == 'wxgf_preview' else 'image'
                            else:
                                kind = 'encrypted_image' if decode_state == 'encrypted' else 'unrecognized_image'
                    db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?)', (relative,sha,run))
                    db.execute('INSERT OR REPLACE INTO files(path,sha,size,mtime,bucket,kind,month,variant,export_rel,updated_run,source_signature) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (relative,sha,stamp[0],stamp[1],bucket,kind,month,variant,image_rel,run,
                                json.dumps(stamp, separators=(',', ':'))))
                    status['processed_files'] += 1
                    status['new_files'] += int(record is None)
                    status['changed_files'] += int(record is not None and record['sha'] != sha)
                    status['baselined_files'] += int(record is not None and record['source_signature'] is None)
                    status['restored_files'] += int(was_missing)
                    status['verified_existing_files'] += int(verify_existing and record is not None)
                except Exception:
                    status['failed_files'] += 1
                    db.execute('INSERT INTO errors VALUES(?,?,?)', (run,relative,'file_archive_failed'))
                if index % 100 == 0:
                    db.commit()
                    status['updated_at'] = datetime.now(timezone.utc).isoformat()
                    atomic_json(status_path,status)
            db.commit()
            catalog_totals(db, status)
            status['state'] = 'completed' if not status['failed_files'] and not skipped else 'partial'
            status['updated_at'] = datetime.now(timezone.utc).isoformat()
            if status['processed_files'] or not report_present(db, export):
                write_report(db,status,export)
                status['report_rebuilt'] = True
            atomic_json(status_path,status)
        finally:
            db.close()
    return status


def refresh_images(image_secrets, private=PRIVATE, export=EXPORT, status_path=STATUS):
    """Decode previously archived media without reading/copying source files."""
    with archive_lock(private):
        db=open_catalog(private)
        status=json.loads(status_path.read_text(encoding='utf-8'))
        status['media_refresh_state']='running'
        status['media_refresh_processed']=0
        status['media_refresh_recovered']=0
        try:
            rows=list(db.execute("SELECT DISTINCT sha FROM files WHERE kind IN ('encrypted_image','unrecognized_image')"))
            def decode_one(sha):
                try:
                    blob=private/'blobs'/sha[:2]/sha
                    if blob.stat().st_size<=MAX_IMAGE_BYTES:
                        return decode_image(blob.read_bytes(),image_secrets)
                except Exception:
                    pass
                return None,None,'unrecognized'
            iterator=iter(row[0] for row in rows)
            with ThreadPoolExecutor(max_workers=3) as pool:
                pending={}
                for _ in range(6):
                    sha=next(iterator,None)
                    if sha is not None: pending[pool.submit(decode_one,sha)]=sha
                while pending:
                    done,_=wait(pending,return_when=FIRST_COMPLETED)
                    for future in done:
                        sha=pending.pop(future)
                        decoded,verified,mode=future.result()
                        if decoded is not None:
                            mappings=list(db.execute('SELECT path,month,variant FROM files WHERE sha=?',(sha,)))
                            for source,month,variant in mappings:
                                rel=export_image(decoded,verified[0],month,variant,export)
                                kind='image_preview' if mode=='wxgf_preview' else 'image'
                                db.execute('UPDATE files SET kind=?,export_rel=? WHERE path=?',(kind,rel,source))
                            status['media_refresh_recovered']+=len(mappings)
                        status['media_refresh_processed']+=1
                        if status['media_refresh_processed']%25==0:
                            db.commit()
                            status['updated_at']=datetime.now(timezone.utc).isoformat()
                            atomic_json(status_path,status)
                        sha=next(iterator,None)
                        if sha is not None: pending[pool.submit(decode_one,sha)]=sha
            db.commit()
            status['categories']=dict(db.execute('SELECT kind,COUNT(*) FROM files GROUP BY kind'))
            status['viewable_images']=db.execute("SELECT COUNT(*) FROM files WHERE export_rel != ''").fetchone()[0]
            status['unique_viewable_images']=db.execute("SELECT COUNT(DISTINCT export_rel) FROM files WHERE export_rel != ''").fetchone()[0]
            status['encrypted_images']=db.execute("SELECT COUNT(*) FROM files WHERE kind IN ('encrypted_image','unrecognized_image')").fetchone()[0]
            status['media_refresh_state']='completed'
            status['updated_at']=datetime.now(timezone.utc).isoformat()
            atomic_json(status_path,status)
            write_report(db,status,export)
        finally:
            db.close()
    return status


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--inspect',action='store_true')
    parser.add_argument('--archive',action='store_true')
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--refresh-images',action='store_true')
    parser.add_argument('--verify-existing',action='store_true',
                        help='With --archive, hash and verify every source and archived blob again.')
    args = parser.parse_args(argv)
    if args.verify_existing and not args.archive:
        parser.error('--verify-existing requires --archive')
    try:
        if args.status:
            result = json.loads(STATUS.read_text(encoding='utf-8')) if STATUS.exists() else {'state':'not_started'}
        else:
            if REPO.drive.upper() == 'C:':
                raise ValueError('non_c_project_required')
            root = account_root()
            if args.archive or args.refresh_images:
                secrets = None
                secret_file = REPO / 'data/private/wechat-sync/image-key-cache.json'
                if secret_file.exists():
                    import sync_all_wechat as sync
                    saved = json.loads(secret_file.read_text(encoding='utf-8'))
                    if saved.get('account_hash') == sync._account_hash(root.name):
                        unpacked = json.loads(sync._unprotect_secret(saved['protected']))
                        secrets = (int(unpacked['cfg_u32']),str(unpacked['wxid']))
                result = refresh_images(secrets) if args.refresh_images else archive(root,image_secrets=secrets,verify_existing=args.verify_existing)
            else:
                files, skipped = inventory(root)
                result = dict(state='inspected',total_files=len(files),total_bytes=sum(r[1] for r in files),skipped_links_or_unreadable=skipped,buckets=dict(Counter(r[2] for r in files)),source_untouched=True)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result.get('state') in {'completed','inspected','running'} else 1
    except Exception as exc:
        import sync_all_wechat as sync
        code = exc.code if isinstance(exc,sync.SyncFailure) and exc.code == 'needs_account_selection' else 'local_archive_failed'
        print(json.dumps({'state':'error','error_code':code,'source_untouched':True}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
