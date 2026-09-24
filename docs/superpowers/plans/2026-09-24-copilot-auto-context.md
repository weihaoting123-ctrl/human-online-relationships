# Current-conversation recognition implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Recognize the current WeChat header locally and preselect an explicitly unverified archive candidate without carrying authorization across conversations.

**Architecture:** Isolated native header observer → validated/stabilized desktop observations → metadata-only resolver and revocable server token → automatic candidate UI with fresh explicit confirmation. No message capture or automatic cloud request.

**Tech Stack:** Existing Python HTTP service, vanilla browser JavaScript, Electron, Windows PowerShell 5.1 / WinRT OCR, synthetic unittest and Node tests.

---

## Task 1: local metadata resolver and binding fence

Files: create `dashboard/copilot/binding.py`, `tests/test_copilot_binding.py`; modify `dashboard/copilot/service.py`, `dashboard/copilot/context.py`, `dashboard/app.py`.

- [x] Add failing synthetic tests. Exact title returns only `suggested`; aliases never identify; hidden/missing duplicate blocks; no archive body reads; old sequence/session/token fails.
- [x] Implement metadata index fingerprint and volatile per-workspace session/observation state under a shared reentrant lock. `start` creates a server session; `observe` accepts `{session_id, seq, target, state, title, source}`; replies contain `{state, reason, bundle_id?, binding_token?, observation_seq, account_verified:false}`.
- [x] Extend preview with optional `binding_token`, and run with explicit `binding_confirmed:true` only for a bound candidate. Freeze and validate token/index on preview and atomic claim. Preserve manual API request compatibility and single-use consumption.
- [x] Verify red then green using `python -m unittest discover -s tests -p 'test_copilot_*.py'` and concurrency barriers; network stays mocked.

```python
def test_hidden_duplicate_never_looks_unique(self):
    result = resolve_title('Example Friend', [
        {'bundle_id': 'a', 'source': {'contact': 'Example Friend', 'source': 'wechat'}},
        {'bundle_id': 'b', 'source': {'contact': 'Example Friend', 'source': 'wechat'}, 'source_missing': True},
    ])
    self.assertEqual(result['state'], 'ambiguous')
    self.assertNotIn('bundle_id', result)
```

## Task 2: supervised native title reader

Files: create `scripts/copilot_title_read.ps1`, `desktop/copilot/title-observer.cjs`, `desktop/copilot/tests/title-observer.test.cjs`; modify `desktop/copilot/main.cjs`, `preload.cjs`, `security.cjs` as needed.

- [x] Add failing tests for strict frames, two-frame stability, A-B-A generation changes, timeout/pause clearing, oversized input, and read-only bridge shape.
- [x] Implement a no-focus, no-message header crop in volatile memory and built-in local OCR. Require one-time explicit two-corner header calibration using Ctrl+Alt+F8, with numeric-only private preferences and no guessed crop. Keep the supported layout profile explicit and validated. Worker emits only bounded private local IPC; its stderr is discarded. No screenshot or title file writes.
- [x] Supervise worker lifetime/timeout; emit a clear state before accepting a changed label. Heartbeat current observations without reviving an old generation. Stop only owned helper processes.
- [x] Verify Windows OCR on synthetic title bitmap and shell/IPC unit tests. An optional real-device aggregate probe must output no title/image/account values.

```js
test('a changed label clears the previous candidate immediately', () => {
  observer.accept(frame('Example A'));
  observer.accept(frame('Example A'));
  observer.accept(frame('Example B'));
  assert.equal(observer.snapshot().state, 'unavailable');
});
```

## Task 3: native automatic candidate UI

Files: `dashboard/static/copilot/index.html`, `copilot.js`, `copilot.css`, `tests/test_copilot_ui.py`.

- [x] Add failing browser tests for native auto mode/manual browser mode, automatic candidate selection without previews/calls, uncertain clearing, stale resolver responses, changed candidate resetting drafts/results, and unchecked identity confirmation.
- [x] Add automatic/manual selector and live local recognition status. Consume the read-only native observation stream; post observations serially with latest-event invalidation and server session fencing. Never treat title-only matching as verified identity.
- [x] Include binding token in previews; require candidate check and explicit cloud consent in run. Invalidate and ignore late results on every observation generation change.
- [x] Verify 320px/compact/bubble rendering, keyboard labels, failure messages, paused mode, and existing copy-only behavior with synthetic fixtures.

## Task 4: integration, release and scoped deployment

- [x] Independent spec then quality review; resolve actual findings with failing regression tests.
- [x] Run full Python/browser suite, Node catalog/desktop tests, syntax and publication scans. Record skip/failure counts honestly.
- [x] Update VERSION/CHANGELOG/docs/requirement record to 1.0.0-rc.2, explaining title-only uncertainty and supported layout limits.
- [x] Deploy only an explicit source allowlist to the private project with source-only rollback; avoid restarting an in-flight model call. Verify HTTP and native aggregate status without chat inspection.
- [ ] Publish reviewed public tree through the authorized GitHub connector; verify exact tree and anonymous author, attach PR and check current CI. Preserve original data and all backups.
