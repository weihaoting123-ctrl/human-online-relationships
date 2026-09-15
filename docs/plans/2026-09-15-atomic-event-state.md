# AI-06 · Atomic event-state output · v0.1.10 candidate

## Diagnosis and bounded decision

The observed first-segment error is `OUTPUT_STATE` at an event's `.status`.
It is raised after JSON, event identity, enum and evidence-pair checks, but
before later event fields/links are fully validated. Three incompatible state
conditions share that code; failed bodies are intentionally not retained, so
the particular combination cannot be recovered from older diagnostics. It is
not evidence of a network/UI failure or an incorrectly rejected valid event.

Current prompts describe the same rules as the validator. Repeating prose alone
does not remove the model's ability to combine three individually valid enums
incorrectly. The delegated, bounded fix is one atomic provider `event_state`
selected from the exact 28 combinations accepted by the existing 60-combination
kind/status/evidence-level matrix. This reduces combination freedom without
relaxing evidence or silently repairing/changing an event.

## Contract and compatibility

- New provider event: exactly eight fields (old ten, minus kind/status/evidence_level,
  plus event_state). Fixed exact string encoding `kind:status:evidence_level`.
- First validate the entire new shape, then decode using the fixed mapping into
  the original ten-field canonical event. No default, fuzzy matching, ignored
  fields, status downgrade/upgrade, mutation of input, event dropping or retry.
- Legacy ten-field events retain the original acceptance and strict failures.
  Mixed new/old shapes and unknown keys fail closed. All evidence/date/text/link
  checks still run; legal new codes cannot bypass immutable merge facts.
- Reports, cache values, UI DTOs and merge input packets stay canonical. Only
  provider output examples change; merge outputs encode the supplied original
  tuple exactly. Old archives/reports/cache records are not rewritten.
- Vocabulary and serialized options are sorted, keeping prompt hashes stable
  across processes. The real prompt change still invalidates old previews and
  can prevent old cache reuse; require fresh preview/consent, no automatic run.
- Both server and browser safe diagnostic-path lists accept event_state; they
  never expose provider values/body, and continue reading old .status errors.

## TDD, validation and delivery

1. Observe synthetic RED for valid atomic events and complete-shape checks.
2. Implement the fixed mapping, strict decoder and provider prompts/examples.
3. Exercise all 60 combinations, direct/segment/merge, invalid evidence/shape,
   immutable merge, hash seeds, no-retry persistence and browser diagnostics.
4. Independent review, full offline synthetic regression, source-only scans,
   versioned candidate publication with remote tree identity verification.
5. Before private deployment, verify source parity, source rollback and idle
   analysis/sync/backup workers. Restart loopback only if safe. Never rerun an
   old failed cloud analysis as a deployment or verification step.

Free JSON generation can still produce an unknown enum or other invalid output;
this is not a guarantee of future provider success. A failed historical task
remains failed. Live model quality is not verified without a new selected-range
user confirmation; synthetic tests are not a claim of a paid live run.
