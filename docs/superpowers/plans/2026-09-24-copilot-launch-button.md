# Desktop launch button Implementation Plan

> Use superpowers:subagent-driven-development with independently owned files,
> test-driven development, spec review, then quality review. The project owner
> delegates ordinary implementation and release choices; do not pause for menus.

**Goal:** Open the installed floating assistant from the browser in one click.

**Architecture:** Separate fixed-target launch service, authenticated local routes,
browser launch card, and explicit paused native presentation. No chat or cloud
permission is coupled to opening a window.

**Tech Stack:** Python stdlib, Windows process inspection, vanilla JS/CSS,
Electron, unittest/Playwright and Node synthetic fixtures.

## Task 1 — Launch service and routes

Files: new `dashboard/copilot/desktop.py`, optional fixed local process probe;
`dashboard/app.py`, new `tests/test_copilot_desktop.py`.

- [ ] Write failing tests for GET status and explicit POST, exact empty body,
  query rejection/1024-byte limit, origin/session guards and fixed port/root.
  Example: `self.assertEqual(self.http('/api/copilot/desktop/launch', {'port': 1})[0], 400)`.
- [ ] Run `python -m unittest tests.test_copilot_desktop` and observe missing
  route/service failures before implementation.
- [ ] Add `status(project_root, port)` and `launch(project_root, port)` returning
  the spec DTO. Only launch fixed Electron with `[desktop_root, '--port=N', '--show']`,
  `shell=False`, no stdout/stderr forwarding, no inherited request arguments.
- [ ] Test unsupported/missing/unknown process state, safe errors and concurrent
  launch suppression. Use synthetic fixture paths and mock only OS spawning.
- [ ] Run service/API regressions and get independent review.

## Task 2 — Explicit native presentation

Files: `desktop/copilot/main.cjs`, `security.cjs`, `window-controller.cjs`, and
their Node tests / synthetic app smoke fixture.

- [ ] Write red tests: `launchOptions(['--port=8765','--show']).show === true`;
  duplicate/unknown flags rejected; unflagged launch behavior unchanged.
- [ ] Implement expanded settings presentation that pauses follow/capture,
  clears binding, shows only the assistant, and does not call focus on WeChat.
- [ ] Handle cold and matching-port second-instance `--show`; page readiness
  queues one presentation request, not duplicate windows. `pause(false)` exits
  presentation and resumes normal guarded following.
- [ ] Run `node --test desktop/copilot/tests/*.test.cjs` and the synthetic app
  smoke; independently review behavior and boundary before integration.

## Task 3 — Browser discoverability and native wording

Files: `dashboard/static/copilot/index.html`, `copilot.css`, `copilot.js`, optional
isolated `desktop.js`; `tests/test_copilot_ui.py`.

- [ ] Add failing browser tests for launcher visibility without AI enablement,
  no POST on load, one POST per explicit click, pending disabled, safe outcome,
  native card hidden and mobile keyboard access.
- [ ] Implement a top-level quiet card using existing palette/type and a clear
  primary launch button. A GET displays readiness; click alone POSTs `{}`. A
  bounded follow-up GET may verify process state but never auto-retries launch.
- [ ] Native paused-presentation state exposes “跟随微信” and no false active
  capture state. Keep existing individual/live confirmation paths unchanged.
- [ ] Run UI tests and inspect a synthetic screenshot at narrow width.

## Task 4 — Review, release and deploy

- [ ] Run independent spec then quality reviews; address findings with red/green tests.
- [ ] Update VERSION/package versions to 1.0.0-rc.5, changelog, usage and backlog.
- [ ] Run final full Python/UI + Node suite and exact publication scans.
- [ ] Back up and copy only reviewed runtime files, patch private app narrowly,
  verify installed source, restart only owned idle server/assistant if needed.
- [ ] Verify safe real loopback status and explicit launch (no live authorization,
  capture calibration or model call). Publish reviewed source, preserve draft
  PR/real acceptance gates and report exact button location.
