# Desktop launch button · COPILOT-04

The user expects a visible button, not a shell command. Routine design decisions
are delegated under project stewardship. Keep the current public source checkout
and branch, separate from private deployment; no additional worktree is needed.

## Chosen design

A browser-only card immediately below the assistant page header provides
“启动悬浮助手” / “打开悬浮助手”, read-only runtime status and concrete safe errors.
The existing local server starts only this project's installed Electron app at
its own bound port. Neither a custom protocol registration nor a download link
is needed: the server is already installed, and a protocol would introduce an
unnecessary OS-wide registration. A shortcut alone would not solve discovery.

GET `/api/copilot/desktop` returns `{status:'ok', desktop:{supported, installed,
state, code}}`. States are `stopped`, `running`, `unknown`, `unavailable`.
POST `/api/copilot/desktop/launch` accepts exactly `{}`, no query or fragment,
body at most 1024 bytes. Successful acceptance returns the same safe envelope
with state `launch_requested`; it is not proof a visible window was shown.
Both use existing loopback/session guards; POST also requires exact same origin.
No caller executable, path, port, URL, arguments or environment is accepted.

Fixed-target launches are hidden (no terminal), rate-limited/single-flight and
use Electron's existing single-instance lock. Status inspects only the fixed
project executable/main arguments, returning no process IDs, paths or titles.
Unknown process state is not reported as running. Missing runtime is explained,
not downloaded. Errors never echo process output or exception details. Launch
works even when AI/reply modules are off and does not enable either module.

## Visible open, not just a hidden process

The fixed native argument `--show` requests the existing or newly launched
assistant to display its own expanded settings window. It does not activate or
move WeChat. Manual presentation pauses following and clears candidate/grants;
title capture stays paused. An explicit “跟随微信” resumes ordinary following,
not a cloud grant. Returning to following may hide the assistant until WeChat is
foreground. Normal unflagged launches retain non-intrusive existing behavior.
Second-instance arguments must match the existing local port before presenting.

The browser card explains paused settings, startup failure, missing runtime and
unsupported platform. Native mode hides the launcher card and explains how to
resume following. No automatic POST on load, reload or status refresh; pending
clicks are disabled, unknown outcomes are not automatically retried.

## Verification and boundary

Synthetic API tests cover authentication/origin/body/path constraints, fixed
target, unsupported/missing runtime, failure/timeout, cooldown and no AI/read.
Browser tests cover visible placement, explicit click, failure, duplicate clicks,
native hiding, and 320 px keyboard layout. Node/Electron fixtures cover --show,
second-instance reuse, paused presentation, no capture/focus stealing, and resume.
Independent review, full synthetic suite and public release scan precede RC5
source publication. Private deployment uses explicit source rollback/allowlist;
do not overwrite unrelated app differences or touch chats, keys or schedules.
