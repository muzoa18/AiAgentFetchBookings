"""
improve.py
----------
Self-improvement system for the MECA Agent System.

TWO MODES:

  Interactive (manual review):
      python improve.py
      → Shows all proposals with diffs, you approve each one.

  Automatic (runs after agent.py):
      python improve.py --auto
      → Applies only safe fixes silently.
        Saves a log of what was changed.

Task Scheduler setup:
  17:00 → agent.py
  17:30 → improve.py --auto
  Every 30 min → email_agent.py
"""

import sys
import os
import json
import difflib
import argparse
import py_compile
import subprocess
from pathlib import Path
from datetime import datetime

# Improve agent reviews and edits real source — use the most capable model.
IMPROVE_MODEL = "claude-opus-4-8"

try:
    import anthropic
except ImportError:
    print("ERROR: pip install anthropic")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("ERROR: pip install pyyaml")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH  = Path("config/config.yaml")
SKILL_FILE   = Path("SKILL.md")
PROPOSAL_DIR = Path("logs/proposals")

# All source files to include in review
SOURCE_FILES = [
    "agent.py",
    "portal_scraper.py",
    "parts_agent.py",
    "email_agent.py",
    "sms_sender.py",
    "state.py",
    "config_loader.py",
    "run_logger.py",
]

# All log files to include
LOG_FILES = [
    ("logs/agent.log",        "BOOKING AGENT LOG"),
    ("logs/email_agent.log",  "EMAIL AGENT LOG"),
    ("logs/parts_agent.log",  "PARTS AGENT LOG"),
    ("logs/improve_auto.log", "AUTO-IMPROVE LOG"),
]

SYSTEM_PROMPT = """
You are an expert Python developer reviewing the MECA Agent System.
This is a real production system used daily at MD Bilreparationer AB,
a Swedish MECA car workshop in Eskilstuna.

THE SYSTEM HAS THREE AGENTS:

1. agent.py — Booking agent (runs daily at 17:00)
   - Logs into meca.promeisterportal.com
   - Opens /Booking/Index → clicks Nya tab
   - For each new booking: opens detail page, extracts all info
   - Sends SMS via 46elks to workshop owner(s)
   - Clicks Hanterad → Bekräfta if booking_type == Bokning
   - If booking_type == Förfrågan → calls parts_agent.py

2. email_agent.py — Email customer service (runs every 30 min)
   - Reads unread Gmail threads (full conversation, not just latest)
   - Claude decides: ask_info | send_quote | general_reply | ignore
   - If send_quote and reg_nr + work exist → calls parts_agent.py
   - Creates Gmail DRAFT for workshop owner to review before sending
   - SMS only when a quote PDF was successfully created
   - Non-customer/supplier emails → marked back as UNREAD
   - Known suppliers excluded: riddermarkbil.se, dssparts.se etc.
   - Service packages: liten service = oljefilter + pollenfilter kolfilter + Mobil 1 motorolja
   - Parts brand rules: always Bosch filters/brakes, always Mobil 1 oil, never ProMeister

3. parts_agent.py — Quote builder (triggered by agent.py or email_agent.py)
   - Logs into pro.meca.se using #ShopLoginForm_Login / #ShopLoginForm_Password
   - Loads vehicle by reg number via URL parameter
   - Searches each part in catalog search bar
   - Claude AI picks best match (prefer Bosch, prefer Mobil 1, reject ProMeister)
   - Adds parts to cart
   - Adds labor via Fritext dialog (Typ=Arbetstid, fills hours and rate)
   - Writes AI-generated Offertnotering in Swedish
   - Clicks "Skriv ut prisförslag (PDF)" → downloads PDF to logs/quotes/
   - PDF is attached to Gmail draft addressed to mdbilreparationer@gmail.com

KNOWN ISSUES TO WATCH FOR:
- pro.meca.se login: has TWO email fields — must use #ShopLoginForm_Login, NOT placeholder*mail
- Cookie popups on both portals must be dismissed before any clicks work
- Specification table scraping stops at Händelselogg to avoid parsing event log
- Claude sometimes returns ask_info with only mätarställning missing — code override forces send_quote
- mätarställning is NEVER required — code strips it from missing_info

YOUR JOB:
- Identify REAL problems visible in logs or code
- Propose concrete, specific fixes
- Do NOT invent hypothetical improvements
- Focus on: login failures, timeouts, wrong selectors, empty results, missing error handling
- Mark auto_apply: true ONLY for clearly safe low-risk fixes (typos, log lines, minor selectors)
- Mark auto_apply: false for structural changes or behavior changes

Return your findings as structured output: an object with a "proposals"
array. Each proposal has:
  priority    (int)    — 1 = highest
  title       (string) — short title
  reason      (string) — specific problem, reference exact log line or code
  file        (string) — filename, e.g. "agent.py"
  change_type (string) — "replace_function" | "replace_file" | "add_function" | "config_change"
  find        (string) — exact string to find (empty "" if not applicable)
  replacement (string) — new code (or the full file for replace_file)
  impact      (string) — "low" | "medium" | "high"
  auto_apply  (bool)   — true ONLY for clearly safe, low-risk fixes

For 'find', copy the EXACT current source text verbatim so it can be located.
Return an empty proposals array if the code is healthy.
"""

# JSON Schema for structured output — guarantees a parseable response.
PROPOSALS_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "priority":    {"type": "integer"},
                    "title":       {"type": "string"},
                    "reason":      {"type": "string"},
                    "file":        {"type": "string"},
                    "change_type": {
                        "type": "string",
                        "enum": ["replace_function", "replace_file",
                                 "add_function", "config_change"],
                    },
                    "find":        {"type": "string"},
                    "replacement": {"type": "string"},
                    "impact":      {"type": "string", "enum": ["low", "medium", "high"]},
                    "auto_apply":  {"type": "boolean"},
                },
                "required": ["priority", "title", "reason", "file", "change_type",
                             "find", "replacement", "impact", "auto_apply"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["proposals"],
    "additionalProperties": False,
}


def load_config() -> dict:
    # In CI there is no config.yaml — the API key comes from the environment
    # (ANTHROPIC_API_KEY). Locally, config.yaml is used if present.
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def read_skill() -> str:
    if SKILL_FILE.exists():
        return f"\n\n=== SKILL.md ===\n{SKILL_FILE.read_text(encoding='utf-8')}"
    return "\n\n=== SKILL.md ===\n(not found — create SKILL.md in project root)"


def read_sources() -> str:
    parts = [read_skill()]
    for fname in SOURCE_FILES:
        p = Path(fname)
        if p.exists():
            text = p.read_text(encoding="utf-8")
            parts.append(f"\n\n=== {fname} ({len(text.splitlines())} lines) ===\n{text}")
        else:
            parts.append(f"\n\n=== {fname} ===\n(FILE NOT FOUND — may need to be created)")
    return "".join(parts)


def read_logs(max_chars_per_log: int = 3000) -> str:
    """Read recent logs from all agents plus structured run history."""
    parts = []

    for log_path_str, label in LOG_FILES:
        log_path = Path(log_path_str)
        if log_path.exists():
            text  = log_path.read_text(encoding="utf-8", errors="replace")
            chunk = text[-max_chars_per_log:] if len(text) > max_chars_per_log else text
            parts.append(f"=== {label} ===\n{chunk}")
        else:
            parts.append(f"=== {label} ===\n(log file not found yet)")

    # Structured run history
    try:
        from run_logger import read_recent_runs
        runs = read_recent_runs(50)
        if runs:
            parts.append("=== STRUCTURED RUN HISTORY (last 50 runs) ===")
            for run in runs:
                parts.append(json.dumps(run, ensure_ascii=False))
    except Exception as e:
        parts.append(f"=== STRUCTURED RUN HISTORY ===\n(unavailable: {e})")

    return "\n\n".join(parts) if parts else "(no logs found)"


def ask_claude(api_key: str, sources: str, logs: str) -> list[dict]:
    client = anthropic.Anthropic(api_key=api_key)
    print(f"Sending codebase + logs to Claude ({IMPROVE_MODEL}) for analysis ...")
    print(f"  Source files: {len(SOURCE_FILES)}")
    print(f"  Log files:    {len(LOG_FILES)}")

    response = client.messages.create(
        model=IMPROVE_MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": PROPOSALS_SCHEMA}},
        messages=[{
            "role": "user",
            "content": (
                f"RECENT LOGS FROM ALL AGENTS:\n{logs}"
                f"\n\nSOURCE CODE + SKILL.md:\n{sources}"
            )
        }],
    )

    if response.stop_reason == "refusal":
        print("Claude declined to respond (refusal). No proposals.")
        return []

    # output_config.format guarantees the first text block is valid JSON.
    content = next((b.text for b in response.content if b.type == "text"), "{}")
    data = json.loads(content)
    return data.get("proposals", [])


def show_diff(original: str, replacement: str, filename: str):
    diff = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        replacement.splitlines(keepends=True),
        fromfile=f"{filename} (current)",
        tofile=f"{filename} (proposed)",
        n=3,
    ))
    if diff:
        print("".join(diff[:80]))
        if len(diff) > 80:
            print(f"  ... ({len(diff) - 80} more lines)")
    else:
        print("  (no textual diff)")


def apply_proposal(proposal: dict) -> bool:
    file    = proposal.get("file", "")
    ctype   = proposal.get("change_type", "")
    find    = proposal.get("find", "")
    replace = proposal.get("replacement", "")
    path    = Path(file)

    if not path.exists():
        print(f"  ERROR: {file} not found.")
        return False

    current = path.read_text(encoding="utf-8")

    if ctype == "replace_file":
        path.write_text(replace, encoding="utf-8")
        print(f"  Replaced entire {file}.")
        return True

    elif ctype in ("replace_function", "add_function"):
        if find and find in current:
            path.write_text(current.replace(find, replace, 1), encoding="utf-8")
            print(f"  Updated {file}.")
            return True
        elif ctype == "add_function":
            path.write_text(current.rstrip() + "\n\n" + replace + "\n", encoding="utf-8")
            print(f"  Appended to {file}.")
            return True
        else:
            print(f"  ERROR: Target code not found in {file}.")
            print(f"  Looking for: {find[:100]}...")
            return False

    elif ctype == "config_change":
        print(f"  Config change — apply manually to config/config.yaml:")
        print(f"  {replace}")
        return True

    return False


def save_proposal_log(proposals: list, applied: list[int], mode: str):
    PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = PROPOSAL_DIR / f"proposal_{mode}_{ts}.json"
    path.write_text(json.dumps({
        "timestamp": ts,
        "mode":      mode,
        "applied_indices": applied,
        "proposals": proposals,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Proposal log saved: {path}")


# ── Interactive mode ──────────────────────────────────────────────────────────

def run_interactive(api_key: str):
    print("\n" + "=" * 60)
    print("  MECA Agent — Self-Improvement (Interactive)")
    print("=" * 60)

    proposals = ask_claude(api_key, read_sources(), read_logs())

    if not proposals:
        print("\nNo improvements proposed — code looks good!")
        return

    print(f"\nClaude proposed {len(proposals)} improvement(s):\n")

    for i, p in enumerate(proposals, 1):
        impact = p.get("impact", "?").upper()
        auto   = "AUTO-SAFE" if p.get("auto_apply") else "needs review"
        print(f"{'─' * 55}")
        print(f"[{i}] {p.get('title')}  [{impact}] [{auto}]")
        print(f"    File  : {p.get('file')}")
        print(f"    Reason: {p.get('reason')}")
        print()

        find  = p.get("find", "")
        fpath = Path(p.get("file", ""))

        if fpath.exists() and find:
            current = fpath.read_text(encoding="utf-8")
            if find in current:
                show_diff(find, p.get("replacement", ""), p.get("file", ""))
            else:
                print("    (target string not found in file — may already be fixed)")
        elif p.get("replacement"):
            preview = p["replacement"][:300]
            print(f"    Preview:\n{preview}{'...' if len(p['replacement']) > 300 else ''}")
        print()

    print("=" * 60)
    print("Which improvements to apply?")
    print("  Numbers: 1,3   All: all   Auto-safe only: auto   Skip: none")
    print("=" * 60)

    raw = input("\nYour choice: ").strip().lower()

    if raw in ("none", "n", ""):
        print("No changes applied.")
        save_proposal_log(proposals, [], "interactive")
        return

    if raw in ("all", "a"):
        chosen = list(range(len(proposals)))
    elif raw == "auto":
        chosen = [i for i, p in enumerate(proposals) if p.get("auto_apply")]
        print(f"Applying {len(chosen)} auto-safe proposal(s).")
    else:
        try:
            chosen = [int(x.strip()) - 1 for x in raw.split(",") if x.strip()]
            chosen = [i for i in chosen if 0 <= i < len(proposals)]
        except ValueError:
            print("Invalid input — no changes applied.")
            return

    applied = []
    for i in chosen:
        p = proposals[i]
        print(f"\n[{i+1}] Applying: {p.get('title')} ...")
        if apply_proposal(p):
            applied.append(i)

    save_proposal_log(proposals, applied, "interactive")
    print(f"\nApplied {len(applied)}/{len(chosen)} improvements.")
    if applied:
        print("Run `python agent.py` or `python email_agent.py` to test.")


# ── Automatic mode ────────────────────────────────────────────────────────────

def run_auto(api_key: str):
    """
    Silent mode — applies only auto_apply=true proposals.
    Run via Task Scheduler after agent.py.
    """
    log_path = Path("logs/improve_auto.log")
    Path("logs").mkdir(exist_ok=True)

    def log(msg: str):
        ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} {msg}"
        print(line)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    log("=" * 50)
    log("MECA Agent — Auto-Improvement starting")
    log(f"Reviewing: {', '.join(SOURCE_FILES)}")
    log("=" * 50)

    try:
        proposals = ask_claude(api_key, read_sources(), read_logs())
    except Exception as e:
        log(f"ERROR: Claude API call failed: {e}")
        return

    if not proposals:
        log("No improvements proposed.")
        return

    auto_proposals = [p for p in proposals if p.get("auto_apply")]
    log(f"Total proposals: {len(proposals)}  |  Auto-safe: {len(auto_proposals)}")

    for p in proposals:
        flag = "AUTO" if p.get("auto_apply") else "MANUAL"
        log(f"  [{flag}] {p.get('title')} ({p.get('file')}) [{p.get('impact','?').upper()}]")

    if not auto_proposals:
        log("No auto-safe proposals — run `python improve.py` for manual review.")
        save_proposal_log(proposals, [], "auto")
        return

    applied = []
    for i, p in enumerate(auto_proposals):
        log(f"Applying [{i+1}/{len(auto_proposals)}]: {p.get('title')} ...")
        ok = apply_proposal(p)
        if ok:
            applied.append(i)
            log(f"  OK")
        else:
            log(f"  FAILED")

    save_proposal_log(proposals, applied, "auto")
    log(f"Done. Applied {len(applied)}/{len(auto_proposals)} auto-safe improvements.")
    if len(proposals) > len(auto_proposals):
        log(f"Run `python improve.py` to review {len(proposals) - len(auto_proposals)} manual proposal(s).")


# ── PR mode (for GitHub Actions weekly-improve workflow) ───────────────────────

REPORT_DIR = Path("improve_reports")


def run_apply_all(api_key: str):
    """
    Apply EVERY proposal to the working tree, syntax-check each changed Python
    file, and revert any file that fails to compile. Writes a markdown report.

    A human reviews the resulting diff in a Pull Request — nothing reaches the
    live portal unreviewed.
    """
    print("\n" + "=" * 60)
    print("  MECA Agent — Self-Improvement (PR mode: apply-all)")
    print("=" * 60)

    proposals = ask_claude(api_key, read_sources(), read_logs())
    if not proposals:
        print("No improvements proposed — code looks healthy.")
        _write_report([], [], [], [])
        return

    # Snapshot originals so we can revert files that end up broken.
    snapshots = {f: Path(f).read_text(encoding="utf-8")
                 for f in SOURCE_FILES if Path(f).exists()}

    applied, failed, skipped = [], [], []
    for p in proposals:
        title = p.get("title", "?")
        if p.get("change_type") == "config_change":
            # Config lives in GitHub Secrets, not in the repo — can't auto-apply.
            skipped.append(p)
            print(f"  [SKIP] {title} (config_change — apply manually in Secrets)")
            continue
        if apply_proposal(p):
            applied.append(p)
            print(f"  [APPLY] {title}")
        else:
            failed.append(p)
            print(f"  [FAIL]  {title} (target not found)")

    # Syntax-check every changed Python file; revert the broken ones.
    reverted = []
    for fname, original in snapshots.items():
        path = Path(fname)
        if not fname.endswith(".py") or not path.exists():
            continue
        if path.read_text(encoding="utf-8") == original:
            continue  # unchanged
        try:
            py_compile.compile(fname, doraise=True)
        except py_compile.PyCompileError as exc:
            path.write_text(original, encoding="utf-8")
            reverted.append((fname, str(exc)))
            applied = [p for p in applied if p.get("file") != fname]
            print(f"  [REVERT] {fname} — does not compile, reverted.")

    save_proposal_log(proposals, list(range(len(applied))), "apply_all")
    _write_report(proposals, applied, skipped + failed, reverted)
    print(f"\nApplied {len(applied)} change(s). Reverted {len(reverted)} broken file(s).")


def _write_report(proposals, applied, not_applied, reverted):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Weekly improve report — {ts}", ""]

    if not proposals:
        lines.append("No improvements proposed this week. Code looks healthy. ✅")
    else:
        lines.append(f"Claude proposed **{len(proposals)}** change(s).")
        lines.append("")
        if applied:
            lines.append("## ✅ Applied (in this PR)")
            for p in applied:
                lines.append(f"- **{p.get('title')}** (`{p.get('file')}`, "
                             f"impact: {p.get('impact','?')}) — {p.get('reason')}")
            lines.append("")
        if not_applied:
            lines.append("## ⚠️ Proposed but NOT applied (review manually)")
            for p in not_applied:
                lines.append(f"- **{p.get('title')}** (`{p.get('file')}`, "
                             f"{p.get('change_type')}) — {p.get('reason')}")
            lines.append("")
        if reverted:
            lines.append("## ⛔ Reverted (failed to compile)")
            for fname, err in reverted:
                lines.append(f"- `{fname}` — reverted, syntax error")
            lines.append("")

    report_path = REPORT_DIR / "latest.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report written: {report_path}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MECA Agent Self-Improvement System")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Automatic mode: applies only safe fixes silently"
    )
    parser.add_argument(
        "--apply-all",
        action="store_true",
        help="PR mode: apply all proposals, syntax-check, revert broken files, write report"
    )
    args = parser.parse_args()

    config  = load_config()
    api_key = config.get("anthropic", {}).get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: No Anthropic API key (set ANTHROPIC_API_KEY).")
        sys.exit(1)

    if args.apply_all:
        run_apply_all(api_key)
    elif args.auto:
        run_auto(api_key)
    else:
        run_interactive(api_key)
