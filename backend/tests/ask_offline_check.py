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

# App wiring
from app.main import app
routes = sorted({r.path for r in app.routes if hasattr(r, "path")})
check("has /ask", "/ask" in routes, True)
check("has /health", "/health" in routes, True)
print("routes:", routes)
print("key loaded:", bool(p._load_key()), "(not printed)")
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
