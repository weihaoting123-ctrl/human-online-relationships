# Contact density calendar — v0.1.6 candidate

## Design and scope

Replace only the relationship timeline's weekly density bars with a local,
content-free calendar. The maintainer has delegated routine design decisions;
this bounded iteration follows project-stewardship without an approval pause.
Work stays in the separate clean public source checkout, on
`codex/density-calendar`, not the private deployment's repository/history.

- Default: latest observed month in the selected scope. Empty scopes fall back
  to the scope end / snapshot clock. Monday-first seven-column month layout.
- Top right: accessible Month / Year segmented buttons. Previous/next period
  controls and a year selector. Year view shows all twelve months; select a
  month to open it. Dates are never sorted by heat.
- Red intensity uses four levels against the maximum daily count in the whole
  returned scope, so switching periods does not recolor identical counts.
- Palette: neutral Apple-style grouped surface, blush through deep red;
  existing system typography, restrained borders, rounded controls, clear
  focus ring. Exact numbers and a legend avoid reliance on color alone.
- Month cells provide exact daily counts on hover, focus, and touch. Year
  tiles provide totals and active-day counts, with miniature daily heatmaps.
- Smooth short transitions; honor reduced motion, dark mode, narrow screens.
  Avoid popover clipping: a stable detail line accompanies the calendar.

Design critique: do not add another competing analytics card, apply emotional
meaning to frequency, fake daily counts from weeks, or use an unlabeled red
block grid. Keep the existing timeline, event dialog, and analysis consent
workflow unchanged.

## Data contract

Extend `build_relationship_timeline` during its existing valid-message loop:
`frequency.daily` is a sorted sparse list of `{date, count}`. Its
`daily_coverage` contains `complete: true`, `date_from`, `date_to`,
`timezone: server_local`, and the existing non-system/non-future scope basis.
Coverage starts at the explicit scope start or first valid scoped record;
coverage ends at the explicit scope end capped to snapshot day, or snapshot
day. With no scoped records and no explicit start, use the capped scope end.
Wholly future coverage has null bounds. Complete means aggregate of the local
archive, not proof of complete WeChat history. Sparse missing days inside valid
coverage mean zero; outside coverage is not zero. No raw text, sender IDs,
media, credentials, network, database migration, or AI calls are added.

Existing detail, preview, and newly saved reports inherit this DTO. Historical
reports without complete daily data show a clear unavailable state, never a
re-query or invented daily counts. Future and out-of-range dates are distinct.
Calendar uses the server's date strings, not the browser timezone.

## Execution plan

1. **Daily DTO (TDD):** add synthetic tests for sparse counts, scope/clock,
   excluded records, empty/future scopes, old long spans and safe fields;
   observe failure, then implement in relationship_timeline.py. Preserve all
   old weekly/node contracts. Independent spec and quality review.
2. **Calendar (TDD):** isolated DensityCalendar renderer with no I/O, focused
   CSS; synthetic browser tests for month/year, leap days, stable red levels,
   accurate details, keyboard/touch, missing data, resets, dark/mobile/reduced
   motion and no requests/storage. Replace density call and add asset hooks.
3. **Integration and verification:** run timeline/browser and full synthetic
   regression suites, inspect synthetic desktop/mobile screenshots; independent
   spec then quality review. No real chat fixtures or cloud flow tests.
4. **Delivery:** bump candidate version/docs, scan reviewed public source and
   staged index, commit and scan clean archive; publish source-only candidate
   on GitHub with verified matching tree. Deploy only reviewed source deltas
   to the running local app with a source-only rollback. Restart only after
   confirming no running analysis/backup/sync; otherwise defer restart visibly.

## Non-goals

No repair/retry of the earlier model schema failure, no new analysis, no
archive import, sync, backup execution, accounts/configuration, automatic
contact inference or destructive data operations. Separate standalone chat
heatmap is not changed or newly deployed by this task.
