# AGENTS.md

## Repository Focus

- This public repository is Human Online Relationships (人类线上关系可视化), a local-first archive, relationship timeline, and explicitly scoped analysis application. The `she-love-me` workflow identifier and legacy HTML formats remain for compatibility.
- It also supports optional WeChat emoji export: `messages.json` can include emoji metadata, and `scripts/export_emojis.py` can download/store emoji assets and generate `reports/emojis_preview.html`.
- Current preferred export layout is per-contact bundles under `data/contacts/<联系人>__<hash>/`, where chat records and emoji records are separated but linked (`messages.json` + `emojis.json`).
- The unified skill entrypoint for all tools is `.agents/skills/she-love-me/SKILL.md` (Claude Code, OpenClaw, Codex, Cursor, Copilot, Gemini CLI).
- Analysis and data-source knowledge is split across `references/` under that directory; SKILL.md is the control plane.
- Follow the current routing in SKILL.md and `references/data-sources.md` for an explicitly requested import/export. Do not bootstrap readers, export chats, register schedules, or call cloud models merely because the repository was cloned or opened.

## Public Repository Boundary

- Keep the private running deployment separate from this public source workspace; never publish its Git history, data, media, reports, credentials, logs, or backups.
- Maintenance and tests use fully synthetic fixtures. Do not open real chat data for source-only tasks or treat renamed/redacted real chats as synthetic fixtures.
- Read `docs/public-release.md` and `CONTRIBUTING.md` before preparing public commits. Review additions before staging, then run `scripts/check_public_release.py` against both the index and working tree.
- Do not upload messages or original media as part of tests or CI. Optional cloud analysis requires a selected conversation/range and a fresh explicit confirmation in the local workflow.
- Cloning this repository creates no scheduled tasks. Original WeChat records stay read-only; recovery and management operations must not overwrite them.

## Codex Guidance

- When a user asks to analyze chat logs with this project, prefer the repo skill `she-love-me`.
- Keep the working directory at the repository root when following the skill workflow.
- Keep generated or sensitive outputs under `vendor/`, `data/`, and `reports/`; do not move personal chat data into tracked files.
- If the user asks about stickers/emojis, use the selected contact bundle's `messages_path`, then run `scripts/export_emojis.py`.
- Prefer `--output-dir data/contacts` over a shared `data/messages.json` whenever exporting a specific contact, so different users do not overwrite each other.
- Do not stop at giving setup commands when the user asks for analysis. Run environment checks, install the selected exporter, initialize it, export JSON, convert it, and continue to the report; pause only for login, approval, token, or contact selection.
- If the user wants to invoke the skill explicitly in Codex, they can mention `$she-love-me`.
