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
    "Are any of my cats in the way? For me to get out of the room?")
check("exit infers door", _pa["goal"], "door")
check("door not obstruction, chair is",
      [o["name"] for o in _pa["on_path"]], ["chair"])
_sw = {"name": "sidewalk", "box": [400, 500, 600, 900],
       "confidence": 0.9, "clock": 12, "size_class": "close"}
_pa2 = p._path_assessment([_sw], "Is the path ahead of me clear?")
check("sidewalk not obstruction", _pa2["verdict"], "clear")
_pa3 = p._path_assessment([_chair], "Can I get to the elevator?")
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
                         "is the path to the fridge clear?")
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
                          "is the path to the fridge clear?")
check("goal part excluded",
      "freezer drawers" in [o["name"] for o in _fa4["on_path"]], False)
check("ice in goal box kept",
      "ice cubes" in [o["name"] for o in _fa4["on_path"]], True)
_ghost = {"name": "countertop appliance", "box": [408, 0, 1075, 941],
          "confidence": 0.8, "clock": 1, "size_class": "close"}
_ghost_goal = {"name": "black refrigerator", "box": [408, 0, 1075, 938],
               "confidence": 0.9, "clock": 1, "size_class": "close"}
_fa7 = p._path_assessment([_ghost_goal, _ghost],
                          "is the path to the fridge clear?")
check("goal echo excluded",
      "countertop appliance" in [o["name"] for o in _fa7["on_path"]],
      False)
_toy = {"name": "plush toy", "box": [500, 900, 560, 960],
        "confidence": 0.8, "clock": 12, "size_class": "a few steps away"}
_couch = {"name": "couch", "box": [400, 500, 800, 1100],
          "confidence": 0.9, "clock": 12, "size_class": "close"}
_fa8 = p._path_assessment([_couch, _toy], "is the path to the couch clear?")
check("toy before couch kept",
      "plush toy" in [o["name"] for o in _fa8["on_path"]], True)
_chair2 = {"name": "chair", "box": [300, 600, 450, 1200],
          "confidence": 0.8, "clock": 11, "size_class": "close"}
_table = {"name": "table", "box": [200, 400, 600, 900],
          "confidence": 0.9, "clock": 11, "size_class": "close"}
_fa2 = p._path_assessment([_chair2, _table], "is the way ahead clear?")
check("tucked chair kept",
      "chair" in [o["name"] for o in _fa2["on_path"]], True)
_book = {"name": "book", "box": [300, 350, 400, 400],
         "confidence": 0.8, "clock": 12, "size_class": "close"}
_fa3 = p._path_assessment([_book, _table], "is the way ahead clear?")
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
                         height=1200)
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
                          "is the way ahead clear?", height=1200)
check("high far sign excluded",
      "sign" in [o["name"] for o in _fa6["on_path"]], False)
check("low far puddle kept",
      "puddle" in [o["name"] for o in _fa6["on_path"]], True)

# App wiring
from app.main import app
routes = sorted({r.path for r in app.routes if hasattr(r, "path")})
check("has /ask", "/ask" in routes, True)
check("has /health", "/health" in routes, True)
print("routes:", routes)
print("key loaded:", bool(p._load_key()), "(not printed)")
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
