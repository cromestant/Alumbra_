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

## Follow-up TTL & persistence (2026-10-28)

**On-disk layout:** one store, the capture folders.

```
backend/captures/2026-10-28/
  20261028-121237-…-ask/        root ask
    photo.jpg  clip.mp4         the only copy of the media
    request.json response.json trace.json
    cache-<ask_id>.json         cache entry: created, scene (full), flow,
                                target, question, answer, behavior,
                                floor_done, read_targets, referent,
                                media_dir = "2026-10-28/20261028-121237-…-ask"
  20261028-121304-…-followup/   follow-up
    audio.wav (if spoken)  request.json response.json trace.json
    cache-<new_id>.json         media_dir -> the ROOT ask's folder
```

**Lookup:** `_load_ask` checks memory (the hot layer, 32 entries).
On a miss, with the store on, it globs `captures/*/*/cache-<ask_id>.json`
and reads the photo/clip bytes from that entry's `media_dir`. The ask id
is checked against `[A-Za-z0-9_-]{1,64}` before any glob, and
`media_dir` must resolve inside `captures/`. If the entry is missing,
corrupt, or its photo is gone, the result is the existing 410 with the
same message (now one constant, `ASK_EXPIRED_MESSAGE`, same text). No
index file: the glob is over at most 50 folders, and an index can't go
stale.

**One copy of each media file:** cache entries hold no bytes. A
follow-up's entry points at its root ask's folder, and so does every
later link in the chain. Verified live: one photo on disk for a
three-ask chain.

**TTL:** `ASK_TTL_S` env, default 3600 s, counted from each entry's
`created`. Each follow-up mints a new id with a fresh TTL (unchanged).

**Expiry deletes on disk:** every store, every load, and server start
(`sweep_ask_store` in `create_app`) sweeps. Expired `cache-*.json` files
are deleted. A folder's `photo.*`/`clip.*`/`audio.*` are deleted once
no unexpired entry holds it (an entry holds its own folder and its
`media_dir`), so a live follow-up keeps its root's photo. The sweep runs
even with the store off: what an earlier run persisted never outlives
its conversation. Request/response/trace JSON stays under the capture
store's newest-50 cap.

**Store off** (`ALUMBRA_ENV=production`, `CAPTURE_ENABLED=0`, or after
2026-11-01): nothing is written, the cache is memory-only, and a restart
ends the chain, exactly as before. Production follow-up persistence is a
separate future decision.

**Failure posture:** a failed disk write is logged and the ask
continues memory-only. A failed rehydration is an expiry (410).

**Bug found while testing:** `_store_ask` swept before writing. A
follow-up whose parent expired while it was being answered (loaded at
TTL−ε, ~30 s to answer) would have lost the root's photo before its own
entry claimed it. Now it writes first, then sweeps. The new offline
test fails with the old order (checked) and passes with the fix.

**For Charles — two consequences of "expiry deletes":**
1. A successful ask's photo/clip now lives only as long as its
   conversation: one hour after the last follow-up, not "the last 50
   asks". The capture store's after-the-fact media audit is now an
   hour.
2. A failed ask (503/502/410) has no cache entry, so its media stays
   under the newest-50 cap. That's what the capture store was for, but
   it means those photos outlive the hour.

If either should go the other way, it's a one-line change. Also, more
than 50 asks within an hour would push a live chain's root folder out
under the count cap (its follow-up then gets a 410).

### Verification (TTL & persistence)

- Offline: 188 checks, FAILURES none. New checks:
  - TTL default 3600;
  - entry persisted next to its capture;
  - restart (memory cleared) rehydrates the same scene, photo and clip;
  - `run_followup` on the original id after a restart answers from the
    cached scene;
  - the follow-up references the root's media, never copies it; one
    photo on disk;
  - TTL−5 s is live; TTL+5 s gives the exact 410 message, with media
    and cache entry gone;
  - the expired follow-up's entry is gone;
  - a parent that expired mid-answer still leaves its photo held by the
    new follow-up;
  - when the whole chain expires, root photo and follow-up audio are
    gone;
  - a corrupt entry gives 410;
  - a failed disk write stays memory-only;
  - store off writes nothing, and a restart ends the chain;
  - `ASK_TTL_S` overrides the TTL.
- Live, port 8124 (scratch captures folder):
  - Ask (200, 25.9 s); kill the server; start a fresh process with
    `ASK_TTL_S=45`; follow-up on the original id: **200** in 9.7 s, same
    scene ("Yes, there is a cat at your 11 o'clock… next to the office
    chair").
  - Follow-up at 38 s of 45: 200.
  - After the chain passed its TTL: **410**
    `{"detail":"that ask has expired or is unknown — please ask again as a new question"}`.
  - Media left on disk: 0; cache files: 0.
- Eval `20261028-121502` (CAPTURE_ENABLED=0): **23/26, 0 errors**. The
  reds are the known trio (case_02.Q2 accepted, case_07.Q3 literal
  `middle`, case_13.Q1 `stand`). No new reds.

## Count cap vs live chains (2026-10-28)

- `capture._prune` (newest-50) now skips every folder an unexpired cache
  entry holds — its own folder or a chain's `media_dir` — using the same
  `_holders()` rule as `sweep_asks`, and never the folder just created.
  The store can therefore exceed 50 by the number of live-held folders;
  they drop back under the cap once their chains expire.
- The TTL (`ASK_TTL_DEFAULT_S`, `ask_ttl_s()`) moved to `capture.py`
  so the cap can apply it; `pipeline` re-exports both names.
- Test: a live chain rooted at the oldest folder, plus 50 newer asks
  (51 in all, inside the TTL). The root survives with its photo, the
  folder count stays 50, and `/followup` on the chain returns 200. With
  the guard removed (scratch copy) the test fails: the root is evicted
  and the follow-up gets a 410.

## Baseten primary synthesis (2026-10-28)

- First synthesis attempt: Baseten `meta-models/muse-spark-open-1026`
  (`/chat/completions`, non-streaming, `reasoning_effort="low"`,
  temperature 0.2), 8 s budget. The retry is contributor Spark with
  its own 60 s budget, under the same `_run_stage` retry-or-error rule.
  The preservation check gates both. Log line: `synth_provider`.
- `BASETEN_ENABLED=0`, or no `BASETEN_API_KEY`, makes both attempts
  Spark (as before). `BASETEN_SYNTH_MODEL` overrides the model.
- **Deviation:** `max_tokens` is 2000, not ~1200. Measured: even at
  "low", reasoning took 630–1340 tokens. At 1200, 1 of 13 calls was
  cut off with empty content; at 2000, 0 of 8 were.
- Live: 2.5–4.7 s per call. One rewrite opened with "No, the way is not
  clear." ahead of the draft's lead, and the lead-first check rejected
  it.
- Eval `20261028-151226` (CAPTURE_ENABLED=0): **22/26, 0 errors**. The
  known trio, plus case_06.Q1 (no "pointed up / sky" orientation line).
  That line comes from the Spark composition prompt, not from either of
  these changes: synthesis ran on 0 of 26 questions here (as in every
  earlier eval). Reran case_06.Q1 alone twice: FAIL, then PASS. This is
  model variance; the check stays frozen. Note that the eval never
  exercises synthesis, so Baseten's evidence is offline plus the live
  probes above.

## Spark synthesis deadline + synthesis-stage eval fixture (2026-10-28)

- Each Spark synthesis attempt now has a **30 s total wall-clock
  deadline** (`SYNTHESIS_DEADLINE_S`, enforced by `_within`: the call
  runs on a daemon thread). The per-read HTTP timeout is 30 s too (was
  60). An attempt still running at 30 s has failed, and the ask errors
  per the existing rule. The abandoned request ends on its own per-read
  timeout. Worst case for synthesis is now Baseten 8 s + Spark 30 s.
  Baseten is unchanged: it is non-streaming, so its 8 s read timeout
  already covers the whole response.
- Offline: a trickling Spark (reads keep arriving, so the per-read
  timeout never fires) fails at the deadline on both attempts →
  StageFailed; a Spark inside its deadline still serves.
- Eval: new `cases.json` → `synthesis` section, run in-process against
  `pipeline.synthesize_answer` with the real providers and graded in the
  normal report. `synth_tablewalk.Q1` is the recorded Oct 27 table-walk
  draft, verbatim. That recording ends at "The lawn is at your 12
  o'clock, far…", so the fixture ends there too; the question wasn't
  recorded, so the fixture assumes "is the way to the table clear?".
  Checks: the stage applied, the warning verbatim, the first sentence
  stays first, patio/table/bench/lawn all kept, output shorter than the
  draft. The report-only line shows which provider served.
- Live: 3/3 PASS on Baseten (3.0–4.6 s), 2/2 PASS on Spark with
  BASETEN_ENABLED=0 (16.4 s, 20.3 s). Output: "Careful — patio is in
  your path at 1 o'clock. The table is at your 12 o'clock, far. The
  bench is at your 12 o'clock, far. The lawn is at your 12 o'clock, far."
- Eval `20261028-154204` (CAPTURE_ENABLED=0): **24/27, 0 errors**,
  including `synth_tablewalk.Q1` PASS (Baseten, 5.2 s). The reds are
  the known trio; case_06.Q1 passed this time.
- That run's output had newlines between sentences (Baseten), which the
  whitespace-normalized checks let through. Synthesis output is now
  collapsed to one line. Offline test added; fixture rerun PASS, one line.

## Metric distance (2026-10-28)

### As landed
- **Pitch sign convention:** `pitch_deg` = degrees the back camera's
  optical axis points DOWN from horizontal. Level = 0, straight at the
  floor = 90, up at the sky negative. On Android it comes from the
  device-frame gravity vector (TYPE_GRAVITY, falling back to the
  accelerometer): `asin(g_z / |g|)`, sampled at `onCaptureStarted` (the
  shutter), else at the `takePicture` call. Roll doesn't affect it, but
  the image is assumed upright.
- **focal_px derivation** (`CaptureGeometry.focalPx`, computed per photo
  from the camera actually bound):
  1. `f_active`: `LENS_INTRINSIC_CALIBRATION` fx when the device reports
     it, else `f_mm / SENSOR_INFO_PHYSICAL_SIZE.width × PIXEL_ARRAY.width`.
  2. Stream scale. The ImageCapture stream is bound beside VideoCapture
     HD, so it comes out 16:9: 1280×720 in sensor orientation, which
     matches every capture photo at 720×1280. It is the largest centered
     crop of the active array at the stream's aspect, scaled to the
     stream. For 16:9 from a 4:3 sensor that is the full width:
     `scale = 1280 / active.width`.
  3. × zoom ratio (1.0; the app never zooms).
  4. × (JPEG long side / CameraX cropRect long side), which is 1.0 when
     nothing more is cropped. The JPEG is rotated to portrait, never
     rescaled, and pixels are square, so the same focal_px applies on
     both axes.
  - **Principal point:** assumed at the image center.
  - Every input is logged (`adb logcat -s Alumbra`, line `focal_px=…`).
    Emulator (Pixel_10 AVD): calibration and spec agree, focal_px 1914
    for a 1856×1392 stream. **The Pixel 9a values are not yet read off
    the device** (no phone on adb this session).
- **Prior table:** door 2.03 m, person 1.70 m, car 1.50 m, matched by
  head noun ("wooden door", "doors"). Nothing else.
- **Gates:**
  - ground: depression ≥ 5°, base not within 3 px of the frame bottom,
    result in [0.3, 12] m;
  - prior: box not within 3 px of the frame top or bottom, same range;
  - reconcile: within 40% of the ground value → ground, `both`; beyond
    that → ground, with `disagree` logged.
- **Floor-standing** = not a surface or ground cover, not resting on a
  holder, and inside the existing rule-2 floor band
  (`_stands_on_floor`). Ground-plane estimates are made only for those,
  so "disagree, not floor-standing → drop" can't fire. Consequence: a
  floor object whose base sits above the band for its size label (for
  example a big object far away at a level pitch) gets no ground value
  and keeps its label. I kept this deliberately; a looser rule gives
  wall-mounted things floor distances.
- `CAM_HEIGHT_M` (default 1.3) and `METRIC_DISTANCE_ENABLED=0` (kill
  switch) are honored.

### Speech
- Bands: < 0.4 m "within arm's reach"; < 0.7 "about half a meter
  away"; ≤ 3 m nearest half meter ("about 2 and a half meters away");
  beyond, nearest meter.
- Used by the path lead, the goal line, "how far" and "On the way", via
  one `_dist_words`: meters when there is a distance, the label
  otherwise. Never both for one object.
- Spark's scene shows the spoken words under `distance` in place of
  `size_class` and the float. The find flow is untouched (`_prompt_scene`
  returns its scene as-is).
- On-path objects are ordered nearest-first when every one has a
  distance; hazards likewise. Neither changes what is on the path.
- Synthesis must-keep: the set of distance phrases in the output must
  equal the draft's, so none are dropped, re-rounded or invented. They
  are also listed in MUST KEEP.

### Old-app identity
- No fields → `camera None`: `apply_distances` returns the same scene
  objects, there is no `goal_distance_m` or `distance_m`, and the log
  line and `request.json` gain no keys. Offline e2e: the table walk with
  no metadata is word-for-word today's answer.
- Clip asks don't send the fields yet; their photo is a frame taken from
  the video, a different capture path.

### Verification
- Offline: 243 checks.
  - Geometry: level 2.0 m within ±0.05; pitched 35°/1.0, 30°/3.0,
    45°/1.5 and 10°/6.0 exact to 2 dp; camera height scaling.
  - Gates: φ < 5°, bottom-clipped, > 12 m, < 0.3 m / past straight
    down.
  - Door prior at 4.0 m; prior table and clipping.
  - Reconcile: all four branches; rounding bands; parse; CAM_HEIGHT_M.
  - Per instance: chair 2.0 m by ground; mug on a table and the wall
    get no metric; door at 2.5 m from both; door disagreement logged.
  - Old app, kill switch, nearest-first ordering for path and hazards,
    "distance never gates", Spark scene, find untouched, synthesis
    must-keep.
  - e2e with and without metadata; route parse/record and old-app
    request.json.
- Emulator end to end on scratch :8124: the app sent
  `pitch_deg=22.84 focal_px=1914.0`, the server recorded it in
  `request.json` and the log line, and the answer came back (no floor
  objects in the emulator's synthetic scene, so `distances: []`).
- **Not yet run:** the tape-measure check (needs the phone).
- Eval `20261028-164243` (no metadata, i.e. old-app path;
  CAPTURE_ENABLED=0): **25/27, 0 errors**. Reds: case_02.Q2 and
  case_13.Q1 (known). case_07.Q3 passed this time (model variance; the
  check is unchanged). No `camera` key in any log line. The one "arm's
  reach" in the run is Spark's own find-flow wording for the glasses
  ("an arm's reach away"); the metric code did not produce it.

### Device test fix (Oct 29): sideways tap photos

Pixel 9a tap photos arrive at 4000x3000 with EXIF orientation 6, and nothing in the backend applied the tag. Every tap-photo ask was therefore read on its side, which affects boxes, clock positions, the floor band and the new ground geometry. Live, that gave a 3.9 m ghost distance for a bottle on the desk. Clip frames (720x1280, no tag) were never affected. The bug predates the metric work: the ImageCapture setup is unchanged from HEAD.

- `metric.upright_photo` applies the EXIF tag and caps the long side at 1280 (the clip-frame size). An upright in-size upload passes through as the same bytes, so old uploads are unchanged.
- In `/ask`, the capture saves the upload as received; the pipeline then gets the upright photo, and `focal_px` is scaled by the same factor (2694.1 → 862.1). `request.json` records the camera as sent.
- `capture.load_ask` normalizes the stored original on a follow-up reload. The function is deterministic, so the reload produces the same pixels.
- Offline: 251 checks pass with no failures (8 new).
- Device: pitch is checked (3.3 / 41.6 / 81.9 for level / ~30° / floor) and focal is checked (calibration fx 2694.1 against 2831 from the spec sheet).

### Tape-measure follow-up (Oct 29)

Replaying the live tape tests (phone at ~5 ft, focal 862.1 at 960x1280):

| Placed | Old (h 1.3) | Now (h 1.52) | Spoken now |
|---|---|---|---|
| 1 m | 0.75 | 0.88 | about 1 meter |
| 2 m, try 1 | 1.72, dropped | 2.01 | about 2 meters |
| 2 m, try 2 | 1.63 | 1.90 | about 2 meters |
| 3 m | 3.06 | 3.57 | about 4 meters |

- `CAM_HEIGHT_DEFAULT_M` is now 1.52 (Charles's measured holding height). `CAM_HEIGHT_M` still overrides it.
- The floor test for distances (`_metric_floor`) now only asks whether the box's base sits below the 40% line, whatever its size label. `_stands_on_floor`, which the path verdicts use, is unchanged, so the 2 m try-1 draft stopper now gets a distance but still isn't on the route.
- `_one_per_box` (IoU ≥ 0.9) keeps the first name when one box comes back under several names. The object stays on the route, so the verdict is unchanged.
- Not changed: the phantom "phone" (a 36x21 px box at the frame's bottom edge). Whether something counts as a hazard is gate and path logic.
- Offline: 256 checks pass with no failures.

### Second tape round (Oct 29, 11:34–11:41)

- **Kitchen spoken as "a few steps away" when it was 5–6 m off.** The island's base sat at 32% of the frame, above the 40% line, so it got no metric distance and fell back to the label. `_metric_floor` now uses the horizon the metadata gives, H/2 − f·tan(pitch), and the base must be below it. Replayed, the island gives 4.7 m, spoken "about 5 meters". Risk: an object mounted on a wall with its base below eye level gets a floor distance that's too far.
- **Same noun, different distances.** `_path_opening` used to merge every same-noun mention into one; it now keeps them separate when their distances differ by more than AGREE_FRAC of the nearer one.
- **Path misses, not changed (path verdicts are out of scope):**
  - "cat scratcher ramp" at 0.9 m: the word "ramp" is in the passage words, so it's treated as walkable.
  - The saucer chair is ruled out as 'elevated' because its base at 45% is under "a few steps away"'s 0.55 line. The "toy" echo of the same box passes under "far"'s 0.4 line and was spoken in its place.
- **Detection misses:** at 17° pitch the 1 m ramp is cut off at the bottom edge of the frame (expected at 5 ft). At 30° the ramp is clearly visible but the naming pass never listed it.
- Offline: 259 checks pass with no failures.

### A + B approved (Oct 29) and segmentation fan-out

- **A.** With capture metadata (and metric enabled), `_not_obstruction` takes `camera` and uses `_metric_floor`, which needs the base below the horizon. Without metadata it uses `_stands_on_floor` as before, so old-app and eval behaviour is unchanged.
- **B.** "ramp" is no longer passable when the name also carries a pet or toy word (`PET_TOY_WORDS`). A wheelchair ramp or a bare ramp stays passable.
- **Fan-out.** The scene build (`sam_many`) and clip keyframes (`sam_many_images`) were already on a pool, `FANOUT_WORKERS=5`. The remaining work:
  - Both pools are now sized from `sam_concurrency()` (env `SAM_CONCURRENCY`, default 6).
  - The floor look's per-region SAM loop, which was sequential, now runs on the same pool and is merged in region order.
  - The `/responses` and ASR calls go through one `requests.Session`, `_HTTP` (pool_maxsize 32), via `_meta_post`, which logs a `"rate_limited"` JSON line on any 429. Retries still go through `_run_stage`.
- Offline: 271 checks pass with no failures. The floor-order test was confirmed to fail when results are merged in finish order.
- Eval, before → after (same day, 8124, CAPTURE_ENABLED=0):
  - **Before** (20261029-123047): 22 pass, 4 fail, 1 error (a 300 s client timeout).
  - **After** (20261029-130300): 24 pass, 3 fail (case_02.Q2, case_07.Q3, case_13.Q1, all known), 0 errors.
  - Median photo-case latency fell from 31 s to 23 s, or 37 s to 23 s by report median. The before run had a slow-API tail (100–300 s on navigation cases). Over Oct 28's runs the median was 36–40 s.
  - No 429s in the after run's server log (the logging is new, so the before run can't be checked the same way).

### Items 1–5 (Oct 29 afternoon)

1. **Clip asks send capture metadata.**
   - While a clip records, the app keeps the gravity samples with their sensor timestamps.
   - The sent frame is now the exact middle frame (OPTION_CLOSEST, not the nearest sync frame), so its instant is known.
   - pitch is the median of the samples within ±150 ms of that instant (`CaptureGeometry.pitchAt`).
   - focal_px is the HD video stream's (`videoCapture.resolutionInfo`), in frame px.
   - The logcat line is `clip meta pitch_deg=… focal_px=… samples=… frame_us=…`.
   - The backend needed nothing new: a 720x1280 upright frame passes through `upright_photo` unchanged.
2. **Objects the naming step misses.**
   - Naming returns 11–14 nouns in a furnished room, and `MAX_NOUNS` 10 cut 1–4 of them on every run. The cap is now 16.
   - The floor-read prompt ("What small object is lying on the floor…") read a cardboard scratcher as "phone" or NONE. The neutral wording reads it 3/3, keeps all three ice reads in the fridge fixture, and still answers NONE on bare floor.
   - The floor look already ran on every path question, so no change was needed there.
3. **A named goal that isn't visible no longer hides what's ahead.**
   - `no_goal_visible` now carries `ahead`, the straight-ahead assessment.
   - The lead adds "The way straight ahead is clear." or the usual "Careful — …".
   - In the dark, the clear sentence is dropped, as with the existing clear verdict.
4. **False detections.**
   - Floor reads naming a body part (foot, hand, toe, finger) are discarded. Shoes are kept.
   - SAM "person" boxes shorter than 3.5% of the frame are dropped. These were the wall-art figures, "person x10".
5. **Latency.**
   - Profile of office_exit, two runs: segmentation under 2 s, naming 3–5 s, floor reads on Spark 8–64 s each (the ask waits on the slowest), final answer 19–28 s.
   - Floor reads now go to Baseten first (10 s budget), with Spark as the retry under a 30 s deadline; `BASETEN_ENABLED=0` makes it Spark only.
   - The same ask now takes 24–28 s (was 71–101 s) with an identical answer.
   - Medication label reads stay on Spark.
- Offline: 281 checks pass with no failures. The old "path lead no goal" unit test was updated to the new behaviour (item 3); no eval check changed.

#### Item 5 revised: race, not Baseten-first

The Baseten-first eval (20261029-141835) scored 23/27 with a median of 24.5 s and no 429s. But fridge_ice failed: Baseten read the floor ice as "rice", "plastic cap" and "white stones". On the fixture crops, Baseten got ice1 and ice2 right 6/6 but called ice3 "garlic cloves" 3/3, while Spark was 9/9 correct in 5–17 s.

`_floor_read` now races the two: Spark's read wins if it lands within 15 s, otherwise Baseten's is used (or Spark's, up to 30 s, if Baseten failed). Both failing raises StageFailed.

Eval 20261029-143157: 22/27, median 25.5 s, 0 errors, 0 429s. fridge_ice passes ("ice cubes … in your path"); the navigation cases take 20–45 s (they were 100–300 s in this morning's slow baseline and 44–73 s on Oct 28). The two new fails don't come from this change:
- case_03.Q1: naming said "wet pavement" instead of "puddle", so scene_must failed. The floor look found 0 in all three runs.
- case_12.Q4: Spark said "can't see aspirin", which isn't on the check's list of phrasings. The find flow isn't touched.

The eval checks stay frozen.

#### First clip test with metadata (Oct 29, 15:37)

The phone sent pitch 24.83, focal 862.1 and 677 gravity samples, and the answer gave meters ("cardboard scratcher … about 2 meters away"). Phone-measured time was 55.7 s.

Replaying the same clip in-process:
- Profile: ASR 1.2 s, naming 7 s, SAM 23 calls in 5 s, floor look 22 s, final Spark 12.8 s.
- The floor look was slow for two reasons: its 8 reads ran in two waves on the 5-worker pool, and one Spark read took 14.9 s against a 15 s grace.
- Fix: reads now run in one wave (a worker per crop, at most 8) with a 10 s grace. The same clip then took 31.6–42.8 s over five runs.
- One of those runs missed the scratcher. Naming didn't list it, and the floor look read it as bare "ramp", which is passable. Floor-look finds now carry `floor_find`, and `_not_obstruction` never treats them as passage.
- Offline: 283 checks pass with no failures.

#### Clip latency, second pass (Oct 29, 16:00)

The field clip at 15:48 took 48 s server-side. Replayed, the stages were: ASR 1.9 s, naming 5–10 s, SAM 5 s, floor look 11 s (within its grace), final answer 16–20 s on Spark.

- **Coded-lead answers race Baseten.** `_race_read` is generalized out of `_floor_read`. When the lead is code-written (`allow_empty`), Spark's answer is used if it lands within 10 s, otherwise Baseten's (15 s budget, 60 s deadline). Find and other answers stay Spark-only, medicine labels included.
  - Eval 20261029-155303: 24/27 (only the known three), median 23.0 s, 0 errors, 0 429s.
- **Naming starts during transcription.** When the question is spoken (audio or clip), `name_scene` runs while ASR does; flow A uses its nouns, and B/C discard them.
  - The floor-look overlap was skipped: its SAM is 1.3 s, and the reads are the cost.
- The same clip now takes 27.8–32.0 s over three runs (it was 48 s).
- Offline: 287 checks pass with no failures.

## Escalation plumbing (Oct 29, 18:40)

ESCALATE now opens a case a volunteer answers from a LAN console; the app waits, then speaks the reply attributed ("<Name>, a volunteer, says: …").

- Backend: `app/escalation.py` (in-memory cases: pending → answered / expired / cancelled), routes in `main.py`: `GET /v1/escalations`, `POST /v1/escalations/{id}/reply`, `GET /v1/escalations/{id}/photo` (console, token), `GET /v1/escalations/{id}` and `POST …/cancel` (app, case id only), `GET /volunteer`. The case is made in `_compose` after the answer is final, so non-escalation answers never touch it. The ask's log line carries `escalation {id, outcome: pending}`; each transition logs `{ask_id, escalation {id, outcome, wait_ms}}`; `escalation.json` lands in the ask's capture folder. The photo is served from the ask's store entry (upright), not copied.
- Access: `VOLUNTEER_TOKEN` (.env), a demo boundary, not a security model. No token or `ESCALATION_ENABLED=0` → no case, all routes 404. Window `ESCALATION_WINDOW_S` (default 120), sent in the response. Restart loses pending cases.
- Android: Answer screen waits on a fresh ESCALATE with a case (status line, 72dp Stop waiting, cancel on leave, survives rotation). Reply → its own VOLUNTEER History record whose text is built only by `Spoken.volunteerSays`; timeout → fallback line on Ask; no case → old placeholder unchanged.
- Drill (8124): ask 17:54:17 → response + case visible 17:54:52 → reply 17:54:57 → app poll answered 17:54:57. Timeout (40 s window): expired, late reply 409. Kill switch: same response keys minus `escalation`, routes 404.
- Eval 20261029-180025 (escalation on): 23/26 passed, failures the known case_02.Q2 / case_07.Q3 / case_13.Q1; fridge_ice errored on a jev DNS failure, then failed once on a Baseten floor read ("white oval objects") with Spark slow (median 35 s this run), then passed. Escalation cases all passed. Offline checks 309/309.
- Not yet run: the phone + laptop drill.

## Voxtral voice wiring (Oct 30, 13:40)

Answers are spoken in the product voice (Voxtral, profile `sheila` by default) and the app's fixed state lines ship as bundled clips in the same voice; phone TTS stays the floor.

- Backend (`pipeline.py`): `synthesize_speech` (raw requests, 20 s timeout, one retry then None, decodes the `audio_data` JSON envelope and rejects anything that is not MP3), `TTS_PROFILES` (sheila 3-0/voice_id/voice_478, paul and jane 2603/voice), `TTS_VOICE_PROFILE` / `TTS_ENABLED`. `answer_audio` builds the script exactly as the app speaks it (escalation line, "didn't catch" preamble when question_source is none, answer verbatim; never the question). Fixed lines are cached under `backend/cache/tts/`; answer text is one uncached call; parts are joined with ffmpeg stream copy. Runs in `_compose`, so follow-ups get audio too. `/ask` gains `audio` (base64 MP3) only; the log line gains `tts {profile, chars, ms, cache_hit, parts}` or null.
- `backend/tools/render_voice_lines.py` reads every literal `const val` from Spoken.kt (minus the two answer-attached lines and ERROR_PREFIX, plus the four `errorDetail` combinations) and writes `android/app/src/main/assets/voice/*.mp3` + `manifest.json`: 42 clips, 744 KB. Three spoken literals moved into Spoken.kt (wording unchanged). `.gitignore`: root `assets/` was also hiding the app's assets dir, so it now has an exception; `cache/` is ignored.
- Android: `AlumbraTts` is now one ordered queue (clip / answer file / TTS, one at a time). Text matching a manifest line exactly plays its clip; a missing or corrupt clip or file falls back to TTS of the same words. Clips play at the Settings rate. Answer audio is saved per History record (pruned with the record); Answer screen, History and the Ask-screen Replay use it when present.
- Verification: spike texts through the helper are real MP3 (21.5 s / 3.9 s / 4.9 s, 22050 Hz mono). E2E: mailbox and crossing both carried audio; Meta ASR of the crossing audio begins with the escalation line verbatim (its "center. center." / "with with" repeats are ASR artifacts: Mistral's transcriber reads both files word-exact). Second identical ESCALATE: fixed line cache_hit, 0 ms vs 1292 ms (TTS total 2262 vs 2766 ms; the answer part runs alongside, so it bounds the saving). TTS_ENABLED=0: no `audio`, `tts: null`, no other key changes. Offline drill (emulator, network off): photo_sent, error_server_unreachable and server_unreachable all played from clips, none fell back to TTS.
- Eval 20261030-130636 (TTS on): 23/26 + synthesis, failures the known three; every ask carried audio, no TTS failures, TTS median 1.8 s, ask median 25.2 s. Offline checks 321/321.
