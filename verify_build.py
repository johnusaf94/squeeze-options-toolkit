"""
verify_build.py
===============
Checks that the files in your PROJECT FOLDER actually contain the fixes
we've made — catches the #1 failure mode of this project: swapping some
files but not others, leaving a stale copy that reintroduces a bug we
already fixed.

Run it from the project folder:  python verify_build.py

It looks for small "marker" strings that only exist in the fixed version
of each file. Green = fix present. Red = STALE FILE, re-swap it.
Nothing is modified — this only reads.
"""

import os

# marker string -> (file, human description). If the marker is missing,
# that file on disk is stale for that fix.
CHECKS = [
    ("options_ev.py", [
        ("iv_fallback = not (iv and iv > 0.05)",
         "Contract P/L IV floor (prevents flat/identical columns)"),
        ("def contract_matrix",
         "Contract P/L engine present"),
        ("def _S_at",
         "Asymmetric scenario paths (up ramp vs down cliff)"),
        ("exit_pnls",
         "True expected P/L (convexity-correct)"),
        ("iv_is_real",
         "Smile IV-interpolation (fixes binary P(ITM))"),
        ("def analyze_strike",
         "Strike matrix core"),
        ("kelly",
         "Kelly best-contract metric"),
    ]),
    ("squeeze_analyzer_gui.py", [
        ("_iv_for",
         "Contract P/L chain-median IV fallback"),
        ("iv=_iv_e",
         "Fallback IV actually wired into the redraw"),
        ("best_dense",
         "Smoothed sub-day path drawing"),
        ("Best contract (Kelly score)",
         "Kelly is the default sim metric"),
        ("VALUE MAP",
         "Value map (no-scenario ruler) present"),
        ("_norm_for",
         "Asinh colour scaling (fixes >100% flat blob)"),
        ("_fit_window",
         "Heatmap window shrink-wrap / no clipped top bar"),
        ("subplots_adjust",
         "3D-safe layout (fixes fullscreen whiteout)"),
    ]),
    ("learning_engine.py", [
        ("_normalize_return_units",
         "Return-units normalization (fraction vs percent bug)"),
    ]),
    ("review_outcomes.py", [
        ("grade_scenarios",
         "Scenario grading wired into the weekly run"),
    ]),
    ("scenario_engine.py", [
        ("def grade_scenarios",
         "Scenario self-grading loop present"),
    ]),
    ("squeeze_deep.py", [
        ("_squeeze_severity",
         "Level-aware ACTIVE SQUEEZE conviction branch"),
        ("force_deep_dive",
         "Severity-forced deep dives (extreme fuel skips stage-1 gate)"),
        ("momentum_score",
         "Price-momentum thrust signal (5d/20d + rel volume)"),
        ("floored: ACTIVE SQUEEZE",
         "Verdict coherence: live squeeze can't read DORMANT"),
        ("worth less than ignorance",
         "TOO_FAR catalyst no longer scores below no-catalyst"),
        ("ftd_impact_factor",
         "FTD closeout weighted by float impact (noise floor + ramp)"),
    ]),
    ("squeeze_logger.py", [
        ("ftd_impact_factor",
         "FTD impact factor logged for future learning"),
    ]),
    ("yfinance_throttle.py", [
        ("CACHE_DIR = os.path.join",
         "yfinance cache in cache/ subfolder (root litter fix)"),
        ("default=str",
         "cache save serializer fixed (entries actually persist)"),
        ("_migrate_and_clean",
         "startup sweep of orphaned cache shards"),
    ]),
    ("buffett_analyzer.py", [
        ("score_100",
         "Continuous 0-100 moat score (tie-breaking granularity)"),
        ("total_debt_raw is not None",
         "Debt-free companies keep their ROIC (truthiness fix)"),
        ("double-negative false pass",
         "FCF quality: negative/negative no longer passes"),
    ]),
    ("squeeze_searcher_gui.py", [
        ("ACTIVE SQUEEZE" + chr(34) + " in conv",
         "Actionable filter recognizes live squeezes (GRPN grayout)"),
        ("force_deep_dive",
         "Severity override wired into stage-2 gate"),
        ("auto_refresh_if_stale",
         "Universe auto-refresh at scan start"),
        ("key=lambda x: (x.get('final_score', 0)",
         "Table ranked by FINAL (multipliers no longer buried)"),
    ]),
    ("squeeze_universe.py", [
        ("TIER_4_RESEARCH_ADDS",
         "Research universe additions (+62 names)"),
    ]),
]

GREEN = "\033[92m"
RED = "\033[91m"
DIM = "\033[90m"
BOLD = "\033[1m"
RST = "\033[0m"


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    total = ok = 0
    stale_files = set()
    missing_files = []

    for fname, markers in CHECKS:
        path = os.path.join(here, fname)
        print(f"\n{BOLD}{fname}{RST}")
        if not os.path.exists(path):
            print(f"  {RED}FILE NOT FOUND{RST} — expected in project folder")
            missing_files.append(fname)
            continue
        try:
            src = open(path, encoding="utf-8").read()
        except OSError as e:
            print(f"  {RED}could not read: {e}{RST}")
            continue
        for marker, desc in markers:
            total += 1
            if marker in src:
                ok += 1
                print(f"  {GREEN}\u2713{RST} {desc}")
            else:
                stale_files.add(fname)
                print(f"  {RED}\u2717 MISSING{RST} {desc}")
                print(f"      {DIM}(marker not found: {marker[:50]}){RST}")

    print(f"\n{BOLD}{'='*56}{RST}")
    print(f"{ok}/{total} checks passed.")
    if missing_files:
        print(f"{RED}Missing files:{RST} {', '.join(missing_files)}")
    if stale_files:
        print(f"{RED}{BOLD}STALE FILES — re-swap these from the latest "
              f"outputs:{RST}")
        for f in sorted(stale_files):
            print(f"  {RED}\u2192 {f}{RST}")
    if not stale_files and not missing_files:
        print(f"{GREEN}{BOLD}All files current. "
              f"Build is consistent.{RST}")


if __name__ == "__main__":
    main()
