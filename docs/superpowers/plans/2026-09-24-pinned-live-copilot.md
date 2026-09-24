# Pinned recent-text implementation plan

> Execute using test-driven development, independently scoped implementation,
> spec compliance review, code-quality review, and verification before completion.

**Goal:** Read newly arrived text from one explicitly authorized direct
conversation and refresh suggestions without manual copying or automatic sending.

**Architecture:** Existing desktop header binding is a pause fence, not identity
proof. Dedicated local snapshot worker verifies pinned archive source identity;
server-owned grant controls calls; a standalone browser controller presents
preview/consent/live counters and terminates on context changes.

**Stack:** Python standard library + existing SQLCipher reader, vanilla JS/CSS,
Electron bridge, unittest, synthetic Playwright and Node test fixtures.

## Task 1 — Read-only selected-contact worker

Files: `scripts/copilot_live_read.py`, `tests/test_copilot_live_read.py`.

- [x] Write failing synthetic tests for source identity, cached-only keys,
  snapshots/read-only queries, unknown sender, tail limits and safe failures.
- [x] Run tests and verify expected failures.
- [x] Implement bounded text-only worker; preserve stable partition/local IDs.
- [x] Run tests; independently inspect requirements and source-read safety.

## Task 2 — Server authorization and streaming state

Files: `dashboard/copilot/live.py`, optionally a narrow transport module,
`dashboard/app.py` routes, `tests/test_copilot_live.py`.

- [x] Write failing tests for explicit one-use consent, baseline/no-send,
  debounce/dedup, outgoing cancel, lifecycle, concurrency and failure handling.
- [x] Implement preview/start/tick/stop and fixed limits. Never reuse per-call
  preview consent automatically. Add guarded routes with small request limits.
- [x] Verify output contains counts/status/result only, and dependencies are
  revalidated before cloud claim. Run targeted tests and independent reviews.

## Task 3 — Visible bounded-session UX

Files: `dashboard/static/copilot/live.js`, `copilot.js`, `index.html`,
`copilot.css`, `tests/test_copilot_ui.py` or a dedicated synthetic UI test file.

- [x] Write failing browser tests for off-by-default, manual-mode disabled,
  unchecked consent, limits/recipient, new suggestion and stop/late-response.
- [x] Implement isolated controller and host hooks. Every context invalidation
  stops the live session. Keep stop available in compact mode.
- [x] Verify keyboard/small-window layout and preserved single-call behavior.

## Task 4 — Review, candidate release, controlled deployment

- [x] Review exact diff and update scoped policy/docs, version and changelog.
- [x] Run targeted tests, desktop smoke, full synthetic suite and public scan.
- [x] Fix independent spec and quality findings; rerun relevant tests.
- [ ] Commit/publish reviewed source only; verify GitHub tree/CI, preserve PR.
  Publication results are recorded on PR #47 after this source checkpoint.
- [x] Deploy explicit source allowlist with baseline checks and local rollback.
  Restart only owned assistant/server if needed; do not touch WeChat.
- [x] Report shipped capabilities separately from unperformed real-source or
  paid-cloud acceptance. Do not enable a real conversation grant for the user.
