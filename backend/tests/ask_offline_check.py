import sys
sys.path.insert(0, "/home/cromestant/code/alumbra/backend")
from app import pipeline as p
from app import capture as _capture
import tempfile as _tf
# Never touch the real backend/captures: the cache sweep reads it.
_capture.CAPTURE_DIR = __import__("pathlib").Path(_tf.mkdtemp())

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
# 2026-10-29: the named goal out of view no longer hides what is
# straight ahead (the chair in front of the user is still said).
check("path lead no goal: then what is straight ahead",
      p._path_opening(_pa3),
      ["I can't see the elevator in this photo.",
       "Careful — chair is in your path at 11 o'clock, close."])
check("path lead no goal, nothing ahead: says the way ahead is clear",
      p._path_opening(dict(_pa3, ahead={"verdict": "clear",
                                        "on_path": []})),
      ["I can't see the elevator in this photo.",
       "The way straight ahead is clear."])
check("path lead no goal, no ahead facts (old shape): the lead only",
      p._path_opening({k: v for k, v in _pa3.items() if k != "ahead"}),
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
# (Spark-only configuration; Baseten-first is tested below.)
import os as _os
_os.environ["BASETEN_ENABLED"] = "0"
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

# Baseten first, contributor Spark the retry (2026-10-28).
del _os.environ["BASETEN_ENABLED"]
_real_bc, _real_secret, _real_post = (p._baseten_complete, p._load_secret,
                                      p.requests.post)
p._load_secret = lambda name: "k" if name == "BASETEN_API_KEY" \
    else _real_secret(name)
_good = ("Careful — bench is in your path at 12 o'clock. The table, "
         "lawn and patio are at your 12 and 1 o'clock; the tree at "
         "your 2 o'clock.")
_hits = []
def _route(answers):
    seq = iter(answers)
    def bc(prompt, timeout):
        _hits.append(("baseten", timeout))
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v
    def tc(model, prompt, timeout):
        _hits.append((model, timeout))
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v
    p._baseten_complete, p._text_complete = bc, tc
_hits.clear(); _route([_good])
_meta = {}
_out, _applied, _ = p.synthesize_answer(_long, "is the way clear?",
                                        _scene_tw, None, [], None, _meta)
check("baseten: primary, 8 s budget, served",
      (_hits, _applied, _meta), ([("baseten", 8)], True,
                                 {"synth_provider": "baseten"}))
_hits.clear(); _route([_good.replace(". ", ".\n")])
check("synthesis output is one line (Baseten sends newlines)",
      p.synthesize_answer(_long, "is the way clear?", _scene_tw, None,
                          [])[0], _good)
_hits.clear(); _route([p.PipelineError("Baseten HTTP 503"), _good])
_meta = {}
p.synthesize_answer(_long, "is the way clear?", _scene_tw, None, [], None,
                    _meta)
check("baseten down: contributor Spark is the retry, own budget",
      (_hits, _meta["synth_provider"]),
      ([("baseten", 8), (p.SPARK_MODEL, p.SYNTHESIS_TIMEOUT)], "spark"))
# The probe's failure: a rewrite that drops a scene landmark ("lawn").
_no_lawn = ("Careful — bench is in your path at 12 o'clock. The table and "
            "patio are at your 12 and 1 o'clock; the tree at your 2 o'clock.")
_hits.clear(); _route([_no_lawn, _good])
_out, _, _ = p.synthesize_answer(_long, "is the way clear?", _scene_tw,
                                 None, [], [{"name": "lawn"}])
check("baseten dropping a landmark fails preservation; Spark retries",
      (len(_hits), "lawn" in _out), (2, True))
_hits.clear(); _route([_no_lawn, _no_lawn])
try:
    p.synthesize_answer(_long, "is the way clear?", _scene_tw, None, [],
                        [{"name": "lawn"}])
    check("both drop the landmark: the ask errors", "shipped", "StageFailed")
except p.StageFailed as e:
    check("both drop the landmark: the ask errors", e.stage, "synthesis")
_os.environ["BASETEN_ENABLED"] = "0"
_hits.clear(); _route([p.PipelineError("stall"), _good])
p.synthesize_answer(_long, "is the way clear?", _scene_tw, None, [])
check("BASETEN_ENABLED=0: Spark serves both attempts",
      [h[0] for h in _hits], [p.SPARK_MODEL, p.SPARK_MODEL])
del _os.environ["BASETEN_ENABLED"]
p._load_secret = lambda name: "" if name == "BASETEN_API_KEY" \
    else _real_secret(name)
check("no Baseten key: off", p.baseten_enabled(), False)
p._load_secret = lambda name: "k" if name == "BASETEN_API_KEY" \
    else _real_secret(name)
p._baseten_complete = _real_bc
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)
    def json(self):
        return self._body
_posted = []
def _post(url, headers, json, timeout):
    _posted.append((url, json, timeout))
    return _resp
p.requests.post = _post
_resp = _Resp(200, {"choices": [{"message": {"content": "  ok  "}}]})
check("baseten request: OpenAI-compatible, required parameters",
      (p._baseten_complete("hi", 8), _posted[-1][0],
       {k: _posted[-1][1][k] for k in ("model", "reasoning_effort",
                                       "max_tokens", "temperature",
                                       "stream")}),
      ("  ok  ", "https://inference.baseten.co/v1/chat/completions",
       {"model": "meta-models/muse-spark-open-1026",
        "reasoning_effort": "low", "max_tokens": 2000, "temperature": 0.2,
        "stream": False}))
_resp = _Resp(200, {"choices": [{"message": {"content": None,
                                             "reasoning_content": "..."}}]})
try:
    p._baseten_complete("hi", 8); _bad = None
except p._BadOutput:
    _bad = "BadOutput"
check("baseten empty content (reasoning ate the budget) is a failure",
      _bad, "BadOutput")
_env_model = __import__("subprocess").run(
    [sys.executable, "-c", "import sys; sys.path.insert(0, %r); "
     "from app import pipeline; print(pipeline.BASETEN_SYNTH_MODEL)"
     % str(__import__("pathlib").Path(p.__file__).parent.parent)],
    env=dict(_os.environ, BASETEN_SYNTH_MODEL="meta-models/other"),
    capture_output=True, text=True).stdout.strip()
check("BASETEN_SYNTH_MODEL overrides the model", _env_model,
      "meta-models/other")
p.requests.post, p._load_secret = _real_post, _real_secret
# Spark retry: a total wall-clock deadline, not just the per-read one.
_os.environ["BASETEN_ENABLED"] = "0"
_dl = p.SYNTHESIS_DEADLINE_S
p.SYNTHESIS_DEADLINE_S = 0.3
_trickle = []
def _slow(model, prompt, timeout):
    _trickle.append(timeout)
    _time_mod.sleep(2)  # reads keep arriving; the per-read timeout never fires
    return _good
_time_mod = __import__("time")
p._text_complete = _slow
_t = _time_mod.time()
try:
    p.synthesize_answer(_long, "is the way clear?", _scene_tw, None, [])
    _dl_res = "shipped"
except p.StageFailed as e:
    _dl_res = e.stage
check("spark past its total deadline fails; the ask errors after retry",
      (_dl_res, len(_trickle), _time_mod.time() - _t < 1.5),
      ("synthesis", 2, True))
p._text_complete = lambda m, pr, t: _good
check("spark inside its deadline still serves",
      p.synthesize_answer(_long, "is the way clear?", _scene_tw, None, [])[1],
      True)
p.SYNTHESIS_DEADLINE_S = _dl
check("spark deadline is 30 s total", p.SYNTHESIS_DEADLINE_S, 30)
del _os.environ["BASETEN_ENABLED"]
p._text_complete = _real_tc
_os.environ["BASETEN_ENABLED"] = "0"  # offline below: never the network

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
_real_http_post = p._HTTP.post
p._HTTP.post = lambda *a, **k: _R()
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
p._HTTP.post = _real_http_post

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

# Metric distance (2026-10-28): geometry from the app's capture
# metadata. pitch_deg = degrees the optical axis points DOWN from level;
# focal_px in upload pixels; principal point at the image center.
from app import metric as _m
import math as _math
# A tap photo as the Pixel 9a stores it: 4000x3000 sensor pixels, EXIF
# orientation 6 (display = raw turned 90 deg clockwise). A bright block
# at the raw top-left belongs at the upright top-right.
_raw = _Image.new("RGB", (4000, 3000), (0, 0, 0))
_raw.paste((255, 255, 255), (0, 0, 400, 300))
_ex = _Image.Exif()
_ex[274] = 6
_b = _io.BytesIO()
_raw.save(_b, format="JPEG", exif=_ex)
_sideways = _b.getvalue()
_up, _sc = _m.upright_photo(_sideways)
_upi = _Image.open(_io.BytesIO(_up))
check("upright: sideways 4000x3000 -> upright 960x1280, scale 0.32, "
      "no rotate tag left",
      (_upi.size, _sc, _upi.getexif().get(274, 1)), ((960, 1280), 0.32, 1))
check("upright: the raw top-left block lands top-right",
      (_upi.getpixel((930, 30))[0] > 200, _upi.getpixel((30, 30))[0] < 50),
      (True, True))
check("upright: an upright 720x1280 frame is the same bytes, scale 1",
      (lambda r: (r[0] == _live_jpeg, r[1]))(_m.upright_photo(_live_jpeg)),
      (True, 1.0))
check("upright: same input -> same pixels (follow-up reload)",
      _m.upright_photo(_sideways)[0] == _up, True)
check("upright: unreadable bytes pass through",
      _m.upright_photo(b"not a jpeg"), (b"not a jpeg", 1.0))
check("scale_camera: focal follows the pixels, pitch untouched",
      (_m.scale_camera({"pitch_deg": 30.0, "focal_px": 2694.1}, 0.32),
       _m.scale_camera(None, 0.32),
       _m.scale_camera({"pitch_deg": 1.0, "focal_px": 9.0}, 1.0)),
      ({"pitch_deg": 30.0, "focal_px": 862.1}, None,
       {"pitch_deg": 1.0, "focal_px": 9.0}))
_F, _H = 914.0, 1280  # ~Pixel main camera at the 720x1280 upload
def _y_for(d, pitch, h=1.3, f=_F, H=_H):
    """Box bottom row that puts a floor contact at d meters."""
    return H / 2 + f * _math.tan(_math.atan(h / d) - _math.radians(pitch))
_box = lambda y2: [300, y2 - 200, 420, y2]
check("ground: level camera, base placed for 2.0 m -> 2.0 +- 0.05",
      abs(_m.ground_distance(_box(_y_for(2.0, 0)), _H, 0, _F, 1.3) - 2.0)
      <= 0.05, True)
for _pitch, _d in ((35, 1.0), (30, 3.0), (45, 1.5), (10, 6.0)):
    check(f"ground: pitched {_pitch} deg, placed for {_d} m",
          round(_m.ground_distance(_box(_y_for(_d, _pitch)), _H, _pitch,
                                   _F, 1.3), 2), _d)
check("ground: cam height scales the distance",
      round(_m.ground_distance(_box(_y_for(2.0, 20, h=1.6)), _H, 20, _F,
                               1.6), 2), 2.0)
check("gate: depression under 5 deg -> None",
      _m.ground_distance(_box(_H / 2 + _F * _math.tan(_math.radians(4))),
                         _H, 0, _F, 1.3), None)
check("gate: base clipped by the frame bottom -> None",
      _m.ground_distance(_box(_H - 1), _H, 60, _F, 1.3), None)
check("gate: beyond 12 m -> None",
      _m.ground_distance(_box(_y_for(13.5, 0)), _H, 0, _F, 1.3), None)
check("gate: nearer than 0.3 m (or past straight down) -> None",
      (_m.ground_distance(_box(_y_for(0.25, 60)), _H, 60, _F, 1.3),
       _m.ground_distance(_box(1200), _H, 75, _F, 1.3)), (None, None))
_door_h = 2.03 * _F / 4.0  # a door 4 m away (prior test)
check("prior: door at a known 4 m",
      round(_m.prior_distance("door", [200, 300, 500, 300 + _door_h], _H,
                              _F), 2), 4.0)
check("prior: v1 table is door/person/car only",
      (sorted(_m.PRIOR_HEIGHT_M), _m.prior_distance(
          "bottle", [0, 300, 50, 500], _H, _F)),
      (["car", "door", "person"], None))
check("prior: clipped at the top or bottom -> None",
      (_m.prior_distance("person", [0, 0, 100, 900], _H, _F),
       _m.prior_distance("person", [0, 200, 100, _H], _H, _F)),
      (None, None))
check("reconcile: within 40% keeps ground, src both",
      _m.reconcile(2.0, 2.6, True), (2.0, "both", False))
check("reconcile: disagree, floor-standing -> ground",
      _m.reconcile(2.0, 3.5, True), (2.0, "ground", True))
check("reconcile: disagree, not floor-standing -> no metric",
      _m.reconcile(2.0, 3.5, False), (None, None, True))
check("reconcile: one present -> that one",
      (_m.reconcile(None, 4.0, False), _m.reconcile(1.2, None, True)),
      ((4.0, "prior", False), (1.2, "ground", False)))
check("spoken: coarse rounding bands",
      [_m.spoken(d) for d in (0.3, 0.5, 0.72, 1.0, 1.3, 2.0, 2.74, 2.76,
                              3.4, 3.6, 11.5)],
      ["within arm's reach", "about half a meter away",
       "about half a meter away", "about 1 meter away",
       "about 1 and a half meters away", "about 2 meters away",
       "about 2 and a half meters away", "about 3 meters away",
       "about 3 meters away", "about 4 meters away",
       "about 12 meters away"])
check("parse: both fields, finite, in range; else None",
      (_m.parse_camera("12.5", "914"), _m.parse_camera(None, "914"),
       _m.parse_camera("nan", "914"), _m.parse_camera("12", "-1"),
       _m.parse_camera("95", "914")),
      ({"pitch_deg": 12.5, "focal_px": 914.0}, None, None, None, None))
_os.environ["CAM_HEIGHT_M"] = "1.6"
check("CAM_HEIGHT_M overrides the 1.52 m default", _m.cam_height_m(), 1.6)
del _os.environ["CAM_HEIGHT_M"]

# Per instance: floor-standing gets ground; on a table / a surface none.
# These fixtures are placed for a 1.3 m phone height.
_os.environ["CAM_HEIGHT_M"] = "1.3"
_cam = {"pitch_deg": 20.0, "focal_px": _F}
_chair = p.entry_for("office chair", (250, int(_y_for(2.0, 20)) - 420, 470,
                                      int(_y_for(2.0, 20))), 0.9, 720, _H)
_mtable = p.entry_for("table", (380, 500, 700, 760), 0.9, 720, _H)
_mug = p.entry_for("mug", (500, 440, 560, 520), 0.9, 720, _H)
_mwall = p.entry_for("white wall", (0, 0, 720, 700), 0.9, 720, _H)
# A door 2.5 m away at 20 deg: its base low enough for the rule-2 floor
# band (which decides floor-standing), its height matching the prior.
_dy, _dh = int(_y_for(2.5, 20)), int(2.03 * _F / 2.5)
_mdoor = p.entry_for("wooden door", (60, _dy - _dh, 240, _dy), 0.9, 720, _H)
_ms, _mlog = p.apply_distances([_chair, _mtable, _mug, _mwall, _mdoor],
                               _cam, _H)
check("instances: chair by ground plane at 2 m",
      (round(_ms[0]["distance_m"], 1), _ms[0]["distance_src"]),
      (2.0, "ground"))
check("instances: mug on the table, the wall: no metric",
      ("distance_m" in _ms[2], "distance_m" in _ms[3]), (False, False))
check("instances: size_class untouched",
      [e["size_class"] for e in _ms],
      [e["size_class"] for e in (_chair, _mtable, _mug, _mwall, _mdoor)])
check("instances: door, ground and prior agree -> both, ground value",
      (_ms[4]["distance_src"], round(_ms[4]["distance_m"], 1)),
      ("both", 2.5))
_short = p.entry_for("door", (60, _dy - _dh // 2, 240, _dy), 0.9,
                     720, _H)  # half the height: the prior says 5 m
_sd, _slog = p.apply_distances([_short], _cam, _H)
check("instances: door, estimators disagree -> ground (floor), logged",
      (_sd[0]["distance_src"], round(_sd[0]["distance_m"], 1),
       _slog[0].get("disagree"), round(_slog[0]["prior_m"])),
      ("ground", 2.5, True, 5))
_plain = [_chair, _mtable]
check("no metadata: the same scene objects back, no log",
      (p.apply_distances(_plain, None, _H)[0] is _plain,
       p.apply_distances(_plain, None, _H)[1]), (True, []))
_os.environ["METRIC_DISTANCE_ENABLED"] = "0"
check("kill switch: no distance computed",
      p.apply_distances(_plain, _cam, _H)[0] is _plain, True)
del _os.environ["METRIC_DISTANCE_ENABLED"]

# Answers: meters replace the label, nearest first; never both.
_near = {"name": "box", "box": [450, 650, 550, 700], "confidence": 0.9,
         "clock": 12, "size_class": "far", "distance_m": 1.3}
_farb = {"name": "bag", "box": [450, 560, 550, 680], "confidence": 0.9,
         "clock": 12, "size_class": "close", "distance_m": 3.4}
_pa = p._path_assessment([_farb, _near], "is the way ahead clear?",
                         height=1200, width=1000)
check("path: nearest first when every mention has a distance",
      [o["name"] for o in _pa["on_path"]], ["box", "bag"])
check("path lead speaks meters, not labels",
      p._path_opening(_pa)[0],
      "Careful — box at 12 o'clock, about 1 and a half meters away, and "
      "bag at 12 o'clock, about 3 meters away, are in your path.")
_nod = [{k: v for k, v in e.items() if k != "distance_m"}
        for e in (_farb, _near)]
_pa1 = p._path_assessment(_nod, "is the way ahead clear?", height=1200,
                          width=1000)
check("path without distances: today's order and labels",
      ([o["name"] for o in _pa1["on_path"]], p._path_opening(_pa1)[0]),
      (["bag", "box"], "Careful — bag at 12 o'clock, close, and box at 12 "
                       "o'clock, far away, are in your path."))
check("hazards: nearest first when each has a distance",
      p._hazards_in([{"name": "puddle", "distance_m": 4.0},
                     {"name": "stairs", "distance_m": 1.0}]),
      ["stairs", "puddle"])
check("hazards: order unchanged without distances",
      p._hazards_in([{"name": "puddle"}, {"name": "stairs"}]),
      ["puddle", "stairs"])
check("distance never gates: a far object in the passage is announced",
      "bag" in [o["name"] for o in p._path_assessment(
          [dict(_farb, distance_m=11.0)], "is the way ahead clear?",
          height=1200, width=1000)["on_path"]], True)
_ps = p._prompt_scene([_near, {"name": "lamp", "size_class": "far"}],
                      "path")
check("Spark sees the spoken words, never the float or the label",
      (_ps[0].get("distance"), "distance_m" in _ps[0],
       "size_class" in _ps[0], _ps[1]),
      ("about 1 and a half meters away", False, False,
       {"name": "lamp", "size_class": "far"}))
check("find flow: Spark scene untouched",
      p._prompt_scene([_near], "find")[0], _near)
_md = ("Careful — box is in your path at 12 o'clock, about 2 meters away. "
       "The table is at your 12 o'clock, about 4 meters away.")
check("synthesis must keep each meter figure exactly",
      (p._synthesis_ok(_md, _md, [], [], None, [], []),
       p._synthesis_ok(_md, _md.replace("about 4 meters", "about 3 meters"),
                       [], [], None, [], []),
       p._synthesis_ok(_md, "Careful — box is in your path at 12 o'clock, "
                       "about 2 meters away. The table is at your 12 "
                       "o'clock.", [], [], None, [], [])),
      (True, False, False))

# End to end: the same table walk with and without capture metadata.
_os.environ["JEV_GATE_ENABLED"] = "0"
_os.environ["FLOOR_LOOK_ENABLED"] = "0"
p._vlm_text = lambda m, img, instr, t: '{"answer": "", "confidence": 0.8}'
def _walk(camera):
    return p._compose(
        _jpeg(200), [dict(_otable), dict(_bench)], "can I get to the table?",
        "text", "can I get to the table?", "A", None, "", None, None,
        200.0, 200, 0.0, 0, camera=camera)[0]["answer"]
_cam12 = {"pitch_deg": 20.0, "focal_px": 900.0}
check("e2e old app (no metadata): today's labels",
      _walk(None), "Careful — bench is in your path at 12 o'clock, far "
                   "away. The table is at your 12 o'clock, far away.")
check("e2e with metadata: meters from geometry",
      _walk(_cam12), "Careful — bench is in your path at 12 o'clock, about "
                     "2 and a half meters away. The table is at your 12 "
                     "o'clock, about 4 meters away.")
p._vlm_text = _real_vlm
del _os.environ["CAM_HEIGHT_M"]

# Tape-measure test 2026-10-29 (Pixel 9a, phone at ~5 ft, uploads
# 960x1280 after upright_photo, focal 2694.1 x 0.32 = 862.1).
check("default phone height is 1.52 m", _m.cam_height_m(), 1.52)
_stopper = dict(p.entry_for("draft stopper", (363, 645, 612, 696), 0.9, 960,
                            1280), size_class="a few steps away")  # as live
_tape, _ = p.apply_distances([_stopper],
                             {"pitch_deg": 33.43, "focal_px": 862.1}, 1280)
check("tape 2 m: a base at 54% of the frame gets a floor distance "
      "(was dropped by the path band), ~2.0 m at 1.52 m",
      (p._stands_on_floor(_stopper, 1280), _tape[0].get("distance_src"),
       abs(_tape[0].get("distance_m", 0) - 2.0) <= 0.1),
      (False, "ground", True))
_live2 = [p.entry_for("tabby cat", (212, 827, 439, 1118), 0.9, 960, 1280),
          p.entry_for("toy", (919, 605, 960, 647), 0.9, 960, 1280)]
_c33 = {"pitch_deg": 33.24, "focal_px": 862.1}  # horizon at row 75
check("metric floor: below the horizon from the metadata, not a band",
      [p._metric_floor(e, 1280, c) for e, c in (
          (_live2[0], _c33),
          (p.entry_for("door", (331, 0, 363, 60), 0.9, 960, 1280), _c33),
          (p.entry_for("tv", (0, 290, 78, 600), 0.9, 960, 1280),
           {"pitch_deg": 0.0, "focal_px": 862.1}))],
      [True, False, False])
_island = dict(p.entry_for("kitchen island", (577, 158, 960, 406), 0.9,
                           960, 1280), size_class="a few steps away")
_isl, _ = p.apply_distances([_island], _c33, 1280)
check("tape: the kitchen island (base at 32% of the frame, ~5-6 m) gets "
      "a floor distance, not the label",
      (_isl[0].get("distance_src"), round(_isl[0].get("distance_m", 0), 1),
       p._dist_words("a few steps away", _isl[0].get("distance_m"))),
      ("ground", 4.7, "about 5 meters away"))
# Path floor test with metadata (approved 2026-10-29): the saucer chair
# at 45% of a 30 deg-tilted frame was 'elevated' under its label band.
_saucer = dict(p.entry_for("gray saucer chair", (378, 257, 622, 571), 0.9,
                           960, 1280), size_class="a few steps away")
_c30 = {"pitch_deg": 29.87, "focal_px": 862.1}
check("path floor: saucer chair is an obstruction with metadata, "
      "'elevated' without (old app unchanged)",
      (p._not_obstruction(_saucer, None, [], [], 1280, _c30),
       p._not_obstruction(_saucer, None, [], [], 1280)), ("", "elevated"))
check("path floor: with metadata, a base above the horizon is elevated",
      p._not_obstruction(p.entry_for("clock", (400, 40, 460, 60), 0.9,
                                     960, 1280), None, [], [], 1280, _c30),
      "elevated")
_os.environ["METRIC_DISTANCE_ENABLED"] = "0"
check("path floor: kill switch -> the label band again",
      p._not_obstruction(_saucer, None, [], [], 1280, _c30), "elevated")
del _os.environ["METRIC_DISTANCE_ENABLED"]
_rmp = lambda n: p._not_obstruction(
    dict(p.entry_for(n, (394, 775, 597, 1046), 0.9, 960, 1280),
         size_class="a few steps away"), None, [], [], 1280)
check("ramp: a cat scratcher ramp is an object; a wheelchair ramp and "
      "a bare ramp stay passable",
      [_rmp(n) for n in ("cat scratcher ramp", "pet ramp",
                         "wheelchair ramp", "ramp")],
      ["", "", "passage", "passage"])
_pa_r = p._path_assessment(
    [dict(p.entry_for("cat scratcher ramp", (394, 775, 597, 1046), 0.9, 960,
                      1280), distance_m=0.9, distance_src="ground")],
    "is the way clear", 1280, width=960, camera=_c30)
check("route: the 1 m scratcher ramp is now in the path",
      (_pa_r["verdict"], [o["name"] for o in _pa_r["on_path"]]),
      ("blocked", ["cat scratcher ramp"]))
_two_cats = {"verdict": "blocked", "goal": None, "goal_desc": "way",
             "on_path": [
                 {"name": "cat", "clock": 11, "size_class": "far",
                  "distance_m": 1.0},
                 {"name": "cat", "clock": 12, "size_class": "far",
                  "distance_m": 3.0}]}
_split = " ".join(p._path_opening(_two_cats))
check("same noun, distances 1 m and 3 m: two mentions, each its own",
      ("about 1 meter" in _split, "about 3 meters" in _split,
       "11 and 12" in _split), (True, True, False))
_two_cats["on_path"][1]["distance_m"] = 1.2
_one = " ".join(p._path_opening(_two_cats))
check("same noun, 1.0 m and 1.2 m: still one obstacle at both clocks",
      ("11 and 12" in _one, _one.count("cat")), (True, 1))
_sameb = [dict(name="scratcher mat", box=[258, 859, 653, 966]),
          dict(name="cat bed", box=[257, 858, 654, 966]),
          dict(name="mop head", box=[257, 858, 654, 967]),
          dict(name="black cat", box=[456, 512, 722, 728])]
check("one box, one mention: the first name stays, other objects kept",
      [e["name"] for e in p._one_per_box(_sameb)],
      ["scratcher mat", "black cat"])
_dup_scene = [p.entry_for(n, b, 0.9, 960, 1280) for n, b in (
    ("scratcher mat", (258, 859, 653, 966)),
    ("mop head", (257, 858, 654, 967)))]
_dpa = p._path_assessment(_dup_scene, "is my way clear", 1280, width=960)
check("route: one draft stopper under two names is one hazard, "
      "still blocked",
      (_dpa["verdict"], [o["name"] for o in _dpa["on_path"]]),
      ("blocked", ["scratcher mat"]))
del _os.environ["JEV_GATE_ENABLED"], _os.environ["FLOOR_LOOK_ENABLED"]

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
_main.run_ask = lambda img, q, m, a, trace, cap=None, camera=None: (
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
_main.run_followup = lambda ask_id, q, a, trace, cap=None: (
    trace.update({"ask_id": "def", "parent_ask_id": ask_id}),
    {"answer": "None.", "ask_id": "def"})[1]
_call("/followup", ask_id="abc", question="how many cups", audio=None)
_f = _json.loads((sorted(_tmp.glob("*/*"))[-1] / "request.json").read_text())
check("capture: follow-up names its parent ask",
      (_f["endpoint"], _f["parent_ask_id"]), ("followup", "abc"))
_cams = []
_main.run_ask = lambda img, q, m, a, trace, cap=None, camera=None: (
    _cams.append(camera), {"answer": "x"})[1]
_call("/ask", image=_Upload(_io.BytesIO(_live_jpeg), filename="p.jpg"),
      question="q", media=None, audio=None, pitch_deg="22.5",
      focal_px="914.2")
_rq = _json.loads((sorted(_tmp.glob("*/*"))[-1] / "request.json")
                  .read_text())
check("route: capture metadata parsed, passed on, recorded",
      (_cams[-1], _rq.get("camera")),
      ({"pitch_deg": 22.5, "focal_px": 914.2},) * 2)
_call("/ask", image=_Upload(_io.BytesIO(_live_jpeg), filename="p.jpg"),
      question="q", media=None, audio=None, pitch_deg=None, focal_px=None)
_rq = _json.loads((sorted(_tmp.glob("*/*"))[-1] / "request.json")
                  .read_text())
check("route: old app (no fields) -> camera None, request.json as before",
      (_cams[-1], "camera" in _rq), (None, False))
# Live test 2026-10-29: a Pixel 9a tap photo arrives 4000x3000 with EXIF
# orientation 6, and was read on its side (a 3.9 m ghost distance).
_imgs = []
_main.run_ask = lambda img, q, m, a, trace, cap=None, camera=None: (
    _imgs.append(img), _cams.append(camera), {"answer": "x"})[1]
_call("/ask", image=_Upload(_io.BytesIO(_sideways), filename="p.jpg"),
      question="q", media=None, audio=None, pitch_deg="30",
      focal_px="2694.1")
_rq_dir = sorted(_tmp.glob("*/*"))[-1]
check("route: sideways tap photo -> pipeline gets it upright, 1280 long, "
      "focal scaled; the capture keeps the original",
      (_Image.open(_io.BytesIO(_imgs[-1])).size, _cams[-1],
       (_rq_dir / "photo.jpg").read_bytes() == _sideways,
       _json.loads((_rq_dir / "request.json").read_text())["camera"]),
      ((960, 1280), {"pitch_deg": 30.0, "focal_px": 862.1}, True,
       {"pitch_deg": 30.0, "focal_px": 2694.1}))
_call("/ask", image=_Upload(_io.BytesIO(_live_jpeg), filename="p.jpg"),
      question="q", media=None, audio=None, pitch_deg="22.5",
      focal_px="914.2")
check("route: an upright in-size upload reaches the pipeline as the "
      "same bytes, camera unscaled",
      (_imgs[-1] == _live_jpeg, _cams[-1]),
      (True, {"pitch_deg": 22.5, "focal_px": 914.2}))
_main.run_ask = lambda img, q, m, a, trace, cap=None, camera=None: {"answer": "x"}
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

# Follow-up cache on disk (2026-10-28): survives a restart, TTL from
# ASK_TTL_S (default 3600), expiry deletes media on disk, one copy of
# each photo/clip, memory-only when the store is off.
import time as _time
_EXPIRED = ("that ask has expired or is unknown — "
            "please ask again as a new question")
_os.environ["CAPTURE_ENABLED"] = "1"
_cdir = _Path(_tempfile.mkdtemp())
_cap.CAPTURE_DIR = _cdir
_ttl = p.ask_ttl_s()
check("ask TTL default is an hour", _ttl, 3600.0)
_root = _cap.begin("ask", "how many cats", image=_live_jpeg, media=b"\x00\x00\x00\x18ftypmp42")
p._ASK_CACHE.clear()
p._store_ask("r1", {
    "created": _time.time(), "image": _live_jpeg, "media": b"clipbytes",
    "scene": _live_cats, "flow": "A", "target": None,
    "question": "how many cats", "answer": "two", "behavior": "ANSWER",
    "floor_done": False, "read_targets": [], "referent": "cat"},
    capture_dir=_root, media_dir=_root)
check("persisted next to its capture",
      sorted(f.name for f in _root.iterdir()),
      ["cache-r1.json", "clip.mp4", "photo.jpg", "request.json"])
p._ASK_CACHE.clear()  # restart: fresh process state, same disk
_re = p._load_ask("r1")
check("restart: rehydrated from disk, same scene and media",
      (_re["image"] == _live_jpeg,
       _re["media"] == b"\x00\x00\x00\x18ftypmp42", _re["scene"] == [
          dict(e, box=list(e["box"])) for e in _live_cats],
       _re["question"]), (True, True, True, "how many cats"))
# Follow-up after the restart, through run_followup (models stubbed).
_os.environ["JEV_GATE_ENABLED"] = "0"
p._ASK_CACHE.clear()
_fu_dir = _cap.begin("followup", "how many cats", parent_ask_id="r1")
_sam_seen = []
p.sam_segment = lambda img, phrase: (_sam_seen.append(phrase), [])[1]
p.clip_frames = lambda media, *a, **k: []  # the stub clip has no video
p._vlm_text = lambda m, img, instr, t: '{"answer": "", "confidence": 0.8}'
_r_fu = p.run_followup("r1", "how many cats are there", None, None, _fu_dir)
check("restart: follow-up on the original id answers from the cached scene",
      _r_fu["answer"].startswith("I can see two cats"), True)
_fu_id = _r_fu["ask_id"]
_fu_rec = _json.loads((_fu_dir / f"cache-{_fu_id}.json").read_text())
check("follow-up references the root's media, never copies it",
      (_fu_rec["media_dir"] == str(_root.relative_to(_cdir)),
       sorted(f.name.split(".")[0] for f in _fu_dir.iterdir())),
      (True, ["cache-" + _fu_id, "request"]))
check("exactly one photo on disk for the chain",
      len(list(_cdir.glob("*/*/photo.*"))), 1)
p.sam_segment, p._vlm_text = _real_sam, _real_vlm
p.clip_frames = _real_frames
def _age(path, secs):
    rec = _json.loads(path.read_text())
    rec["created"] = _time.time() - secs
    path.write_text(_json.dumps(rec))
# TTL edges on the root (the follow-up entry is aged out of the way).
_age(_fu_dir / f"cache-{_fu_id}.json", _ttl + 60)
_age(_root / "cache-r1.json", _ttl - 5)
p._ASK_CACHE.clear()
check("TTL - 5s: still a conversation",
      p._load_ask("r1")["answer"], "two")
_age(_root / "cache-r1.json", _ttl + 5)
p._ASK_CACHE.clear()
try:
    p._load_ask("r1"); _msg = None
except p.FollowupExpired as e:
    _msg = str(e)
check("TTL + 5s: the same 410 message", _msg, _EXPIRED)
check("expired: media and cache entry gone from disk",
      sorted(f.name for f in _root.iterdir()), ["request.json"])
check("expired follow-up's entry gone too",
      list(_fu_dir.glob("cache-*")), [])
# A live follow-up keeps its root's media; when it expires, all go.
_root2 = _cap.begin("ask", "q", image=_live_jpeg)
p._store_ask("r2", {"created": _time.time(), "image": _live_jpeg,
                    "media": None, "scene": [], "question": "q"},
             capture_dir=_root2, media_dir=_root2)
_age(_root2 / "cache-r2.json", _ttl + 5)  # expires while f2 is answered
p._ASK_CACHE["r2"]["created"] = _time.time() - _ttl - 5
_fu2 = _cap.begin("followup", "q2", audio=b"RIFF0000WAVEfmt ", parent_ask_id="r2")
p._store_ask("f2", {"created": _time.time(), "image": _live_jpeg,
                    "media": None, "scene": [], "question": "q2"},
             capture_dir=_fu2, media_dir=_root2)
p._sweep_asks()
check("parent expired mid-answer: the new follow-up still holds its photo",
      [f.name for f in _root2.glob("photo.*")], ["photo.jpg"])
_age(_fu2 / "cache-f2.json", _ttl + 5)
p._ASK_CACHE.clear()
p._sweep_asks()
check("chain expired: root photo and follow-up audio gone",
      (list(_root2.glob("photo.*")), list(_fu2.glob("audio.*"))), ([], []))
# Failure posture: corrupt entry is an expiry; failed write is memory-only.
_root3 = _cap.begin("ask", "q", image=_live_jpeg)
p._store_ask("r3", {"created": _time.time(), "image": _live_jpeg,
                    "media": None, "scene": []},
             capture_dir=_root3, media_dir=_root3)
(_root3 / "cache-r3.json").write_text("{not json")
p._ASK_CACHE.clear()
try:
    p._load_ask("r3"); _msg = None
except p.FollowupExpired as e:
    _msg = str(e)
check("corrupt entry: the same 410", _msg, _EXPIRED)
_gone = _cdir / "no-such-day" / "no-such-ask"
p._store_ask("r4", {"created": _time.time(), "image": _live_jpeg,
                    "media": None, "scene": []},
             capture_dir=_gone, media_dir=_gone)
check("failed disk write: the ask still caches in memory",
      p._load_ask("r4")["created"] > 0, True)
# Store off: memory-only, exactly as before.
_os.environ["CAPTURE_ENABLED"] = "0"
_root5 = _cdir / "off"
_root5.mkdir()
p._store_ask("r5", {"created": _time.time(), "image": _live_jpeg,
                    "media": None, "scene": []},
             capture_dir=_root5, media_dir=_root5)
check("store off: nothing written", list(_root5.iterdir()), [])
p._ASK_CACHE.clear()
try:
    p._load_ask("r5"); _msg = None
except p.FollowupExpired as e:
    _msg = str(e)
check("store off: a restart ends the chain (today's behavior)", _msg,
      _EXPIRED)
_os.environ["ASK_TTL_S"] = "10"
p._store_ask("r6", {"created": _time.time() - 20, "image": b"",
                    "media": None, "scene": []})
try:
    p._load_ask("r6"); _msg = None
except p.FollowupExpired as e:
    _msg = str(e)
check("ASK_TTL_S overrides the TTL", (p.ask_ttl_s(), _msg), (10.0, _EXPIRED))
del _os.environ["ASK_TTL_S"]
# Count cap vs a live chain: 51 asks inside the TTL, the chain rooted at
# the oldest folder (root photo held via the follow-up's media_dir).
__import__("shutil").rmtree(_cdir)
_cdir = _Path(_tempfile.mkdtemp())
_cap.CAPTURE_DIR = _cdir
_os.environ["CAPTURE_ENABLED"] = "1"
p._ASK_CACHE.clear()
_croot = _cap.begin("ask", "how many cats", image=_live_jpeg)
p._store_ask("c0", {
    "created": _time.time(), "image": _live_jpeg, "media": None,
    "scene": _live_cats, "flow": "A", "target": None,
    "question": "how many cats", "answer": "two", "behavior": "ANSWER",
    "floor_done": False, "read_targets": [], "referent": "cat"},
    capture_dir=_croot, media_dir=_croot)
_cfu = _cap.begin("followup", "and now", parent_ask_id="c0")
p._store_ask("c1", dict(p._ASK_CACHE["c0"], created=_time.time()),
             capture_dir=_cfu, media_dir=_croot)
_age(_croot / "cache-c0.json", _ttl + 5)  # root entry expired, chain live
for _ in range(50):
    _cap.begin("ask", "q", image=_live_jpeg)
_left = sorted(_cdir.glob("*/*"))
check("count cap: a live chain's root survives 51 asks",
      (len(_left), _croot in _left, _cfu in _left,
       [f.name for f in _croot.glob("photo.*")]),
      (_cap.CAPTURE_KEEP, True, True, ["photo.jpg"]))
p._ASK_CACHE.clear()  # and after a restart, through the route
p.sam_segment = lambda img, phrase: []
p.clip_frames = lambda media, *a, **k: []
p._vlm_text = lambda m, img, instr, t: '{"answer": "", "confidence": 0.8}'
_cr, _cerr = _call("/followup", ask_id="c1",
                   question="how many cats are there", audio=None)
check("count cap: the chain's follow-up still answers (200)",
      (_cerr, getattr(_cr, "status_code", None)), (None, 200))
p.sam_segment, p._vlm_text = _real_sam, _real_vlm
p.clip_frames = _real_frames
del _os.environ["CAPTURE_ENABLED"]
del _os.environ["JEV_GATE_ENABLED"]
__import__("shutil").rmtree(_cdir)

# Segmentation fan-out (2026-10-29): bounded by SAM_CONCURRENCY
# (default 6), results in phrase / frame / region order, Meta calls on
# one shared session, 429s logged.
import threading as _thr, time as _time, random as _rnd, contextlib as _ctx
_inflight, _peak, _lk = [0], [0], _thr.Lock()
def _slow_sam(img, phrase):
    with _lk:
        _inflight[0] += 1
        _peak[0] = max(_peak[0], _inflight[0])
    _time.sleep(_rnd.uniform(0.01, 0.05))
    with _lk:
        _inflight[0] -= 1
    return [{"box": (0, 0, 10, 10), "confidence": 0.9, "tag": phrase}]
_real_seg2 = p.sam_segment
p.sam_segment = _slow_sam
_phr = [f"thing{i}" for i in range(20)]
_os.environ["SAM_CONCURRENCY"] = "3"
_peak[0] = 0
_many = p.sam_many(b"img", _phr)
check("fan-out: phrase order kept, every phrase answered",
      (list(_many), [v[0]["tag"] for v in _many.values()]), (_phr, _phr))
check("fan-out: SAM_CONCURRENCY=3 bounds in-flight calls (and they "
      "overlap)", (_peak[0] <= 3, _peak[0] > 1), (True, True))
del _os.environ["SAM_CONCURRENCY"]
check("fan-out: default 6; a bad value falls back to 6",
      (p.sam_concurrency(),
       (_os.environ.__setitem__("SAM_CONCURRENCY", "zero"),
        p.sam_concurrency())[1]), (6, 6))
del _os.environ["SAM_CONCURRENCY"]
_peak[0] = 0
_frames = [bytes([i]) for i in range(12)]
p.sam_segment = lambda f, phrase: (_slow_sam(f, phrase), [
    {"box": (f[0], 0, f[0] + 1, 1), "confidence": 0.5}])[1]
check("fan-out: keyframes come back in frame order, at most 6 at once",
      ([r[0][0][0] for r in p.sam_many_images(_frames, "cat")],
       _peak[0] <= 6), (list(range(12)), True))
# Floor look: regions segment concurrently, merged in region order.
_fl_calls = []
def _region_sam(img, phrase):
    with _Image.open(_io.BytesIO(img)) as _im:
        w = _im.size[0]
    _fl_calls.append(w)
    _time.sleep(0.1 if w < 500 else 0.0)  # region 0 (400 px) finishes last
    return [{"box": (5, 5, 400, 300), "confidence": 0.9}]
p.sam_segment = _region_sam
_real_vlm2 = p._vlm_text
p._vlm_text = lambda m, img, instr, t: "box"
_fl_scene = [p.entry_for("rug", (100, 700, 500, 1000), 0.9, 1000, 1200)]
_fl = p.floor_look(_jpeg(120), _fl_scene)
_regs = p._floor_regions(_fl_scene, 1000, 1200)
check("floor look: one SAM call per region; the first region's find "
      "is kept first (region order, not finish order)",
      (len(_fl_calls), _fl[0]["box"][0] if _fl else None),
      (len(_regs), 5 + max(0, _regs[0][0])))
p._vlm_text = _real_vlm2
p.sam_segment = _real_seg2
# Shared session and 429 logging.
class _R429:
    status_code = 429
    text = "slow down"
    headers = {"Retry-After": "2"}
_sess_calls = []
_real_hp = p._HTTP.post
p._HTTP.post = lambda url, **k: (_sess_calls.append(url), _R429())[1]
_buf = _io.StringIO()
with _ctx.redirect_stdout(_buf):
    try:
        p._stream_output_text({"model": "m"}, 5)
    except p.PipelineError as e:
        _e429 = str(e)
_line = [l for l in _buf.getvalue().splitlines() if "rate_limited" in l]
check("session: Meta calls go through the shared session; a 429 is "
      "logged and still errors",
      (_sess_calls == [p.ENDPOINT], len(_line),
       _json.loads(_line[0])["retry_after"] if _line else None,
       "HTTP 429" in _e429), (True, 1, "2", True))
p._HTTP.post = _real_hp
check("session: one pooled session, sized above the fan-out",
      (isinstance(p._HTTP, p.requests.Session),
       p._HTTP.get_adapter("https://api.meta.ai")._pool_maxsize
       >= p.sam_concurrency()), (True, True))

# Items 2/4 (2026-10-29): noun cap, floor reads, tiny persons.
check("naming keeps up to 16 nouns",
      len(p.parse_nouns(_json.dumps([f"thing {i}" for i in range(20)]))), 16)
check("floor read: body parts are discarded, a shoe is kept",
      [p._discard_read(x) for x in ("foot", "Feet", "hand", "NONE", "shoe",
                                    "scratcher", "ice cubes")],
      [True, True, True, True, False, False, False])
check("floor read prompt: neutral, not 'small object'",
      ("small object" in p.FLOOR_READ_PROMPT, "NONE" in p.FLOOR_READ_PROMPT),
      (False, True))
_real_sm = p.sam_many
p.sam_many = lambda img, phrases: {
    ph: ([{"box": (695, 143, 715, 172), "confidence": 0.76},
          {"box": (0, 98, 45, 399), "confidence": 0.86}]
         if ph == "person" else []) for ph in phrases}
_ppl = [e for e in p._ground(_live_jpeg, ["person", "lamp"], 720, 1280)
        if e["name"] == "person"]
check("person: a 29 px figure (wall art) is dropped, a 300 px one kept",
      [list(e["box"]) for e in _ppl], [[0, 98, 45, 399]])
p.sam_many = _real_sm

# Item 5 (2026-10-29): floor reads race Spark (preferred within the
# grace) against Baseten (used when Spark is late).
_rb, _rv, _rs = p._baseten_complete, p._vlm_text, p._load_secret
_grace = p.FLOOR_READ_SPARK_GRACE_S
p.FLOOR_READ_SPARK_GRACE_S = 0.3
p._load_secret = lambda name: "k" if name == "BASETEN_API_KEY" else _rs(name)
del _os.environ["BASETEN_ENABLED"]
p._baseten_complete = lambda instr, t, image=None: (
    _time.sleep(0.01), " garlic cloves\n")[1]
p._vlm_text = lambda m, img, instr, t: (_time.sleep(0.05), "ice cubes")[1]
check("floor read: Spark on time wins over a faster Baseten",
      p._floor_read(b"crop"), "ice cubes")
p._vlm_text = lambda m, img, instr, t: (_time.sleep(2), "late")[1]
_t0 = _time.time()
_late = p._floor_read(b"crop")
check("floor read: Spark past the grace -> Baseten's read, no wait for "
      "Spark", (_late, _time.time() - _t0 < 1.5), ("garlic cloves", True))
def _bt_fail(instr, t, image=None):
    raise p.PipelineError("Baseten HTTP 503")
p._baseten_complete = _bt_fail
p._vlm_text = lambda m, img, instr, t: (_time.sleep(0.5), "toy")[1]
check("floor read: Baseten fails, Spark late -> Spark's read",
      p._floor_read(b"crop"), "toy")
p._vlm_text = lambda m, img, instr, t: (_ for _ in ()).throw(
    p.PipelineError("spark down"))
try:
    p._floor_read(b"crop"); _fr = "no error"
except p.StageFailed as e:
    _fr = e.stage
check("floor read: both fail -> the ask errors (floor_look)", _fr,
      "floor_look")
_os.environ["BASETEN_ENABLED"] = "0"
_reads = []
p._vlm_text = lambda m, img, instr, t: (_reads.append("spark"), "cord")[1]
check("floor read: BASETEN_ENABLED=0 -> Spark only",
      (p._floor_read(b"crop"), _reads), ("cord", ["spark"]))
p._baseten_complete, p._vlm_text, p._load_secret = _rb, _rv, _rs
p.FLOOR_READ_SPARK_GRACE_S = _grace

check("floor find read as bare 'ramp' is an object; a named ramp passes",
      (p._not_obstruction(dict(p.entry_for("ramp", (287, 674, 455, 843), 0.9,
                                           720, 1280), floor_find=True),
                          None, [], [], 1280),
       p._not_obstruction(p.entry_for("ramp", (287, 674, 455, 843), 0.9,
                                      720, 1280), None, [], [], 1280)),
      ("", "passage"))

# Coded-lead answers race Baseten (2026-10-29); find answers stay Spark.
_rb, _rv, _rs = p._baseten_complete, p._vlm_text, p._load_secret
_ag = p.ANSWER_SPARK_GRACE_S
p.ANSWER_SPARK_GRACE_S = 0.2
p._load_secret = lambda name: "k" if name == "BASETEN_API_KEY" else _rs(name)
del _os.environ["BASETEN_ENABLED"]
_who = []
p._baseten_complete = lambda instr, t, image=None: (
    _who.append("baseten"), '{"answer": "B.", "confidence": 0.7}')[1]
p._vlm_text = lambda m, img, instr, t: (
    _who.append("spark"), _time.sleep(1), '{"answer": "S.", "confidence": 0.9}')[2]
check("answer, coded lead: Spark late -> Baseten's detail",
      p._spark_answer(_live_jpeg, "is my path clear", [], "A",
                      allow_empty=True)[0], "B.")
_who.clear()
check("answer, find/other: Spark only, however slow",
      (p._spark_answer(_live_jpeg, "where's the aspirin", [], "C")[0],
       "baseten" in _who), ("S.", False))
p._baseten_complete, p._vlm_text, p._load_secret = _rb, _rv, _rs
p.ANSWER_SPARK_GRACE_S = _ag
_os.environ["BASETEN_ENABLED"] = "0"

# Naming starts while a spoken question is transcribed (2026-10-29).
_rt, _rn, _rfa = p.transcribe_question, p.name_scene, p.flow_a
_order = []
p.transcribe_question = lambda m: (_order.append("asr+"), _time.sleep(0.4),
                                   _order.append("asr-"), "what is here")[3]
p.name_scene = lambda img: (_order.append("name+"), _time.sleep(0.4),
                            _order.append("name-"), ["lamp"])[3]
_got = []
p.flow_a = lambda img, nouns=None: (_got.append(nouns), [])[1]
_real_compose = p._compose
p._compose = lambda *a, **k: ({"answer": "x"}, False, None, [])
_t0 = _time.time()
p.run_ask(_live_jpeg, "", b"clip", None, {}, None, None)
check("naming overlaps transcription; flow A gets the nouns",
      (_order[:2] in (["asr+", "name+"], ["name+", "asr+"]),
       _time.time() - _t0 < 0.7, _got), (True, True, [["lamp"]]))
_order.clear(); _got.clear()
p.run_ask(_live_jpeg, "what is here", b"clip", None, {}, None, None)
check("typed question: no speculative naming, flow A names itself",
      (_order, _got), ([], [None]))
p.transcribe_question, p.name_scene, p.flow_a = _rt, _rn, _rfa
p._compose = _real_compose

# App wiring
from app.main import app
routes = sorted({r.path for r in app.routes if hasattr(r, "path")})
check("has /ask", "/ask" in routes, True)
check("has /health", "/health" in routes, True)
print("routes:", routes)
print("key loaded:", bool(p._load_key()), "(not printed)")
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
