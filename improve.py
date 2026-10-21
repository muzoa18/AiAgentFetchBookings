"""
improve.py
----------
Self-improvement system for the MECA Agent System.

THREE MODES:

  PR mode (GitHub Actions, .github/workflows/weekly-improve.yml, Mondays):
      python improve.py --apply-all
      → Applies every proposal that does not touch a protected path, py_compiles
        each changed .py (reverting any that fail), writes
        improve_reports/latest.md, and the workflow opens a Pull Request.

  Interactive (manual review):
      python improve.py
      → Shows all proposals with diffs, you approve each one.

  Automatic (local, by hand):
      python improve.py --auto
      → Applies only proposals marked auto_apply. Logs to logs/improve_auto.log.

In every mode a proposal for a PROTECTED_PATHS file is never written (is_protected).
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
IMPROVE_MODEL = "claude-opus-5-5"

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

# All source files to include in review (the real current modules; see SKILL.md).
SOURCE_FILES = [
    "agent.py",
    "approve.py",
    "notifier.py",
    "orders_state.py",
    "parts_order.py",
    "parts_order_helpers.py",
    "parts_matcher.py",
    "parts_portal.py",
    "parts_portal_parse.py",
    "portal_scraper.py",
    "rules.py",
    "parts_rules.yaml",
    "config_loader.py",
    "state.py",
    "run_logger.py",
]

# Log files that can exist (logs/agent.log only inside a CI run or a local run).
LOG_FILES = [
    ("logs/agent.log",        "BOOKING AGENT LOG"),
    ("logs/improve_auto.log", "AUTO-IMPROVE LOG"),
]

# Paths a proposal may never write, enforced in code (is_protected) before any file
# is touched, whatever the model says. An entry ending in "/" protects a directory.
# The safety logic named in SYSTEM_PROMPT lives in whole files below, so those files
# are protected entirely: a proposal for them is listed as "skipped (protected)" in
# the report for a human to apply by hand.
PROTECTED_PATHS = (
    ".github/",                 # workflows, schedules, secrets wiring
    ".claude/",                 # agent context
    "config/",                  # config, state files (seen_ids.txt, orders.json)
    "parts_rules.yaml",         # brand rules, 10,000 SEK ceiling, approve/auto mode
    "rules.py",                 # validation of the rules file
    "approve.py",               # approval authorisation (owner, private chat, message id)
    "parts_matcher.py",         # ProMeister / fluid / placement / brand safety
    "parts_order.py",           # ceiling and cart checks, attempted-before-checkout
    "parts_order_helpers.py",   # cart and total checks
    "orders_state.py",          # order-state validation (never order twice)
    "improve.py",               # this file, including this list
)


def is_protected(file: str) -> bool:
    """True if a proposal may not write `file`. Fails closed: anything that is not
    a plain relative path inside the repo (absolute, '..', empty) counts as protected.
    Compared case-insensitively, with '\\' as a separator, before and after resolving
    symlinks."""
    raw = str(file or "").strip().replace("\\", "/")
    # ":" covers drive letters and NTFS streams; a trailing dot/space names the
    # same file on Windows ("parts_rules.yaml." == "parts_rules.yaml").
    if not raw or raw.startswith("/") or ":" in raw:
        return True
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or ".." in parts or any(part[-1] in ". " for part in parts):
        return True
    root = Path.cwd().resolve()
    try:
        resolved = (root / "/".join(parts)).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return True
    return any(_matches_protected(rel) for rel in ("/".join(parts), resolved))


def _matches_protected(rel: str) -> bool:
    rel = rel.casefold()
    for entry in PROTECTED_PATHS:
        entry = entry.casefold()
        if entry.endswith("/"):
            if rel == entry.rstrip("/") or rel.startswith(entry):
                return True
        elif rel == entry:
            return True
    return False


SYSTEM_PROMPT = """
You are an expert Python developer reviewing the MECA Agent System.
This is a real production system used daily at MD Bilreparationer AB,
a Swedish MECA car workshop in Eskilstuna. It runs unattended on GitHub
Actions and places real parts orders, so it must fail closed.
SKILL.md (included below) is the full description; this is the summary.

THE SYSTEM:

1. agent.py — daily booking run (bookings.yml, 15:00 UTC Mon-Sat)
   - Logs into meca.promeisterportal.com (portal_scraper.py, Playwright),
     opens /Booking/Index, Nya tab, reads every listed booking's detail page.
   - The PORTAL is the truth for a Bokning: listed on the Nya tab = not handled.
     config/seen_ids.txt only means "the owner was notified": one Telegram
     message per booking (notifier.py), never repeated unless the outcome changes.
   - Bokning without spare parts (fluids only count as no parts): Hanterad ->
     Bekräfta right away, retried every run until it succeeds.
   - Bokning with spare parts: never marked Hanterad by agent.py. It stages a
     pro.meca.se order (parts_order.py, parts_portal.py, parts_matcher.py) and
     sends a Telegram message with Approve / Cancel buttons.
   - Offert / Förfrågan: no Hanterad button; notified once (local storage).

2. approve.py — daily approval poller (approve.yml, 06:00 UTC every day)
   - Reads the owner's Telegram taps. Approve -> re-checks the live cart against
     the stored plan and the ceiling, then checks out. Cancel -> clears the cart.
     Approvals expire after 48 h.
   - Klar: every resolution message (ordered, blocked, failed, cancelled, expired)
     has a Klar button; the tap marks the booking Hanterad in the MECA portal.
   - Only the owner, in the private chat, on the booking's own message is accepted.

3. Parts rules — parts_rules.yaml (owner-edited, loaded by rules.py)
   - Prefer Bosch, then another brand; never ProMeister; never fluids or oils.
   - Only parts under "Kopplade reservdelar" (linked to the vehicle) are candidates.
   - Axle/side placement must match; axle-specific parts need an axle in the line.
   - Ceiling 10,000 SEK per booking order; mode: approve (ask owner) or auto.

4. State — config/orders.json (orders_state.py): one status per booking so a
   booking is never ordered twice; "attempted" is written before checkout.

PROTECTED AREAS — proposals must NOT touch:
- anything under .github/ (workflows, schedules, secrets) or config/,
- parts_rules.yaml and rules.py,
- the approval authorisation in approve.py (owner / private chat / message id),
- the ceiling, ProMeister, fluid and placement safety logic in parts_matcher.py,
  parts_order.py and parts_order_helpers.py,
- the validation in orders_state.py,
- improve.py itself.
This is enforced in code: a proposal for approve.py, parts_matcher.py,
parts_order.py, parts_order_helpers.py, orders_state.py, rules.py, improve.py,
parts_rules.yaml or any path under .github/ or config/ is NOT applied; it is
listed in the report as "skipped (protected)" for a human to review. Only
propose changes there for a real, clearly explained bug.

WELCOME: selector fixes and parsing robustness (portal_scraper.py,
parts_portal.py, parts_portal_parse.py), logging, error handling, clearer
owner messages that stay Swedish and never carry raw exception text, and tests.

KNOWN ISSUES TO WATCH FOR:
- Cookie popups on both portals must be dismissed before any clicks work.
- pro.meca.se login has TWO email fields: use #ShopLoginForm_Login.
- Specification scraping stops at Händelselogg so event-log rows are not parsed.
- The pro.meca.se cart is per vehicle; the order confirmation page is unverified.

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

    if is_protected(file):
        print(f"  SKIPPED: {file} is a protected path — not written.")
        return False

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
        print("Run the test suite (python -m pytest) before running anything live.")


# ── Automatic mode ────────────────────────────────────────────────────────────

def run_auto(api_key: str):
    """
    Silent mode — applies only auto_apply=true proposals.
    Run by hand; protected paths are never written (apply_proposal).
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
        _write_report([], [], [], [], [])
        return

    # Snapshot originals so we can revert files that end up broken.
    snapshots = {f: Path(f).read_text(encoding="utf-8")
                 for f in SOURCE_FILES if Path(f).exists()}

    applied, failed, skipped, protected = [], [], [], []
    for p in proposals:
        title = p.get("title", "?")
        if is_protected(p.get("file", "")):
            # Checked before anything is written (apply_proposal checks again).
            protected.append(p)
            print(f"  [PROTECTED] {title} ({p.get('file')}) — skipped (protected)")
            continue
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
    _write_report(proposals, applied, skipped + failed, reverted, protected)
    print(f"\nApplied {len(applied)} change(s). Reverted {len(reverted)} broken file(s).")


def _write_report(proposals, applied, not_applied, reverted, protected=()):
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
        if protected:
            lines.append("## 🔒 Skipped (protected) — NOT applied, review by hand")
            for p in protected:
                lines.append(f"- **{p.get('title')}** (`{p.get('file')}`) — "
                             f"skipped (protected) — {p.get('reason')}")
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
