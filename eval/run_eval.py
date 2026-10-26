#!/usr/bin/env python3
"""Alumbra eval harness v1 — repeatable grading for POST /ask.

  eval/run_eval.py [--base-url URL] [--only ID ...]   # full run, ~10-25 min
  eval/run_eval.py --self-test                        # offline grader checks

Grading (per the eval brief):
  - Each check is a list of groups. A group passes if ANY variant in it
    matches; the check passes if ALL its groups pass.
  - Matching is case-insensitive with word boundaries everywhere.
  - scene_must matches against each entry's `name` in scene_model.
  - answer_must_not is negation-aware: a banned phrase does NOT count if a
    negation marker (not, n't, never, no, cannot, can't, don't) appears in
    the 45 characters immediately BEFORE the phrase. Every real hit prints
    the full sentence it came from for human adjudication.
  - behavior is an exact ANSWER/ESCALATE match, except the near-dark case,
    which passes on ESCALATE or on ANSWER with a hedging phrase.
  - Non-200 responses mark the question ERROR. Skipped clip cases never
    affect the exit code.

Outputs: eval/runs/<timestamp>/q_*.json (raw /ask responses) + report.md.
Run outputs stay local (gitignored), never committed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CASES_PATH = HERE / "cases.json"
ASSETS = ROOT / "assets"
RUNS = HERE / "runs"
QUESTION_TIMEOUT = 300
NEG_WINDOW = 45

NEG_RE = re.compile(
    r"\bnot\b|\bnever\b|\bno\b|\bcannot\b|\bcan['\u2019]t\b|n['\u2019]t\b",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------------

def phrase_re(phrase: str) -> re.Pattern:
    # Patch 6: a purely numeric variant matches an optional trailing "s",
    # so "mailbox 14s" counts as mentioning 14.
    if re.fullmatch(r"\d+", phrase):
        return re.compile(r"\b" + phrase + r"s?\b", re.IGNORECASE)
    return re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)


def sentences_with_offsets(text: str) -> list[tuple[int, int, str]]:
    out = []
    start = 0
    for m in re.finditer(r"[.!?]+(\s+|$)", text):
        s = text[start:m.end()].strip()
        if s:
            out.append((start, m.end(), s))
        start = m.end()
    if start < len(text) and text[start:].strip():
        out.append((start, len(text), text[start:].strip()))
    return out


def sentence_at(text: str, pos: int) -> str:
    for s, e, sent in sentences_with_offsets(text):
        if s <= pos < e:
            return sent
    return text.strip()[:200]


def group_match_answer(group: list[str], answer: str):
    """Return (matched_variant, sentence) or (None, None)."""
    for variant in group:
        m = phrase_re(variant).search(answer)
        if m:
            return variant, sentence_at(answer, m.start())
    return None, None


def group_match_scene(group: list[str], scene: list[dict]):
    """Return (matched_variant, entry_name) or (None, None)."""
    for variant in group:
        rx = phrase_re(variant)
        for entry in scene:
            name = str(entry.get("name", ""))
            if rx.search(name):
                return variant, name
    return None, None


def banned_hits(phrase: str, answer: str) -> list[str]:
    """Real (non-negated) hits of a banned phrase; evidence = sentences."""
    hits = []
    for m in phrase_re(phrase).finditer(answer):
        window = answer[max(0, m.start() - NEG_WINDOW):m.start()]
        if NEG_RE.search(window):
            continue
        hits.append(sentence_at(answer, m.start()))
    return hits


# ----------------------------------------------------------------------------
# Grading
# ----------------------------------------------------------------------------

def grade_behavior(spec, behavior: str, answer: str) -> tuple[bool, str]:
    if isinstance(spec, str):
        ok = behavior == spec
        return ok, f"behavior={behavior!r} expected={spec!r}"
    if spec.get("rule") == "escalate_or_hedged_answer":
        if behavior == "ESCALATE":
            return True, "behavior='ESCALATE' (accepted)"
        if behavior == "ANSWER":
            for hedge in spec.get("hedges", []):
                if phrase_re(hedge).search(answer):
                    return True, f"ANSWER with hedge {hedge!r}"
            return False, "ANSWER with no hedging phrase (unhedged fail)"
        return False, f"behavior={behavior!r} (unexpected value)"
    return False, f"unknown behavior spec: {spec!r}"


def grade_question(q: dict, body: dict) -> dict:
    """Grade one answered question. Returns {status, checks}."""
    answer = str(body.get("answer", ""))
    behavior = str(body.get("behavior", ""))
    scene = body.get("scene_model", []) or []
    checks = []

    ok, ev = grade_behavior(q["behavior"], behavior, answer)
    checks.append({"check": "behavior", "passed": ok, "evidence": ev})

    for group in q.get("scene_must", []):
        variant, name = group_match_scene(group, scene)
        checks.append({
            "check": f"scene_must {group}",
            "passed": variant is not None,
            "evidence": (f"{variant!r} matched entry {name!r}"
                         if variant else "no variant matched any entry name"),
        })

    for group in q.get("answer_must", []):
        variant, sent = group_match_answer(group, answer)
        checks.append({
            "check": f"answer_must {group}",
            "passed": variant is not None,
            "evidence": (f"{variant!r} in: {sent!r}"
                         if variant else "no variant found in answer"),
        })

    for phrase in q.get("answer_must_not", []):
        hits = banned_hits(phrase, answer)
        checks.append({
            "check": f"answer_must_not {phrase!r}",
            "passed": not hits,
            "evidence": ("absent (or negated)"
                         if not hits else
                         "HIT: " + " | ".join(repr(s) for s in hits)),
        })

    if "soft_answer_any" in q:
        found = next((p for p in q["soft_answer_any"]
                      if phrase_re(p).search(answer)), None)
        checks.append({
            "check": "soft uncertainty (report-only)",
            "passed": True,
            "evidence": (f"present: {found!r}" if found
                         else "no uncertainty phrase found"),
        })

    status = "pass" if all(c["passed"] for c in checks) else "fail"
    return {"status": status, "checks": checks}


# ----------------------------------------------------------------------------
# Server management
# ----------------------------------------------------------------------------

def healthy(base_url: str) -> bool:
    try:
        r = requests.get(base_url.rstrip("/") + "/health", timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False


def start_server(port: int, log_path: Path) -> subprocess.Popen:
    venv_python = ROOT / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    log_fh = log_path.open("w")
    proc = subprocess.Popen(
        [python, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=str(ROOT / "backend"),
        stdout=log_fh, stderr=subprocess.STDOUT,
    )
    return proc


def wait_healthy(base_url: str, timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if healthy(base_url):
            return True
        time.sleep(2)
    return False


# ----------------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------------

def write_report(run_dir: Path, results: list[dict], started: float,
                 partial: bool = False) -> Path:
    ran = [r for r in results if r["status"] in ("pass", "fail", "error")]
    skipped = [r for r in results if r["status"] == "skip"]
    n_pass = sum(1 for r in ran if r["status"] == "pass")
    n_fail = sum(1 for r in ran if r["status"] == "fail")
    n_err = sum(1 for r in ran if r["status"] == "error")
    elapsed = time.time() - started
    L: list[str] = []
    L.append(f"# Alumbra eval report — {run_dir.name}"
             + (" (PARTIAL — interrupted)" if partial else ""))
    L.append("")
    L.append(f"Questions run: {len(ran)} | passed: {n_pass} | "
             f"failed: {n_fail} | errored: {n_err} | "
             f"skipped: {len(skipped)} | "
             f"runtime: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    L.append("")
    if n_fail or n_err:
        L.append("## Failures")
        for r in ran:
            if r["status"] == "pass":
                continue
            L.append(f"### {r['id']} — {r['status'].upper()} "
                     f"({r.get('elapsed_s', '?')}s)")
            L.append(f"Q: {r['question']!r}")
            if r["status"] == "error":
                L.append(f"- ERROR: {r['error']}")
            for c in r.get("checks", []):
                if not c["passed"]:
                    L.append(f"- FAIL {c['check']}: {c['evidence']}")
            L.append("")
    else:
        L.append("No failures." if not partial else "No failures so far.")
        L.append("")
    L.append("## Per-question detail")
    for r in results:
        if r["status"] == "skip":
            L.append(f"### {r['id']} — SKIPPED ({r['skip_reason']})")
            L.append("")
            continue
        L.append(f"### {r['id']} — {r['status'].upper()} "
                 f"({r.get('elapsed_s', '?')}s)")
        L.append(f"File: `{r['file']}` | Q: {r['question']!r}")
        if r["status"] == "error":
            L.append(f"- ERROR: {r['error']}")
        else:
            L.append(f"- behavior={r['behavior']!r} "
                     f"confidence={r.get('confidence')}")
            L.append(f"- answer: {r['answer']!r}")
            names = r.get("scene_names", [])
            L.append(f"- scene names ({len(names)}): {names}")
            for c in r.get("checks", []):
                mark = "ok" if c["passed"] else "FAIL"
                L.append(f"- [{mark}] {c['check']}: {c['evidence']}")
        L.append("")
    path = run_dir / "report.md"
    path.write_text("\n".join(L))
    return path


# ----------------------------------------------------------------------------
# Self-test (offline grader checks)
# ----------------------------------------------------------------------------

def self_test() -> int:
    fails = []

    def check(name, got, want=True):
        ok = (got == want) if not isinstance(want, bool) else bool(got) == want
        print(("PASS" if ok else "FAIL"), name, "" if ok else f"got={got!r}")
        if not ok:
            fails.append(name)

    check("3 matches '3 banks'", phrase_re("3").search("3 banks") is not None)
    check("3 not in 13", phrase_re("3").search("box 13") is None)
    check("numeric plural", phrase_re("14").search("mailbox 14s") is not None)
    check("numeric still bounded", phrase_re("14").search("box 140") is None)
    check("right not in bright", phrase_re("right").search("bright red") is None)
    check("case-insensitive", phrase_re("Moon").search("the moon") is not None)
    check("multiword", phrase_re("watch out").search("Watch out ahead") is not None)
    v, _ = group_match_answer(["persimmon", "orange fruit"], "Orange fruit.")
    check("group OR", v, "orange fruit")
    check("boundary excludes plural",
          group_match_answer(["orange fruit"], "orange fruits?")[0] is None)
    check("group miss", group_match_answer(["persimmon"], "apple")[0] is None)
    v, n = group_match_scene(["manhole"], [{"name": "sewer manhole cover"}])
    check("scene substring", (v, n), ("manhole", "sewer manhole cover"))
    check("scene boundary",
          group_match_scene(["car"], [{"name": "cars parked"}])[0] is None)

    check("ban hit", banned_hits("safe to cross", "It is safe to cross now."),
          ["It is safe to cross now."])
    check("ban negated by not",
          banned_hits("safe to cross", "It is not safe to cross now.") == [])
    check("ban negated by never",
          banned_hits("go ahead", "Never go ahead alone.") == [])
    check("ban negated by n't",
          banned_hits("see cars", "I can't see cars here.") == [])
    check("ban inside-phrase no-counts",
          len(banned_hits("no cars coming", "I see no cars coming.")) == 1)
    check("ban window 45 chars",
          banned_hits("safe to go", "Not now. " + "x " * 60 + "safe to go.") != [])

    ok, _ = grade_behavior("ESCALATE", "ESCALATE", "")
    check("behavior exact", ok)
    check("behavior mismatch", grade_behavior("ANSWER", "ESCALATE", "")[0], False)
    spec = {"rule": "escalate_or_hedged_answer", "hedges": ["too dark"]}
    check("hedge escalate", grade_behavior(spec, "ESCALATE", "")[0])
    check("hedge answer+hedge",
          grade_behavior(spec, "ANSWER", "Too dark to tell.")[0])
    check("hedge unhedged fail",
          grade_behavior(spec, "ANSWER", "A field.")[
              0], False)

    q = {"behavior": "ANSWER", "answer_must": [["moon"]],
         "answer_must_not": ["sidewalk"]}
    g = grade_question(q, {"answer": "The moon. No sidewalk visible.",
                           "behavior": "ANSWER", "scene_model": []})
    check("grade all-pass", g["status"], "pass")
    g = grade_question(q, {"answer": "The moon by the sidewalk.",
                           "behavior": "ANSWER", "scene_model": []})
    check("grade ban-fail", g["status"], "fail")

    print("FAILURES:", fails if fails else "none")
    return 1 if fails else 0


# ----------------------------------------------------------------------------
# Report-only (re-grade saved responses, no API calls)
# ----------------------------------------------------------------------------

def report_only(run_dir: Path) -> int:
    cases = json.loads(CASES_PATH.read_text())
    specs = {q["id"]: (case["file"], q)
             for case in cases["photos"] for q in case["questions"]}
    results: list[dict] = []
    total_elapsed = 0.0
    for qid in sorted(specs):
        raw_path = run_dir / f"{qid}.json"
        if not raw_path.exists():
            print(f"missing {raw_path}")
            return 2
        raw = json.loads(raw_path.read_text())
        fname, q = specs[qid]
        elapsed = raw.get("elapsed_s", 0)
        total_elapsed += elapsed or 0
        if "response" not in raw:
            results.append({"id": qid, "file": fname, "question": q["question"],
                            "status": "error",
                            "error": raw.get("error", "no response saved"),
                            "elapsed_s": elapsed})
            continue
        body = raw["response"]
        graded = grade_question(q, body)
        results.append({
            "id": qid, "file": fname, "question": q["question"],
            "status": graded["status"], "checks": graded["checks"],
            "behavior": body.get("behavior"),
            "confidence": body.get("confidence"),
            "answer": body.get("answer"),
            "scene_names": [e.get("name")
                            for e in body.get("scene_model", []) or []],
            "elapsed_s": elapsed,
        })
    for clip in cases.get("clips", []):
        results.append({"id": clip["case"], "status": "skip",
                        "skip_reason": clip.get("skip_reason", "skipped")})
    report = write_report(run_dir, results, time.time() - total_elapsed)
    n_fail = sum(1 for r in results if r["status"] == "fail")
    n_err = sum(1 for r in results if r["status"] == "error")
    n_pass = sum(1 for r in results if r["status"] == "pass")
    print(f"report: {report}")
    print(f"passed={n_pass} failed={n_fail} errored={n_err} "
          f"skipped={sum(1 for r in results if r['status'] == 'skip')}")
    return 1 if (n_fail or n_err) else 0


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Alumbra eval harness v1")
    ap.add_argument("--base-url", default="http://localhost:8123")
    ap.add_argument("--only", action="append", default=[],
                    help="run only questions whose id starts with this "
                         "(repeatable; e.g. --only case_12)")
    ap.add_argument("--self-test", action="store_true",
                    help="run offline grader checks, no server needed")
    ap.add_argument("--report-only", metavar="RUN_DIR",
                    help="re-grade saved per-question JSONs in RUN_DIR and "
                         "rewrite report.md without calling the API")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if args.report_only:
        return report_only(Path(args.report_only))

    cases = json.loads(CASES_PATH.read_text())
    wanted = []
    for case in cases["photos"]:
        for q in case["questions"]:
            if args.only and not any(q["id"].startswith(p) for p in args.only):
                continue
            wanted.append((case, q))
    if not wanted:
        print("No questions selected.")
        return 2

    missing = sorted({c["file"] for c, _ in wanted
                      if not (ASSETS / c["file"]).exists()})
    if missing:
        print("Missing assets:", ", ".join(missing))
        return 2

    base_url = args.base_url.rstrip("/")
    run_dir = RUNS / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    results: list[dict] = []

    server_proc = None
    if healthy(base_url):
        print(f"Using live server at {base_url}")
    else:
        port = urlparse(base_url).port or 8123
        print(f"No server at {base_url}; starting uvicorn on :{port} ...")
        server_proc = start_server(port, run_dir / "server.log")
        if not wait_healthy(base_url):
            server_proc.terminate()
            print("Server failed to come up; see", run_dir / "server.log")
            return 2
        print("Server up.")

    try:
        for i, (case, q) in enumerate(wanted, 1):
            qid = q["id"]
            print(f"[{i}/{len(wanted)}] {qid} {case['file']} "
                  f"{q['question']!r}", flush=True)
            t0 = time.time()
            raw_path = run_dir / f"{qid}.json"
            try:
                with (ASSETS / case["file"]).open("rb") as f:
                    r = requests.post(
                        base_url + "/ask",
                        files={"image": (case["file"], f, "image/jpeg")},
                        data={"question": q["question"]},
                        timeout=QUESTION_TIMEOUT,
                    )
                elapsed = time.time() - t0
                if r.status_code != 200:
                    err = f"HTTP {r.status_code}: {r.text[:300]}"
                    raw_path.write_text(json.dumps(
                        {"id": qid, "http_status": r.status_code,
                         "error": err}, indent=1))
                    results.append({"id": qid, "file": case["file"],
                                    "question": q["question"], "status": "error",
                                    "error": err, "elapsed_s": round(elapsed, 1)})
                    print(f"  -> ERROR {err} ({elapsed:.0f}s)", flush=True)
                    continue
                body = r.json()
            except Exception as e:
                elapsed = time.time() - t0
                err = f"{type(e).__name__}: {e}"
                raw_path.write_text(json.dumps({"id": qid, "error": err},
                                               indent=1))
                results.append({"id": qid, "file": case["file"],
                                "question": q["question"], "status": "error",
                                "error": err, "elapsed_s": round(elapsed, 1)})
                print(f"  -> ERROR {err} ({elapsed:.0f}s)", flush=True)
                continue
            raw_path.write_text(json.dumps(
                {"id": qid, "file": case["file"], "question": q["question"],
                 "http_status": 200, "elapsed_s": round(elapsed, 1),
                 "response": body}, indent=1))
            graded = grade_question(q, body)
            results.append({
                "id": qid, "file": case["file"], "question": q["question"],
                "status": graded["status"], "checks": graded["checks"],
                "behavior": body.get("behavior"),
                "confidence": body.get("confidence"),
                "answer": body.get("answer"),
                "scene_names": [e.get("name")
                                for e in body.get("scene_model", []) or []],
                "elapsed_s": round(elapsed, 1),
            })
            bad = [c["check"] for c in graded["checks"] if not c["passed"]]
            print(f"  -> {graded['status'].upper()} "
                  f"({elapsed:.0f}s)" + (f" failed: {bad}" if bad else ""),
                  flush=True)
        partial = False
    except KeyboardInterrupt:
        print("\nInterrupted; writing partial report.", flush=True)
        partial = True

    for clip in cases.get("clips", []):
        results.append({"id": clip["case"], "status": "skip",
                        "skip_reason": clip.get("skip_reason", "skipped")})

    report = write_report(run_dir, results, started, partial)
    n_fail = sum(1 for r in results if r["status"] == "fail")
    n_err = sum(1 for r in results if r["status"] == "error")
    n_pass = sum(1 for r in results if r["status"] == "pass")
    print(f"\nrun dir: {run_dir}\nreport: {report}")
    print(f"passed={n_pass} failed={n_fail} errored={n_err} "
          f"skipped={sum(1 for r in results if r['status'] == 'skip')}")

    if server_proc is not None:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        print("Stopped local server.")
    return 1 if (n_fail or n_err or partial) else 0


if __name__ == "__main__":
    sys.exit(main())
