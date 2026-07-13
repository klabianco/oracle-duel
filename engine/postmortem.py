"""Round close: grade the prompt version, auto-revert if evolution hurt,
otherwise let the agent propose exactly ONE change to its prompt.
"""

import json
import subprocess
from datetime import datetime, timezone

from engine.adapters import extract_json
from engine.config import ROOT
from engine.prompt_guard import diff_hunks, lint_prompt, word_count

POSTMORTEM_SYSTEM = """You are reviewing your own graded forecasting record to improve your
strategy prompt. First write a structured post-mortem (what went wrong, why, what pattern),
THEN propose exactly ONE change: one addition, one deletion, or one rewrite of a single
rule/paragraph. The prompt may only contain forecasting philosophy, research process,
category preferences and calibration heuristics — never position sizes, loss limits or
bet frequency (a linter will reject those). Hard cap: 500 words.

Respond with JSON only:
{"postmortem": "...", "change_description": "...", "new_prompt": "<full new prompt text>"}"""

POSTMORTEM_SCHEMA = {
    "type": "object",
    "properties": {
        "postmortem": {"type": "string"},
        "change_description": {"type": "string"},
        "new_prompt": {"type": "string"},
    },
    "required": ["postmortem", "change_description", "new_prompt"],
    "additionalProperties": False,
}


def _git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def _commit_prompt(runner, version: int, message: str):
    try:
        rel = runner.prompt_path.relative_to(ROOT)
        subprocess.run(["git", "add", str(rel)], cwd=ROOT, capture_output=True)
        subprocess.run(["git", "commit", "-m", message], cwd=ROOT, capture_output=True)
        subprocess.run(["git", "tag", "-f", f"agent-{runner.name}-v{version}"],
                       cwd=ROOT, capture_output=True)
    except Exception:
        pass  # versioning in git is best-effort; the DB is the source of truth


def _note_round_result(agent: str, version: int, brier: float, n: int, reverted: bool):
    """Write the round's Brier back into git notes on the tagged commit (best effort)."""
    try:
        commit = _git("rev-list", "-n", "1", f"agent-{agent}-v{version}")
        if commit:
            msg = f"round_brier={brier:.4f} n={n} reverted={reverted}"
            subprocess.run(["git", "notes", "add", "-f", "-m", msg, commit],
                           cwd=ROOT, capture_output=True)
    except Exception:
        pass


def round_complete(telemetry, agent: str, cfg: dict, now: datetime = None) -> bool:
    """A round = min_days elapsed AND min_resolved_forecasts graded, whichever is later."""
    cur = telemetry.current_version(agent)
    if not cur:
        return False
    now = now or datetime.now(timezone.utc)
    deployed = datetime.fromisoformat(cur["deployed_at"])
    # Calendar days, not elapsed seconds: rounds can only close during the
    # daily cycle, so a deploy stamped later in the day than the cycle's
    # postmortem step would otherwise always miss the check by minutes and
    # slip a full day (claude missed round 1 by 44 minutes this way).
    days = (now.date() - deployed.date()).days
    resolved = telemetry.resolved_count_for_version(agent, cur["version"])
    r = cfg["round"]
    return days >= r["min_days"] and resolved >= r["min_resolved_forecasts"]


def _build_report(telemetry, agent: str, version: int) -> str:
    brier, n = telemetry.version_brier(agent, version)
    lines = [f"PROMPT VERSION: v{version}",
             f"OVERALL: brier={brier:.4f} over {n} resolved forecasts",
             "", "BRIER BY CATEGORY:"]
    for r in telemetry.category_error_log(agent, version):
        lines.append(f"- {r['category']}: n={r['n']} brier={r['brier']:.3f} "
                     f"avg_forecast={r['avg_prob']:.2f} hit_rate={r['hit_rate']:.2f}")
    lines += ["", "CALIBRATION CURVE (forecast bucket -> actual hit rate):"]
    for b in telemetry.calibration_bins(agent, version):
        lines.append(f"- {b['bin']}: n={b['n']} mean_forecast={b['mean_prob']:.2f} "
                     f"actual={b['hit_rate']:.2f}")
    lines += ["", "WORST MISSES (with your original reasoning):"]
    for m in telemetry.worst_misses(agent, version):
        lines.append(f"- [{m['category']}] {m['market_title']} | forecast={m['prob']:.2f} "
                     f"outcome={m['outcome']} brier={m['brier']:.2f}\n"
                     f"  reasoning: {(m['confidence_notes'] or '')[:300]}")
    return "\n".join(lines)


def run_postmortem(runner, telemetry, cfg: dict, alerts=None, now: datetime = None) -> dict:
    """Close the current round for one agent. Returns a summary dict."""
    agent = runner.name
    cur = telemetry.current_version(agent)
    version = cur["version"]
    brier, n = telemetry.version_brier(agent, version)
    telemetry.close_version_round(agent, version, brier, n, reverted=False)
    _note_round_result(agent, version, brier, n, False)

    # ---- auto-revert: evolution requires selection ------------------------
    if version > 1:
        prev = telemetry.conn.execute(
            "SELECT * FROM prompt_versions WHERE agent=? AND version<? "
            "AND reverted=0 AND round_brier IS NOT NULL "
            "ORDER BY version DESC LIMIT 1",
            (agent, version),
        ).fetchone()
        # Revert only when this round actually CHANGED the prompt (text
        # differs from the last selected version). An unchanged prompt that
        # scores worse is round-to-round noise, not a failed mutation —
        # reverting past it would discard the best selected text.
        if (prev and prev["round_brier"] is not None
                and prev["text"] != cur["text"] and brier > prev["round_brier"]):
            new_version = version + 1
            runner.deploy_prompt(prev["text"], new_version)
            telemetry.close_version_round(agent, version, brier, n, reverted=True)
            telemetry.record_postmortem(agent, version,
                                        f"auto-revert: v{version} brier {brier:.4f} worse than "
                                        f"v{prev['version']} {prev['round_brier']:.4f}",
                                        "revert to previous prompt", accepted=True)
            _commit_prompt(runner, new_version,
                           f"{agent} v{new_version}: auto-revert of failed mutation v{version}")
            if alerts:
                alerts.send(
                    f"{agent} — round over. Its last prompt change made it WORSE "
                    f"(score {brier:.4f} vs {prev['round_brier']:.4f} before), so the "
                    f"change was thrown away and the old prompt is back. "
                    f"That's the safety net working, not a failure.",
                    title=f"what {agent} learned")
            return {"agent": agent, "action": "reverted", "version": new_version,
                    "brier": brier}

    # ---- agent proposes one change ---------------------------------------
    report = _build_report(telemetry, agent, version)
    old_prompt = runner.prompt_path.read_text()
    user = (f"{report}\n\nCURRENT PROMPT:\n---\n{old_prompt}\n---\n\n"
            "Write your post-mortem, then propose exactly one change.")

    word_cap = cfg["round"]["prompt_word_cap"]
    accepted, out, reason = False, None, ""
    for attempt in range(2):
        try:
            text = runner.adapter.complete(POSTMORTEM_SYSTEM, user,
                                           json_schema=POSTMORTEM_SCHEMA, max_tokens=8000)
            out = extract_json(text)
        except Exception as e:
            reason = f"postmortem generation failed: {e}"
            continue
        new_prompt = out["new_prompt"].strip() + "\n"
        errs = lint_prompt(new_prompt, word_cap)
        hunks = diff_hunks(old_prompt, new_prompt)
        if hunks > 1:
            errs.append(f"{hunks} separate changes detected; exactly one allowed")
        if hunks == 0:
            errs.append("no change detected")
        if not errs:
            accepted = True
            break
        reason = "; ".join(errs)
        user += f"\n\nYour previous proposal was rejected: {reason}. Try again."

    runner._flush_spend(datetime.now(timezone.utc).date().isoformat(), "postmortem")

    if not accepted and reason.startswith("postmortem generation failed"):
        # The model call itself failed (outage, timeout) — that is not a
        # rejected proposal. Leave the version in place so tomorrow's cycle
        # retries the postmortem instead of burning the round's mutation.
        telemetry.incident("postmortem_deferred", agent, reason)
        if alerts:
            alerts.send(f"[{agent}] postmortem could not be generated ({reason}); "
                        "retrying next cycle")
        return {"agent": agent, "action": "deferred", "version": version, "brier": brier}

    if accepted:
        new_version = version + 1
        runner.deploy_prompt(out["new_prompt"].strip() + "\n", new_version)
        telemetry.record_postmortem(agent, version, out["postmortem"],
                                    out["change_description"], accepted=True)
        _commit_prompt(runner, new_version,
                       f"{agent} v{new_version}: {out['change_description'][:100]}")
        if alerts:
            new_wc = word_count(out["new_prompt"])
            old_wc = word_count(old_prompt)
            alerts.send(
                f"{agent} — round over (score {brier:.4f} across {n} answered guesses).\n\n"
                f"WHAT IT SAYS WENT WRONG:\n{out['postmortem'][:900]}\n\n"
                f"THE ONE CHANGE IT MADE:\n{out['change_description'][:300]}\n\n"
                f"Its rulebook is now {new_wc} words (was {old_wc}, limit 500). "
                f"If this change makes it worse next round, it gets undone automatically.",
                title=f"what {agent} learned")
        return {"agent": agent, "action": "mutated", "version": new_version, "brier": brier}

    new_version = version + 1
    runner.deploy_prompt(old_prompt, new_version)
    _commit_prompt(runner, new_version, f"{agent} v{new_version}: prompt kept unchanged")
    telemetry.record_postmortem(agent, version, (out or {}).get("postmortem", ""),
                                f"REJECTED: {reason}", accepted=False)
    telemetry.incident("mutation_rejected", agent, reason)
    if alerts:
        alerts.send(f"[{agent}] prompt mutation rejected ({reason}); keeping v{version}")
    return {"agent": agent, "action": "kept", "version": new_version, "brier": brier}
