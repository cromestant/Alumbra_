# Potential enhancements

Ideas raised and measured, but not built. Each has the evidence behind
it and what it would cost. Newest first.

## Perception

### Combined naming list (Spark + Baseten) for the SAM wave
Run Baseten naming alongside Spark naming and send the union of the two
lists to SAM in the same wave.

- **Why:** each model misses things the other names. In the 2026-11-02
  benchmark (10 eval photos, 2 runs each), Spark named a fire hydrant
  and trash bin on a sidewalk 2 of 2 times and Baseten 1 of 2; Baseten
  never named the persimmon photo's paved path, which Spark named 1 of 2.
  Baseten alone found a charging cable, a puddle and wet footprints.
- **Timing:** Baseten names in 6.3 s median (max 7.5 s), Spark 7.4 s
  (max 11.1 s), so the Baseten list is ready before the wave starts.
  Little added delay.
- **Quality:** the two models' lists overlap about as much as two Spark
  runs do (exact-phrase Jaccard 0.31 vs 0.33). No invented objects seen.
  SAM drops a wrong word when it finds nothing for it.
- **Cost:** one Baseten call and about 5 more SAM calls per ask.
- **Rejected variant:** sending Baseten's list to the answer as text
  only. Its objects would have no boxes, so the answer would state
  positions nothing measured.

### General-noun fallback when SAM misses the asked word
When SAM finds nothing for the asked word, ask a model for its everyday
noun ("ragdoll" -> "cat", "mailbox bank" -> "mailbox") and run SAM on
that.

- **Why:** "Where is my ragdoll?" (VPS, 2026-11-02): SAM found no
  ragdoll; the answer's position came from Spark looking at the photo,
  unmeasured (it was right). "How many mailbox banks?" answered "none"
  when naming worded them "cluster mailbox units".
- **How:** start the noun request (Baseten) when the question is known;
  use it only on a miss. Matches join the scene under the user's word.
- **Added time:** 0 s when SAM hits; about 0 s on a count miss (it
  finishes during naming); about +4 s on a find miss.
- **Open decisions:** several matches ("two cats"): say so, don't pick
  one. Both words miss but Spark sees it: say it is unmeasured ("I think
  it's at 10 o'clock, but I couldn't locate it exactly").

### Clip asks: search more than the middle frame
An ask with apples in the clip missed them: only the middle frame was
used for naming. Options: search every key frame for named items
(generalising clip_union), or send the sharpest frame.

### Skip the fixed hazard checks on count questions
Car, person, stairs, door and curb are searched on every ask; a count
question doesn't use them. Saves about 5 SAM calls per count ask. Go
carefully: these checks exist for safety.

### Naming wasted on spoken find questions
With a spoken question, naming starts while the question is being
transcribed, and a find question discards it (VPS ragdoll ask: a 10.5 s
Spark call, unused). It costs no time but is a paid call. Could start
naming only after routing, at the cost of the head start on open-scene
asks.

## Model races (Spark vs Baseten)

From the 2026-11-02 Baseten benchmark.

- **Floor reads:** Baseten wins 14 of 18 (3.2 s vs Spark 12.7 s median),
  but its wrong labels ("teeth", "garlic clove" for ice) reached
  answers. Options: take Spark's read when the two disagree, or require
  both to agree on a hazard.
- **Log the race winner** in the ask's log line, to see this in
  production.
- **Coded-lead answer race:** both models always returned an empty
  answer; the call may be skippable.
- **Losing calls run on:** a race's loser keeps running in the background
  (one ran 9 s into the next ask) and is still billed. Same for a SAM
  backup that loses.
- **Synthesis:** gated on 4+ non-locked sentences, so it almost never
  runs on live asks.

## Deployment

- **nginx template** in deploy/ next to the systemd unit:
  client_max_body_size above the largest clip (10.6 MB seen),
  proxy_read_timeout and proxy_send_timeout 180 s (the app waits 180 s).
- **Volunteer token in URLs:** the console accepts ?token=, which nginx
  writes to its access log. Use the header only, or turn off access
  logging for /volunteer and /v1/escalations.
