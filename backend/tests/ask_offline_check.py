import sys
sys.path.insert(0, "/home/cromestant/code/alumbra/backend")
from app import pipeline as p

fails = []
def check(name, got, want):
    ok = got == want
    print(("PASS" if ok else "FAIL"), name, "->", repr(got), "" if ok else f"(want {want!r})")
    if not ok:
        fails.append(name)

# Routing
check("route ibuprofen", p.route("Where's the ibuprofen?"), ("C", "ibuprofen"))
check("route ADHD med", p.route("find my ADHD medicine")[0], "C")
check("resolve ADHD", p.resolve_target("find my ADHD medicine"), "LISDEXAMFETAMINE")
check("resolve vyvanse", p.resolve_target("where is my vyvanse"), "LISDEXAMFETAMINE")
check("route car", p.route("where is the car"), ("B", "car"))
check("route mailbox", p.route("Help me find my mailbox, it's number 16 in the middle."), ("B", "mailbox"))
check("route open", p.route("What's ahead of me on the path?")[0], "A")
check("route empty", p.route("")[0], "A")
check("route crossing", p.route("Can I cross the street here?")[0], "A")
check("route med-question-not-C?", p.route("What dose of ibuprofen?")[0], "C")  # labeled item still C; dosing rule escalates

# Rule predicates
check("crossing?", p.is_crossing_question("Can I cross the street here?"), True)
check("not crossing", p.is_crossing_question("What's ahead of me on the path?"), False)
check("dosing?", p.is_dosing_question("How many pills should I take?"), True)
check("dosing mg", p.is_dosing_question("What is the dose in mg I should take?"), True)
check("not dosing", p.is_dosing_question("Where's the ibuprofen?"), False)

# Geometry: 1000x1000 image
check("clock center", p.clock_of((400, 0, 600, 100), 1000), 12)
check("clock left", p.clock_of((0, 0, 100, 100), 1000), 9)
check("clock right", p.clock_of((900, 0, 1000, 100), 1000), 3)
check("size close", p.size_class_of((0, 0, 10, 500), 1000), "close")
check("size mid", p.size_class_of((0, 0, 10, 300), 1000), "a few steps away")
check("size far", p.size_class_of((0, 0, 10, 100), 1000), "far")

# Prompts byte-exact vs probes
check("naming prompt", p.SPARK_NAMING_PROMPT,
      "what is in this image? reply only with a JSON array of short noun phrases")
check("wonder prompt", p.WONDER_READ_PROMPT,
      "Read the label on the main bottle or container in this image. "
      "Reply with ONLY the product or drug name in capitals, or NONE if no "
      "label text is readable. No other words.")
check("safety list", p.SAFETY_LIST, ["car", "person", "stairs", "door", "curb"])

# Answer tightening — merge: same head noun + overlap collapses;
# disjoint same-name boxes are distinct instances and survive.
_door_a = {"name": "open white door", "box": [400, 100, 600, 900],
           "confidence": 0.9, "clock": 12, "size_class": "close"}
_door_b = {"name": "door", "box": [410, 110, 590, 890],
           "confidence": 0.7, "clock": 12, "size_class": "a few steps away"}
_bank_a = {"name": "mailbox", "box": [0, 0, 100, 200],
           "confidence": 0.9, "clock": 9, "size_class": "far"}
_bank_b = {"name": "mailbox", "box": [500, 0, 600, 200],
           "confidence": 0.8, "clock": 12, "size_class": "far"}
_m = p.merge_entries([_door_a, _door_b, _bank_a, _bank_b])
check("merge count", len(_m), 3)
check("merge name", _m[0]["name"], "open white door")
check("merge conf", _m[0]["confidence"], 0.9)
check("merge size", _m[0]["size_class"], "close")
check("merge keeps banks", [_m[1]["box"], _m[2]["box"]],
      [[0, 0, 100, 200], [500, 0, 600, 200]])

# Answer tightening — path valence: exit phrasing infers a visible door
# goal; goal/passageway/surface entries are never obstructions.
_far_door = {"name": "door", "box": [450, 100, 550, 500],
             "confidence": 0.8, "clock": 12,
             "size_class": "a few steps away"}
_chair = {"name": "chair", "box": [300, 400, 450, 800],
          "confidence": 0.8, "clock": 11, "size_class": "close"}
_pa = p._path_assessment(
    [_far_door, _chair],
    "Are any of my cats in the way? For me to get out of the room?", width=1000)
check("exit infers door", _pa["goal"], "door")
check("door not obstruction, chair is",
      [o["name"] for o in _pa["on_path"]], ["chair"])
_sw = {"name": "sidewalk", "box": [400, 500, 600, 900],
       "confidence": 0.9, "clock": 12, "size_class": "close"}
_pa2 = p._path_assessment([_sw], "Is the path ahead of me clear?", width=1000)
check("sidewalk not obstruction", _pa2["verdict"], "clear")
_pa3 = p._path_assessment([_chair], "Can I get to the elevator?", width=1000)
check("named goal missing", _pa3["verdict"], "no_goal_visible")

# Answer tightening — synthesis lock + preservation (mechanical halves).
_draft = ("No cats are in your way. The open door is at your 12 o'clock, "
          "close. It's very dark, so I can't be sure of these details.")
check("locked hedge", p._locked_sentences(_draft),
      ["It's very dark, so I can't be sure of these details."])
check("subjects", p._question_subjects("Are any of my cats in the way?",
                                       _draft), ["cats", "way"])
_good = ("No cats are in your way. The open door is at your 12 o'clock, "
         "close. It's very dark, so I can't be sure of these details.")
check("ok identical", p._synthesis_ok(
    _draft, _good, p._locked_sentences(_draft), [], "door",
    ["cats", "way"], ["door", "cat"]), True)
_bad = "No cats are in your way. The open door is ahead."
check("ok drops clock", p._synthesis_ok(
    _draft, _bad, p._locked_sentences(_draft), [], "door",
    ["cats", "way"], ["door", "cat"]), False)
_d2 = "Yes \u2014 the way is clear. No cats are in your way."
_moji = "Yes \u00e2\u0080\u0094 the way is clear. No cats are in your way."
check("ok rejects mojibake", p._synthesis_ok(
    _d2, _moji, [], [], None, ["cats", "way"], []), False)
check("demojibake", p._demojibake("Yes \u00e2\u0080\u0094 clear."),
      "Yes \u2014 clear.")
check("demojibake mixed",
      p._demojibake("Yes \u00e2\u0080\u0094 12 o\u2019clock, caf\u00e9."),
      "Yes \u2014 12 o\u2019clock, caf\u00e9.")
check("demojibake none", p._demojibake("Plain ascii."), None)
check("clock set single", p._clock_set("at your 2 o'clock"), {"2"})
check("clock set list",
      p._clock_set("hang far away at your 11, 12, 1 and 2 o'clock"),
      {"11", "12", "1", "2"})
check("clock set pair", p._clock_set("ahead at your 11 and 12 o'clock"),
      {"11", "12"})

# Field test — goal is whatever the question asks to reach.
check("reach fridge", p._reach_target("is the path to the fridge clear?"),
      "fridge")
check("reach couch", p._reach_target("How do I get to my couch?"), "couch")
check("reach counter", p._reach_target("which way to the counter?"),
      "counter")
check("reach not exit", p._reach_target("how do I get out of here?"), "")
check("reach aliases", "refrigerator" in p._reach_aliases("fridge"), True)
_fridge = {"name": "black refrigerator", "box": [400, 0, 800, 1200],
          "confidence": 0.9, "clock": 12, "size_class": "close"}
_app = {"name": "countertop appliance", "box": [850, 300, 1050, 500],
        "confidence": 0.8, "clock": 12, "size_class": "close"}
_counter = {"name": "kitchen counter", "box": [820, 500, 1100, 1200],
            "confidence": 0.9, "clock": 2, "size_class": "close"}
_ice = {"name": "ice cubes", "box": [500, 1000, 540, 1030],
        "confidence": 0.85, "clock": 12, "size_class": "a few steps away"}
_fa = p._path_assessment([_fridge, _app, _counter, _ice],
                         "is the path to the fridge clear?", width=1000)
check("fridge is goal", _fa["goal"], "black refrigerator")
check("goal never obstruction",
      "black refrigerator" in [o["name"] for o in _fa["on_path"]], False)
check("appliance on holder excluded",
      "countertop appliance" in [o["name"] for o in _fa["on_path"]], False)
check("ice on path",
      "ice cubes" in [o["name"] for o in _fa["on_path"]], True)
check("fridge verdict", _fa["verdict"], "blocked")
_drawer = {"name": "freezer drawers", "box": [450, 200, 750, 500],
           "confidence": 0.8, "clock": 12, "size_class": "close"}
_fa4 = p._path_assessment([_fridge, _drawer, _ice],
                          "is the path to the fridge clear?", width=1000)
check("goal part excluded",
      "freezer drawers" in [o["name"] for o in _fa4["on_path"]], False)
check("ice in goal box kept",
      "ice cubes" in [o["name"] for o in _fa4["on_path"]], True)
_ghost = {"name": "countertop appliance", "box": [408, 0, 1075, 941],
          "confidence": 0.8, "clock": 1, "size_class": "close"}
_ghost_goal = {"name": "black refrigerator", "box": [408, 0, 1075, 938],
               "confidence": 0.9, "clock": 1, "size_class": "close"}
_fa7 = p._path_assessment([_ghost_goal, _ghost],
                          "is the path to the fridge clear?", width=1000)
check("goal echo excluded",
      "countertop appliance" in [o["name"] for o in _fa7["on_path"]],
      False)
_toy = {"name": "plush toy", "box": [500, 900, 560, 960],
        "confidence": 0.8, "clock": 12, "size_class": "a few steps away"}
_couch = {"name": "couch", "box": [400, 500, 800, 1100],
          "confidence": 0.9, "clock": 12, "size_class": "close"}
_fa8 = p._path_assessment([_couch, _toy], "is the path to the couch clear?", width=1000)
check("toy before couch kept",
      "plush toy" in [o["name"] for o in _fa8["on_path"]], True)
_chair2 = {"name": "chair", "box": [300, 600, 450, 1200],
          "confidence": 0.8, "clock": 11, "size_class": "close"}
_table = {"name": "table", "box": [200, 400, 600, 900],
          "confidence": 0.9, "clock": 11, "size_class": "close"}
_fa2 = p._path_assessment([_chair2, _table], "is the way ahead clear?", width=1000)
check("tucked chair kept",
      "chair" in [o["name"] for o in _fa2["on_path"]], True)
_book = {"name": "book", "box": [300, 350, 400, 400],
         "confidence": 0.8, "clock": 12, "size_class": "close"}
_fa3 = p._path_assessment([_book, _table], "is the way ahead clear?", width=1000)
check("book on table excluded",
      "book" in [o["name"] for o in _fa3["on_path"]], False)

# Field test — NONE ingestion + floor helpers.
check("discard NONE", p._discard_read("NONE"), True)
check("discard empty", p._discard_read("  "), True)
check("discard none-lower", p._discard_read("none"), True)
check("keep label", p._discard_read("IBUPROFEN"), False)
check("floor size near", p._floor_entry_size((0, 900, 10, 1000), 1200),
      "close")
check("floor size mid", p._floor_entry_size((0, 600, 10, 800), 1200),
      "a few steps away")
check("floor size far", p._floor_entry_size((0, 100, 10, 200), 1200),
      "far")
_hi_app = {"name": "countertop appliance", "box": [850, 300, 1050, 500],
           "confidence": 0.8, "clock": 12, "size_class": "close"}
_lo_chair = {"name": "chair", "box": [300, 800, 450, 1150],
             "confidence": 0.8, "clock": 12, "size_class": "close"}
_fa5 = p._path_assessment([_hi_app, _lo_chair], "is the way ahead clear?",
                         height=1200, width=1000)
check("high appliance excluded",
      "countertop appliance" in [o["name"] for o in _fa5["on_path"]],
      False)
check("low chair kept",
      "chair" in [o["name"] for o in _fa5["on_path"]], True)
_far_sign = {"name": "sign", "box": [500, 100, 600, 200],
             "confidence": 0.8, "clock": 12, "size_class": "far"}
_far_puddle = {"name": "puddle", "box": [500, 700, 700, 800],
               "confidence": 0.8, "clock": 12, "size_class": "far"}
_fa6 = p._path_assessment([_far_sign, _far_puddle],
                          "is the way ahead clear?", height=1200, width=1000)
check("high far sign excluded",
      "sign" in [o["name"] for o in _fa6["on_path"]], False)
check("low far puddle kept",
      "puddle" in [o["name"] for o in _fa6["on_path"]], True)

# Rebuild 2026-10-27 — rule 2: ground cover is never an obstruction
# (table walk: "Careful — patio is in your path").
_patio = {"name": "patio", "box": [500, 600, 900, 1200],
          "confidence": 0.9, "clock": 1, "size_class": "a few steps away"}
_patio2 = {"name": "patio ahead", "box": [520, 300, 900, 700],
           "confidence": 0.8, "clock": 1, "size_class": "far"}
_lawn = {"name": "lawn", "box": [0, 500, 1000, 1200],
         "confidence": 0.9, "clock": 12, "size_class": "far"}
_verge = {"name": "grass verge", "box": [600, 700, 800, 1200],
          "confidence": 0.9, "clock": 1, "size_class": "close"}
_otable = {"name": "table", "box": [450, 450, 550, 600],
           "confidence": 0.9, "clock": 12, "size_class": "far"}
# Between the user and the table (base nearer, same columns).
_bench = {"name": "bench", "box": [430, 620, 570, 720],
          "confidence": 0.9, "clock": 12, "size_class": "far"}
_bench_side = dict(_bench, box=[300, 470, 420, 610])  # beside the table
_tw = p._path_assessment([_patio, _patio2, _lawn, _verge, _otable, _bench],
                         "is the way ahead clear?", height=1200, width=1000)
check("ground cover never on path",
      sorted({"patio", "patio ahead", "lawn", "grass verge"}
             & {o["name"] for o in _tw["on_path"]}), [])
check("floor objects still on path",
      sorted(o["name"] for o in _tw["on_path"]), ["bench", "table"])
_tw2 = p._path_assessment([_patio, _lawn, _otable],
                          "can I get to the table?", height=1200, width=1000)
_tw3 = p._path_assessment([_patio, _lawn, _otable, _bench],
                          "can I get to the table?", height=1200, width=1000)
check("bench before table blocks",
      [o["name"] for o in _tw3["on_path"]], ["bench"])
check("bench beside the table does not block",
      p._path_assessment([_patio, _lawn, _otable, _bench_side],
                         "can I get to the table?", height=1200,
                         width=1000)["verdict"], "clear")
check("table walk goal is table", _tw2["goal"], "table")
check("table walk ground-only path clear", _tw2["verdict"], "clear")
check("head drops position tail", p._head_noun("patio ahead"), "patio")
check("patio ahead merges with patio",
      len(p.merge_entries([_patio, dict(_patio, name="patio ahead")])), 1)

# Rule 1 — the reach target outranks a goal word mentioned in passing.
_door2 = {"name": "door", "box": [0, 0, 100, 900], "confidence": 0.9,
          "clock": 9, "size_class": "close"}
_fa9 = p._path_assessment([_fridge, _door2, _ice],
                          "is the path to the fridge clear past the door?", width=1000)
check("reach outranks goal word", _fa9["goal"], "black refrigerator")
# Goal-echo preservation through the rebuilt rule chain.
_fa10 = p._path_assessment([_ghost_goal, _ghost, _ice],
                           "is the path to the fridge clear?", width=1000)
check("echo still excluded after rebuild",
      [o["name"] for o in _fa10["on_path"]], ["ice cubes"])

# Rule 3 — question kinds decide the lead.
check("kind distance", p.question_kind("how far are they", "A", True),
      "distance")
check("kind path", p.question_kind("is the path to the fridge clear?",
                                   "A", False), "path")
check("kind find beats jev nav",
      p.question_kind("Find mailbox 14.", "B", True), "find")
check("kind stepping on", p.question_kind("What am I stepping on?",
                                          "A", True), "other")
check("kind jev-only path", p.question_kind("anything I should avoid?",
                                            "A", True), "path")
check("kind describe", p.question_kind("", "A", False), "describe")

# Rule 3 — the glasses follow-up: a stationary distance question about
# the prior subject leads with the distance, never "Careful —".
_glasses = {"name": "glasses", "box": [450, 700, 550, 760],
            "confidence": 0.9, "clock": 12, "size_class": "a few steps away"}
_unit = {"name": "white unit", "box": [600, 300, 900, 900],
         "confidence": 0.9, "clock": 2, "size_class": "close"}
_ga = p._path_assessment([_glasses, _unit], "how far are they",
                         height=1200, hint="glasses", width=1000)
check("pronoun resolves to prior referent", _ga["goal"], "glasses")
check("distance lead",
      p._distance_opening(_ga, "how far are they"),
      ["They are a few steps away, at your 12 o'clock."])
check("goal is never its own obstruction", _ga["on_path"], [])
_gn = p._path_assessment([_unit], "how far is the door?", height=1200, width=1000)
check("distance named goal missing",
      p._distance_opening(_gn, "how far is the door?"),
      ["I can't see the door in this photo."])

# Rule 3 — path leads: "Careful —" only for a rule-2 obstruction.
check("path lead blocked", p._path_opening(_fa),
      ["Careful — ice cubes are in your path at 12 o'clock, "
       "a few steps away.",
       "The fridge is at your 12 o'clock, close."])
check("path lead clear to goal", p._path_opening(_tw2),
      ["Yes — the way to the table is clear, at your 12 o'clock, "
       "far away."])
check("path lead no goal", p._path_opening(_pa3),
      ["I can't see the elevator in this photo."])

# Rule 4 — tidy: the recitation collapses before synthesis.
_scene_tw = [_patio, _lawn, _otable, _bench]
_recital = ("The patio is at your 1 o'clock, a few steps away. The table "
            "is at your 12 o'clock, far. The bench is at your 12 o'clock, "
            "far. The lawn is at your 12 o'clock, far. You are on a patio.")
_lead = ["Careful — bench is in your path at 12 o'clock, far away."]
_tw_a = p._tidy(_lead, _recital, "path", _scene_tw,
                {"on_path": [{"name": "bench"}], "goal": None,
                 "goal_desc": "exit"}, [], "is the way ahead clear?")
check("tidy table walk",
      _tw_a, "Careful — bench is in your path at 12 o'clock, far away. "
             "The table is at your 12 o'clock, far.")
_ice_body = ("Ice cubes are at your 12 o'clock, a few steps away. They are "
             "on the floor, so watch your footing. You are on tile floor.")
_fl = {"name": "tile floor", "box": [0, 600, 1000, 1200], "confidence": 0.9,
       "clock": 12, "size_class": "close"}
check("tidy keeps footing, drops restatement + bare floor",
      p._tidy(p._path_opening(_fa), _ice_body, "path",
              [_fridge, _ice, _fl], _fa, [], "path to the fridge clear?"),
      "Careful — ice cubes are in your path at 12 o'clock, a few steps "
      "away. The fridge is at your 12 o'clock, close. They are on the "
      "floor, so watch your footing.")
check("tidy keeps warnings",
      p._tidy([], "Grass at 1 o'clock. Warning: puddle ahead.", "path",
              [_verge], None, ["puddle"], "is the path clear?"),
      "Grass at 1 o'clock. Warning: puddle ahead.")
check("tidy surface question keeps ground",
      p._tidy([], "A frog is close. You are on a sidewalk.", "other",
              [{"name": "sidewalk", "box": [0, 0, 1, 1], "clock": 12,
                "size_class": "close"}], None, [],
              "What am I stepping on?"),
      "A frog is close. You are on a sidewalk.")

# Part 2 — failure rule: retry once with a fresh budget, then error.
_real_tc = p._text_complete
_calls = []
def _stall(model, prompt, timeout):
    _calls.append(timeout)
    raise p.PipelineError("Model API unreachable: Read timed out.")
p._text_complete = _stall
_long = ("Careful — bench is in your path at 12 o'clock. The table is at "
         "your 12 o'clock, far. The lawn is at your 12 o'clock, far. The "
         "patio is at your 1 o'clock. The tree is at your 2 o'clock.")
try:
    p.synthesize_answer(_long, "is the way clear?", _scene_tw, None, [])
    check("synthesis stall errors", "shipped draft", "StageFailed")
except p.StageFailed as e:
    check("synthesis stall errors", e.stage, "synthesis")
check("synthesis retried once, fresh budget", _calls,
      [p.SYNTHESIS_TIMEOUT, p.SYNTHESIS_TIMEOUT])
_seq = iter([p.PipelineError("stall"),
             "Careful — bench is in your path at 12 o'clock. The table, "
             "lawn and patio are at your 12 and 1 o'clock; the tree at "
             "your 2 o'clock."])
def _flaky(model, prompt, timeout):
    v = next(_seq)
    if isinstance(v, Exception):
        raise v
    return v
p._text_complete = _flaky
_out, _applied, _ = p.synthesize_answer(_long, "is the way clear?",
                                        _scene_tw, None, [])
check("synthesis retry succeeds", _applied, True)
_bad_lead = iter(["The table is at your 12 o'clock. Careful — bench is in "
                  "your path at 12 o'clock."] * 2)
p._text_complete = lambda m, pr, t: next(_bad_lead)
try:
    p.synthesize_answer(_long, "is the way clear?", _scene_tw, None, [])
    check("synthesis moving the lead errors", "shipped", "StageFailed")
except p.StageFailed:
    check("synthesis moving the lead errors", True, True)
p._text_complete = _stall
check("short draft skips synthesis (contract, not failure)",
      p.synthesize_answer("Yes — clear. Warning: puddle ahead.",
                          "is it clear?", [], None, ["puddle"])[1], False)
import os as _os
_os.environ["SYNTHESIS_ENABLED"] = "0"
check("kill switch is configuration, not failure",
      p.synthesize_answer(_long, "is the way clear?", _scene_tw, None,
                          [])[0], _long)
del _os.environ["SYNTHESIS_ENABLED"]
p._text_complete = _real_tc

# Other stages: segmentation, crop reads, ASR, Jev error after retry.
_real_seg = p.sam_segment
p.sam_segment = lambda img, ph: (_ for _ in ()).throw(
    p.PipelineError("SAM 500"))
try:
    p.sam_many(b"", ["chair"])
    check("segmentation failure errors", "silent []", "StageFailed")
except p.StageFailed as e:
    check("segmentation failure errors", e.stage, "segmentation")
p.sam_segment = _real_seg
_real_post = p.requests.post
class _R:
    status_code = 500
    text = "boom"
p.requests.post = lambda *a, **k: _R()
try:
    p.transcribe_question(b"RIFF....WAVE")
    check("ASR failure errors", "silent ''", "StageFailed")
except p.StageFailed as e:
    check("ASR failure errors", e.stage, "transcription")
check("no audio is not a failure", p.transcribe_audio(b""), "")
_real_jev = p.jev_enabled
p.jev_enabled = lambda: True
try:
    p.jev_intents("state")
    check("Jev error errors", "regex fallback", "StageFailed")
except p.StageFailed as e:
    check("Jev error errors", e.stage, "intent_gate")
p.jev_enabled = lambda: False
check("Jev disabled is configuration", p.jev_intents("state"), None)
p.jev_enabled = _real_jev
p.requests.post = _real_post

# End to end through _compose / run_followup (models stubbed, no
# network): the leads Charles hears on device.
import io as _io
from PIL import Image as _Image
def _jpeg(level):
    b = _io.BytesIO()
    _Image.new("RGB", (1000, 1200), (level,) * 3).save(b, format="JPEG")
    return b.getvalue()
_os.environ["JEV_GATE_ENABLED"] = "0"
_os.environ["FLOOR_LOOK_ENABLED"] = "0"
_real_vlm = p._vlm_text
_recital_json = ('{"answer": "The patio is at your 1 o\'clock, a few steps '
                 'away. The table is at your 12 o\'clock, far. The bench is '
                 'at your 12 o\'clock, far. The lawn is at your 12 o\'clock, '
                 'far.", "confidence": 0.8}')
p._vlm_text = lambda m, img, instr, t: _recital_json
_res, _, _ref, _ = p._compose(
    _jpeg(200), [_patio, _lawn, _otable], "can I get to the table?",
    "text", "can I get to the table?", "A", None, "", None, None,
    200.0, 200, 0.0, 0)
# The stub's bench is not in the scene: it survives as the one allowed
# orientation landmark; the patio/lawn/table recitation does not.
check("e2e table walk: tight, no recitation", _res["answer"],
      "Yes — the way to the table is clear, at your 12 o'clock, far away. "
      "The bench is at your 12 o'clock, far.")
check("e2e table walk referent", _ref, "table")
_resd, _, _, _ = p._compose(
    _jpeg(10), [_patio, _lawn], "is the path ahead clear?",
    "text", "is the path ahead clear?", "A", None, "", None, None,
    10.0, 10, 0.0, 0)
check("e2e dark never certifies clear",
      _resd["answer"].startswith("It's too dark for me to tell"), True)
p._vlm_text = lambda m, img, instr, t: (
    '{"answer": "They are at your 12 o\'clock, a few steps away. They are '
    'on the floor beside the white unit.", "confidence": 0.8}')
p._store_ask("glasses-ask", {
    "created": __import__("time").time(), "image": _jpeg(200),
    "scene": [_glasses, _unit], "flow": "B", "target": "glasses",
    "question": "where are my glasses", "answer": "I found your glasses.",
    "behavior": "ANSWER", "floor_done": True, "read_targets": [],
    "referent": "glasses"})
_fu = p.run_followup("glasses-ask", "how far are they")
check("e2e glasses follow-up leads with distance", _fu["answer"],
      "They are a few steps away, at your 12 o'clock. They are on the "
      "floor beside the white unit.")
check("e2e follow-up has no hazard lead", "Careful" in _fu["answer"], False)
p._vlm_text = _real_vlm
del _os.environ["JEV_GATE_ENABLED"], _os.environ["FLOOR_LOOK_ENABLED"]

check("exit word names the door found", p._path_opening(_pa),
      ["Careful — chair is in your path at 11 o'clock, close.",
       "The door is at your 12 o'clock, a few steps away."])
_ice2 = dict(_ice, name="ice cube", box=[600, 1000, 630, 1030])
check("same object once in the lead",
      p._path_opening(p._path_assessment(
          [_fridge, _ice, _ice2], "is the path to the fridge clear?", width=1000)),
      ["Careful — ice cubes are in your path at 12 o'clock, "
       "a few steps away.", "The fridge is at your 12 o'clock, close."])
check("clause qualifier",
      p.clause_qualifier("Help me find my mailbox, it's number 16 in the "
                         "middle."), "number 16")
check("no clause qualifier", p.clause_qualifier("Find my keys."), None)
check("two-obstacle lead punctuation",
      p._path_opening({"verdict": "blocked", "goal": None,
                       "goal_desc": "exit", "on_path": [
                           {"name": "chair", "clock": 12,
                            "size_class": "close"},
                           {"name": "box", "clock": 1, "size_class": "far"}]}),
      ["Careful — chair at 12 o'clock, close, and box at 1 o'clock, "
       "far away, are in your path."])

# Live test 2026-10-28 — the route is tested against the passage (the
# door's opening), not the leaf's clock. Live: cat lying in the doorway
# at 10 o'clock cleared, wall-side guitars at the leaf's clock flagged.
def _e(name, box, size="a few steps away"):
    return {"name": name, "box": box, "confidence": 0.9,
            "clock": p.clock_of(box, 1000), "size_class": size}
_leaf = _e("open white door", [340, 150, 560, 700])
_door_cat = _e("tabby cat", [130, 650, 320, 715])
_guitars = _e("electric guitars", [570, 350, 700, 690])
_opening = (120, 150, 330, 720)
check("live fixture: cat 10, guitars 1, leaf 12",
      (_door_cat["clock"], _guitars["clock"], _leaf["clock"]), (10, 1, 12))
_dp = p._path_assessment([_leaf, _door_cat, _guitars],
                         "can I get out of the room?", height=1200,
                         width=1000, openings=[_opening])
check("passage is the opening", (_dp["passage_source"], _dp["goal_clock"]),
      ("opening", 10))
check("cat in the doorway is on the route, guitars beside it are not",
      [o["name"] for o in _dp["on_path"]], ["tabby cat"])
check("lead points through the opening", p._path_opening(_dp),
      ["Careful — tabby cat is in your path at 10 o'clock, a few steps "
       "away.", "The way through the door is at your 10 o'clock, close."])
_hall = _e("hallway", [125, 160, 330, 715])
check("a hallway entry beside the leaf is the opening",
      [o["name"] for o in p._path_assessment(
          [_leaf, _door_cat, _guitars, _hall], "can I get out of the room?",
          height=1200, width=1000)["on_path"]], ["tabby cat"])
_clear = p._path_assessment([_leaf, _guitars], "can I get out of the room?",
                            height=1200, width=1000, openings=[_opening])
check("guitars alone leave the doorway clear",
      p._path_opening(_clear),
      ["Yes — the way through the door is clear, at your 10 o'clock, "
       "close."])
check("baseboard is the wall, never an obstruction",
      p._path_assessment([_leaf, _e("white baseboard", [100, 690, 900, 720],
                                    "far")], "can I get out of the room?",
                         height=1200, width=1000,
                         openings=[_opening])["verdict"], "clear")
check("french-door fridge is not a door goal",
      p._is_door_goal({"name": "black french-door refrigerator"}), False)
_arm = _e("black office chair", [0, 556, 320, 1200], "close")  # 20px in
check("box edge grazing the corridor is beside you",
      p._in_corridor(_arm, (360, 0, 640, 0), 1000, 1200), False)
_cube = _e("ice cube", [490, 1000, 520, 1030])
check("small thing wholly inside the corridor is on it",
      p._in_corridor(_cube, (300, 0, 900, 900), 1000, 1200), True)

# Live test 2026-10-27 — merge fuses only segmentation echoes; distinct
# instances whose boxes overlap stay separate, and counts read the scene.
_cat_a = {"name": "tortoiseshell cat", "box": [100, 500, 400, 700],
          "confidence": 0.9, "clock": 11, "size_class": "far"}
_cat_b = {"name": "cat", "box": [250, 520, 550, 720],   # overlaps a, IoU ~0.33
          "confidence": 0.8, "clock": 12, "size_class": "far"}
_cat_echo = {"name": "cat", "box": [102, 502, 398, 699],  # echo of a
             "confidence": 0.85, "clock": 11, "size_class": "far"}
check("overlapping cats stay two",
      len(p.merge_entries([_cat_a, _cat_b])), 2)
check("cat echo across phrases merges",
      len(p.merge_entries([_cat_a, _cat_echo, _cat_b])), 2)
_cuddle_a = {"name": "cat", "box": [100, 500, 400, 700], "confidence": 0.9,
             "clock": 11, "size_class": "far"}
_cuddle_b = dict(_cuddle_a, box=[160, 500, 460, 700])  # IoU 0.67: distinct
check("IoU 0.67 is not an echo",
      len(p.merge_entries([_cuddle_a, _cuddle_b])), 2)
_nest_big = dict(_cuddle_a, box=[100, 400, 500, 800])
_nest_small = dict(_cuddle_a, box=[110, 410, 480, 790])  # inside, ratio 0.88
check("contained near-equal box is an echo",
      len(p.merge_entries([_nest_big, _nest_small])), 1)
_kitten = dict(_cuddle_a, box=[150, 600, 300, 750])  # inside, ratio 0.14
check("small cat inside a big cat's box is its own instance",
      len(p.merge_entries([_nest_big, _kitten])), 2)
_c1 = dict(_cuddle_a, box=[100, 500, 400, 700])
_c2 = dict(_cuddle_a, box=[130, 500, 430, 700])   # echoes c1 (IoU 0.82)
_c3 = dict(_cuddle_a, box=[160, 500, 460, 700])   # echoes c2, not c1
check("echoes never chain distinct instances together",
      len(p.merge_entries([_c1, _c2, _c3])), 2)

check("count subject", p.count_subject("How many cats are in this room?"),
      "cats")
check("count subject follow-up",
      p.count_subject("let's try again how many cups do you see"), "cups")
check("count subject trailing please",
      p.count_subject("count the chairs please"), "chairs")
check("how many steps is distance, not count",
      (p.count_subject("how many steps to the door"),
       p.question_kind("how many steps to the door", "A", False)),
      (None, "distance"))
check("count kind", p.question_kind("How many cats are in this room?",
                                    "A", True), "count")
check("singular", [p._singular(w) for w in
                   ["cats", "boxes", "puppies", "people", "glasses", "bus"]],
      ["cat", "box", "puppy", "person", "glasses", "bus"])
_live_cats = [  # the live frame: 720x1280, second cat cut by the edge
    {"name": "tortoiseshell cat", "box": [10, 709, 268, 876], "clock": 10,
     "size_class": "far", "confidence": 0.95},
    {"name": "tortoiseshell cat", "box": [590, 459, 720, 625], "clock": 2,
     "size_class": "far", "confidence": 0.92},
    {"name": "gray sofa", "box": [0, 560, 720, 1230], "clock": 12,
     "size_class": "close", "confidence": 0.9}]
check("count from scene, edge instance partial",
      p._count_opening(_live_cats, "cats", 720, 1280),
      ["I can see two cats in this photo: one at your 10 o'clock, and part "
       "of another at the edge, at your 2 o'clock."])
check("one instance says one",
      p._count_opening(_live_cats[:1], "cats", 720, 1280),
      ["I can see one cat in this photo, at your 10 o'clock."])
check("zero instances",
      p._count_opening(_live_cats, "cups", 720, 1280),
      ["I don't see any cups in this photo."])
_banks = [{"name": "cluster mailboxes", "box": b, "clock": c,
           "size_class": "close", "confidence": 0.8}
          for b, c in [([643, 792, 1633, 2262], 11),
                       ([1646, 804, 2674, 2308], 12),
                       ([2613, 817, 3831, 2358], 2),
                       ([3628, 841, 4000, 2383], 3)]]
check("group noun counts its members (case_07.Q2)",
      p._count_opening(_banks + [dict(_banks[0], name="parcel lockers")],
                       "mailbox banks", 4000, 3000),
      ["I can see four mailbox banks in this photo: three full at your 11, "
       "12 and 2 o'clock, and part of a fourth at the edge, at your 3 "
       "o'clock."])
check("many instances give a span",
      p._count_opening([dict(_cuddle_a, box=[i * 100, 500, i * 100 + 80,
                                              580], clock=k)
                        for i, k in enumerate([9, 10, 10, 11, 12, 1, 2])],
                       "cats", 1000, 1000),
      ["I can see seven cats in this photo, from your 9 to your 2 "
       "o'clock."])

_os.environ["JEV_GATE_ENABLED"] = "0"
_real_sam = p.sam_segment
_sam_calls = []
p.sam_segment = lambda img, phrase: (_sam_calls.append(phrase), [])[1]
p._vlm_text = lambda m, img, instr, t: (
    '{"answer": "There are 3 cats, one on the sofa and two on the floor. '
    'The sofa is at your 12 o\'clock.", "confidence": 0.8}')
_b = _io.BytesIO()
_Image.new("RGB", (720, 1280), (200,) * 3).save(_b, format="JPEG")
_live_jpeg = _b.getvalue()
_cres, _, _cref, _cfull = p._compose(
    _live_jpeg, _live_cats, "How many cats are in this room?", "clip",
    "How many cats are in this room?", "A", None, "", None, None,
    200.0, 200, 0.0, 0)
check("e2e count: the scene's count, the model's tally dropped",
      _cres["answer"],
      "I can see two cats in this photo: one at your 10 o'clock, and part "
      "of another at the edge, at your 2 o'clock. The sofa is at your 12 "
      "o'clock.")
check("e2e count referent", _cref, "cat")

# Chips: same name + clock collapse in the shipped scene only.
_dup = [_e("sunburst electric guitar", [600, 300, 700, 700]),
        _e("sunburst electric guitar", [610, 310, 690, 690]),
        _e("off-white wall", [0, 0, 300, 600]),
        _e("off-white wall", [10, 0, 290, 600]),
        _e("off-white wall", [700, 0, 1000, 600])]
_shipped = p.ship_scene(_dup)
check("chips collapse same name + clock",
      [(e["name"], e["clock"], e.get("instances", 1)) for e in _shipped],
      [("sunburst electric guitar", 1, 2), ("off-white wall", 10, 2),
       ("off-white wall", 2, 1)])
check("shipping leaves the scene itself whole", len(_dup), 5)
_twins = [dict(_live_cats[0]), dict(_live_cats[0], box=[30, 700, 280, 870])]
_tres, _, _, _tfull = p._compose(
    _live_jpeg, _twins, "How many cats are in this room?", "clip",
    "How many cats are in this room?", "A", None, "", None, None,
    200.0, 200, 0.0, 0)
check("two cats at one clock: one chip, counted as two, cached as two",
      (len(_tres["scene_model"]), _tres["answer"].split(":")[0].split(",")[0],
       len(_tfull)),
      (1, "I can see two cats in this photo", 2))
p._store_ask("cats-ask", {
    "created": __import__("time").time(), "image": _live_jpeg,
    "scene": _live_cats, "flow": "A", "target": None,
    "question": "How many cats are in this room?", "answer": "…",
    "behavior": "ANSWER", "floor_done": False, "read_targets": [],
    "referent": "cat"})
p._vlm_text = lambda m, img, instr, t: (
    '{"answer": "I see two cups on the table.", "confidence": 0.8}')
_cups = p.run_followup("cats-ask", "how many cups do you see")
check("e2e count follow-up segments the noun", _sam_calls[-1:], ["cup"])
check("e2e count follow-up: none in scene says none", _cups["answer"],
      "I don't see any cups in this photo.")
p.sam_segment = _real_sam
p._vlm_text = _real_vlm
del _os.environ["JEV_GATE_ENABLED"]

# Live test 2026-10-28 — a count over a clip is the union across its
# frames. Synthetic sweep: a textured wall, a 400px window panning right;
# cat A is visible only early, cat B only late, the sent photo (middle)
# shows a sliver of A. SAM is stubbed with each frame's true visible boxes.
import random as _random
from PIL import ImageDraw as _Draw
_rng = _random.Random(7)
_wall = _Image.new("RGB", (900, 600), (128, 128, 128))
_d = _Draw.Draw(_wall)
for _ in range(400):
    _x, _y = _rng.randrange(900), _rng.randrange(600)
    _d.rectangle([_x, _y, _x + _rng.randrange(8, 60), _y + _rng.randrange(8, 60)],
                 fill=tuple(_rng.randrange(256) for _ in range(3)))
_cats_abs = [(40, 380, 260, 520), (640, 360, 860, 500)]  # absolute px
_xs = [0, 100, 200, 300, 400, 500]
def _crop_jpeg(x):
    b = _io.BytesIO()
    _wall.crop((x, 0, x + 400, 600)).save(b, format="JPEG", quality=95)
    return b.getvalue()
_frames = [_crop_jpeg(x) for x in _xs]
_photo = _crop_jpeg(230)
def _visible(x):
    out = []
    for (a1, b1, a2, b2) in _cats_abs:
        v1, v2 = max(a1, x), min(a2, x + 400)
        if v2 - v1 > 10:
            out.append({"box": (v1 - x, b1, v2 - x, b2), "confidence": 0.9})
    return out
_by_bytes = {f: _visible(x) for f, x in zip(_frames + [_photo], _xs + [230])}
_real_frames, _real_sam = p.clip_frames, p.sam_segment
p.clip_frames = lambda media, n=6: list(_frames)
p.sam_segment = lambda img, phrase: list(_by_bytes.get(img, []))
_one = p.count_look(_photo, [], "how many cats are there")
check("clip: the sent photo alone sees one (a sliver)",
      len(p.count_instances(_one, "cats")), 1)
_all = p.count_look(_photo, [], "how many cats are there", media=b"clip")
check("clip: union across the sweep counts both",
      len(p.count_instances(_all, "cats")), 2)
check("clip: lead says clip, both placed",
      p._count_opening(_all, "cats", 400, 600)[0].startswith(
          "I can see two cats in this clip"), True)
_flat = _Image.new("RGB", (400, 600), (90, 90, 90))
_fb = _io.BytesIO(); _flat.save(_fb, format="JPEG"); _flat_j = _fb.getvalue()
p.clip_frames = lambda media, n=6: [_flat_j] * 5  # 5 unlinked segments
p.sam_segment = lambda img, phrase: (
    [{"box": (10, 10, 100, 100), "confidence": 0.9},
     {"box": (200, 10, 300, 100), "confidence": 0.9}] if img == _flat_j
    else [])
_fall = p.count_look(_photo, [], "how many cats are there", media=b"clip")
check("clip: unregistrable sweep falls back to the best single frame",
      ([e.get("clip_union") for e in p.count_instances(_fall, "cats")]),
      ["max_frame", "max_frame"])
# A fast turn: views that barely overlap each other still link through
# the dense scan between them; key frames only where the view moved.
_dense = [_crop_jpeg(x) for x in range(0, 501, 25)]  # 21 scan frames
_by_bytes.update({f: _visible(x) for f, x in zip(_dense, range(0, 501, 25))})
p.clip_frames = lambda media, n=6: list(_dense)
_calls = []
p.sam_segment = lambda img, phrase: (_calls.append(img),
                                     list(_by_bytes.get(img, [])))[1]
_sweep = p.count_look(_photo, [], "how many cats are there", media=b"clip")
check("dense scan: both cats over the full sweep",
      len(p.count_instances(_sweep, "cats")), 2)
check("dense scan: SAM on key frames only (photo + <= 3 views of 21)",
      len(_calls) <= 4, True)
_still = [_crop_jpeg(200)] * 12
p.clip_frames = lambda media, n=6: list(_still)
_calls.clear()
_by_bytes[_still[0]] = _visible(200)
p.count_look(_photo, [], "how many cats are there", media=b"clip")
check("dense scan: a still clip costs one key frame", len(_calls), 2)
_split = [_crop_jpeg(0), _crop_jpeg(25), _flat_j, _crop_jpeg(475),
          _crop_jpeg(500)]
_by_bytes.update({_crop_jpeg(475): _visible(475), _flat_j: []})
p.clip_frames = lambda media, n=6: list(_split)
_sp = p.count_look(_crop_jpeg(250), [], "how many cats are there",
                   media=b"clip")
check("unlinked segments: counted, said as approximate",
      p._count_opening(_sp, "cats", 400, 600)[0].startswith(
          "I can see about"), True)
p.clip_frames, p.sam_segment = _real_frames, _real_sam
check("pan: a known shift is found",
      p._pan(p._thumb(_crop_jpeg(100), (64, 96)),
             p._thumb(_crop_jpeg(164), (64, 96))), (10, 0))

# Dev capture store: inputs + outcome per ask, failures included,
# newest CAPTURE_KEEP kept; off in production.
import asyncio as _asyncio, tempfile as _tempfile, json as _json
from pathlib import Path as _Path
from starlette.datastructures import UploadFile as _Upload
from app import capture as _cap, main as _main
_tmp = _Path(_tempfile.mkdtemp())
_cap.CAPTURE_DIR = _tmp
_os.environ["CAPTURE_ENABLED"] = "1"
_ep = {r.path: r.endpoint for r in _main.app.routes if hasattr(r, "path")}
def _call(path, **kw):
    try:
        return _asyncio.run(_ep[path](**kw)), None
    except Exception as e:
        return None, e
_real_ask, _real_fu = _main.run_ask, _main.run_followup
_main.run_ask = lambda img, q, m, a, trace: (
    trace.update({"ask_id": "abc", "scene_full": [{"name": "cat"}] * 2}),
    {"answer": "I can see two cats in this photo.", "ask_id": "abc"})[1]
_ok, _ = _call("/ask", image=_Upload(_io.BytesIO(_live_jpeg), filename="p.jpg"),
               question="how many cats", media=None, audio=None)
_d = sorted(_tmp.glob("*/*"))
check("capture: one folder with photo, request, response, trace",
      sorted(f.name for f in _d[0].iterdir()) if _d else [],
      ["photo.jpg", "request.json", "response.json", "trace.json"])
check("capture: full scene kept",
      len(_json.loads((_d[0] / "trace.json").read_text())["scene_full"]), 2)
def _stall(*a, **k):
    raise p.StageFailed("synthesis", TimeoutError("stalled"))
_main.run_ask = _stall
_, _err = _call("/ask", image=_Upload(_io.BytesIO(_live_jpeg), filename="p.jpg"),
                question="is the path clear", media=None, audio=None)
_e = _json.loads((sorted(_tmp.glob("*/*"))[-1] / "error.json").read_text())
check("capture: a failed ask is kept with its error",
      (getattr(_err, "status_code", None), _e["status"],
       _e["detail"]["stage"]), (503, 503, "synthesis"))
_main.run_followup = lambda ask_id, q, a, trace: (
    trace.update({"ask_id": "def", "parent_ask_id": ask_id}),
    {"answer": "None.", "ask_id": "def"})[1]
_call("/followup", ask_id="abc", question="how many cups", audio=None)
_f = _json.loads((sorted(_tmp.glob("*/*"))[-1] / "request.json").read_text())
check("capture: follow-up names its parent ask",
      (_f["endpoint"], _f["parent_ask_id"]), ("followup", "abc"))
_main.run_ask = lambda img, q, m, a, trace: {"answer": "x"}
_keep = _cap.CAPTURE_KEEP
_cap.CAPTURE_KEEP = 5
for _ in range(8):
    _call("/ask", image=_Upload(_io.BytesIO(_live_jpeg), filename="p.jpg"),
          question="q", media=None, audio=None)
check("capture: pruned to the newest CAPTURE_KEEP",
      len(list(_tmp.glob("*/*"))), 5)
_cap.CAPTURE_KEEP = _keep
_os.environ["ALUMBRA_ENV"] = "production"
check("capture: off in production even when enabled", _cap.enabled(), False)
del _os.environ["ALUMBRA_ENV"], _os.environ["CAPTURE_ENABLED"]
check("capture: on by default through the hackathon week",
      _cap.enabled(), __import__("datetime").date.today()
      <= _cap.CAPTURE_DEFAULT_UNTIL)
_main.run_ask, _main.run_followup = _real_ask, _real_fu
__import__("shutil").rmtree(_tmp)

# App wiring
from app.main import app
routes = sorted({r.path for r in app.routes if hasattr(r, "path")})
check("has /ask", "/ask" in routes, True)
check("has /health", "/health" in routes, True)
print("routes:", routes)
print("key loaded:", bool(p._load_key()), "(not printed)")
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
