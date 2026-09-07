# v0.1.2 Import Storage Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task, with spec review before quality review. Routine decisions are delegated by the maintainer.

**Goal:** Make the four offline converter imports non-overwriting and idempotent after integrity verification; align web import identity and locking without touching private archives.

**Architecture:** A prepared-payload storage module owns canonical identity, OS locking, immutable staging/publication and verified reuse. Offline format adapters keep their existing single normalization pass. The web pipeline retains raw input/statistics/DTO handling and uses the shared identity and lock primitives.

**Tech Stack:** Python 3.12 standard library, unittest, existing synthetic Playwright and Node tests. No new dependencies or external model calls.

**Design:** [Reviewed design](../specs/2026-09-07-v012-import-storage-design.md). DATA-01 phase 1 only; parent Issue #15 stays open. Public source normal checkout and dedicated `codex/v0.1.2-import-storage` branch are the selected isolation; no private deployment update.

## Task 1 — Immutable store and four offline adapters

Owned files: new `scripts/import_store.py`, `scripts/external_chat_import.py`, four `scripts/convert_*.py` named in the design, new `tests/test_import_store.py` and `tests/test_offline_import_storage.py`.

- [x] Add CLI regression tests before production edits, observe the existing overwrite/rewrite failure. Use subprocesses with synthetic files and `sys.executable`; run `python -X utf8 -m unittest discover -s tests -p test_offline_import_storage.py`.
- [x] Check A→A, A→B and input filename relocation for every adapter. Assert bundle IDs, bytes, nanosecond mtime, old report preservation, and all actual duplicate records. Assert unknown invalid records preserve the adapter's original dropped count.
- [x] Implement these exact common APIs:

```python
fingerprint_import(payload, *, identity_context, sidecars=None) -> str
exclusive_import_lock(contacts_dir)  # OS context manager; non-blocking
save_import_bundle(payload, *, contact, contact_id, output_dir,
                   identity_context, sidecars=None) -> dict
# {"payload": prepared_copy, "bundle": all_existing_path_keys,
#  "created": bool, "reused": bool}
```

- [x] Canonical JSON rejects non-JSON/non-finite input and empty messages; exclude only each artifact's top-level bundle_dir. Sidecars allow exactly emojis.json. Use fixed safe exceptions/codes, do not log input or paths on failure.
- [x] Validate root/private ancestors and lock/staging/material paths against links and Windows reparse points. Scan only complete non-hidden regular bundle directories. Manifest version 1 verifies fingerprint and the complete expected set of artifact hashes; also verify canonical loaded materials, not just a claimed fingerprint. Legacy or damaged bundles remain untouched and result in a new bundle.
- [x] Stage in contacts.parent/private, write exclusive files with flush/fsync, publish one complete directory after occupancy recheck under the shared dashboard-import.lock. Preserve existing files/directories/broken links. Rename success is commit: never clean a committed target if later response work fails. POSIX directory sync where supported; no power-loss guarantee claimed.
- [x] Adapt external_chat_import.write_contact_bundle without changing its existing two-value return contract; result metadata may be extra bundle keys created/reused. Normalize there exactly once. Markdown and WeFlow already normalize, so call the saver directly. Capture CipherTalk raw meta.ownerId before converting and include explicit request options in identity_context.
- [x] Preserve all existing CLI flags and success fields; add created/reused booleans. Use fixed CLI failures for missing/invalid input, busy import and I/O; no traceback, input or path in errors.
- [x] Add store tests for identity isolation, exact duplicate arrays, sidecar omissions/tampering, caller immutability, links, serialization, injected file/manifest/rename/post-commit failure. Use real separate processes for contention and process-exit lock release; skip only unsupported OS link privileges explicitly.
- [x] Run both new test modules and existing pipeline tests. Self-review and report actual RED/GREEN evidence. Do not stage or commit concurrently with the controller.

Concrete behavioral assertion for the first RED:

```python
first = run_converter("first synthetic message")
before = Path(first["messages_path"]).read_bytes()
second = run_converter("changed synthetic message")
self.assertNotEqual(first["bundle_dir"], second["bundle_dir"])
self.assertEqual(before, Path(first["messages_path"]).read_bytes())
```

## Task 2 — Web identity, integrity and private staging

Owned files: controller changes `dashboard/app.py` and new `tests/test_web_import_identity.py`; no adapter/store edits while Task 1 runs.

- [x] Write failing tests with real web import/stat generation for distinct contact_id, distinct payload owner/source, and CipherTalk raw owner identity. Same content alone must not cause reuse. Observe failures against the baseline.
- [x] Write failures for tampered messages, legacy v1 manifests and legacy hidden staging directories. Preserve original bundle and original raw copy; reimport creates another full bundle. Same intact v2 input still reuses without mutation.
- [x] Replace only the OS lock implementation with a compatibility wrapper around exclusive_import_lock; keep _IMPORT_LOCK thread serialization. Use fingerprint_import with explicit request values and raw source identity values retained before conversion.
- [x] Write v2 dashboard manifest with identity context and messages digest. Verify manifest version, context, digest, canonical stored payload and safe regular paths before reuse. Do not return context or hashes in public DTOs.
- [x] Move web staging to validated private sibling root; always exclude hidden or linked reuse candidates. Recheck occupied target before publication; retain existing raw rollback on failure and preserve committed bundle after post-publication failure.
- [x] Run `python -X utf8 -m unittest discover -s tests -p test_web_import_identity.py` and `python -X utf8 -m unittest discover -s tests -p test_dashboard.py` including original concurrent requests and rollback tests.

## Task 3 — Review, documentation and verified release

- [x] Fresh independent spec reviewer reads actual code and compares the complete design/acceptance checklist. Fix and re-review findings before requesting a separate quality review. Quality review includes cross-platform behavior, privacy, failure paths and tests.
- [x] Document immutable import semantics, v1 web first-reimport duplication, only four CLI entrypoints covered, cooperative-lock scope, no old bundle migration, and explicit merge/native/legacy exclusions. Update BACKLOG phase progress without closing #15 or claiming preview wizard completion.
- [ ] Run all synthetic tests with TEMP/TMP in scripts/tmp and UI screenshot saving disabled:

```powershell
$env:TEMP = (Resolve-Path scripts/tmp).Path
$env:TMP = $env:TEMP
$env:SHE_LOVE_ME_UI_TESTS = '1'
$env:SHE_LOVE_ME_UI_SCREENSHOTS = '0'
Remove-Item Env:SHE_LOVE_ME_DATA_DIR -ErrorAction SilentlyContinue
Remove-Item Env:SHE_LOVE_ME_SYNC_STATUS -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_catalog.cjs
```

- [ ] After local pass, update VERSION/CHANGELOG to 0.1.2; review exact additions/diff and anonymous Git metadata, stage only named source files, run `python scripts/check_public_release.py --root .` and source-only extracted candidate `--tree` scan.
- [ ] Publish reviewed source via authenticated GitHub tools, verify remote tree equals local staged tree, create bounded PR and version milestone/issue records. Do not upload runtime files or logs.
- [ ] Require Windows/Linux Python/UI and Node/audit CI on the exact candidate head, fix any failures with TDD, then merge. Create v0.1.2 release/tag on verified merge; retain previous tags. Read back release/commit/tree and report actual tests, limitations and next slice.

No step runs real exports, paid inference, private UI, media access, schedule changes or private deployment replacement.

提交前状态：任务 1/2 与两轮独立审查完成；本机已有完整通过记录，最终候选及远端发布门禁继续核验。此文件保留提交前快照，远端执行结果见对应 Release，不在发布前预勾远端步骤。
