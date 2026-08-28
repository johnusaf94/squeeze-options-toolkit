"""
nightly_grade.py
================
The scheduled job that keeps the learning loop alive.

THE PROBLEM THIS EXISTS TO SOLVE
--------------------------------
The toolkit records everything it predicts (squeeze_logger -> squeeze_log.csv,
scenario_engine -> scenario_log.csv) and has all the machinery needed to grade
those predictions against reality. It just never ran. As of the first version
of this file: 2,575 logged squeeze candidates and 157 logged scenario sets,
NONE of them graded. Every tier gate downstream — the learning engine's
parameter tilts (30+ rows), the scenario engine's empirical blend (30+ rows),
the calibrated P(+15%/10d) model (150+ rows) — was therefore never reached, and
the whole system ran forever on its day-one heuristics with the evidence needed
to improve sitting unread on disk.

A loop that depends on somebody remembering to run a script is not a loop.
This file is the thing the scheduler calls so that nobody has to remember.

WHAT IT DOES (one pass, in order)
---------------------------------
  1. review_outcomes.fill_outcomes()      — 5/10/20-day forward returns for
                                            every matured squeeze candidate
  2. scenario_engine.grade_scenarios()    — realized outcome for every matured
                                            auto-generated scenario set
  3. learning_engine.update_from_logs()   — refits learned_params.json from the
                                            newly graded rows (incl. the
                                            calibration model)

DESIGN RULES
------------
  * NEVER CRASH THE SCHEDULER. Every step is isolated; a failure is recorded
    and the next step still runs. Exit code is 0 unless every step failed.
  * NEVER CORRUPT THE LOG. Grading rewrites squeeze_log.csv wholesale while a
    running GUI scan may be appending to it. This job takes the same lockfile
    squeeze_logger uses, and skips the run entirely rather than racing.
  * ALWAYS LEAVE A TRAIL. Every run writes grading_status.json (machine-read,
    for the dashboard) and appends to grading_runs.log (human-read). A job that
    fails silently is indistinguishable from the rot it was built to prevent —
    so silence is not an available outcome.

USAGE
-----
    python nightly_grade.py           # run the full pass
    python nightly_grade.py --status  # print last run's result, touch nothing
    python nightly_grade.py --dry     # counts only, no network, no writes
"""

import io
import json
import os
import sys
import time
import traceback
from contextlib import redirect_stdout
from datetime import datetime

# Must precede any yfinance use: installs the global rate limiter + cache that
# keeps a 150-ticker grading sweep from tripping Yahoo's throttle.
import yfinance_throttle  # noqa: F401  # installs global throttle

# Every downstream module resolves its data files RELATIVE to the working
# directory, so a scheduler launching us from C:\Windows\System32 would happily
# grade an empty log and report success. Anchor to this file's folder first.
_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_DIR)

# The graded modules print emoji (✅, 🔧). A Windows console/Task Scheduler pipe
# defaults to cp1252, where those raise UnicodeEncodeError mid-print — which is
# how the very first run of this file died AFTER successfully grading 2,444
# rows. Force UTF-8 with replacement so a display glyph can never take down a
# data job.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):    # pre-3.7, or already redirected
        pass

SQUEEZE_LOG = "squeeze_log.csv"
SCENARIO_LOG = "scenario_log.csv"
STATUS_FILE = "grading_status.json"
RUN_LOG = "grading_runs.log"

LOCK_FILE = SQUEEZE_LOG + ".lock"      # same convention as squeeze_logger.py
LOCK_STALE_S = 600
LOCK_TIMEOUT_S = 180                   # a bulk scan can hold it a while


# ─────────────────────────────────────────────
# LOCKING (interoperates with squeeze_logger.py)
# ─────────────────────────────────────────────

def _acquire_lock(timeout_s: float = LOCK_TIMEOUT_S) -> bool:
    """Exclusive-create lockfile, honoring squeeze_logger's staleness rule.
    Returns False on timeout — the caller must then SKIP, not proceed."""
    deadline = time.time() + timeout_s
    while True:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"nightly_grade {os.getpid()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK_FILE) > LOCK_STALE_S:
                    os.remove(LOCK_FILE)      # holder died; reclaim
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(2.0)
        except OSError:
            return False


def _release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


# ─────────────────────────────────────────────
# COUNTS (the before/after evidence)
# ─────────────────────────────────────────────

def _csv_counts(path: str, graded_col: str) -> dict:
    """{rows, graded, ungraded} for a log, without pandas."""
    import csv
    if not os.path.exists(path):
        return {"rows": 0, "graded": 0, "ungraded": 0, "missing": True}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return {"rows": 0, "graded": 0, "ungraded": 0, "unreadable": True}
    graded = sum(1 for r in rows if (r.get(graded_col) or "").strip())
    return {"rows": len(rows), "graded": graded,
            "ungraded": len(rows) - graded}


def counts() -> dict:
    return {"squeeze": _csv_counts(SQUEEZE_LOG, "outcome_checked"),
            "scenario": _csv_counts(SCENARIO_LOG, "outcome_checked")}


def tier_state() -> dict:
    """What the graded data has actually unlocked — the point of the job."""
    out = {}
    try:
        with open("learned_params.json", encoding="utf-8") as f:
            p = json.load(f)
        out["squeeze_learning"] = {
            "active": bool(p.get("squeeze", {}).get("active")),
            "n": p.get("squeeze", {}).get("n_samples", 0)}
        cal = p.get("calibration", {})
        out["calibration"] = {"active": bool(cal.get("active")),
                              "n": cal.get("n", 0),
                              "gate": cal.get("gate", ""),
                              "beats_baseline": cal.get("beats_baseline")}
    except (OSError, ValueError):
        out["squeeze_learning"] = {"active": False, "n": 0}
        out["calibration"] = {"active": False, "n": 0,
                              "gate": "learned_params.json not written yet"}
    try:
        with open("scenario_params.json", encoding="utf-8") as f:
            sp = json.load(f)
        out["scenario_tier"] = {"active": bool(sp.get("active")),
                                "n": sp.get("n_graded", 0),
                                "gate": sp.get("gate", "")}
    except (OSError, ValueError):
        out["scenario_tier"] = {"active": False, "n": 0,
                                "gate": "scenario_params.json not written yet"}
    return out


# ─────────────────────────────────────────────
# STEP RUNNER
# ─────────────────────────────────────────────

def _step(name: str, fn, verbose: bool = True) -> dict:
    """Run one grading step in isolation. Captures its console output so the
    run log holds the evidence, and converts any exception into a recorded
    failure rather than a dead scheduled task."""
    buf = io.StringIO()
    t0 = time.time()
    rec = {"step": name, "ok": True, "seconds": 0.0, "error": ""}
    try:
        with redirect_stdout(buf):
            rec["result"] = fn()
    except Exception as e:
        rec["ok"] = False
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["traceback"] = traceback.format_exc(limit=6)
    rec["seconds"] = round(time.time() - t0, 1)
    rec["output"] = buf.getvalue().strip()
    if verbose:
        # Belt and braces alongside the UTF-8 reconfigure above: DISPLAY is
        # never allowed to abort GRADING. A step that did its work and then
        # failed to echo has still done its work.
        try:
            print(f"\n-- {name} --")
            if rec["output"]:
                print(rec["output"])
            if not rec["ok"]:
                print(f"  FAILED — {rec['error']}")
            print(f"  ({rec['seconds']}s)")
        except Exception as _pe:            # noqa: BLE001 — display only
            rec["print_error"] = f"{type(_pe).__name__}: {_pe}"
    return rec


def _fill_outcomes():
    import review_outcomes
    review_outcomes.fill_outcomes()
    return "ok"


def _grade_scenarios():
    import scenario_engine
    n = scenario_engine.grade_scenarios()
    return {"newly_graded": n}


def _cache_health():
    """Standing guard against the cache silently returning wrong TYPES.

    A stringification bug in the cache cost months of degraded features and
    never raised until it reached code that touched an attribute. Checking it
    nightly means the next occurrence surfaces in one day, not one quarter.
    Report-only: it never deletes on its own."""
    import cache_doctor
    return cache_doctor.run(fix=False, verbose=True)


def _journal():
    """Keep the options journal current without the UI being open. Positions
    that expire unattended get settled at intrinsic and flagged auto_closed,
    so forgotten trades cannot sit OPEN forever and quietly poison every
    aggregate in the report."""
    import options_journal as ojn
    closed = ojn.auto_close_expired(verbose=True)
    marks = ojn.update_marks(verbose=True)
    return {"auto_closed": closed, **marks}


def _learn():
    import learning_engine
    # grade_stock=True also fills stock_log.csv outcomes; it takes its own
    # separate lock internally, so it is safe alongside ours.
    p = learning_engine.update_from_logs(grade_stock=True)
    return {"squeeze_active": bool(p.get("squeeze", {}).get("active")),
            "calibration_active": bool(p.get("calibration", {}).get("active"))}


# ─────────────────────────────────────────────
# MAIN PASS
# ─────────────────────────────────────────────

def run(verbose: bool = True) -> dict:
    started = datetime.now()
    before = counts()
    if verbose:
        print("=" * 64)
        print(f"  NIGHTLY GRADE — {started:%Y-%m-%d %H:%M}")
        print(f"  squeeze_log: {before['squeeze']['rows']} rows, "
              f"{before['squeeze']['ungraded']} ungraded")
        print(f"  scenario_log: {before['scenario']['rows']} rows, "
              f"{before['scenario']['ungraded']} ungraded")
        print("=" * 64)

    status = {"started_at": started.isoformat(timespec="seconds"),
              "before": before, "steps": [], "skipped": False}

    if not _acquire_lock():
        status.update({
            "skipped": True,
            "skip_reason": (f"{LOCK_FILE} held for >{LOCK_TIMEOUT_S}s — a scan "
                            f"is probably running. Not racing it; the next "
                            f"scheduled pass will pick this up."),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "after": before, "tiers": tier_state(), "ok": True})
        _write_status(status)
        if verbose:
            print(f"\n  SKIPPED — {status['skip_reason']}")
        return status

    try:
        status["steps"].append(_step("grade squeeze candidates "
                                     "(review_outcomes)", _fill_outcomes,
                                     verbose))
        status["steps"].append(_step("grade scenario sets "
                                     "(scenario_engine)", _grade_scenarios,
                                     verbose))
        status["steps"].append(_step("refit learned params "
                                     "(learning_engine)", _learn, verbose))
        status["steps"].append(_step("update options journal "
                                     "(options_journal)", _journal, verbose))
        status["steps"].append(_step("cache integrity (cache_doctor)",
                                     _cache_health, verbose))
    finally:
        _release_lock()

    after = counts()
    status.update({
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "seconds": round(sum(s["seconds"] for s in status["steps"]), 1),
        "after": after,
        "newly_graded": {
            "squeeze": after["squeeze"]["graded"] - before["squeeze"]["graded"],
            "scenario": after["scenario"]["graded"] - before["scenario"]["graded"]},
        "tiers": tier_state(),
        "ok": any(s["ok"] for s in status["steps"])})
    _write_status(status)

    if verbose:
        _print_summary(status)
    return status


def _write_status(status: dict):
    """Atomic write — the dashboard may read this at any moment."""
    tmp = STATUS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass
    # Human-readable one-liner history, appended forever (it is tiny).
    try:
        ng = status.get("newly_graded", {})
        t = status.get("tiers", {})
        verdict = ("SKIP" if status.get("skipped")
                   else ("OK  " if status.get("ok") else "FAIL"))
        line = (f"{status.get('finished_at', '?')}  {verdict}  "
                f"graded +{ng.get('squeeze', 0)} squeeze / "
                f"+{ng.get('scenario', 0)} scenario  |  "
                f"learning={'on' if t.get('squeeze_learning', {}).get('active') else 'off'} "
                f"calib={'on' if t.get('calibration', {}).get('active') else 'off'} "
                f"scn_tier={'on' if t.get('scenario_tier', {}).get('active') else 'off'}"
                + (f"  [{status.get('skip_reason', '')}]"
                   if status.get("skipped") else "")
                + "\n")
        with open(RUN_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _print_summary(status: dict):
    ng = status.get("newly_graded", {})
    t = status.get("tiers", {})
    print("\n" + "=" * 64)
    print(f"  DONE in {status.get('seconds', 0)}s — "
          f"graded +{ng.get('squeeze', 0)} squeeze, "
          f"+{ng.get('scenario', 0)} scenario")
    a = status.get("after", {})
    print(f"  squeeze_log: {a.get('squeeze', {}).get('graded', 0)}"
          f"/{a.get('squeeze', {}).get('rows', 0)} graded")
    print(f"  scenario_log: {a.get('scenario', {}).get('graded', 0)}"
          f"/{a.get('scenario', {}).get('rows', 0)} graded")
    print("\n  WHAT IS UNLOCKED NOW:")
    sl = t.get("squeeze_learning", {})
    cal = t.get("calibration", {})
    st = t.get("scenario_tier", {})
    print(f"    squeeze param tilts   "
          f"{'ACTIVE' if sl.get('active') else 'gated'}  (n={sl.get('n', 0)})")
    print(f"    scenario Tier 1 blend "
          f"{'ACTIVE' if st.get('active') else 'gated'}  (n={st.get('n', 0)})"
          + (f"  — {st.get('gate')}"
             if not st.get("active") and st.get("gate") else ""))
    print(f"    calibrated P (Tier 2) "
          f"{'ACTIVE' if cal.get('active') else 'gated'}  (n={cal.get('n', 0)})"
          + (f"  — {cal.get('gate')}"
             if not cal.get("active") and cal.get("gate") else ""))
    if cal.get("active") and cal.get("beats_baseline") is False:
        print("    WARNING: calibration fit does NOT beat the majority-class "
              "baseline — it is decoration, not signal. Treat Tier 2 "
              "probabilities with suspicion until more data arrives.")
    for s in status.get("steps", []):
        if not s["ok"]:
            print(f"\n  FAILED {s['step']}: {s['error']}")
    print("=" * 64)


def print_status():
    """Read-only: what happened last time, and how stale is it."""
    if not os.path.exists(STATUS_FILE):
        print("No grading run recorded yet. Run: python nightly_grade.py")
        c = counts()
        print(f"  squeeze_log: {c['squeeze']['rows']} rows, "
              f"{c['squeeze']['ungraded']} ungraded")
        print(f"  scenario_log: {c['scenario']['rows']} rows, "
              f"{c['scenario']['ungraded']} ungraded")
        return
    with open(STATUS_FILE, encoding="utf-8") as f:
        s = json.load(f)
    fin = s.get("finished_at", "?")
    age = ""
    try:
        age_h = (datetime.now()
                 - datetime.fromisoformat(fin)).total_seconds() / 3600.0
        age = (f"  ({age_h:.0f}h ago)" if age_h < 48
               else f"  ({age_h / 24:.0f} DAYS ago — is the task running?)")
    except (ValueError, TypeError):
        pass
    print(f"  last run: {fin}{age}")
    _print_summary(s)


if __name__ == "__main__":
    arg = (sys.argv[1].lower() if len(sys.argv) > 1 else "")
    if arg in ("--status", "-s", "status"):
        print_status()
    elif arg in ("--dry", "-n", "dry"):
        c = counts()
        print(json.dumps({"counts": c, "tiers": tier_state()}, indent=2))
    else:
        st = run(verbose=True)
        sys.exit(0 if st.get("ok", False) else 1)
