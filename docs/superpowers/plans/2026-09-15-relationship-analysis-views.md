# Relationship Analysis Views Implementation Plan

> **For agentic workers:** Use subagent-driven-development for the bounded UI task and independent review. Follow test-driven development, then verification-before-completion. Routine design/publication choices are delegated in `docs/project-stewardship.md`.

**Goal:** Replace generic/business-focused choices with six relationship-oriented analysis perspectives that actually influence analysis, preserving historical meanings and explicit consent.

**Architecture:** One fixed static JSON catalog shared by a small Python loader and the existing dashboard UI. Append trusted selected guidance to both provider system prompts. Hash the effective prompts per focus, preserving the old empty-guidance prompt hashes. No database or output schema migration.

**Tech stack:** Python standard library, existing vanilla JavaScript/native select, unittest and isolated Playwright fixtures. All test data synthetic, random local ports, no live model calls.

## Task 1 — Catalog and effective prompts (main agent)

- [x] Add failing `tests/test_analysis_focus.py` cases for six active entries, four exact legacy labels, valid unknown-ID rejection, distinct leaf/merge guidance, request override rejection and revision/cache compatibility.
- [x] Add `dashboard/static/analysis-focus.json` and `dashboard/analysis_focus.py`. Public API: `PRESETS` indexed by ID, `FOCUSES`, `DEFAULT_FOCUS`, and `prompt_for(base_prompt, focus)`; legacy empty guidance returns base prompt byte-for-byte.
- [x] Wire `dashboard/analysis.py` scope validation and both cloud calls to this module. Wire `analysis_segments.prompt_revision(focus='overview')` into preview creation, run validation, node identity and effective-prompt budget estimates.
- [x] Run targeted backend tests and inspect assertions proving no external transport is invoked.

## Task 2 — UI and historical recheck (UI implementer)

- [x] Add failing isolated browser tests for catalog/default/description, exact preview focus, changed-focus consent invalidation, all legacy titles/recheck scopes, unknown ID and failed catalog loading.
- [x] Change only the select in `dashboard/static/index.html` and focus-related behavior in `analysis-ui.js`; fetch catalog before rendering labels, retain history/config access if the catalog fails.
- [x] Show six active choices with concise live descriptions. Insert only the selected legacy option for recheck, preserve its ID and note legacy semantics. Do not submit unknown historical IDs.
- [x] Update the shared synthetic UI fixture's default selection; retain legacy fixtures as compatibility coverage. Verify keyboard and narrow viewport with no overflow or model calls.

## Task 3 — Review, version, delivery (main + independent reviewers)

- [x] Read actual changes for spec compliance, then independent quality review; fix and recheck findings.
- [x] Update VERSION, CHANGELOG, BACKLOG and product documentation for AI-05. Run all Python synthetic tests with UI enabled, Node tests, syntax checks and public release guard after source freeze.
- [x] Inspect idle state without reading chats, snapshot affected private source, apply only this iteration's delta and verify private deployment using synthetic tests plus static HTTP/hash checks. Never auto-run analysis. Deployment tests also required parity with the already-public post-submit confirmation clearing guard; verified the final script matches reviewed source.
- [ ] Commit explicitly reviewed public files; scan clean git archive, publish exact tree to a versioned GitHub candidate branch/PR, and report actual CI/release state. Preserve private files and rollback source.

This checklist records the pre-publication checkpoint. Candidate publication and CI status are recorded in [AI-05](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/36) and its linked pull request; draft publication is not a formal Release.
