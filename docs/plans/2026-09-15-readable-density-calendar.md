# UI-05 · Readable density calendar · v0.1.9 candidate

## Bounded design

The maintainer requested softer styling and visible numbers in the calendar.
Routine design decisions are delegated. This iteration only refines the existing
contact-density module; it is not a site-wide redesign or new analysis workflow.

- Keep system typography: semibold period headings, tabular day/count numerals,
  quieter weekday and explanatory labels. Show real date numbers in all views.
- Palette: existing neutral surfaces and text, with heat colors `#fcebed`,
  `#f5bdc5`, `#bd3e55`, `#90263e`; dark mode uses the existing accessible rose
  scale. Color expresses message volume, not relationship quality.
- Signature: softly rounded numbered day tiles within airy monthly cards.
  Weekday headers make the year view a set of real mini-calendars. Month view
  retains both day numbers and exact counts, with explicit count units nearby.
- Use container-aware year columns rather than four cramped fixed columns.
  Monthly totals are emphasized. Hovering miniature dates gives exact counts
  both in the nearby month card and the existing live readout; clicking the
  month still opens the accessible full calendar.
- Replace noisy dashed/hatched unknown dates with muted numerals and a subtle
  future-date underline. Keep zero, outside-scope and future distinct through
  labels and a visible key; never replace unknown counts with zero.
- Retain keyboard/touch drilldown, focus, red scale, date arithmetic and reduced
  motion. No remote fonts/assets, dependencies, network or persistence.

Critique: do not cram daily counts into tiny year cells, hide date numerals in
low-contrast red, or make all twelve months equally visually heavy. Exact year
day counts belong in the adjacent readout; the full month provides both numbers.
Keep the same daily DTO and historic-report fallback, without recomputing reports.

## Execution and acceptance

1. Add failing synthetic browser tests for numbered mini-calendars, weekday
   alignment, day readout, unknown states, container responsiveness and no I/O.
2. Refine only calendar JS/CSS and the old hatch-specific test expectation.
3. Independently review and inspect synthetic desktop/mobile light/dark views;
   run relevant and full synthetic regression tests, then public-source scans.
4. Deploy the two source files only after checking parity and making a verified
   source rollback. Static files are served no-store, so no restart is needed.
5. Record candidate version and verification, publish reviewed source with an
   exact remote tree check. GitHub CI and dependent baseline merges remain
   prerequisites for a formal Release.

No chat reading, export, sync, backup execution, model call, settings change,
database migration, report mutation or automatic reanalysis is in scope.
