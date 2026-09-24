# Fixed-conversation recent-text assistant

## Decision and scope

The maintainer delegated routine implementation decisions and requested automatic
new-message suggestions. This iteration removes manual copying for an explicitly
selected, source-verified **fixed direct conversation**. It does not claim to
verify the active WeChat account/contact from OCR. The existing title reader is
only a conservative pause fence; it never redirects an existing authorization.

Alternatives considered: repeatedly running full archive synchronization is too
expensive; message-pane OCR lacks reliable sender/message identity; archive-only
polling is not a live source. Use a dedicated, read-only selected-contact database
snapshot worker with existing cached keys instead. Latency is best-effort and
must be reported honestly, not promised as instantaneous.

## Boundaries

- Default off, desktop title binding required. Browser/manual mode keeps the
  existing one-request preview/confirmation workflow.
- A distinct local preview states the pinned archive, selected historical date
  range, direction, recipient/model, and future new-text authorization. An
  unchecked identity/scope checkbox and explicit start action are required.
- Only a native direct-conversation bundle with stable own/contact identity and
  matching account-bound checkpoint is eligible. Multiple accounts, unknown
  sender/source, missing cached keys, or inconsistent snapshots stop safely.
- The authorization covers that fixed source, NOT whichever conversation OCR
  later recognizes. UI says the active account cannot be machine-verified. OCR
  can miss same-name or other UI switches; it is a best-effort pause fence, not
  a guarantee that every switch is detected. The upload source never changes.
- Fixed 15-minute lifetime, at most 6 new call attempts, at least 20 seconds
  between attempts; total context at most 200 messages/20,000 characters. A new
  inbound batch is at most 20 messages/4,000 characters. No audio, media, system
  messages, transcriptions, hooks, key scans, downloads, or source writes.
- Baseline read never calls cloud. Newly observed peer text waits for a stable
  burst (at least 3 seconds); new self text cancels earlier pending suggestions
  and clears prior result generations. Process peer/self/peer in trusted order;
  polling cannot instantly invalidate an outgoing message not yet observed.
- Dedup uses stable server IDs plus partition/local-ID aliases for pending IDs
  upgraded from zero server ID. Same server ID across partitions never triggers
  again. Never dedup by text alone, or assume numerically contiguous local IDs.
  The bounded tail must overlap the previous trusted anchor; anchor loss,
  ambiguous ordering or source generation change stops, never silently skips.
- Changed content for an existing ID, overflow/gaps, identity/config/prompt or
  archive visibility changes, window/title loss, stop, expiry, and uncertain
  provider outcomes terminate the session. No restart/renewal or automatic retry.
- Server claims attempts and message IDs atomically before a request. Never hold
  the state lock across file reads, subprocesses, or network calls. Stop remains
  responsive; late results after revocation are discarded. Already sent requests
  cannot be recalled and may be billed.
- All session state is bounded process memory. No new message bodies in public
  logs, API status DTOs, local storage, or test fixtures. Daily sync is unchanged.
- The sixth successful result remains visible as budget-exhausted, never starts
  another call. Stop/expiry or a subsequently observed outgoing message clears
  it. Expired or stopped grants cannot be resumed by heartbeat or recognition.

## Components and interfaces

1. `scripts/copilot_live_read.py`: isolated, single-flight CLI; stdin contains
   selected bundle and optional opaque previous cursor; stdout contains internal
   structured rows only for its local parent, or an allowlisted failure code.
   Uses cached keys, the normal sync lock, validated private snapshots and read-
   only snapshot queries. Source files are never opened by SQLite directly.
2. `dashboard/copilot/live.py`: local preview/start/tick/stop endpoints behind
   existing loopback/session/origin protections; scoped in-memory state machine,
   reader transport, redaction, output validation and bounded cloud requests.
3. `dashboard/static/copilot/live.js`: separate controller integrated through a
   narrow host callback contract; no autonomous source or cloud access on load.
   Five-second ticks after the previous tick completes; backend enforces limits.
4. Existing title-binding, manual preview/run, and archive synchronization retain
   their current guarantees. New paths never loop the old per-call consent API.

## Validation and release

Synthetic unit/HTTP/browser fixtures only. Cover baseline suppression, duplicate
and identical-text distinct IDs, new self cancellation, changed identity, gaps,
overlapping ticks, expiry/budget, stop during read/request, late results, unknown
errors, and zero cloud calls without a fresh explicit authorization. Inspect
original source access modes and safe error outputs. Run full regression and
independent spec then quality review before deployment or publication. Real
latency and source compatibility remain unclaimed until safely observed.

Existing RC3 title-calibration work is preserved. Source work remains in the
separate public repository branch; no private deployment history or data enters
the release. Publish as a release candidate, not a falsely completed 1.0 stable.
