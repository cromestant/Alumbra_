# Independent Review & Rebuild — verdict/answer assembly (2026-10-27)

Reviewer did not write this code. Tree left uncommitted. No gate
thresholds, no escalation decisions, no eval checks changed.

## 1. Review note — how verdicts were actually assembled

**What the code did.** Two writers, no single owner of the answer:

1. `_path_assessment` computed facts as an unordered chain of `continue`
   exclusions accreted one field test at a time (goal aliases, passable,
   holder, part, echo, floor line). Ground cover was not in it: patio,
   lawn, grass verge, grassy field all became "Careful —" subjects (4 of
   the baseline eval answers lead with a ground-cover hazard).
2. Spark then wrote the **entire** answer from a prompt that handed it
   the full scene JSON plus "use the clock-face positions from the scene
   model" and, for navigation, "lead with 'Careful — <obstacle>…', name
   obstacles, mention off-path items briefly". Spark obliged literally:
   lead, restate the lead, one sentence per scene entry. 20/26 baseline
   answers were >= 4 sentences of "At your N o'clock, X." — the
   recitation was universal, not navigation-only.
3. Synthesis was the only thing meant to collapse that, and it **never
   ran**: `SYNTHESIS_TIMEOUT = 3` s against a Wonder first byte of 20–28 s
   (measured). 0/21 applied in the last status run was structural, not
   flaky; every ask shipped the raw recitation "fail-soft".

Other accretion found:

- **Rule 1 inverted.** Goal words beat the reach target ("path to the
  fridge, past the door" → goal door).
- **No notion of question kind.** Any Jev `navigation_intent` hit got the
  hazard lead — find questions ("Find mailbox 14." → "Careful — mailbox
  is in your path"), surface questions ("What am I stepping on?" →
  "Careful — frog…"), and the stationary glasses distance follow-up.
- **Follow-ups had no referent.** "how far are they" resolved no goal,
  fell to straight-ahead, and the glasses (on the floor, 12 o'clock)
  became their own obstruction.
- `_head_noun("patio ahead") == "ahead"`, so "patio" and "patio ahead"
  never merged and both got recited.
- Flow C stacked "Found your Lisdexamfetamine. Found your ADHD medicine."

**New structure** (`backend/app/pipeline.py`):

| rule | where it lives, once |
|---|---|
| 1 goal = what the question names | `_resolve_goal`: reach target → goal word → distance subject → pronoun = prior referent → exit inference (fallback only) |
| 2 obstruction = floor-standing object in the corridor | `_not_obstruction`: one ordered list — goal, goal part, goal echo (>=90% / >=0.5, fixture kept), surface, **ground cover** (`GROUND_WORDS`), passage, resting on a holder, elevated |
| 3 lead answers the question | `question_kind` (distance > path > find > Jev-path > other); `_path_opening` / `_distance_opening` write the lead in code from computed facts. "Careful —" exists only in `_path_opening`'s blocked branch. Dark + clear → "too dark to tell", never "Yes —" |
| 4 drafts right before synthesis | Spark prompt: answer first, each object once, no walk-through, <= 3 sentences, say so when the photo can't answer. After a code lead Spark only adds <= 2 detail sentences. `_tidy` enforces deterministically: drops re-locations (incl. pronoun ones), ground mentions without footing info, extra landmarks |

The referent (find target, goal, distance subject) is stored in the ask
cache so a follow-up pronoun resolves to it.

**Additional defects found and fixed (small, in the rules' spirit):**

- Mid-stream read timeouts escaped `_stream_output_text` as raw
  `requests.ConnectionError` → HTTP 500 instead of a handled error.
- `FOOTING_RE` must not read "a few steps away" as footing (caught by the
  new tests).
- Spark's malformed JSON (`{"answer": …` unparseable) was spoken raw.
- Labeled-number qualifier in a trailing clause ("find my mailbox, it's
  number 16") was cut with the clause, so the instance instruction never
  fired (`clause_qualifier`).
- Find answers now say where the instance sits within its group —
  case_07.Q1 baseline passed only because the recitation said "right"
  about *other* mailboxes; 14 was always reported as "center".
- Android: the expired-follow-up path turned the screen into an Ask
  screen with no camera bound ("Camera is not ready yet"); a typed
  question survived the return to Ask and would be resent silently.

**Listed, not fixed:**

- `NAVIGATION_RE` misses "is the way ahead clear?" — Jev covers it on
  device; with Jev off it is kind `other`.
- "Are there stairs in my way?" makes stairs the goal (goal word). Needs
  a destination-vs-subject call.
- Synthesis's own output still reorders the lead and mojibakes; the lead
  check now rejects that (→ retry → error). Rare now that drafts are
  short, but it is the stage most likely to produce a loud error.

## 2. Failure rule — stages that degraded silently

Every model-backed stage now runs through `_run_stage`: one bounded retry
with a fresh budget, then `StageFailed` → HTTP 503 `{"error":
"stage_failed", "stage", "message": "Something went wrong — please try
again."}`. Kill switches (SYNTHESIS/FLOOR_LOOK/AUDIO/JEV off, no Jev key)
are configuration and never error. Escalated answers skip synthesis so a
polish failure can never stand between the user and the handoff.

| stage | silent degradation before | now |
|---|---|---|
| synthesis | stall/reject → unsynthesized draft shipped | retry → 503 (lead must stay first) |
| naming (Spark) | failure → scene from the safety list only | retry → 503 |
| segmentation (SAM) | failed phrase → `[]`, a hole in the scene | retry → 503 |
| label reads (Flow C) | API error read as NONE → "I cannot confirm ibuprofen" | retry → 503 |
| floor look | any failure → no entries, path certified unchecked | retry → 503 |
| transcription (ASR + ffmpeg) | failure → "I didn't catch a question" | retry → 503; no audio stream / silence stays legitimate |
| intent gate (Jev, enabled) | error → silent regex-only gate | retry → 503 (0/102 Jev failures in the last 4 runs) |
| answer (Spark) | empty → 502, broken JSON → spoken raw | retry → 503 |

## 3–4. Android

- No spoken "You asked:" — the script is the answer (escalation line
  first when escalated, warnings as today). "I didn't catch a question…"
  stays. Replay speaks the same script.
- Answer screen: "◀ New question" is the first control under the title.
  System Back after an ask returns to Ask; from History, to History.
- Follow-up mode: banner "Follow-up on: <prior question>" with its own
  "New question" exit; announced "Follow-up on: <q>. Hold and talk to
  ask." Hold-and-talk now listens while held (before, speech during a
  hold was lost: listening started on release); tap keeps the timed
  window for TalkBack. No camera. A spent follow-up screen closes once
  its answer arrives.
- 503/502 speak "Something went wrong — please try again." as-is.

Emulator-verified (Pixel_10 AVD, API 37, stub backend): TTS log shows the
answer with no echo; Replay identical; Back → Ask with a single
MainActivity on the stack; follow-up banner, announcement, no camera
open; follow-up answer then Back → Ask; banner exit → fresh Ask; History
→ Answer → Back → History; 503 → "Something went wrong — please try
again."; a 3 s hold opens "Listening…" and resolves on release. Real
voice capture is Charles's device check.

## Model tier (steering 2026-10-27)

Wonder is no longer used. Naming, answer composition, crop reads (Flow C
and floor look) and synthesis run on `muse-spark-1.3-contributor`, which
**trains on inputs, photos included** — consented by Charles for his test
data for this event. `SPARK_MODEL=muse-spark-1.3` reverts in one line;
revert before any non-Charles traffic. SAM and ASR unchanged. Baseten
Llama is the planned synthesis fallback (not wired; pending approval).
Synthesis budget 60 s per read gap (contributor tier measured 11–46 s on
a full rewrite).

## Verification

- Offline: `backend/tests/ask_offline_check.py` — 120 checks, FAILURES
  none (was ~70). New: ground-cover exclusion (patio/lawn/verge/"patio
  ahead"), rule-1 ordering, goal-echo preservation through the rebuilt
  chain, question kinds, distance lead + pronoun referent, path leads
  (one mention per object, door named for "exit"), tidy, synthesis
  stall → retry → `StageFailed` (exactly 2 calls, same budget),
  retry-then-success, lead-reorder rejection, kill switch and short-draft
  skip as non-failures, SAM/ASR/Jev errors → `StageFailed`, end-to-end
  `_compose`/`run_followup` with stubbed models (table walk, dark, glasses
  follow-up).
- Eval, final run `20261027-182618`: **23/26, 0 errors** — reds are
  exactly the known trio (case_02.Q2 accepted; case_07.Q3 — now locates
  16 correctly, "right-hand column, just above the 4P locker", and fails
  only the literal `middle` where it says "center"; case_13.Q1 `stand`
  drift). No new reds. 26/26 drafts were contract-short, so synthesis
  skipped everywhere (0 applied, 0 failed).
- Intermediate run `20261027-181006` was 22/26: exit_room failed because
  the lead said "exit" for the door it found; fixed (goal words name the
  thing found) before the final run.

Navigation answers, verbatim, before (`20261027-162949`) → after
(`20261027-182618`). The set has no patio/table-walk photo; the table
walk is covered offline (end-to-end stub) and by Charles's device retest.

**exit_room** — before: "Careful — brown plush toy is in your path at 2
o'clock. The open door is at your 2 o'clock, close. A brown plush toy is
at your 2 o'clock, a few steps away. A microphone is also at your 2
o'clock, a few steps away. You are on floor. For orientation, a cat is at
your 11 o'clock, and a chair is at your 10 o'clock."
→ after: "Careful — toothpick is in your path at 3 o'clock, a few steps
away. The door is at your 2 o'clock, close."

**path_clear_dusk** — before: "Careful — grass verge is in your path at 1
o'clock. Grass verge at your 1 o'clock, close by. Warning: manhole at your
1 o'clock, far ahead. Warning: puddle at your 11 o'clock, far ahead. You
are on a sidewalk. Lamppost at your 12 o'clock, far ahead. Sign at your 2
o'clock, far ahead."
→ after: "Careful — manhole cover at 1 o'clock, far away, and puddle at 11
o'clock, far away, are in your path. The sidewalk is wet underfoot."

**fridge_ice** — before: "Careful — countertop appliance is in your path
at 1 o'clock. The black refrigerator is at your 1 o'clock, close. A
countertop appliance is at your 1 o'clock, close. Ice cubes are at your 12
o'clock, a few steps away. An ice cube is at your 12 o'clock, a few steps
away. Ice cubes are at your 1 o'clock, a few steps away. You are on tile
floor. Watch your step. Cabinets at your 2 o'clock are off to the side."
→ after: "Careful — ice cubes are in your path at 12 and 1 o'clock, a few
steps away. The fridge is at your 1 o'clock, close."

**case_03.Q1** (ground-cover lead before) — before: "Careful — grass is in
your path at 1 o'clock. Grass is at your 1 o'clock, close by. Warning:
manhole at your 1 o'clock, far ahead. Warning: puddle at …"
→ after: "Careful — manhole cover at 1 o'clock, far away, and water puddle
at 11 o'clock, far away, are in your path."

Open for Charles: exit_room's obstruction flips between runs (plush toy,
wrench, toothpick) — perception variance at 3 o'clock, not assembly.

## Files changed

backend/app/pipeline.py, backend/app/main.py,
backend/tests/ask_offline_check.py, android/app/src/main/java/ai/alumbra/
app/{AnswerActivity,AskApi,HistoryActivity,MainActivity,Spoken}.kt,
android/app/src/main/res/layout/{activity_answer,activity_main}.xml,
android/app/src/main/res/values/strings.xml; new: this file.
`assembleDebug` + `lintDebug` pass (no new lint classes).

## Live-test round (2026-10-27 evening)

### 1. Counting / instances

**Evidence check first.** The live ask "How many cats are in this room?"
(ask `4b2e3d7f…`, frame pulled from the phone's cache) did **not** hold
one cat entry: History's stored scene has two `tortoiseshell cat` entries
(10 and 2 o'clock), and re-running SAM on the frame gives the same two.
The frame shows two of the four cats (the second cut off by the right
edge); the other two were out of shot. The "one entry" reading came from
the log line, which deduplicated names (`scene_objects` listed
`tortoiseshell cat` once). The positions in the answer were the scene's.
What was actually wrong: Spark did the counting ("1 full and part of
another", "in this room") and the merge rule *could* fuse distinct
instances (IoU > 0.5 plus transitive chaining).

- **Merge = echoes only.** Same head noun and IoU ≥ 0.8, or the smaller
  box ≥ 90% inside the larger at ≥ 0.7 area ratio. An entry joins a group
  only if it echoes *every* member (no chaining). Overlapping distinct
  instances stay separate.
- **Counts are code.** New question kind `count` (after distance:
  "how many steps" stays distance). `count_look` SAMs the asked noun on
  fresh asks and follow-ups (the "how many cups" follow-up segments
  "cup"). `_count_opening` writes the lead from scene instances. The
  lead includes clock positions, and instances cut off by the frame edge
  are reported as "part of another" / "part of a fourth". It says "in
  this photo", never "in this room". The `_tidy` step drops any body
  sentence that names the counted noun again, so the model's tally never
  reaches the user. Patch 5's "count carefully" prompt is gone.
- Group nouns ("mailbox banks") with no entries of their own count their
  member noun (case_07.Q2 — SAM finds nothing for "mailbox bank").
- Log line: `scene_objects` now shows instance counts
  (`tortoiseshell cat x2`).

### 2. Recording state

The big button turns red (#D7263D, white ink and border) for the whole
time the mic is live: "Recording… / Release to send" on a hold (normal
clip and follow-up), and "Listening… / Speak your question now" on a tap
listen window, where the finger is already up. The content description
changes with it. It returns to amber the moment the mic closes. The
emulator screenshots confirm all three paths.

### 3. TTS audit (app voice = answers + true state only)

| line | before | now |
|---|---|---|
| answer, escalation line, "Still trying to reach someone…", "I didn't catch a question…" | spoken | spoken |
| errors (photo/clip/recording failed, camera unavailable/not ready, server errors, expired/media gone, permissions denied, nothing heard) | spoken | spoken |
| "Sent, analyzing." / "Clip sent, analyzing." / "Follow-up sent, analyzing." | spoken | spoken: ask in flight, 10–40 s wait (judgment call, one line to drop) |
| "I heard: <transcript>" before "Sent" | spoken | **removed** (interaction echo; question is on screen and in History) |
| "Answer arrived." | spoken | status text only (the Answer screen speaks the answer) |
| "New question. Hold the button…" (back to Ask) | spoken | status text only |
| "Follow-up on: <q>. Hold and talk to ask." | spoken | status text/banner only — supersedes this morning's "announced" |
| Flashlight on/off, Cancelled, "No answer yet" | spoken | status text only |
| permission preamble before the system dialog; "Hold the button…" after grant | spoken | status text only |
| "Voice unavailable." | spoken (engine dead anyway) | status text only |

Status text is a polite live region, so TalkBack users still hear these
lines. Recording/listening are signalled by an earcon and haptics, plus
the red button; the mic is hot, so app voice must stay silent then.

### Verification (this round)

- Offline: 141 checks, FAILURES none. New checks: overlapping cats stay
  two; a cross-phrase echo merges; IoU 0.67 is not an echo; a contained
  near-equal box is an echo; a small cat inside a big cat's box survives;
  echoes don't chain; count subject and kind, including "how many steps"
  staying a distance question; plurals; the live cat frame (two, one
  partial), one, zero, group noun (case_07.Q2), and the span form;
  end-to-end, the model's "3 cats" is dropped for the scene's two; the
  follow-up "how many cups" segments "cup" and answers none.
- Eval `20261027-205417`: **24/26, 0 errors**. Reds: case_02.Q2
  (accepted) and case_13.Q1 (`stand` drift). case_07.Q3 passed this run
  (known flake). case_07.Q2 (code-counted): "I can see four mailbox
  banks in this photo: three full at your 11, 12 and 2 o'clock, and part
  of a fourth at the edge, at your 3 o'clock."
- Seen, not fixed: path_clear_dusk's body added "No, the way ahead is
  blocked." after the code lead. Spark restated the verdict, and `_tidy`
  doesn't treat a verdict sentence as a restatement. exit_room came out
  clear this run (the plush toy was off the corridor) and fridge_ice's
  floor look read "pills". Both are perception variance; neither check
  failed.

## Live-test round 2 (2026-10-28)

I couldn't pull the live door/cat ask: the server logs only to its
terminal, and the phone wasn't connected. That gap is item 4 below.
The fixes were checked against the eval's `office_exit_still.jpg` (same
room: door, tabby cat, guitars) and a fixture that reproduces your
geometry.

### 1. Route verdict tested against the passage

The route was tested against the **goal's clock** (±1 hour). When the
goal is the door leaf, the opening beside it was never tested, so a cat
in the doorway at 10 o'clock was cleared while guitars at the leaf's
clock were flagged.

Now (`_passage`, `_in_corridor`, `passage_look`):

- The **passage** is where the route ends. For a door it is the
  opening: SAM "open doorway", or a doorway/hallway entry, beside or
  over the leaf, nearest first. If none is found, an open leaf is widened
  by its own width on each side, and a closed leaf is its own opening.
  Any other goal is its own box. With no goal, the route runs straight
  ahead.
- The **corridor** runs from your feet (the middle 40% of the frame
  bottom) to the passage's base. An object is on the route when its
  footprint (box base) overlaps the corridor at that depth by at least
  10% of the narrower of the two, or when it lies in the opening's lower
  band. Clock offset plays no part. There is no margin at the passage,
  so wall-side objects beside an opening are not in it.
- The lead points at the way through, not the leaf: "The way through
  the door is at your 10 o'clock".
- Door goals are decided by head noun, so a "french-door refrigerator"
  is a fridge.
- On office_exit_still the opening is 625–768 px. The cat (tail across
  the corridor) is on the route. The chair's arm grazes 16 px of box
  edge and is not.
- Limit: when no opening is found, the widened-leaf fallback can still
  catch wall-side objects next to an open leaf. That's what
  `passage_source` in the log line (`leaf_widened`) is for.

### 2. Counting over clips

`clip_union` takes 6 frames from the clip (ffmpeg), segments the asked
noun in each, and estimates the camera pan between consecutive frames.
That estimate is a thumbnail search (translation only, pure PIL — no
numpy in the venv). Boxes are mapped into the sent photo's coordinates
and clustered across frames, never within one. A box sliced by a frame
edge matches the full view it sits inside. If the sweep can't be
registered, the count falls back to the best single frame (logged as
`max_frame`), never a guess. Clip counts say "in this clip". The clip is
cached for follow-up counts. Live check on a synthetic 4 s sweep over
your cat photo: the middle frame alone says "one cat"; the union says
two, the off-frame one partial (3 s total). Limit: a pan only — turning
on the spot (rotation) or walking forward (zoom) registers poorly and
falls back.

### 3. Chips

`ship_scene` collapses entries with the same name and clock in the
shipped `scene_model`, carrying `instances: n`. The cache, counting and
the path use the full scene; `_compose` now returns it.

### 4. Dev capture store (`backend/app/capture.py`)

Per /ask and /followup, in `backend/captures/<date>/<timestamp>-<kind>/`:
photo, clip and audio as uploaded, `request.json`, then `response.json`
plus `trace.json` (full uncollapsed scene, ask id, referent), or
`error.json` (status + stage) for failed asks. The newest 50 are kept,
pruned on each new ask. On by default through 2026-11-01, then off by
itself; always off with `ALUMBRA_ENV=production`; `CAPTURE_ENABLED=0/1`
overrides. `captures/` is gitignored (personal photos). Note: an eval
run against a capturing server writes 26 captures and pushes live ones
out. I ran mine with `CAPTURE_ENABLED=0`.

### Verification (round 2)

- Offline: 167 checks, FAILURES none. New checks cover:
  - your live geometry (opening at 10, leaf at 12, guitars at 1): the
    cat is on the route, the guitars aren't, and the lead points
    through the opening;
  - a hallway entry used as the opening;
  - guitars alone leave the route clear;
  - a bench beside the table vs in front of it;
  - a box edge grazing the corridor vs an ice cube inside it;
  - the french-door fridge;
  - baseboard;
  - chips collapse while counts stay whole;
  - the clip union on a synthetic sweep (one from the photo, two from
    the clip), the unregistrable-sweep fallback, and pan recovery;
  - capture store: files written, failed ask kept with its stage,
    follow-up parent recorded, pruning, production off, date default.
- Eval `20261028-110820` (CAPTURE_ENABLED=0): **23/26, 0 errors**. Reds
  are the known trio: case_02.Q2 accepted, case_07.Q3 literal `middle`
  (flaky), case_13.Q1 `stand`. Nav answers:
  - exit_room: "Careful — fluffy tabby cat … at 11 o'clock … The way
    through the door is at your 1 o'clock, close."
  - fridge_ice: ice at 12 and 1, fridge at 1.
  - dusk: manhole + puddle.
- That run surfaced "white baseboard" as an obstruction in exit_room and
  fridge_ice. Baseboard/skirting/molding are now wall surfaces. I
  replayed that run's own scenes offline rather than rerunning the
  25-minute eval. Results: exit_room is the cat only, fridge_ice is ice
  only, dusk is unchanged; each answer still meets its frozen check.

### Key frames for clip counts (after live test 3)

Live: a pan from the chairs to the window ("how many cats", 3 by the
chairs, 1 on the sill) answered three. The capture (`11:37:32`) showed
why: 6 evenly spaced frames over 5.6 s could not link a fast turn. The
desk, curtain and window frames barely overlapped, one was
motion-blurred, so the union fell back to frame 0 (`max_frame`).

Now `key_frames` scans the clip locally at 8 fps (45 frames, about 2 s,
no model calls). Consecutive scan frames always overlap, so the pan
links the whole sweep. A new key frame is cut once the view has moved
60% of a frame, using the sharpest frame in each view. SAM runs on the
key frames only, at most 8. A still clip costs 1 SAM call; this sweep
cost 5. A sweep that breaks into unlinked segments is counted per
segment and said as "about N" with no positions. More than 3 segments
is not a sweep (shake, blank wall), so it falls back to the best single
key frame.

Replay of the captured clip: key frames [0, 29, 31, 33, 41] of 45, one
segment, **four cats** (sill cat included), 6.4 s in total. Clocks are
relative to the sent photo, the clip's middle frame. In a sweep that
frame is mid-turn, so positions are "where the phone pointed halfway
through"; things beyond the photo clamp to 9 or 3 o'clock.
