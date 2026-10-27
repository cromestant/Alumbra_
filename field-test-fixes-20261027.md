# Field-Test Fixes — status (2026-10-27)

Backend + Android round from Charles's live phone tests (fridge/ice, follow-up,
cabinet, dark-corner torch). No gate thresholds, no escalation changes.

## Outcome

Eval **23/26** on run `20261027-162949` (baseline 24/25 + new fridge case
passing, 0 errors). All six brief items implemented and verified. Tree
committed by Charles; this doc records the round.

## Before / after

| case | base 175257 | mid 160609 | final 162949 |
|---|---|---|---|
| 21 others | PASS | PASS | PASS |
| case_02 Q2 (granules) | FAIL | FAIL | FAIL (accepted) |
| case_05 Q3 | PASS | FAIL | PASS |
| case_06 Q1 (moon) | PASS | FAIL | PASS |
| case_07 Q3 (mailbox 16) | PASS | FAIL | FAIL (flake) |
| case_12 Q4 (aspirin) | PASS | FAIL | PASS |
| case_13 Q1 (glasses) | PASS | FAIL | FAIL (drift) |
| fridge_ice Q1 (new) | — | PASS | PASS |

- Q3: known flake, pre-existing shape — all five runs open with the identical
  "Careful — mailbox is in your path at 12 o'clock." Only the 4P/parcel
  landmark flickers. Synthesis uninvolved.
- Q13: Spark drift, Charles's call. Final answer is correct and
  ruling-compliant ("on the shelf under the monitor, beside the earbuds
  case") but the check wants `stand`. Control probe with the baseline
  prompt returned "wooden shelf": "shelf" is drift, "wooden" obeys the
  material ban. Proposal: widen to `stand OR shelf`, or accept.

## Fixes within the round

- Q6: §6 suppressed sky-talk; added "pointed up at the sky is orientation,
  not decoration". Verified: "Camera is pointed up at the sky…" → PASS.
- Q4: can't-confirm template said "could NOT be confirmed" (no check
  variant matches); aligned template + fallback to "cannot".
- Q13 (synthesis ate the landmark): mid run shipped `synth_applied=True`
  with the stand/earbuds sentence dropped. Drafts under 4 sentences now
  skip synthesis; Fix-6 scene landmarks join MUST KEEP + preservation.
- Goal echo: final fridge answer verdicts a ghost — SAM returned the
  fridge's exact box under the name "countertop appliance". New
  `is_goal_echo`: entries ≥90% inside the goal box with area ratio ≥0.5
  are the goal, never obstructions. Ratio floor keeps small hazards on
  big goals (toy before couch) on-path. Offline ghost/toy fixtures;
  replay on the saved scene drops the ghost and keeps the ice.

## Section evidence

- §1 floor look: 6/6 live fridge asks (`floor_found=3`, ice in scene),
  navigation-only, `FLOOR_LOOK_ENABLED=0`.
- §2 goal: fridge/couch/counter reach-targets + holder/part/floor/echo
  exclusions. Post-fix fridge probe: "Careful — ice cubes is in your path
  … They are on the floor, so watch your footing. The black refrigerator
  is at your 1 o'clock." — verdict about ice, fridge as goal, no
  countertop in the corridor.
- §3 follow-up: per-ask log shows parent → `POST /followup → 200` with the
  identical cached scene, 6–9 s, no perception; `410 Gone` expiry path
  exercised. Android: no camera, `ASK_EXPIRED`/`MEDIA_GONE` plain speech.
  Emulator-verified end to end.
- §4 torch: bound to capture; emulator logcat on → off in 214 ms, out
  before Working. Pixel 9a dark-room check is Charles's.
- §5 NONE: discarded at ingestion; zero NONE entries in final cabinet
  scenes; count in log only.
- §6 surfaces: no color/material in final answers; wet-patch WARN path
  unchanged.

## Notes

- Synthesis applied-rate: 0/21 attempted in the final run (6 short-skipped).
  Not recovered; stalls persist. Informational only.
- Offline checks: FAILURES none (~70 assertions).
- Flagged, not fixed (out of scope): verdict subjects still ground-cover by
  variance (sidewalk/grass, pre-existing); Q3 sibling mailboxes verdict as
  "in your path" though the goal is a mailbox (§2 spirit question).

## Reproduce

- `.venv/bin/python backend/tests/ask_offline_check.py`
- `.venv/bin/python eval/run_eval.py --base-url http://127.0.0.1:8124`
- `JAVA_HOME=<jdk> ./gradlew :app:assembleDebug :app:lintDebug`
