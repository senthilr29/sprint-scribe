"""Proactive daily digest — Sprint Scribe pushes a per-team morning risk briefing
to Google Chat on its own, with no human in the loop.

For each configured Jira project (env JIRA_PROJECTS), the digest builds a clearly
labeled section using the team's friendly display_name from the EM config. The
LLM prompt names the current roster explicitly so it cannot reference team
members who have moved off the team.

Run on a schedule (cron / Cloud Scheduler / GitHub Action):
    python digest.py            # all configured projects
    python digest.py REP        # a single project
    python digest.py "REP,VS"   # a specific subset

Or trigger it on demand for a demo via:  POST /api/digest/run   (see app.py)

The EM the digest is attributed to (for the prediction track record) comes from
the DIGEST_USER env var, falling back to the same single-config resolution the
app uses.
"""

import os
import re
import sys

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

import tools
import db

# Same fallback chain as the chat app, kept local to avoid importing app.py.
_MODELS = ["gpt-4o", "gpt-5.4-mini", "gpt-4o-mini"]

_TEAM_DIGEST_PROMPT = """You are Sprint Scribe writing the BODY of one team's section of the morning digest for Google Chat.

The team header line (*🟪 <Team> — <Sprint>*) is added automatically by the system — do NOT include it in your output. Start your output with the Status line.

Inputs:
- The team's friendly display name (use this in any prose, not the raw project key like REP/VS).
- The CURRENT roster — only mention people in this list. If a name appears in the data but is NOT in the current roster, they have moved off the team; SKIP them and do not recommend assigning work to them.
- Raw morning briefing + spillover risk data. The briefing's "Status: …" line gives done/total + days left, and the "🟢 IN PROGRESS" section lists current in-flight tickets.

Output EXACTLY this body shape (Google Chat friendly: *bold*, emoji as section markers; no markdown headers; keep each line short; max 5 lines):
*Status:* <done>/<total> done (<pct>%) · <N days left | sprint ended>
🟢 In progress: <comma-separated <KEY> (<first name>), max ~5; if many, end with "+N more">
🔴 Risks: <top 1-2 ticket keys with the single most important reason each, or "None today">
👉 Today: <one concrete recommendation — name a CURRENT team member if relevant>

Be direct and specific. No hedging. No "let me know if". Use ONLY facts present in the data — never invent ticket keys, names, or numbers. Use first names (e.g. "Alex" not "Alex Johnson")."""


# Header line emitted by tools.get_morning_briefing — used to pull the sprint name
# deterministically so the digest is GUARANTEED to label every team with its sprint.
_BRIEFING_HEADER_RE = re.compile(r"=== MORNING BRIEFING — (?P<team>.+?) · (?P<sprint>.+?) ===")


def _extract_sprint_name(briefing: str) -> str:
    """Pull the sprint name out of the briefing header. Empty string if absent
    (e.g. 'No issues found in any recent sprint.')."""
    m = _BRIEFING_HEADER_RE.search(briefing or "")
    return m.group("sprint").strip() if m else ""


def _team_header(display_name: str, sprint_name: str) -> str:
    """Deterministic *Team — Sprint* header. Sprint name is included whenever it's
    available from the briefing; otherwise only the team name is shown."""
    if sprint_name:
        return f"*🟪 {display_name} — {sprint_name}*"
    return f"*🟪 {display_name}*"


def _summarize_team(display_name: str, project: str, briefing: str, risks: str,
                    current_members: list) -> str | None:
    """LLM-polish a single team's section BODY (Status/In-progress/Risks/Today).
    The team-and-sprint header is composed deterministically by the caller, so the
    sprint label can never be dropped by the model. Returns None on failure."""
    try:
        client = OpenAI()
    except Exception:
        return None

    if current_members:
        roster_line = (f"CURRENT {display_name} team members (ONLY mention these — anyone else "
                       f"has moved off the team): {', '.join(current_members)}")
    else:
        roster_line = "(No roster on file — use only names that appear in the data below.)"

    user_msg = (
        f"Team display name (for prose only — do NOT output the header line): {display_name}\n"
        f"Project key (do NOT use in output): {project}\n"
        f"{roster_line}\n\n"
        f"=== MORNING BRIEFING ===\n{briefing}\n\n"
        f"=== SPILLOVER RISK ===\n{risks}"
    )

    for model in _MODELS:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _TEAM_DIGEST_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
            return (resp.choices[0].message.content or "").strip() or None
        except Exception as e:
            if any(k in str(e) for k in ("429", "503", "rate_limit", "capacity", "overloaded")):
                continue
            return None
    return None


def _team_display_names() -> dict:
    """{project_key: friendly_name}. Empty dict if config unavailable."""
    try:
        from user_config import get_current_user, get_team_display_names
        cfg = get_current_user(None)
        if cfg:
            return get_team_display_names(cfg)
    except Exception:
        pass
    return {}


def _roster_by_team() -> dict:
    """{project_key: [current member names]}. Empty dict if config unavailable."""
    try:
        from user_config import get_current_user, get_members_by_team
        cfg = get_current_user(None)
        if cfg:
            return get_members_by_team(cfg)
    except Exception:
        pass
    return {}


def _projects_to_cover(project_arg: str) -> list:
    """Single project string, comma-separated list, or empty -> all configured projects."""
    if project_arg:
        return [p.strip() for p in project_arg.split(",") if p.strip()]
    return list(tools.JIRA_PROJECTS) or ["REP", "VS"]


def build_digest(project: str = "") -> str:
    """Build the digest body — one section per configured project, each clearly
    labeled with the friendly team name and roster-guarded so departed devs are
    not referenced."""
    projects = _projects_to_cover(project)
    display_names = _team_display_names()
    rosters = _roster_by_team()

    sections = []
    for proj in projects:
        display = display_names.get(proj, proj)
        members = rosters.get(proj, [])
        try:
            briefing = tools.get_morning_briefing(proj)
        except Exception as e:
            briefing = f"(Could not load morning briefing: {e})"
        try:
            risks = tools.predict_spillovers(proj)
        except Exception as e:
            risks = f"(Could not load spillover risk: {e})"

        # Sprint name is extracted from the briefing and baked into the header
        # programmatically — the LLM does NOT control whether it appears.
        sprint_name = _extract_sprint_name(briefing)
        header = _team_header(display, sprint_name)

        body = _summarize_team(display, proj, briefing, risks, members)
        if body:
            sections.append(f"{header}\n{body}")
        else:
            # LLM unavailable — header is still guaranteed, raw data follows.
            sections.append(f"{header}\n{briefing}\n\n_Spillover risk:_\n{risks}".strip())

    body = "\n\n".join(sections) if sections else "(No projects configured to digest.)"

    # Credibility line — only if we've actually graded predictions before.
    try:
        acc = db.get_prediction_accuracy(tools.CURRENT_USER_ID)
        if acc.get("avg_accuracy") is not None:
            body += (f"\n\n_My spillover calls have been {acc['avg_accuracy']}% accurate "
                     f"over {acc['total']} graded sprint(s)._")
    except Exception:
        pass

    return body


def run_digest(project: str = "", send: bool = True) -> dict:
    """Build the digest and (optionally) push it to Google Chat.
    Returns a dict with the message and the send result for the API/demo path."""
    digest = build_digest(project)
    message = f"☀️ *Sprint Scribe — Daily Digest*\n\n{digest}"
    if send:
        result = tools.send_to_gchat(message)
    else:
        result = "(preview only — not sent)"
    return {"sent": send, "gchat_result": result, "message": message}


def _resolve_user() -> str:
    """Pick the EM the digest is attributed to: DIGEST_USER if set, else the same
    single-config fallback the app uses, else 'default'."""
    if os.getenv("DIGEST_USER"):
        return os.getenv("DIGEST_USER")
    try:
        from user_config import get_current_user
        uc = get_current_user(None)
        if uc:
            return uc["user_id"]
    except Exception:
        pass
    return "default"


def main():
    project = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DIGEST_PROJECT", "")
    tools.CURRENT_USER_ID = _resolve_user()
    out = run_digest(project, send=True)
    print(out["gchat_result"])
    print("---")
    print(out["message"])


if __name__ == "__main__":
    main()
