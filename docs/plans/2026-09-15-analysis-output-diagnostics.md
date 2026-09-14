# AI-04 output contract and safe diagnostics — v0.1.7 candidate

## Goal and evidence

Resume the requested format-constraint/error-detail work, not cloud analysis.
Prior failure stopped at the first segment with a generic timeline validation
message; its invalid response/field was not retained, so the exact historical
reason cannot be recovered. Source tracing shows all timeline validation
branches share one error, call tombstones discard diagnostics, and progress
counts only successful/cache-completed calls. A synthetic valid leaf event
whose start date has no evidence ref is incorrectly rejected unchanged in a
merge: merge allowed dates include evidence dates but not immutable event
endpoints. Fix that inconsistency without expanding allowed evidence pairs.

## Bounded implementation

1. Add a local fixed diagnostic vocabulary and safe field paths. Each failure
   carries only a code, schema location and locally authored explanation.
   Unknown values/fields, response bodies, chat snippets, identities, paths,
   keys and provider error text must never be persisted or reflected.
2. Annotate timeline validation branches; preserve date/evidence/status/link
   protections and legacy report compatibility. Add the valid leaf-to-merge
   round-trip regression before fixing endpoint membership.
3. Define a mode-specific output contract with a valid complete JSON example,
   exact field/type/limit rules, evidence pair/uniqueness/window requirements,
   and unambiguous leaf versus merge IDs. Keep current JSON-object transport,
   token cap, model selection and consent unchanged. Prompt digest changes
   invalidate old previews and cache fingerprints; old files remain intact,
   but reuse under new prompts is not promised.
4. Distinguish malformed JSON, incomplete provider response, top-level schema
   and timeline/evidence failures. Persist only sanitized diagnostic metadata
   in call/job/partial report checkpoints; sanitize it again on public reads.
   Count attempted calls separately from successful/cache-completed calls;
   retries remain explicit and never automatic.
5. UI shows failure phase/segment/schema location and a concrete explanation,
   plus possible billing and manual re-preview instructions. Old jobs without
   detail explicitly remain unspecified; do not infer an attempted-call count
   from zero successes. Use existing feedback/report areas, no visual redesign.
6. Synthetic red/green unit, mocked-transport, pipeline and browser tests;
   independent spec/quality review, full regression, public-source scans and
   exact remote tree verification. Deploy only verified source deltas with
   rollback and restart after confirming no active work.

## Acceptance / boundaries

- A first-call invalid evidence response yields one attempt, zero successful
  completions, no automatic retry, no fabricated report, and a safe persisted
  code/path explaining the error. A later failure keeps prior successful work.
- Cached calls do not increment attempts. Merge failures identify merge phase.
- Arbitrary diagnostic values cannot leak into API/UI; no raw failed response
  is logged or saved. Existing cache/billing locks and permission gates remain.
- Do not reanalyse, access original chats, change credentials, export/sync or
  run backups. Once the maintenance work is done, ask the user whether to
  re-preview the same selected range, with new call counts and billing risk;
  the local workflow still requires fresh explicit send confirmation.
- Candidate release depends on preceding unreleased branches and its own CI;
  do not label it a final Release without the actual release gates.
