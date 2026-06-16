import os
import io
import re
import json
import time
import functools
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from atlassian import Jira
from github import Github
from dotenv import load_dotenv

import db

load_dotenv()

# Set per-request by app.py so memory writes are attributed to the right EM.
CURRENT_USER_ID = "default"

# --- Integration health (quiet probes for the header status dots) -----------
# Probes each external integration so the UI can show a small green/red dot
# in the header. Cached briefly so we don't hammer the APIs.
_HEALTH_CACHE = {}            # {"jira": (timestamp, "ok|down|unknown"), ...}
_HEALTH_TTL = 60              # seconds


def _check_jira_health() -> str:
    try:
        _get_jira().myself()
        return "ok"
    except Exception:
        return "down"


def _check_github_health() -> str:
    try:
        # Lightweight authenticated probe — surfaces 401 on bad/expired tokens.
        _ = _get_github().get_user().login
        return "ok"
    except Exception:
        return "down"


def _check_confluence_health() -> str:
    # Confluence integration is a pre-fetched cache file in this app; healthy
    # means the cache exists and parses (no live API to probe).
    try:
        cache_path = os.path.join(os.path.dirname(__file__) or ".", "confluence_cache.json")
        with open(cache_path) as f:
            data = json.load(f)
        return "ok" if (data.get("tech_demos") or data.get("blogs")) else "down"
    except Exception:
        return "down"


def _check_gchat_health() -> str:
    # We don't probe the webhook (it'd post a real message). 'ok' means configured.
    return "ok" if GCHAT_WEBHOOK_URL else "down"


_HEALTH_CHECKS = {
    "jira": _check_jira_health,
    "github": _check_github_health,
    "confluence": _check_confluence_health,
    "gchat": _check_gchat_health,
}


def get_integration_health() -> dict:
    """Return {integration: 'ok'|'down'} for all known integrations, cached briefly."""
    now = time.time()
    out = {}
    for name, fn in _HEALTH_CHECKS.items():
        cached = _HEALTH_CACHE.get(name)
        if cached and (now - cached[0]) < _HEALTH_TTL:
            out[name] = cached[1]
        else:
            status = fn()
            _HEALTH_CACHE[name] = (now, status)
            out[name] = status
    return out


def is_github_healthy() -> bool:
    """Convenience used by individual tools to decide whether to skip live GitHub
    calls and degrade gracefully (no zero-charts, quiet caption instead)."""
    return get_integration_health().get("github") == "ok"


# --- Resilience layer: stale-while-error cache for read-only tools ----------
# Keeps the live demo from hanging or surfacing raw errors. Fresh results are
# cached; if a later live call fails (exception or "Error..." string), we serve
# the last good result, clearly labeled, instead of breaking the conversation.
_TOOL_CACHE = {}                       # key -> (timestamp, result)
_CACHE_TTL = int(os.getenv("TOOL_CACHE_TTL", "180"))  # seconds


def _looks_like_failure(result) -> bool:
    """A read tool signals failure by returning a string starting with 'Error'."""
    return isinstance(result, str) and result.strip().lower().startswith("error")


def _resilient(func):
    """Wrap a read-only tool: cache fresh results, and fall back to the last good
    result (labeled stale) when a live call fails. Never raises to the caller."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        key = (func.__name__, args, tuple(sorted(kwargs.items())))
        now = time.time()
        cached = _TOOL_CACHE.get(key)
        # Serve a fresh cache hit to avoid slow repeat calls during a demo.
        if cached and (now - cached[0]) < _CACHE_TTL:
            return cached[1]
        try:
            result = func(*args, **kwargs)
        except Exception as e:
            result = f"Error in {func.__name__}: {e}"
        if _looks_like_failure(result):
            if cached:  # stale-while-error: better a labeled stale answer than a crash
                age_min = int((now - cached[0]) / 60)
                age = f"{age_min} min ago" if age_min else "moments ago"
                return f"{cached[1]}\n\n_⚠️ Live data unavailable right now — showing the last good result from {age}._"
            return result  # nothing cached; surface the error as before
        _TOOL_CACHE[key] = (now, result)
        return result
    return wrapper

JIRA_URL = os.getenv("JIRA_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_CREATE_PROJECT = os.getenv("JIRA_CREATE_PROJECT", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_ORG = os.getenv("GITHUB_ORG", "vendasta")
GITHUB_REPOS = [r.strip() for r in os.getenv("GITHUB_REPOS", "").split(",") if r.strip()]
JIRA_PROJECTS = [p.strip() for p in os.getenv("JIRA_PROJECTS", "").split(",") if p.strip()]
GCHAT_WEBHOOK_URL = os.getenv("GCHAT_WEBHOOK_URL", "")


def _gh_scope() -> str:
    """GitHub search scope qualifier: narrow to the configured repos when set
    (GITHUB_REPOS, from env or config), otherwise search the whole org."""
    if GITHUB_REPOS:
        return " ".join(f"repo:{GITHUB_ORG}/{r}" for r in GITHUB_REPOS)
    return f"org:{GITHUB_ORG}"

# Minimum approvals before a PR is "ready to merge". Vendasta convention is
# two approvals; override per deployment via PR_MIN_APPROVALS env if your team
# differs.
_MIN_APPROVALS = int(os.getenv("PR_MIN_APPROVALS", "2"))

# EMs — exclude from workload insights and reviewer suggestions.
# Populated from configs/<name>.yaml (exclude_from_workload); empty by default.
_EXCLUDE_FROM_WORKLOAD = set()


def _working_days_between(start_dt, end_dt) -> int:
    """Count weekday (Mon-Fri) days between two datetimes, inclusive of start, exclusive of end."""
    if start_dt > end_dt:
        return 0
    count = 0
    current = start_dt.date() if hasattr(start_dt, 'date') else start_dt
    end = end_dt.date() if hasattr(end_dt, 'date') else end_dt
    while current < end:
        if current.weekday() < 5:  # Mon=0 ... Fri=4
            count += 1
        current += timedelta(days=1)
    return count

# Team member mapping: Jira display name → GitHub username.
# Illustrative placeholders ONLY — your real roster comes from configs/<your>.yaml
# (see configs/example.yaml). This dict is just a last-resort fallback shape.
_DEFAULT_TEAM_MEMBERS = {
    "Developer One": "github-id-1",
    "Developer Two": "github-id-2",
}
# Override via env if needed: TEAM_MEMBERS={"Name": "gh-user"}
_env_members = os.getenv("TEAM_MEMBERS", "")
TEAM_MEMBERS = json.loads(_env_members) if _env_members else _DEFAULT_TEAM_MEMBERS

# Per-project membership (who counts as that team's dev). Hardcoded fallback only —
# overridden below by the active EM config so the roster lives in ONE place.
_MEMBERS_BY_PROJECT = {
    "PROJ": {"Developer One", "Developer Two"},
}

# Project key -> friendly display name (e.g. "Team Apollo", "Team Atlas"). Overridden
# from config below. Used in morning briefing + anywhere the team is named.
_PROJECT_DISPLAY_NAMES = {}

# Single source of truth: pull the roster from configs/<user>.yaml. This makes
# get_team_availability (PR allocation) and get_person_github_activity reflect the
# same team the system prompt sees. Precedence: env TEAM_MEMBERS > config > hardcoded.
try:
    import user_config as _user_config
    _cfg = _user_config.get_current_user(None)
    if _cfg and _cfg.get("teams"):
        _cfg_members, _by_project = {}, {}
        for _team, _data in _cfg["teams"].items():
            _members = (_data.get("members") or {})
            _by_project[_team] = set(_members.keys())
            _cfg_members.update(_members)
        if _by_project:
            _MEMBERS_BY_PROJECT = _by_project
        if _cfg_members and not _env_members:
            TEAM_MEMBERS = _cfg_members
        for _team_key, _data in _cfg["teams"].items():
            if _data.get("display_name"):
                _PROJECT_DISPLAY_NAMES[_team_key] = _data["display_name"]
        if _cfg.get("exclude_from_workload"):
            _EXCLUDE_FROM_WORKLOAD = set(_cfg["exclude_from_workload"])
        # Projects / repos / default board from config (env still wins if set).
        if _cfg.get("jira_projects") and not os.getenv("JIRA_PROJECTS"):
            JIRA_PROJECTS = [str(p).strip() for p in _cfg["jira_projects"] if str(p).strip()]
        if _cfg.get("github_repos") and not os.getenv("GITHUB_REPOS"):
            GITHUB_REPOS = [str(r).strip() for r in _cfg["github_repos"] if str(r).strip()]
        if _cfg.get("create_project") and not os.getenv("JIRA_CREATE_PROJECT"):
            JIRA_CREATE_PROJECT = str(_cfg["create_project"]).strip()
except Exception:
    pass  # config unavailable — keep hardcoded fallbacks

_jira = None
_gh = None


def _get_jira() -> Jira:
    global _jira
    if _jira is None:
        _jira = Jira(url=JIRA_URL, username=JIRA_EMAIL, password=JIRA_API_TOKEN, cloud=True, timeout=15)
    return _jira


def _get_github() -> Github:
    global _gh
    if _gh is None:
        _gh = Github(GITHUB_TOKEN, timeout=15)
    return _gh


# ---------------------------------------------------------------------------
# Jira read tools
# ---------------------------------------------------------------------------

def get_jira_issue(issue_key: str) -> str:
    """Get full details, status, assignee, and recent comments for a Jira ticket.

    Args:
        issue_key: Jira issue key e.g. PROJ-1234
    """
    try:
        jira = _get_jira()
        issue = jira.issue(issue_key)
        fields = issue["fields"]

        summary = fields.get("summary", "")
        status = fields.get("status", {}).get("name", "Unknown")
        assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
        description = (fields.get("description") or "")[:500]
        updated = fields.get("updated", "")[:10]
        priority = (fields.get("priority") or {}).get("name", "")
        sprint_info = ""
        for f in fields.get("customfield_10020") or []:
            if isinstance(f, dict) and f.get("state") == "active":
                sprint_info = f.get("name", "")
                break

        comments_raw = jira.issue_get_comments(issue_key)
        comments = comments_raw.get("comments", []) if isinstance(comments_raw, dict) else []
        recent = comments[-5:] if len(comments) > 5 else comments
        comment_text = "\n".join(
            f"  [{c['author']['displayName']} on {c['created'][:10]}]: {c['body'][:300]}"
            for c in recent
        )

        return (
            f"Ticket: {issue_key}\n"
            f"URL: {JIRA_URL}/browse/{issue_key}\n"
            f"Summary: {summary}\n"
            f"Status: {status}\n"
            f"Assignee: {assignee}\n"
            f"Priority: {priority}\n"
            f"Sprint: {sprint_info or 'unknown'}\n"
            f"Last updated: {updated}\n"
            f"Description: {description}\n"
            f"Recent comments ({len(recent)} shown):\n{comment_text or '  (none)'}"
        )
    except Exception as e:
        return f"Error fetching {issue_key}: {e}"


def search_sprint(jql: str) -> str:
    """Search Jira issues using JQL. Use for sprint health, assignee workload, blocked items.

    Args:
        jql: Valid JQL string e.g. 'project in (PROJ1, PROJ2) AND sprint in openSprints()'
    """
    try:
        jira = _get_jira()
        result = jira.jql(jql, limit=30)
        issues = result.get("issues", []) if isinstance(result, dict) else result
        if not issues:
            return "No issues found."

        lines = []
        for issue in issues:
            fields = issue["fields"]
            key = issue["key"]
            summary = fields.get("summary", "")
            status = fields.get("status", {}).get("name", "")
            assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
            updated = fields.get("updated", "")[:10]
            points = fields.get("story_points") or fields.get("customfield_10013") or ""
            points_str = f" [{points}pts]" if points else ""
            lines.append(f"{key}{points_str} [{status}] {assignee}: {summary} (updated {updated})")

        return "\n".join(lines)
    except Exception as e:
        return f"Error searching Jira: {e}"


# ---------------------------------------------------------------------------
# Jira write tools
# ---------------------------------------------------------------------------

def create_jira_ticket(summary: str, description: str, issue_type: str = "Bug", project: str = "") -> str:
    """Create a new Jira ticket with proper formatting template.

    Args:
        summary: One-line ticket title
        description: Detailed description — will be formatted into the appropriate template
        issue_type: Bug, Story, Task, or Improvement. Default is Bug.
        project: Jira project key e.g. PROJ1. Uses default if not specified.
    """
    try:
        jira = _get_jira()
        proj = project.upper() if project else JIRA_CREATE_PROJECT

        # Format description using templates
        if issue_type == "Bug":
            formatted_desc = (
                f"h3. Description\n{description}\n\n"
                f"h3. Steps to Reproduce\n# \n# \n# \n\n"
                f"h3. Expected Result\n\n\n"
                f"h3. Actual Result\n\n\n"
                f"h3. Environment\n* Browser: \n* URL: \n\n"
                f"h3. Screenshot\n_See attachment if available_"
            )
        elif issue_type == "Story":
            formatted_desc = (
                f"h3. User Story\nAs a [user], I want to [action] so that [benefit].\n\n"
                f"h3. Description\n{description}\n\n"
                f"h3. Acceptance Criteria\n* \n* \n\n"
                f"h3. Technical Notes\n"
            )
        else:
            formatted_desc = description

        result = jira.create_issue(fields={
            "project": {"key": proj},
            "summary": summary,
            "description": formatted_desc,
            "issuetype": {"name": issue_type},
        })
        key = result.get("key", "unknown")

        # Auto-attach pending screenshot if available
        try:
            from app import session
            pending = session.get("pending_image")
            if pending:
                import base64 as b64
                image_bytes = b64.b64decode(pending["b64"])
                jira.add_attachment(
                    issue_key=key,
                    filename=pending.get("filename", "screenshot.png"),
                    content=io.BytesIO(image_bytes)
                )
                session.pop("pending_image", None)
                return f"Created {key}: {summary}\nScreenshot attached.\nURL: {JIRA_URL}/browse/{key}"
        except Exception:
            pass

        return f"Created {key}: {summary}\nURL: {JIRA_URL}/browse/{key}"
    except Exception as e:
        return f"Error creating ticket: {e}"


def update_jira_status(issue_key: str, new_status: str) -> str:
    """Transition a Jira ticket to a new status.

    Args:
        issue_key: Jira issue key e.g. PROJ-1234
        new_status: Target status name e.g. 'In Review', 'In Progress', 'Done', 'To Do'
    """
    try:
        jira = _get_jira()
        transitions = jira.get_issue_transitions(issue_key)
        match = None
        for t in transitions:
            if new_status.lower() in t["name"].lower():
                match = t
                break
        if not match:
            available = ", ".join(t["name"] for t in transitions)
            return (
                f"Could not transition {issue_key} to '{new_status}'. "
                f"Available transitions: {available}. "
                f"Add a comment manually or try one of the available statuses."
            )
        jira.issue_transition(issue_key, match["id"])
        return f"Updated {issue_key} → {match['name']}"
    except Exception as e:
        return f"Error updating status for {issue_key}: {e}"


def add_jira_comment(issue_key: str, comment: str) -> str:
    """Add a comment to a Jira ticket.

    Args:
        issue_key: Jira issue key e.g. PROJ-1234
        comment: Comment text to add
    """
    try:
        jira = _get_jira()
        jira.issue_add_comment(issue_key, comment)
        return f"Comment added to {issue_key}."
    except Exception as e:
        return f"Error adding comment to {issue_key}: {e}"


def attach_to_jira(issue_key: str, filename: str = "", image_base64: str = "") -> str:
    """Attach the uploaded screenshot to a Jira ticket. If a screenshot was uploaded
    in the current conversation, it will be attached automatically.

    Args:
        issue_key: Jira issue key e.g. PROJ-567
        filename: Filename for the attachment. Optional — uses uploaded filename if available.
        image_base64: Base64-encoded image. Optional — uses uploaded image if available.
    """
    try:
        import base64

        # Try to get image from session (uploaded via chat)
        # This is set by app.py when an image is uploaded
        if not image_base64:
            from app import session
            pending = session.get("pending_image")
            if pending:
                image_base64 = pending["b64"]
                filename = filename or pending.get("filename", "screenshot.png")
            else:
                return f"No image to attach to {issue_key}. Upload a screenshot first."

        jira = _get_jira()
        image_bytes = base64.b64decode(image_base64)
        jira.add_attachment(issue_key=issue_key, filename=filename or "screenshot.png", content=io.BytesIO(image_bytes))
        return f"Screenshot '{filename}' attached to {issue_key}."
    except Exception as e:
        return f"Error attaching file to {issue_key}: {e}"


# ---------------------------------------------------------------------------
# Sprint Analytics tools
# ---------------------------------------------------------------------------

_RESOLVED_STATUSES = {
    "done", "closed", "resolved", "completed",
    "won't fix", "wont fix", "won't do",
    "closed-won't fix", "closed-wont fix",
    "duplicate", "ga closed", "will not do",
}


def _is_resolved(fields: dict) -> bool:
    """Check if a Jira issue is resolved — by resolution field or status name."""
    if fields.get("resolution") is not None:
        return True
    status = (fields.get("status", {}).get("name", "") or "").lower()
    return status in _RESOLVED_STATUSES


def _resolve_projects(project: str = "") -> list:
    """Resolve project parameter to a list of project keys.
    If empty or 'all', returns all configured projects."""
    if not project or project.lower() == "all":
        return JIRA_PROJECTS
    return [p.strip().upper() for p in project.split(",") if p.strip()]


def _team_display_label(proj_list: list) -> str:
    """Friendly team name(s) for a list of project keys — uses display_name from
    the active EM config (e.g. 'Team Apollo' for PROJ). Joins multiple with '+'."""
    if not proj_list:
        return ""
    names = [_PROJECT_DISPLAY_NAMES.get(p, p) for p in proj_list]
    return " + ".join(names)


def _get_boards_for_projects(jira, projects: list) -> list:
    """Find scrum boards for given projects. Returns list of {id, name, project}."""
    boards_out = []
    seen = set()
    for proj in projects:
        try:
            boards = jira.get_all_agile_boards(project_key=proj)
            values = boards.get("values", []) if isinstance(boards, dict) else boards
            for b in values:
                if b["id"] not in seen and b.get("type") == "scrum":
                    boards_out.append({"id": b["id"], "name": b.get("name", ""), "project": proj})
                    seen.add(b["id"])
        except Exception:
            continue
    return boards_out


def _get_all_sprints_paginated(jira, board_id: int, state: str = "closed") -> list:
    """Get ALL sprints from a board, handling Jira's 50-per-page pagination."""
    import requests
    all_values = []
    start_at = 0
    while True:
        resp = requests.get(
            f"{JIRA_URL}/rest/agile/1.0/board/{board_id}/sprint",
            params={"state": state, "startAt": start_at, "maxResults": 50},
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json"},
            timeout=(5, 15),
        )
        if resp.status_code != 200:
            break
        data = resp.json()
        values = data.get("values", [])
        all_values.extend(values)
        if data.get("isLast", True) or not values:
            break
        start_at += len(values)
    return all_values


def _get_sprints_from_all_boards(jira, state="closed", projects: list = None) -> list:
    """Get sprints from boards, filtered by projects. Deduplicated by sprint ID.
    Excludes closed sprints older than 6 months."""
    boards = _get_boards_for_projects(jira, projects or JIRA_PROJECTS)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
    all_sprints = {}
    for b in boards:
        try:
            values = _get_all_sprints_paginated(jira, b["id"], state=state)
            for s in values:
                if s["id"] not in all_sprints:
                    if state == "closed":
                        end_date = s.get("endDate", "") or ""
                        if end_date and end_date < cutoff:
                            continue
                    s["_board_project"] = b["project"]
                    s["_board_name"] = b["name"]
                    all_sprints[s["id"]] = s
        except Exception:
            continue
    return list(all_sprints.values())


def _get_current_sprint_jql(jira, projects: list, extra_filters: str = "") -> tuple:
    """Try openSprints() first, fall back to most recent closed sprint for given projects.
    Returns (jql_result_issues, sprint_label) tuple."""
    projects_str = ", ".join(projects)

    # Try active sprint
    jql = f"project in ({projects_str}) AND sprint in openSprints(){extra_filters}"
    result = jira.jql(jql, limit=80)
    issues = result.get("issues", []) if isinstance(result, dict) else result
    if issues:
        # Get the active sprint name from board API
        sprint_name = "current sprint"
        try:
            boards = _get_boards_for_projects(jira, projects)
            for b in boards:
                active = _get_all_sprints_paginated(jira, b["id"], state="active")
                if active:
                    sprint_name = active[0].get("name", sprint_name)
                    break
        except Exception:
            pass
        return issues, sprint_name

    # Fall back to most recently closed sprint for THESE projects
    sprints = _get_sprints_from_all_boards(jira, state="closed", projects=projects)
    if sprints:
        latest = sorted(sprints, key=lambda s: s.get("endDate", ""), reverse=True)[0]
        sid = latest["id"]
        label = latest.get("name", f"Sprint {sid}")
        jql = f"project in ({projects_str}) AND sprint = {sid}{extra_filters}"
        result = jira.jql(jql, limit=80)
        issues = result.get("issues", []) if isinstance(result, dict) else result
        return issues, f"last sprint ({label})"

    return [], "no sprint found"


def get_sprint_history(num_sprints: int = 5, project: str = "") -> str:
    """Get historical sprint data for the last N closed sprints.
    Returns completion rates, spillover counts, and velocity per sprint.
    Use this for predicting spillovers, tracking velocity trends, and sprint planning.

    Args:
        num_sprints: Number of past sprints to analyze. Default 5.
        project: Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all configured projects.
    """
    try:
        jira = _get_jira()
        proj_list = _resolve_projects(project)
        projects = ", ".join(proj_list)

        # Get closed sprints for the specified projects
        sprints = _get_sprints_from_all_boards(jira, state="closed", projects=proj_list)
        sprints = sorted(sprints, key=lambda s: s.get("endDate", ""), reverse=True)[:num_sprints]

        if not sprints:
            return "No closed sprints found."

        results = []
        total_completed = 0
        total_committed = 0
        total_spillovers = 0

        for sprint in sprints:
            sid = sprint["id"]
            name = sprint.get("name", f"Sprint {sid}")
            start = sprint.get("startDate", "")[:10]
            end = sprint.get("endDate", "")[:10]

            # Get sprint report data via JQL
            all_issues = jira.jql(
                f"project in ({projects}) AND sprint = {sid}",
                limit=100
            )
            issues = all_issues.get("issues", []) if isinstance(all_issues, dict) else all_issues

            done_count = 0
            not_done_count = 0
            done_points = 0
            total_points = 0
            spillover_tickets = []
            assignee_stats = {}

            for issue in issues:
                fields = issue["fields"]
                status = fields.get("status", {}).get("name", "").lower()
                assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
                points = fields.get("customfield_10013") or 0
                try:
                    points = float(points)
                except (ValueError, TypeError):
                    points = 0

                total_points += points

                if assignee not in assignee_stats:
                    assignee_stats[assignee] = {"done": 0, "not_done": 0, "points_done": 0, "points_total": 0}
                assignee_stats[assignee]["points_total"] += points

                if _is_resolved(issue["fields"]):
                    done_count += 1
                    done_points += points
                    assignee_stats[assignee]["done"] += 1
                    assignee_stats[assignee]["points_done"] += points
                else:
                    not_done_count += 1
                    spillover_tickets.append(f"{issue['key']} ({assignee}): {fields.get('summary', '')[:60]}")
                    assignee_stats[assignee]["not_done"] += 1

            committed = done_count + not_done_count
            completion_rate = round((done_count / committed * 100) if committed else 0, 1)
            velocity = done_points

            total_completed += done_count
            total_committed += committed
            total_spillovers += not_done_count

            # Per-assignee summary
            assignee_lines = []
            for person, stats in sorted(assignee_stats.items(), key=lambda x: x[1]["not_done"], reverse=True):
                if stats["not_done"] > 0:
                    assignee_lines.append(
                        f"    {person}: {stats['done']} done, {stats['not_done']} spilled ({stats['points_done']}/{stats['points_total']} pts)"
                    )

            result = (
                f"--- {name} ({start} to {end}) ---\n"
                f"  Committed: {committed} tickets ({total_points} pts)\n"
                f"  Completed: {done_count} tickets ({done_points} pts)\n"
                f"  Spillovers: {not_done_count} tickets\n"
                f"  Completion rate: {completion_rate}%\n"
                f"  Velocity: {velocity} pts\n"
            )
            if assignee_lines:
                result += "  Spillover by person:\n" + "\n".join(assignee_lines) + "\n"
            if spillover_tickets[:5]:
                result += "  Spilled tickets: " + "; ".join(spillover_tickets[:5]) + "\n"

            results.append(result)

        # Summary across sprints
        avg_completion = round((total_completed / total_committed * 100) if total_committed else 0, 1)
        avg_spillover = round(total_spillovers / len(sprints), 1)

        summary = (
            f"\n=== SUMMARY (last {len(sprints)} sprints) ===\n"
            f"Average completion rate: {avg_completion}%\n"
            f"Average spillovers per sprint: {avg_spillover} tickets\n"
            f"Total committed: {total_committed} | Total completed: {total_completed}\n"
        )

        return "\n".join(results) + summary

    except Exception as e:
        return f"Error fetching sprint history: {e}"


def predict_spillovers(project: str = "") -> str:
    """Analyze the current sprint and predict which tickets are at risk of spilling over.
    Scores each ticket based on: days without update, no PR linked, high story points,
    status still in To Do/Backlog, blocked status, and proximity to sprint end.
    If no active sprint, analyzes the most recently closed sprint.

    Args:
        project: Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all configured projects.
    """
    try:
        jira = _get_jira()
        proj_list = _resolve_projects(project)
        projects = ", ".join(proj_list)
        now = datetime.now(timezone.utc)
        is_active_sprint = False

        # Try active sprint first
        result = jira.jql(
            f"project in ({projects}) AND sprint in openSprints() AND status != Done AND status != Closed ORDER BY updated ASC",
            limit=50
        )
        issues = result.get("issues", []) if isinstance(result, dict) else result

        sprint_label = "current sprint"
        if issues:
            is_active_sprint = True
        else:
            # No active sprint — fall back to most recently closed sprint for these projects
            sprints = _get_sprints_from_all_boards(jira, state="closed", projects=proj_list)
            if sprints:
                latest = sorted(sprints, key=lambda s: s.get("endDate", ""), reverse=True)[0]
                sid = latest["id"]
                sprint_label = latest.get("name", f"Sprint {sid}")
                end_date = latest.get("endDate", "")[:10]
                result = jira.jql(
                    f"project in ({projects}) AND sprint = {sid} AND status != Done AND status != Closed ORDER BY updated ASC",
                    limit=50
                )
                issues = result.get("issues", []) if isinstance(result, dict) else result
                if not issues:
                    return f"No active sprint for {projects}. Last completed sprint ({sprint_label}, ended {end_date}) — all tickets are Done."
            else:
                return f"No active or recent sprints found for {projects}."

        # Get sprint end date and days left
        sprint_end = None
        active_sprint_id = None
        active_sprint_name = None
        if is_active_sprint:
            for issue in issues:
                for s in issue["fields"].get("customfield_10020") or []:
                    if isinstance(s, dict) and s.get("state") == "active" and s.get("endDate"):
                        sprint_end = datetime.fromisoformat(s["endDate"].replace("Z", "+00:00"))
                        active_sprint_id = s.get("id")
                        active_sprint_name = s.get("name")
                        break
                if sprint_end:
                    break
            if active_sprint_name:
                sprint_label = active_sprint_name

        if is_active_sprint and sprint_end:
            days_left = _working_days_between(now, sprint_end)
            time_context = f"Sprint ends in {days_left} working day(s)."
        else:
            days_left = 0
            time_context = f"NOTE: No active sprint. Analyzing leftover tickets from last closed sprint: {sprint_label}. These are carry-overs that were not completed."

        risk_tickets = []

        for issue in issues:
            fields = issue["fields"]
            key = issue["key"]
            summary = fields.get("summary", "")[:70]
            status = fields.get("status", {}).get("name", "")
            assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
            updated = fields.get("updated", "")
            points = fields.get("customfield_10013") or 0
            try:
                points = float(points)
            except (ValueError, TypeError):
                points = 0

            # Calculate risk score (0-100)
            risk_score = 0
            risk_reasons = []

            # Factor 1: Days since last update
            if updated:
                last_update = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                days_stale = (now - last_update).days
                if days_stale >= 7:
                    risk_score += 30
                    risk_reasons.append(f"stale {days_stale}d")
                elif days_stale >= 4:
                    risk_score += 20
                    risk_reasons.append(f"quiet {days_stale}d")
                elif days_stale >= 2:
                    risk_score += 10

            # Factor 2: Status
            status_lower = status.lower()
            if status_lower in ("to do", "backlog", "open", "new"):
                risk_score += 30
                risk_reasons.append("not started")
            elif status_lower in ("blocked", "on hold"):
                risk_score += 35
                risk_reasons.append("blocked")
            elif status_lower in ("in progress",):
                risk_score += 10

            # Factor 3: High story points with few days left
            if points >= 5 and days_left <= 3:
                risk_score += 20
                risk_reasons.append(f"{points}pts, {days_left}d left")
            elif points >= 3 and days_left <= 2:
                risk_score += 15
                risk_reasons.append(f"{points}pts, {days_left}d left")

            # Factor 4: Sprint almost over
            if days_left <= 1:
                risk_score += 15
            elif days_left <= 2:
                risk_score += 10

            risk_level = "HIGH" if risk_score >= 50 else "MEDIUM" if risk_score >= 30 else "LOW"

            risk_tickets.append({
                "key": key,
                "summary": summary,
                "assignee": assignee,
                "status": status,
                "points": points,
                "risk_score": risk_score,
                "risk_level": risk_level,
                "reasons": risk_reasons,
            })

        # Sort by risk score descending
        risk_tickets.sort(key=lambda t: t["risk_score"], reverse=True)

        high_risk = [t for t in risk_tickets if t["risk_level"] == "HIGH"]
        medium_risk = [t for t in risk_tickets if t["risk_level"] == "MEDIUM"]

        # --- Learning loop: persist this prediction so it can be graded later ---
        # Only record forward-looking predictions on a live sprint (not carry-over analysis).
        if is_active_sprint and active_sprint_id and high_risk:
            _record_spillover_prediction(
                sprint_id=active_sprint_id,
                sprint_name=active_sprint_name or sprint_label,
                project=projects,
                predicted_keys=[t["key"] for t in high_risk],
            )

        lines = []
        # Lead with track record if we've graded past predictions — the "it learns" signal.
        track = db.get_prediction_accuracy(CURRENT_USER_ID)
        if track.get("avg_accuracy") is not None:
            lines.append(
                f"📈 My spillover calls have been {track['avg_accuracy']}% accurate "
                f"over {track['total']} graded prediction(s).\n"
            )

        lines.append(f"{time_context} Analyzing {len(issues)} open tickets...\n")

        if high_risk:
            lines.append(f"🔴 HIGH RISK ({len(high_risk)} tickets likely to spill):")
            for t in high_risk:
                reasons = ", ".join(t["reasons"]) if t["reasons"] else "multiple factors"
                lines.append(f"  {t['key']} [{t['status']}] {t['assignee']}: {t['summary']} — {reasons} (score: {t['risk_score']})")

        if medium_risk:
            lines.append(f"\n🟡 MEDIUM RISK ({len(medium_risk)} tickets need attention):")
            for t in medium_risk:
                reasons = ", ".join(t["reasons"]) if t["reasons"] else "minor factors"
                lines.append(f"  {t['key']} [{t['status']}] {t['assignee']}: {t['summary']} — {reasons} (score: {t['risk_score']})")

        low_count = len(risk_tickets) - len(high_risk) - len(medium_risk)
        if low_count:
            lines.append(f"\n🟢 LOW RISK: {low_count} ticket(s) on track")

        lines.append(f"\nPredicted spillovers: {len(high_risk)} tickets")
        lines.append(f"Needs attention: {len(medium_risk)} tickets")

        return "\n".join(lines)

    except Exception as e:
        return f"Error predicting spillovers: {e}"


# ---------------------------------------------------------------------------
# Learning loop — record predictions, grade them against reality
# ---------------------------------------------------------------------------

def _record_spillover_prediction(sprint_id, sprint_name: str, project: str,
                                 predicted_keys: list) -> None:
    """Persist a spillover prediction so it can be graded once the sprint closes.
    Idempotent per sprint — re-running predict_spillovers won't create duplicates."""
    try:
        # Skip if we already have an unresolved prediction for this sprint.
        for p in db.get_unresolved_predictions(CURRENT_USER_ID):
            if p.get("prediction_type") != "spillover":
                continue
            try:
                content = json.loads(p.get("prediction_content") or "{}")
            except (ValueError, TypeError):
                continue
            if str(content.get("sprint_id")) == str(sprint_id):
                return  # already recorded for this sprint
        db.save_prediction(
            CURRENT_USER_ID,
            prediction_type="spillover",
            prediction_content=json.dumps({
                "sprint_id": sprint_id,
                "sprint_name": sprint_name,
                "project": project,
                "predicted_keys": predicted_keys,
                "predicted_at": datetime.now(timezone.utc).isoformat(),
            }),
            context=f"{sprint_name} ({project})",
        )
    except Exception:
        pass  # memory is best-effort; never break the prediction itself


def score_predictions(project: str = "") -> str:
    """Grade past spillover predictions against what actually happened.
    For every prediction whose sprint has since closed, compares the tickets Sprint Scribe
    flagged as HIGH risk against the tickets that actually didn't finish, and records the
    accuracy. Run this after a sprint closes to update the track record.

    Args:
        project: Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all configured projects.
    """
    try:
        jira = _get_jira()
        proj_list = _resolve_projects(project)
        pending = [p for p in db.get_unresolved_predictions(CURRENT_USER_ID)
                   if p.get("prediction_type") == "spillover"]
        if not pending:
            return "No pending spillover predictions to grade. Run predict_spillovers on an active sprint first."

        # Which of this EM's tracked sprints are now closed?
        closed = _get_sprints_from_all_boards(jira, state="closed", projects=proj_list)
        closed_by_id = {str(s["id"]): s for s in closed}

        graded, still_open = [], 0
        for p in pending:
            try:
                content = json.loads(p.get("prediction_content") or "{}")
            except (ValueError, TypeError):
                continue
            sid = str(content.get("sprint_id"))
            if sid not in closed_by_id:
                still_open += 1
                continue  # sprint not closed yet — can't grade

            sname = content.get("sprint_name", f"Sprint {sid}")
            predicted = set(content.get("predicted_keys", []))

            # Actual spillovers = tickets in that sprint that did not resolve.
            projs = content.get("project") or ", ".join(proj_list)
            result = jira.jql(f"project in ({projs}) AND sprint = {sid}", limit=100)
            sprint_issues = result.get("issues", []) if isinstance(result, dict) else result
            actual = {i["key"] for i in sprint_issues if not _is_resolved(i["fields"])}
            committed = len(sprint_issues)
            completed = committed - len(actual)

            # Precision/recall/F1 on the set of tickets we said would spill.
            hits = predicted & actual
            precision = len(hits) / len(predicted) if predicted else 0.0
            recall = len(hits) / len(actual) if actual else (1.0 if not predicted else 0.0)
            f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
            accuracy = round(f1 * 100, 1)

            db.resolve_prediction(
                p["id"],
                outcome=json.dumps({
                    "actual_spillovers": sorted(actual),
                    "correctly_called": sorted(hits),
                    "missed": sorted(actual - predicted),
                    "false_alarms": sorted(predicted - actual),
                }),
                accuracy=accuracy,
            )
            db.save_sprint_outcome(
                CURRENT_USER_ID, sprint_name=sname, project=projs,
                completion_rate=round(completed / committed * 100, 1) if committed else 0,
                committed=committed, completed=completed, spillovers=len(actual),
                predicted_spillovers=json.dumps(sorted(predicted)),
                actual_spillovers=json.dumps(sorted(actual)),
                prediction_accuracy=accuracy,
            )
            graded.append({
                "sprint": sname, "accuracy": accuracy,
                "predicted": len(predicted), "actual": len(actual),
                "hits": len(hits), "precision": round(precision * 100),
                "recall": round(recall * 100),
            })

        if not graded:
            return (f"No predictions ready to grade yet — {still_open} prediction(s) "
                    f"are for sprints still in progress. Check back once they close.")

        lines = [f"✅ Graded {len(graded)} prediction(s):\n"]
        for g in graded:
            lines.append(
                f"  {g['sprint']}: flagged {g['predicted']}, {g['actual']} actually spilled, "
                f"{g['hits']} called correctly → {g['accuracy']}% "
                f"(precision {g['precision']}%, recall {g['recall']}%)"
            )
        overall = db.get_prediction_accuracy(CURRENT_USER_ID)
        if overall.get("avg_accuracy") is not None:
            lines.append(f"\n📈 Track record now: {overall['avg_accuracy']}% over {overall['total']} graded prediction(s).")
        return "\n".join(lines)

    except Exception as e:
        return f"Error scoring predictions: {e}"


def get_prediction_track_record() -> str:
    """Show how accurate Sprint Scribe's spillover predictions have been over time.
    Use when the manager asks 'how accurate are you?', 'what's your track record?',
    or wants proof the predictions are trustworthy."""
    try:
        overall = db.get_prediction_accuracy(CURRENT_USER_ID)
        if not overall or overall.get("total", 0) == 0:
            pending = [p for p in db.get_unresolved_predictions(CURRENT_USER_ID)
                       if p.get("prediction_type") == "spillover"]
            if pending:
                return (f"No graded predictions yet — I have {len(pending)} prediction(s) waiting on "
                        f"sprints to close. Run score_predictions once a sprint wraps to build the track record.")
            return "No predictions on record yet. Run predict_spillovers on an active sprint to start the track record."

        lines = [f"📈 Spillover prediction track record: {overall['avg_accuracy']}% accurate "
                 f"over {overall['total']} graded sprint(s).\n"]

        outcomes = db.get_recent_sprint_outcomes(CURRENT_USER_ID, limit=5)
        graded = [o for o in outcomes if o.get("prediction_accuracy") is not None]
        if graded:
            lines.append("Recent sprints:")
            for o in graded:
                lines.append(
                    f"  {o['sprint_name']}: {o['prediction_accuracy']}% — "
                    f"predicted vs {o['spillovers']} actual spillover(s), "
                    f"{o['completion_rate']}% completion"
                )
        trend = [o["prediction_accuracy"] for o in reversed(graded)]
        if len(trend) >= 2 and trend[-1] > trend[0]:
            lines.append(f"\nTrend: improving — {trend[0]}% → {trend[-1]}% as I learn your team's patterns.")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching track record: {e}"


# ---------------------------------------------------------------------------
# GitHub tool
# ---------------------------------------------------------------------------

def get_github_prs(ticket_key: str) -> str:
    """Find open or recently merged GitHub PRs related to a Jira ticket key.
    Uses Jira's dev info API (primary) and GitHub search (fallback).

    Args:
        ticket_key: Jira issue key to search for e.g. PROJ-1234
    """
    try:
        import requests

        # Primary: Jira dev info API — sees PRs linked via GitHub integration
        jira = _get_jira()
        issue = jira.issue(ticket_key)
        issue_id = issue.get("id")
        results = []

        if issue_id:
            resp = requests.get(
                f"{JIRA_URL}/rest/dev-status/latest/issue/detail",
                params={"issueId": issue_id, "applicationType": "GitHub", "dataType": "pullrequest"},
                auth=(JIRA_EMAIL, JIRA_API_TOKEN),
                headers={"Accept": "application/json"},
                timeout=(5, 15),
            )
            if resp.status_code == 200:
                data = resp.json()
                gh = _get_github()
                for detail in data.get("detail", []):
                    for pr in detail.get("pullRequests", []):
                        pr_id = pr.get("id", "").lstrip("#")
                        pr_name = pr.get("name", "")
                        pr_status = pr.get("status", "UNKNOWN")
                        pr_url = pr.get("url", "")
                        author = pr.get("author", {}).get("name", "Unknown")
                        source_branch = pr.get("source", {}).get("branch", "")
                        dest_branch = pr.get("destination", {}).get("branch", "")
                        reviewers = [r.get("name", "") for r in pr.get("reviewers", [])]
                        reviewer_str = ", ".join(reviewers) if reviewers else "No reviewers"
                        comment_count = pr.get("commentCount", 0)

                        # Enrich with GitHub review state if accessible
                        review_state = ""
                        review_detail = ""
                        if pr_url and pr_id:
                            try:
                                # Extract repo from URL: https://github.com/vendasta/galaxy/pull/31063
                                url_parts = pr_url.replace("https://github.com/", "").split("/")
                                if len(url_parts) >= 4:
                                    repo_full = f"{url_parts[0]}/{url_parts[1]}"
                                    pr_number = int(url_parts[3])
                                    gh_pr = gh.get_repo(repo_full).get_pull(pr_number)

                                    # Review states
                                    reviews = list(gh_pr.get_reviews())
                                    approved_by = [r.user.login for r in reviews if r.state == "APPROVED"]
                                    changes_requested = [r.user.login for r in reviews if r.state == "CHANGES_REQUESTED"]
                                    commented_by = [r.user.login for r in reviews if r.state == "COMMENTED"]

                                    # Review comments (inline code comments)
                                    review_comments = list(gh_pr.get_review_comments())
                                    total_review_comments = len(review_comments)

                                    # PR age
                                    age_days = (datetime.now(timezone.utc) - gh_pr.created_at.replace(tzinfo=timezone.utc)).days

                                    # Determine overall state
                                    if approved_by and not changes_requested:
                                        review_state = f"APPROVED by {', '.join(approved_by)}"
                                        if gh_pr.mergeable and pr_status == "OPEN":
                                            review_state += " — ready to merge"
                                    elif changes_requested:
                                        review_state = f"CHANGES REQUESTED by {', '.join(changes_requested)}"
                                        # Check if author pushed new commits after the review
                                        last_review_date = max(
                                            (r.submitted_at for r in reviews if r.state == "CHANGES_REQUESTED"),
                                            default=None
                                        )
                                        last_commit_date = list(gh_pr.get_commits())[-1].commit.committer.date if gh_pr.commits > 0 else None
                                        if last_review_date and last_commit_date:
                                            if last_commit_date.replace(tzinfo=timezone.utc) > last_review_date.replace(tzinfo=timezone.utc):
                                                review_state += " — author pushed new commits, waiting for re-review"
                                            else:
                                                review_state += " — author hasn't addressed feedback yet"
                                    elif commented_by:
                                        review_state = f"Review comments from {', '.join(set(commented_by))} — needs response"
                                    elif pr_status == "OPEN":
                                        review_state = f"No reviews yet — waiting for review ({age_days}d old)"

                                    # Comment summary
                                    if total_review_comments > 0:
                                        review_detail = f"  Code comments: {total_review_comments} inline comments"
                                        if not approved_by:
                                            review_detail += " (may need responses)"
                                        else:
                                            review_detail += " (all addressed — approved)"
                            except Exception:
                                pass

                        pr_line = (
                            f"PR #{pr_id}: {pr_name}\n"
                            f"  Status: {pr_status}\n"
                            f"  Author: {author}\n"
                            f"  Branch: {source_branch} → {dest_branch}\n"
                            f"  Reviewers: {reviewer_str}\n"
                        )
                        if review_state:
                            pr_line += f"  Review: {review_state}\n"
                        if review_detail:
                            pr_line += f"{review_detail}\n"
                        elif not review_state:
                            pr_line += f"  Comments: {comment_count}\n"
                        pr_line += f"  URL: {pr_url}"

                        results.append(pr_line)

        if results:
            return "\n\n".join(results)

        # Fallback: GitHub search API — org-wide
        try:
            gh = _get_github()
            query = f"{ticket_key} {_gh_scope()} is:pr"
            search_results = gh.search_issues(query, sort="updated", order="desc")
            for i, pr_issue in enumerate(search_results):
                if i >= 5:
                    break
                try:
                    repo_name = pr_issue.repository.name
                    pr = pr_issue.as_pull_request()
                    reviews = list(pr.get_reviews())
                    approved_by = [r.user.login for r in reviews if r.state == "APPROVED"]
                    review_summary = (
                        f"Approved by: {', '.join(approved_by)}" if approved_by
                        else "No approvals yet"
                    )
                    results.append(
                        f"[{repo_name}] PR #{pr.number}: {pr.title}\n"
                        f"  State: {pr.state} | Branch: {pr.head.ref}\n"
                        f"  Raised by: {pr.user.login}\n"
                        f"  {review_summary}\n"
                        f"  Merged: {'Yes' if pr.merged else 'No'}\n"
                        f"  URL: {pr.html_url}"
                    )
                except Exception:
                    continue
        except Exception:
            pass

        return "\n\n".join(results) if results else f"No PRs found for {ticket_key}."
    except Exception as e:
        return f"Error fetching PRs for {ticket_key}: {e}"


# ---------------------------------------------------------------------------
# Team Availability
# ---------------------------------------------------------------------------

def get_team_availability(project: str = "") -> str:
    """Get developer workload to identify who has bandwidth for PR reviews.
    ONLY includes developers from the configured TEAM_MEMBERS list — excludes managers,
    unassigned, and anyone not in the team.

    Args:
        project: Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all configured projects.
    """
    try:
        jira = _get_jira()
        proj_list = _resolve_projects(project)
        now = datetime.now(timezone.utc)

        # Which team members belong to the requested projects (from the EM config).
        all_known = set().union(*_MEMBERS_BY_PROJECT.values()) if _MEMBERS_BY_PROJECT else set()
        allowed_members = set()
        for proj in proj_list:
            # Known project -> its roster; unknown -> everyone we know.
            allowed_members.update(_MEMBERS_BY_PROJECT.get(proj, all_known))

        issues, sprint_label = _get_current_sprint_jql(jira, proj_list, " ORDER BY assignee ASC")

        if not issues:
            return f"No sprint data found for {', '.join(proj_list)}."

        # Effort weight: mostly by issue type, priority is a light modifier
        _TYPE_WEIGHT = {"Bug": 2, "Story": 3, "Task": 1, "Improvement": 2, "Sub-task": 1}
        _PRIORITY_WEIGHT = {"Blocker": 2, "Critical": 1.5, "Major": 1.2, "Minor": 1, "Trivial": 1}

        # Build workload ONLY for configured team developers
        workload = {}
        for member in allowed_members:
            workload[member] = {
                "total": 0, "done": 0, "in_progress": 0, "todo": 0,
                "bugs": 0, "stories": 0, "tasks": 0,
                "effort_remaining": 0, "effort_done": 0,
            }

        for issue in issues:
            fields = issue["fields"]
            assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")

            if assignee not in allowed_members:
                continue

            status = fields.get("status", {}).get("name", "").lower()
            issue_type = fields.get("issuetype", {}).get("name", "Task")
            priority = fields.get("priority", {}).get("name", "Minor")

            # Calculate effort weight from type + priority
            effort = _TYPE_WEIGHT.get(issue_type, 1) * _PRIORITY_WEIGHT.get(priority, 1)

            workload[assignee]["total"] += 1

            # Track issue types
            if issue_type == "Bug":
                workload[assignee]["bugs"] += 1
            elif issue_type == "Story":
                workload[assignee]["stories"] += 1
            else:
                workload[assignee]["tasks"] += 1

            if _is_resolved(issue["fields"]):
                workload[assignee]["done"] += 1
                workload[assignee]["effort_done"] += effort
            elif status in ("in progress", "in review", "in development", "code review / testing"):
                workload[assignee]["in_progress"] += 1
                workload[assignee]["effort_remaining"] += effort
            else:
                workload[assignee]["todo"] += 1
                workload[assignee]["effort_remaining"] += effort

        # Check GitHub review load for each developer
        gh = _get_github()
        for person_name, stats in workload.items():
            gh_username = TEAM_MEMBERS.get(person_name, "")
            if gh_username:
                try:
                    query = f"{_gh_scope()} is:pr is:open review-requested:{gh_username}"
                    pending_reviews = sum(1 for _ in gh.search_issues(query))
                    stats["pending_reviews"] = pending_reviews
                except Exception:
                    stats["pending_reviews"] = "?"
            else:
                stats["pending_reviews"] = "?"

        # Sort by effort remaining (weighted by type + priority), then by pending reviews
        lines = [f"=== DEVELOPER AVAILABILITY ({sprint_label}) ==="]
        lines.append(f"Projects: {', '.join(proj_list)} | Only showing developers (no managers)")
        lines.append(f"Effort = issue type (Story=3, Bug=2, Task=1) x priority (Blocker=2, Critical=1.5, Major=1.2, Minor=1)\n")
        sorted_members = sorted(
            workload.items(),
            key=lambda x: (x[1]["effort_remaining"], x[1].get("pending_reviews", 99))
        )

        for person, stats in sorted_members:
            remaining = stats["in_progress"] + stats["todo"]
            completion = round(stats["done"] / stats["total"] * 100) if stats["total"] else 0
            type_breakdown = []
            if stats["bugs"]: type_breakdown.append(f"{stats['bugs']} bugs")
            if stats["stories"]: type_breakdown.append(f"{stats['stories']} stories")
            if stats["tasks"]: type_breakdown.append(f"{stats['tasks']} tasks")
            type_str = f" ({', '.join(type_breakdown)})" if type_breakdown else ""
            lines.append(
                f"{person}: {stats['done']}/{stats['total']} done ({completion}%){type_str} | "
                f"{stats['in_progress']} in progress, {stats['todo']} to do | "
                f"Effort remaining: {stats['effort_remaining']} weighted pts | "
                f"Pending PR reviews: {stats['pending_reviews']}"
            )

        # Rank best candidates for reviews (by effort remaining + PR load)
        lines.append(f"\n💡 BEST CANDIDATES FOR PR REVIEW (ranked by availability):")
        rank = 1
        for person, stats in sorted_members:
            remaining = stats["in_progress"] + stats["todo"]
            pr_load = stats["pending_reviews"] if isinstance(stats["pending_reviews"], int) else 99
            effort = stats["effort_remaining"]

            reasons = []
            if remaining == 0:
                reasons.append("all tickets done")
            elif remaining <= 2:
                reasons.append(f"only {remaining} tickets left")
            else:
                reasons.append(f"{remaining} tickets remaining")

            if effort == 0:
                reasons.append("no effort remaining")
            else:
                reasons.append(f"{effort} effort pts remaining")

            if pr_load == 0:
                reasons.append("no pending PR reviews")
            elif pr_load <= 3:
                reasons.append(f"{pr_load} pending PR reviews")
            else:
                reasons.append(f"{pr_load} pending PR reviews (heavy)")

            # Note issue types in remaining work
            if stats["bugs"] and remaining > 0:
                reasons.append(f"handling {stats['bugs']} bugs")

            lines.append(f"  {rank}. {person} — {', '.join(reasons)}")
            rank += 1

        return "\n".join(lines)

    except Exception as e:
        return f"Error getting team availability: {e}"


# ---------------------------------------------------------------------------
# Morning Risk Briefing
# ---------------------------------------------------------------------------

def get_morning_briefing(project: str = "") -> str:
    """Generate a morning risk briefing by cross-referencing Jira and GitHub.
    Identifies: stale tickets, In Progress tickets with no PR, aging PRs without review,
    blocked items, and workload imbalance across the team.

    Args:
        project: Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all configured projects.
    """
    try:
        jira = _get_jira()
        proj_list = _resolve_projects(project)
        now = datetime.now(timezone.utc)

        # Current roster for the projects we're briefing on. Used to keep departed
        # devs out of named workload/insights sections; their tickets still count
        # in done/total but they shouldn't be recommended for new work.
        current_roster = set()
        for _p in proj_list:
            current_roster.update(_MEMBERS_BY_PROJECT.get(_p, set()))

        def _is_current(person: str) -> bool:
            if not current_roster:  # no config -> preserve old behavior
                return True
            return person == "Unassigned" or person in current_roster

        # Get sprint tickets (active or most recent closed)
        issues, sprint_label = _get_current_sprint_jql(jira, proj_list, " ORDER BY assignee ASC")

        if not issues:
            return "No issues found in any recent sprint."

        # Get sprint name and end date
        sprint_name = sprint_label
        sprint_end = None
        is_active = not sprint_label.startswith("last sprint")

        # Try getting end date from board API (more reliable than issue sprint field)
        sprint_start = None
        if is_active:
            try:
                boards = _get_boards_for_projects(jira, proj_list)
                for b in boards:
                    active_sprints = _get_all_sprints_paginated(jira, b["id"], state="active")
                    if active_sprints:
                        s = active_sprints[0]
                        sprint_name = s.get("name", sprint_name)
                        if s.get("endDate"):
                            sprint_end = datetime.fromisoformat(s["endDate"].replace("Z", "+00:00"))
                        if s.get("startDate"):
                            sprint_start = datetime.fromisoformat(s["startDate"].replace("Z", "+00:00"))
                        break
            except Exception:
                pass

        if is_active:
            if sprint_start:
                sprint_age = _working_days_between(sprint_start, now)
                if sprint_age == 0:
                    age_str = "started today"
                elif sprint_age == 1:
                    age_str = "day 1"
                else:
                    age_str = f"day {sprint_age + 1}"
            else:
                age_str = "active"
            if sprint_end:
                days_left = _working_days_between(now, sprint_end)
                time_info = f"{age_str} of sprint, {days_left} working day(s) left"
            else:
                time_info = age_str
        else:
            time_info = f"sprint ended — showing data from {sprint_name}"

        _TYPE_WEIGHT = {"Bug": 2, "Story": 3, "Task": 1, "Improvement": 2, "Sub-task": 1}
        _PRIORITY_WEIGHT = {"Blocker": 2, "Critical": 1.5, "Major": 1.2, "Minor": 1, "Trivial": 1}

        stale_tickets = []       # not updated in 3+ days
        no_pr_tickets = []       # In Progress but no PR found
        blocked_tickets = []     # Blocked status
        workload = {}            # assignee → stats with effort
        done_count = 0
        total_count = len(issues)

        # Collect all "In Progress" ticket keys for PR check
        in_progress_keys = []

        for issue in issues:
            fields = issue["fields"]
            key = issue["key"]
            summary = fields.get("summary", "")[:60]
            status = fields.get("status", {}).get("name", "")
            status_lower = status.lower()
            assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
            updated = fields.get("updated", "")
            issue_type = fields.get("issuetype", {}).get("name", "Task")
            priority = fields.get("priority", {}).get("name", "Minor")

            effort = _TYPE_WEIGHT.get(issue_type, 1) * _PRIORITY_WEIGHT.get(priority, 1)

            # Workload tracking
            if assignee not in workload:
                workload[assignee] = {"done": 0, "in_progress": 0, "in_review": 0, "todo": 0, "total": 0,
                                      "effort_remaining": 0, "effort_done": 0, "bugs": 0, "stories": 0}
            workload[assignee]["total"] += 1
            if issue_type == "Bug":
                workload[assignee]["bugs"] += 1
            elif issue_type == "Story":
                workload[assignee]["stories"] += 1

            if _is_resolved(fields):
                workload[assignee]["done"] += 1
                workload[assignee]["effort_done"] += effort
                done_count += 1
                continue
            elif status_lower in ("code review / testing", "code review", "in review"):
                workload[assignee]["in_review"] += 1
                workload[assignee]["effort_remaining"] += effort
                in_progress_keys.append((key, assignee, summary, status))
            elif status_lower in ("in progress", "in development"):
                workload[assignee]["in_progress"] += 1
                workload[assignee]["effort_remaining"] += effort
                in_progress_keys.append((key, assignee, summary, status))
            else:
                workload[assignee]["todo"] += 1
                workload[assignee]["effort_remaining"] += effort

            # Blocked check
            if status_lower in ("blocked", "on hold"):
                blocked_tickets.append(f"{key} ({assignee}): {summary} [{priority}]")

            # Stale check — only flag if not updated since sprint started
            # For new sprints (started < 3 days ago), don't flag anything as stale
            if updated and is_active and sprint_start:
                last_update = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                sprint_age_days = (now - sprint_start).days
                days_since_update = (now - last_update).days
                # Only flag stale if sprint is at least 3 days old AND ticket hasn't been updated since sprint start
                if sprint_age_days >= 3 and last_update < sprint_start:
                    stale_tickets.append(f"{key} ({assignee}): {summary} — not updated since sprint started [{status}]")
            elif updated and not is_active:
                last_update = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                days_stale = (now - last_update).days
                if days_stale >= 3:
                    stale_tickets.append(f"{key} ({assignee}): {summary} — {days_stale}d silent [{status}]")

        # Check GitHub for In Progress tickets with no PR (parallelized)
        if in_progress_keys:
            gh = _get_github()

            def _check_has_pr(ticket_info):
                key, assignee, summary = ticket_info[:3]
                try:
                    query = f"{key} {_gh_scope()} is:pr"
                    has_pr = any(True for _ in gh.search_issues(query))
                    return key, assignee, summary, has_pr
                except Exception:
                    return key, assignee, summary, False  # conservative: assume no PR on error

            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(_check_has_pr, t) for t in in_progress_keys]
                for future in as_completed(futures):
                    try:
                        key, assignee, summary, has_pr = future.result()
                        if not has_pr:
                            no_pr_tickets.append(f"{key} ({assignee}): {summary}")
                    except Exception:
                        continue  # skip failed futures gracefully

        # Check for aging PRs without review — org-wide search
        aging_prs = []
        try:
            gh = _get_github()
            age_cutoff = (now - timedelta(days=2)).strftime("%Y-%m-%d")
            query = f"{_gh_scope()} is:pr is:open created:<={age_cutoff} review:none"
            for i, pr_issue in enumerate(gh.search_issues(query, sort="created", order="asc")):
                if i >= 10:
                    break
                try:
                    pr = pr_issue.as_pull_request()
                    age_days = (now - pr.created_at.replace(tzinfo=timezone.utc)).days
                    aging_prs.append(
                        f"[{pr.repository.name}] PR #{pr.number}: {pr.title[:50]} "
                        f"— {age_days}d old, by {pr.user.login}"
                    )
                except Exception:
                    continue
        except Exception:
            pass

        # Build report — lead with team name + sprint + status + what's in flight
        team_label = _team_display_label(proj_list) or ", ".join(proj_list)
        pct = round(done_count / total_count * 100) if total_count else 0
        lines = [f"=== MORNING BRIEFING — {team_label} · {sprint_name} ==="]
        lines.append(f"Status: {done_count}/{total_count} done ({pct}%) · {time_info}\n")

        # At-a-glance dashboard — counts first, so the EM can size up the sprint
        # and pick the next conversation before reading any ticket list. The
        # per-status breakdown comes from current-team workload only; departed
        # devs are excluded (their tickets get the reassignment callout below).
        _current_workload = {p: s for p, s in workload.items()
                             if _is_current(p) and p not in _EXCLUDE_FROM_WORKLOAD}
        in_progress_total = sum(s.get("in_progress", 0) for s in _current_workload.values())
        in_review_total   = sum(s.get("in_review", 0)   for s in _current_workload.values())
        todo_total        = sum(s.get("todo", 0)        for s in _current_workload.values())

        lines.append("📋 AT A GLANCE:")
        lines.append(f"  {done_count} done · {in_progress_total} in progress · "
                     f"{in_review_total} in review · {todo_total} to do")

        # Action items the EM can act on. Skip categories with zero count to
        # keep the line tight (no noise).
        action_chips = []
        if blocked_tickets:
            action_chips.append(f"🔴 {len(blocked_tickets)} blocked")
        if stale_tickets:
            action_chips.append(f"⚠️ {len(stale_tickets)} stale")
        if no_pr_tickets:
            action_chips.append(f"🔍 {len(no_pr_tickets)} in progress without PR")
        if aging_prs:
            action_chips.append(f"👀 {len(aging_prs)} PRs awaiting review (2+ days)")
        if action_chips:
            lines.append(f"  {' · '.join(action_chips)}")
        else:
            lines.append("  ✅ Nothing flagged for attention")
        lines.append("")

        if blocked_tickets:
            lines.append(f"🔴 BLOCKED ({len(blocked_tickets)}):")
            for t in blocked_tickets:
                lines.append(f"  {t}")
            lines.append("")

        if stale_tickets:
            lines.append(f"⚠️ STALE — no updates in 3+ days ({len(stale_tickets)}):")
            for t in stale_tickets[:8]:
                lines.append(f"  {t}")
            lines.append("")

        if no_pr_tickets:
            lines.append(f"🔍 IN PROGRESS BUT NO PR ({len(no_pr_tickets)}):")
            for t in no_pr_tickets[:8]:
                lines.append(f"  {t}")
            lines.append("")

        if aging_prs:
            lines.append(f"👀 AGING PRs WITHOUT APPROVAL ({len(aging_prs)}):")
            for p in aging_prs[:8]:
                lines.append(f"  {p}")
            lines.append("")

        # Workload distribution — exclude EMs AND people no longer on the team.
        # Tickets still owned by former members are routed to a separate callout
        # so the EM sees the reassignment signal explicitly.
        dev_workload = {}
        former_workload = {}
        for p, s in workload.items():
            if p in _EXCLUDE_FROM_WORKLOAD:
                continue
            if _is_current(p):
                dev_workload[p] = s
            else:
                former_workload[p] = s

        # Collect per-person ticket details + check PR status for in-review tickets
        import requests as _req
        person_tickets = {}
        former_member_open = {}  # {former_member: [ticket keys]} — only OPEN items
        pr_status_cache = {}  # key -> "approved" | "pending" | "changes_requested"
        # PR pipeline buckets — the workflow stage AFTER "in progress" but BEFORE
        # "done", surfacing what's actively waiting on someone to act.
        pending_pr_tickets = []        # PR raised, no approval yet (action: nudge reviewer)
        needs_changes_tickets = []     # PR raised, reviewer requested changes (action: author fixes)
        approved_ready_to_merge = []   # PR approved, not merged yet (action: merge it)
        for issue in issues:
            fields = issue["fields"]
            assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
            if assignee in _EXCLUDE_FROM_WORKLOAD:
                continue
            if not _is_current(assignee):
                # Track former-member open tickets for the reassignment callout
                if not _is_resolved(fields):
                    former_member_open.setdefault(assignee, []).append(issue["key"])
                continue
            key = issue["key"]
            status = fields.get("status", {}).get("name", "")
            summary = fields.get("summary", "")[:60]
            issue_id = issue.get("id")
            if assignee not in person_tickets:
                person_tickets[assignee] = []
            person_tickets[assignee].append({"key": key, "status": status})

            # Check PR approval status for in-review tickets
            if status.lower() in ("code review / testing", "code review", "in review") and issue_id:
                try:
                    resp = _req.get(
                        f"{JIRA_URL}/rest/dev-status/latest/issue/detail",
                        params={"issueId": issue_id, "applicationType": "GitHub", "dataType": "pullrequest"},
                        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
                        headers={"Accept": "application/json"},
                        timeout=(5, 15),
                    )
                    if resp.status_code == 200:
                        for detail in resp.json().get("detail", []):
                            for pr in detail.get("pullRequests", []):
                                if pr.get("status") != "OPEN":
                                    continue
                                pr_id = pr.get("id", "").lstrip("#")
                                # Aggregate pr_status_cache so the WORKLOAD section can show
                                # multiple PRs per ticket joined with '; '.
                                def _append_status(new_status: str):
                                    existing = pr_status_cache.get(key, "")
                                    pr_status_cache[key] = f"{existing}; {new_status}" if existing else new_status

                                try:
                                    pr_url = pr.get("url", "")
                                    url_parts = pr_url.replace("https://github.com/", "").split("/")
                                    if len(url_parts) >= 4:
                                        gh = _get_github()
                                        gh_pr = gh.get_repo(f"{url_parts[0]}/{url_parts[1]}").get_pull(int(url_parts[3]))
                                        reviews = list(gh_pr.get_reviews())
                                        # Dedupe by reviewer — one user approving twice = one approval.
                                        approved_users = {r.user.login for r in reviews if r.state == "APPROVED"}
                                        changes_users = {r.user.login for r in reviews if r.state == "CHANGES_REQUESTED"}
                                        n_app, n_chg = len(approved_users), len(changes_users)
                                        if n_chg > 0:
                                            _append_status(f"PR #{pr_id} needs fixes ({', '.join(sorted(changes_users))})")
                                            needs_changes_tickets.append(f"{key} ({assignee}): {summary} — PR #{pr_id} needs fixes from {', '.join(sorted(changes_users))}")
                                        elif n_app >= _MIN_APPROVALS:
                                            _append_status(f"PR #{pr_id} approved ({n_app}/{_MIN_APPROVALS}) — ready to merge")
                                            approved_ready_to_merge.append(f"{key} ({assignee}): {summary} — PR #{pr_id} ({n_app}/{_MIN_APPROVALS} approvals)")
                                        else:
                                            # Includes 0 approvals AND partially-approved (e.g. 1 of 2 approvals).
                                            # The PR is still waiting on a reviewer to finish the job.
                                            approvers_note = f"approved by {', '.join(sorted(approved_users))}" if approved_users else "no approvals yet"
                                            _append_status(f"PR #{pr_id} {n_app}/{_MIN_APPROVALS} approvals — awaiting reviewers")
                                            pending_pr_tickets.append(f"{key} ({assignee}): {summary} — PR #{pr_id} ({n_app}/{_MIN_APPROVALS} approvals, {approvers_note})")
                                except Exception:
                                    _append_status(f"PR #{pr_id} raised")
                                    pending_pr_tickets.append(f"{key} ({assignee}): {summary} — PR #{pr_id} (review state unknown)")
                                # NOTE: no break here — a ticket can have multiple linked PRs
                                # (e.g. frontend + backend, main + follow-up). Each gets its own
                                # entry in the right bucket so EM sees what to nudge vs merge.
                except Exception:
                    pass

        # PR pipeline sections — what's waiting on whom in the review workflow.
        # Renders between the per-ticket sections and the WORKLOAD breakdown so
        # the EM can see "what needs a nudge?" at a glance.
        if pending_pr_tickets:
            lines.append(f"📥 PRs RAISED, AWAITING APPROVAL ({len(pending_pr_tickets)}):")
            for t in pending_pr_tickets[:10]:
                lines.append(f"  {t}")
            lines.append("")
        if needs_changes_tickets:
            lines.append(f"❌ PRs WITH CHANGES REQUESTED ({len(needs_changes_tickets)}):")
            for t in needs_changes_tickets[:10]:
                lines.append(f"  {t}")
            lines.append("")
        if approved_ready_to_merge:
            lines.append(f"✅ PRs APPROVED, READY TO MERGE ({len(approved_ready_to_merge)}):")
            for t in approved_ready_to_merge[:10]:
                lines.append(f"  {t}")
            lines.append("")

        # Former team members holding open tickets — surface these as a reassignment
        # signal instead of silently letting the LLM recommend "she has bandwidth".
        if former_member_open:
            lines.append("⚠️ TICKETS ASSIGNED TO FORMER TEAM MEMBERS — NEEDS REASSIGNMENT:")
            for fmr, keys in sorted(former_member_open.items()):
                preview = ", ".join(keys[:5]) + (f" +{len(keys) - 5} more" if len(keys) > 5 else "")
                lines.append(f"  {fmr} (no longer on team): {len(keys)} open — {preview}")
            lines.append("")

        lines.append("📊 WORKLOAD:")
        for person, stats in sorted(dev_workload.items(), key=lambda x: x[1]["effort_remaining"], reverse=True):
            type_parts = []
            if stats["stories"]: type_parts.append(f"{stats['stories']} stories")
            if stats["bugs"]: type_parts.append(f"{stats['bugs']} bugs")
            tasks_count = stats["total"] - stats["stories"] - stats["bugs"]
            if tasks_count > 0: type_parts.append(f"{tasks_count} tasks")
            type_str = ", ".join(type_parts) if type_parts else f"{stats['total']} tickets"

            status_parts = []
            if stats["done"]: status_parts.append(f"{stats['done']} done")
            if stats["in_review"]: status_parts.append(f"{stats['in_review']} in review")
            if stats["in_progress"]: status_parts.append(f"{stats['in_progress']} in progress")
            if stats["todo"]: status_parts.append(f"{stats['todo']} to do")

            line = f"  {person}: {type_str} assigned — {', '.join(status_parts)}"

            # Only show ticket details for ones with pending PRs
            tickets = person_tickets.get(person, [])
            pr_lines = []
            for t in tickets:
                pr_info = pr_status_cache.get(t["key"])
                if pr_info:
                    pr_lines.append(f"    → {t['key']}: {pr_info}")
            if pr_lines:
                line += "\n" + "\n".join(pr_lines)

            lines.append(line)

        # Generate insights
        not_done = total_count - done_count
        completion_pct = round(done_count / total_count * 100) if total_count else 0
        lines.append(f"\n📋 INSIGHTS:")

        # Completion assessment — context-aware for active vs ended sprints
        if is_active:
            lines.append(f"  📊 Sprint is active: {done_count}/{total_count} done so far ({completion_pct}%)")
        elif completion_pct >= 90:
            lines.append(f"  ✅ Strong sprint: {completion_pct}% completion ({done_count}/{total_count})")
        elif completion_pct >= 70:
            lines.append(f"  ⚠️ {completion_pct}% completion — {not_done} tickets didn't make it")
        else:
            lines.append(f"  🔴 Low completion: only {completion_pct}% — {not_done} tickets incomplete")

        # Heaviest remaining workload (by effort, not just count) — devs only
        max_effort = max(dev_workload.items(), key=lambda x: x[1]["effort_remaining"], default=None)
        if max_effort and max_effort[1]["effort_remaining"] > 0:
            s = max_effort[1]
            remaining_tickets = s["in_progress"] + s["todo"]
            detail = []
            if s["bugs"]: detail.append(f"{s['bugs']} bugs")
            if s["stories"]: detail.append(f"{s['stories']} stories")
            detail_str = f" ({', '.join(detail)})" if detail else ""
            lines.append(f"  👀 {max_effort[0]} has the heaviest remaining workload: {remaining_tickets} tickets{detail_str}, effort score {s['effort_remaining']:.0f}")

        # Completed all assigned work — devs only
        completed_all = [(p, s) for p, s in dev_workload.items()
                         if s["total"] > 0 and s["done"] == s["total"]]
        if completed_all:
            names = [f"{p} ({s['done']}/{s['total']})" for p, s in completed_all]
            lines.append(f"  ✅ Completed all assigned tickets: {', '.join(names)}")

        # Most volume delivered — devs only
        avg_done = done_count / len(dev_workload) if dev_workload else 0
        most_volume = max(dev_workload.items(), key=lambda x: x[1]["effort_done"], default=None)
        if most_volume and most_volume[1]["done"] > avg_done * 1.3 and most_volume[1]["done"] >= 4:
            lines.append(f"  📈 {most_volume[0]} delivered the most effort this sprint: {most_volume[1]['done']} tickets, effort score {most_volume[1]['effort_done']:.0f}")

        # Carry-overs flag (only relevant for ended sprints)
        if not_done > 0 and not is_active:
            lines.append(f"  🔁 {not_done} tickets carrying over to next sprint — review in planning")
        elif not_done > 0 and is_active:
            lines.append(f"  📋 {not_done} tickets still in progress or to do")

        # Who's free to help — devs only
        free_people = [(p, s) for p, s in dev_workload.items()
                       if s["effort_remaining"] == 0 and s["total"] > 0]
        if free_people and not_done > 0:
            names = [p for p, s in free_people]
            lines.append(f"  💡 {', '.join(names)} have no remaining work — available to help with carry-overs or reviews")

        # Escalated bugs (support-raised bugs). Open escalated bugs only, Bugs only
        # (no Tasks), excluding every terminal status. Assumes the team flags these
        # with the `jira_escalated` label (Vendasta convention) — teams that don't
        # use the label simply get zero results here.
        _ESC_STATUS_EXCL = ('("CLOSED-Won\'t Fix", Closed, Done, Duplicate, "GA CLOSED", '
                            '"Will Not Do", "Won\'t Do", "Won\'t Fix", Aborted, Cancelled)')
        escalated_lines = []
        total_escalated = 0
        missing_start = 0
        for proj in proj_list:
            jql = (f'project = {proj} AND issuetype = Bug AND labels = jira_escalated '
                   f'AND status NOT IN {_ESC_STATUS_EXCL} ORDER BY created DESC')
            try:
                esc_result = jira.jql(jql, limit=50)
                esc_issues = esc_result.get("issues", []) if isinstance(esc_result, dict) else esc_result
                for ei in esc_issues:
                    total_escalated += 1
                    ekey = ei["key"]
                    ef = ei["fields"]
                    esummary = ef.get("summary", "")[:50]
                    eassignee = (ef.get("assignee") or {}).get("displayName", "Unassigned")
                    estatus = ef.get("status", {}).get("name", "")
                    epriority = ef.get("priority", {}).get("name", "")
                    estart = ef.get("customfield_13983")  # estimated start date
                    if not estart:
                        missing_start += 1
                    start_str = estart if estart else "NOT SET"
                    escalated_lines.append(f"  {ekey} [{estatus}] {eassignee}: {esummary} | Priority: {epriority} | Est. Start: {start_str}")
            except Exception:
                continue

        if total_escalated > 0:
            lines.append("")
            lines.append(f"🚨 ESCALATED BUGS (support-raised): {total_escalated} open")
            if missing_start > 0:
                lines.append(f"  ⚠️ {missing_start} of {total_escalated} missing estimated start date — needs attention")
            else:
                lines.append(f"  ✅ All {total_escalated} have estimated start dates set")
            for el in escalated_lines:
                lines.append(el)

        return "\n".join(lines)

    except Exception as e:
        return f"Error generating morning briefing: {e}"


# ---------------------------------------------------------------------------
# 1:1 Prep Assistant
# ---------------------------------------------------------------------------

def get_person_summary(person_name: str, project: str = "") -> str:
    """Get a comprehensive summary of a team member's work for 1:1 prep.
    Shows their current sprint tickets, completion rate, carried-over items,
    and recent activity.

    Args:
        person_name: Team member's display name as it appears in Jira (e.g. 'Alex Johnson')
        project: Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all configured projects.
    """
    try:
        jira = _get_jira()
        proj_list = _resolve_projects(project)
        projects = ", ".join(proj_list)
        now = datetime.now(timezone.utc)

        # Current sprint tickets for this person (active or most recent closed)
        current_issues, sprint_label = _get_current_sprint_jql(
            jira, proj_list, f" AND assignee = '{person_name}' ORDER BY status ASC"
        )

        # Recent completed tickets (last 30 days)
        thirty_days_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        completed_result = jira.jql(
            f"project in ({projects}) AND assignee = '{person_name}' AND status in (Done, Closed) AND updated >= '{thirty_days_ago}' ORDER BY updated DESC",
            limit=20
        )
        completed_issues = completed_result.get("issues", []) if isinstance(completed_result, dict) else completed_result

        # Analyze current sprint
        done = []
        in_progress = []
        todo = []
        blocked = []
        total_points = 0
        done_points = 0

        for issue in current_issues:
            fields = issue["fields"]
            key = issue["key"]
            summary = fields.get("summary", "")[:60]
            status = fields.get("status", {}).get("name", "")
            status_lower = status.lower()
            points = fields.get("customfield_10013") or 0
            try:
                points = float(points)
            except (ValueError, TypeError):
                points = 0
            total_points += points
            updated = fields.get("updated", "")[:10]

            entry = f"{key} [{status}] {summary} ({points}pts, updated {updated})"

            if _is_resolved(fields):
                done.append(entry)
                done_points += points
            elif status_lower in ("blocked", "on hold"):
                blocked.append(entry)
            elif status_lower in ("in progress", "in review", "in development"):
                in_progress.append(entry)
            else:
                todo.append(entry)

        # Check for carried-over tickets (in current sprint but created before sprint start)
        # We approximate by checking if the ticket was in a previous sprint too
        carried_over = []
        for issue in current_issues:
            sprints = issue["fields"].get("customfield_10020") or []
            sprint_count = len([s for s in sprints if isinstance(s, dict)])
            if sprint_count > 1:
                key = issue["key"]
                summary = issue["fields"].get("summary", "")[:50]
                status = issue["fields"].get("status", {}).get("name", "")
                if not _is_resolved(issue["fields"]):
                    carried_over.append(f"{key}: {summary} (in {sprint_count} sprints)")

        total_current = len(current_issues)
        completion_rate = round(len(done) / total_current * 100) if total_current else 0

        lines = [f"=== 1:1 PREP: {person_name} ({sprint_label}) ===\n"]

        # Sprint snapshot
        lines.append(f"📊 CURRENT SPRINT: {len(done)}/{total_current} done ({completion_rate}%) | {done_points}/{total_points} pts")
        lines.append("")

        if blocked:
            lines.append(f"🔴 BLOCKED ({len(blocked)}):")
            for t in blocked:
                lines.append(f"  {t}")
            lines.append("")

        if in_progress:
            lines.append(f"🔄 IN PROGRESS ({len(in_progress)}):")
            for t in in_progress:
                lines.append(f"  {t}")
            lines.append("")

        if todo:
            lines.append(f"📋 TO DO ({len(todo)}):")
            for t in todo:
                lines.append(f"  {t}")
            lines.append("")

        if done:
            lines.append(f"✅ DONE ({len(done)}):")
            for t in done:
                lines.append(f"  {t}")
            lines.append("")

        if carried_over:
            lines.append(f"🔁 CARRIED OVER ({len(carried_over)}):")
            for t in carried_over:
                lines.append(f"  {t}")
            lines.append("")

        # Recent 30-day completion count
        lines.append(f"📈 LAST 30 DAYS: {len(completed_issues)} tickets completed")

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching person summary for {person_name}: {e}"


def _search_confluence_by_author(cql: str, person_name: str) -> list:
    """Generic Confluence content search filtered client-side by creator name.
    CQL filters by content type/space/date; we filter results by author display
    name (loose contains-match, both directions). Returns [{title, date, url,
    creator}] sorted newest-first. Empty list on any failure."""
    try:
        import requests as _req
        resp = _req.get(
            f"{JIRA_URL}/wiki/rest/api/content/search",
            params={"cql": cql, "limit": 200, "expand": "history,_links"},
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json"},
            timeout=(5, 15),
        )
        if resp.status_code != 200:
            return []
        body = resp.json()
        base = (body.get("_links") or {}).get("base", JIRA_URL + "/wiki")
        results = []
        name_lower = (person_name or "").lower().strip()
        if not name_lower:
            return []
        for p in body.get("results", []):
            hist = p.get("history") or {}
            creator = (hist.get("createdBy") or {}).get("displayName", "") or ""
            cl = creator.lower()
            if name_lower in cl or cl in name_lower:
                webui = ((p.get("_links") or {}).get("webui") or "")
                results.append({
                    "title": p.get("title", "")[:120],
                    "date": (hist.get("createdDate") or "")[:10],
                    "url": (base + webui) if webui else "",
                    "creator": creator,
                })
        results.sort(key=lambda x: x["date"], reverse=True)
        return results
    except Exception:
        return []


# Confluence space + parent locations. Override via env if your space differs.
CONFLUENCE_SPACE = os.getenv("CONFLUENCE_SPACE", "RD")
# Parent page of the "Request for Comment (RFC)" index — all RFCs live as
# descendants (the main RFC + sub-pages for Technical Details / Engineering /
# Product & Design / Sprint Goals). Override via CONFLUENCE_RFC_PARENT_ID.
CONFLUENCE_RFC_PARENT_ID = os.getenv("CONFLUENCE_RFC_PARENT_ID", "")


def _get_recent_rfcs_for_person(person_name: str, days: int = 90) -> list:
    """RFC contributions the person authored in the last N days.

    Vendasta has TWO RFC patterns and we need both:
      1. Multi-page RFCs filed under the RD 'Request for Comment (RFC)' parent
         (Technical Details / Engineering / Product & Design / Sprint Goals
         sub-pages — often unprefixed in title). Caught by ancestor scope.
      2. Single-page RFCs in any product space following the
         'RFC: …' title convention. Caught by title pattern, org-wide.

    We union both and dedupe by URL so the same page never appears twice."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    title_cql = (f'title ~ "RFC" AND type = "page" '
                 f'AND lastmodified >= "{cutoff}"')
    cqls = [title_cql]
    # Ancestor scope only applies if an RFC index parent page is configured.
    if CONFLUENCE_RFC_PARENT_ID:
        cqls.insert(0, f'ancestor = "{CONFLUENCE_RFC_PARENT_ID}" AND type = "page" '
                       f'AND lastmodified >= "{cutoff}"')

    seen, merged = set(), []
    for cql in cqls:
        for r in _search_confluence_by_author(cql, person_name):
            key = r.get("url") or (r.get("title"), r.get("date"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)
    merged.sort(key=lambda x: x["date"], reverse=True)
    return merged


def _get_recent_blogs_for_person(person_name: str, days: int = 90) -> list:
    """Blog posts the person published in the last N days. In Confluence, blogs
    are type=blogpost (a distinct content type, not regular pages)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    cql = (f'space = "{CONFLUENCE_SPACE}" AND type = "blogpost" '
           f'AND created >= "{cutoff}"')
    return _search_confluence_by_author(cql, person_name)


def get_person_github_activity(person_name: str) -> str:
    """Get a team member's comprehensive activity for 1:1 prep: GitHub PRs (monthly,
    last 3 months), Jira work types (last 3 months), and Confluence contributions
    over the last 3 months — RFCs authored (live CQL by title), tech demos and
    blog posts (from cached folder).

    Args:
        person_name: Team member's display name as it appears in Jira (e.g. 'Alex Johnson')
    """
    try:
        import requests as _req

        gh_username = TEAM_MEMBERS.get(person_name)
        if not gh_username:
            available = ", ".join(TEAM_MEMBERS.keys()) if TEAM_MEMBERS else "none configured"
            return (
                f"No GitHub username mapped for '{person_name}'. "
                f"Available mappings: {available}."
            )

        jira = _get_jira()
        now = datetime.now(timezone.utc)

        # Probe GitHub once before doing any work. If the token is dead or the
        # org is unreachable, we degrade gracefully — skip the per-month queries,
        # skip the chart marker, and append a quiet caption later. The Jira and
        # Confluence portions of the 1:1 view still render normally.
        github_ok = is_github_healthy()
        gh = _get_github() if github_ok else None

        # --- GitHub: monthly counts (only when GitHub is healthy) ---
        months = []
        for i in range(3):
            m_start = (now.replace(day=1) - timedelta(days=30 * i)).replace(day=1)
            if i == 0:
                m_end = now
            else:
                m_end = (m_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            months.append((m_start.strftime("%Y-%m"), m_start.strftime("%Y-%m-%d"), m_end.strftime("%Y-%m-%d")))

        months.reverse()  # oldest first

        authored_by_month = {label: 0 for label, _, _ in months}
        reviewed_by_month = {label: 0 for label, _, _ in months}
        if github_ok:
            for label, start, end in months:
                try:
                    aq = f"author:{gh_username} {_gh_scope()} is:pr created:{start}..{end}"
                    authored_by_month[label] = gh.search_issues(aq).totalCount
                except Exception:
                    authored_by_month[label] = 0
                try:
                    rq = f"reviewed-by:{gh_username} {_gh_scope()} is:pr created:{start}..{end}"
                    reviewed_by_month[label] = gh.search_issues(rq).totalCount
                except Exception:
                    reviewed_by_month[label] = 0

        total_authored = sum(authored_by_month.values())
        total_reviewed = sum(reviewed_by_month.values())

        # --- Jira: Work type breakdown (last 3 months) ---
        three_months_ago = (now - timedelta(days=90)).strftime("%Y-%m-%d")
        projects = ", ".join(JIRA_PROJECTS)
        proj_clause = f"project in ({projects}) AND " if projects else ""
        jql = (
            f"assignee = '{person_name}' AND {proj_clause}updated >= '{three_months_ago}' "
            f"ORDER BY updated DESC"
        )
        jira_result = jira.jql(jql, limit=50)
        jira_issues = jira_result.get("issues", []) if isinstance(jira_result, dict) else jira_result

        work_types = {}  # Bug, Story, Task, Spike, RFC, etc.
        resolved_count = 0
        for issue in jira_issues:
            itype = issue["fields"].get("issuetype", {}).get("name", "Other")
            work_types[itype] = work_types.get(itype, 0) + 1
            if _is_resolved(issue["fields"]):
                resolved_count += 1

        # --- Confluence: RFCs (live, ancestor scope) + blogs (live, RD space) +
        # tech demos (still cached — needs parent page URL to go live too) ---
        rfcs_by_person = _get_recent_rfcs_for_person(person_name, days=90)
        blogs_by_person = _get_recent_blogs_for_person(person_name, days=90)

        # Tech demos still come from the pre-built cache file — the parent page
        # URL hasn't been wired yet, so this is the one Confluence surface that
        # may be stale until we replace it with a live query like the others.
        cache_path = os.path.join(os.path.dirname(__file__) or ".", "confluence_cache.json")
        recency_cutoff = (now - timedelta(days=90)).strftime("%Y-%m-%d")
        tech_demos_by_person = []
        try:
            with open(cache_path) as f:
                cache = json.load(f)
            for td in cache.get("tech_demos", []):
                if td.get("date", "") < recency_cutoff:
                    continue
                if person_name.lower() in td["author"].lower() or (
                    gh_username and gh_username.lower() in td["author"].lower()
                ):
                    tech_demos_by_person.append(td)
        except Exception:
            pass

        # --- Build output ---
        all_months = sorted(authored_by_month.keys())
        lines = []

        # Emit the chart marker ONLY when we have real GitHub data. When the
        # integration is down we'd otherwise render a chart of zeros, which
        # looks broken on screen. Quiet caption later instead.
        if github_ok:
            chart_data = json.dumps({
                "labels": all_months,
                "authored": [authored_by_month.get(m, 0) for m in all_months],
                "reviewed": [reviewed_by_month.get(m, 0) for m in all_months],
                "person": person_name,
            })
            lines.append(f"<!--PERSONCHART:{chart_data}-->")

        lines.append(f"=== 1:1 INSIGHTS: {person_name} (@{gh_username}) ===\n")

        # GitHub summary — only when the integration is healthy. The current
        # month is a partial window (month-to-date); we label it explicitly so
        # neither human readers nor the LLM mistake the partial number for a
        # full-month value and infer a phantom "decline."
        current_month_label = now.strftime("%Y-%m")
        today_str = now.strftime("%b %d")
        if github_ok:
            lines.append(f"💻 GITHUB (last 3 months): {total_authored} PRs authored, {total_reviewed} PRs reviewed")
            for m in all_months:
                a = authored_by_month.get(m, 0)
                r = reviewed_by_month.get(m, 0)
                suffix = f"  (month-to-date as of {today_str})" if m == current_month_label else ""
                lines.append(f"  {m}: {a} authored, {r} reviewed{suffix}")
            lines.append("")

            # Collaboration ratio
            if total_authored > 0:
                ratio = round(total_reviewed / total_authored, 1)
                lines.append(f"📊 COLLABORATION RATIO: {ratio}x ({total_reviewed} reviews / {total_authored} PRs)")
                if ratio >= 1.5:
                    lines.append("  ✅ Strong collaborator — reviews more than they author")
                elif ratio >= 0.8:
                    lines.append("  ✅ Good balance between authoring and reviewing")
                elif ratio < 0.5:
                    lines.append("  ⚠️ Low review participation — discuss team collaboration expectations")
            lines.append("")
        else:
            # Graceful caption — execs gloss over it, the EM knows what's up.
            lines.append("_💻 GitHub data temporarily unavailable — showing Jira-only view._\n")

        # Jira work types
        if work_types:
            lines.append(f"🎫 JIRA WORK TYPES (last 3 months): {len(jira_issues)} tickets, {resolved_count} resolved")
            for wtype, count in sorted(work_types.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {wtype}: {count}")

            # Strengths
            types_set = set(work_types.keys())
            strengths = []
            if "Story" in types_set and "Bug" in types_set:
                strengths.append("works across stories and bugs")
            if any(t in types_set for t in ("Spike", "RFC")):
                strengths.append("contributes to technical exploration (spikes/RFCs)")
            if work_types.get("Bug", 0) >= 3:
                strengths.append("reliable bug fixer")
            if work_types.get("Story", 0) >= 3:
                strengths.append("strong feature delivery")
            if strengths:
                lines.append(f"  💪 Strengths: {', '.join(strengths)}")
        lines.append("")

        # Confluence contributions (last 3 months) — RFCs, tech demos, blogs
        lines.append("📝 WIDER CONTRIBUTIONS (Confluence, last 3 months):")
        if rfcs_by_person:
            # Includes main RFC + sub-section pages (Technical Details / Engineering / etc.)
            lines.append(f"  📄 RFC contributions: {len(rfcs_by_person)}")
            for rfc in rfcs_by_person[:5]:
                lines.append(f"    - {rfc['date']}: {rfc['title']}")
        else:
            lines.append("  📄 RFC contributions: None in last 3 months")

        if tech_demos_by_person:
            lines.append(f"  🎤 Tech Demo folder pages: {len(tech_demos_by_person)}")
            for td in tech_demos_by_person[:3]:
                lines.append(f"    - {td['date']}: {td['title']}")
        else:
            lines.append("  🎤 Tech Demo folder pages: None in last 3 months")

        if blogs_by_person:
            lines.append(f"  📰 Blog posts: {len(blogs_by_person)}")
            for bl in blogs_by_person[:3]:
                lines.append(f"    - {bl['date']}: {bl['title']}")
        else:
            lines.append("  📰 Blog posts: None in last 3 months")
        lines.append("")

        # Raw data for AI to interpret (not displayed directly — AI synthesizes this into insights)
        lines.append("RAW DATA FOR AI INTERPRETATION (do not dump this — synthesize into insights):")
        if github_ok:
            lines.append(f"  Total authored: {total_authored}, Total reviewed: {total_reviewed}")
        else:
            lines.append("  GitHub data: UNAVAILABLE — omit any PR/collaboration claims; do not mention zero PRs.")
        lines.append(f"  Resolved tickets: {resolved_count}/{len(jira_issues)}")
        lines.append(f"  RFCs in last 3 months: {len(rfcs_by_person)}")
        lines.append(f"  Tech demos in last 3 months: {len(tech_demos_by_person)}")
        lines.append(f"  Blog posts in last 3 months: {len(blogs_by_person)}")
        if github_ok:
            # Trend lines exclude the partial current month — comparing partial to
            # complete is mathematically a phantom decline. Use the last two
            # COMPLETE months only.
            complete_months = [m for m in all_months if m != current_month_label]
            if len(complete_months) >= 2:
                m_prev, m_latest = complete_months[-2], complete_months[-1]
                lines.append(f"  PR trend (complete months only): "
                             f"{authored_by_month.get(m_prev, 0)} ({m_prev}) → "
                             f"{authored_by_month.get(m_latest, 0)} ({m_latest})")
                lines.append(f"  Review trend (complete months only): "
                             f"{reviewed_by_month.get(m_prev, 0)} ({m_prev}) → "
                             f"{reviewed_by_month.get(m_latest, 0)} ({m_latest})")
            lines.append(f"  Note: {current_month_label} is the current (partial) month "
                         f"— only {now.day} day(s) in. Do NOT use it to infer trends "
                         f"or flag declines.")

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching activity for {person_name}: {e}"


# ---------------------------------------------------------------------------
# Sprint Retro Intelligence
# ---------------------------------------------------------------------------

def get_sprint_retro_data(sprint_name: str = "", project: str = "") -> str:
    """Analyze a completed sprint for retrospective insights.
    Returns: completion rate, scope creep detection, carry-overs, cycle time patterns,
    and suggested retro discussion topics.

    Args:
        sprint_name: Name of the sprint to analyze. Leave empty for the most recently closed sprint.
        project: Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all configured projects.
    """
    try:
        jira = _get_jira()
        proj_list = _resolve_projects(project)
        projects = ", ".join(proj_list)
        now = datetime.now(timezone.utc)

        # Find the target sprint for the specified projects
        sprints = _get_sprints_from_all_boards(jira, state="closed", projects=proj_list)
        sprints = sorted(sprints, key=lambda s: s.get("endDate", ""), reverse=True)

        if not sprints:
            return "No closed sprints found."

        # Find the target sprint
        target = None
        if sprint_name:
            for s in sprints:
                if sprint_name.lower() in s.get("name", "").lower():
                    target = s
                    break
            if not target:
                available = ", ".join(s["name"] for s in sprints[:5])
                return f"Sprint '{sprint_name}' not found. Recent sprints: {available}"
        else:
            target = sprints[0]

        sid = target["id"]
        sname = target.get("name", f"Sprint {sid}")
        sprint_start = target.get("startDate", "")
        sprint_end = target.get("endDate", "")
        sprint_start_dt = datetime.fromisoformat(sprint_start.replace("Z", "+00:00")) if sprint_start else None
        sprint_end_dt = datetime.fromisoformat(sprint_end.replace("Z", "+00:00")) if sprint_end else None

        # Get all issues in the sprint with changelog (avoids N+1 API calls for cycle time)
        all_issues = jira.jql(
            f"project in ({projects}) AND sprint = {sid}",
            limit=100,
            expand="changelog"
        )
        issues = all_issues.get("issues", []) if isinstance(all_issues, dict) else all_issues

        if not issues:
            return f"No issues found in {sname}."

        # Analyze each issue
        done_tickets = []
        not_done_tickets = []
        scope_creep = []       # tickets added after sprint start
        carried_over = []      # tickets that were in previous sprints too
        cycle_times = []       # days from In Progress to Done
        assignee_stats = {}
        total_points = 0
        done_points = 0

        for issue in issues:
            fields = issue["fields"]
            key = issue["key"]
            summary = fields.get("summary", "")[:60]
            status = fields.get("status", {}).get("name", "")
            status_lower = status.lower()
            assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
            created = fields.get("created", "")
            points = fields.get("customfield_10013") or 0
            try:
                points = float(points)
            except (ValueError, TypeError):
                points = 0
            total_points += points

            if assignee not in assignee_stats:
                assignee_stats[assignee] = {"done": 0, "not_done": 0, "points_done": 0, "points_total": 0}
            assignee_stats[assignee]["points_total"] += points

            is_done = _is_resolved(issue["fields"])

            if is_done:
                done_tickets.append(key)
                done_points += points
                assignee_stats[assignee]["done"] += 1
                assignee_stats[assignee]["points_done"] += points
            else:
                not_done_tickets.append(f"{key} ({assignee}): {summary} [{status}]")
                assignee_stats[assignee]["not_done"] += 1

            # Scope creep: ticket created after sprint started
            if created and sprint_start_dt:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if created_dt > sprint_start_dt + timedelta(days=1):
                    scope_creep.append(f"{key} ({assignee}): {summary} — added {created[:10]}")

            # Carried over: ticket in multiple sprints
            sprint_field = fields.get("customfield_10020") or []
            sprint_count = len([s for s in sprint_field if isinstance(s, dict)])
            if sprint_count > 1 and not is_done:
                carried_over.append(f"{key} ({assignee}): {summary} — in {sprint_count} sprints")

            # Cycle time via changelog (already fetched via expand="changelog")
            try:
                changelog = issue.get("changelog", {}).get("histories", [])
                in_progress_date = None
                done_date = None
                for history in changelog:
                    for item in history.get("items", []):
                        if item.get("field") == "status":
                            to_status = (item.get("toString") or "").lower()
                            change_date = datetime.fromisoformat(
                                history["created"].replace("Z", "+00:00")
                            )
                            if to_status in ("in progress", "in development") and not in_progress_date:
                                in_progress_date = change_date
                            if to_status in ("done", "closed", "resolved", "completed"):
                                done_date = change_date
                if in_progress_date and done_date and done_date > in_progress_date:
                    ct = (done_date - in_progress_date).days
                    cycle_times.append({"key": key, "days": ct, "assignee": assignee})
            except Exception:
                pass  # changelog not available, skip cycle time for this ticket

        # Build report
        total = len(issues)
        done_count = len(done_tickets)
        completion_rate = round(done_count / total * 100) if total else 0

        lines = [f"=== SPRINT RETRO: {sname} ==="]
        lines.append(f"Period: {sprint_start[:10] if sprint_start else '?'} to {sprint_end[:10] if sprint_end else '?'}\n")

        # Completion summary
        lines.append(f"📊 COMPLETION: {done_count}/{total} tickets ({completion_rate}%) | {done_points}/{total_points} pts")
        lines.append("")

        # Scope creep
        if scope_creep:
            lines.append(f"🔀 SCOPE CREEP — {len(scope_creep)} ticket(s) added mid-sprint:")
            for t in scope_creep[:8]:
                lines.append(f"  {t}")
            lines.append("")

        # Carry-overs
        if carried_over:
            lines.append(f"🔁 CARRY-OVERS — {len(carried_over)} ticket(s) have been in multiple sprints:")
            for t in carried_over[:8]:
                lines.append(f"  {t}")
            lines.append("")

        # Not done
        if not_done_tickets:
            lines.append(f"❌ NOT COMPLETED ({len(not_done_tickets)}):")
            for t in not_done_tickets[:10]:
                lines.append(f"  {t}")
            lines.append("")

        # Cycle time
        if cycle_times:
            avg_ct = round(sum(c["days"] for c in cycle_times) / len(cycle_times), 1)
            fastest = min(cycle_times, key=lambda c: c["days"])
            slowest = max(cycle_times, key=lambda c: c["days"])
            lines.append(f"⏱️ CYCLE TIME (In Progress → Done):")
            lines.append(f"  Average: {avg_ct} days ({len(cycle_times)} tickets measured)")
            lines.append(f"  Fastest: {fastest['key']} — {fastest['days']}d ({fastest['assignee']})")
            lines.append(f"  Slowest: {slowest['key']} — {slowest['days']}d ({slowest['assignee']})")
            lines.append("")

        # Per-person breakdown
        lines.append("👥 PER-PERSON:")
        for person, stats in sorted(assignee_stats.items(), key=lambda x: x[1]["done"], reverse=True):
            person_total = stats["done"] + stats["not_done"]
            person_rate = round(stats["done"] / person_total * 100) if person_total else 0
            lines.append(
                f"  {person}: {stats['done']}/{person_total} done ({person_rate}%) "
                f"| {stats['points_done']}/{stats['points_total']} pts"
            )
        lines.append("")

        # Suggested discussion topics
        lines.append("💡 SUGGESTED RETRO TOPICS:")
        if completion_rate < 70:
            lines.append(f"  - Completion rate was {completion_rate}% — what prevented us from finishing?")
        if scope_creep:
            lines.append(f"  - {len(scope_creep)} tickets added mid-sprint — is our sprint planning realistic?")
        if carried_over:
            lines.append(f"  - {len(carried_over)} tickets carried over — should we break these down smaller?")
        if cycle_times:
            avg_ct = round(sum(c["days"] for c in cycle_times) / len(cycle_times), 1)
            if avg_ct > 5:
                lines.append(f"  - Average cycle time is {avg_ct} days — what's slowing us down?")
        # Check for unbalanced workload
        if assignee_stats:
            loads = [s["done"] + s["not_done"] for s in assignee_stats.values()]
            if max(loads) > 2 * min(loads) and min(loads) > 0:
                lines.append("  - Workload is uneven — should we redistribute better next sprint?")
        if not any(line.startswith("  -") for line in lines[-6:]):
            lines.append("  - What went well? What should we keep doing?")
            lines.append("  - What's one thing we can improve next sprint?")

        return "\n".join(lines)

    except Exception as e:
        return f"Error generating retro data: {e}"


# ---------------------------------------------------------------------------
# Sprint Comparison Charts
# ---------------------------------------------------------------------------

def get_sprint_charts(num_sprints: int = 4, project: str = "") -> str:
    """Get sprint comparison data for the last N sprints with chart-ready data.
    Returns completion rates, spillovers, and per-person stats across sprints,
    plus a 'Wins' section highlighting improvements. Includes chart markers
    that the frontend renders as visual charts.

    Args:
        num_sprints: Number of sprints to compare. Default 4.
        project: Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all.
    """
    try:
        jira = _get_jira()
        proj_list = _resolve_projects(project)
        projects = ", ".join(proj_list)

        sprints = _get_sprints_from_all_boards(jira, state="closed", projects=proj_list)
        sprints = sorted(sprints, key=lambda s: s.get("endDate", ""), reverse=True)[:num_sprints]

        if not sprints:
            return f"No closed sprints found for {projects}."

        # Reverse so oldest is first (for chart X-axis)
        sprints = list(reversed(sprints))

        sprint_data = []
        for sprint in sprints:
            sid = sprint["id"]
            name = sprint.get("name", f"Sprint {sid}")
            # Shorten name for chart labels
            short_name = name.split(" - ")[-1] if " - " in name else name
            short_name = re.sub(r'^[A-Za-z][A-Za-z0-9]*[-\s]', '', short_name)[:20]

            result = jira.jql(f"project in ({projects}) AND sprint = {sid}", limit=100)
            issues = result.get("issues", []) if isinstance(result, dict) else result

            done = 0
            not_done = 0
            blocked = 0
            scope_added = 0
            assignee_done = {}

            sprint_start = sprint.get("startDate", "")
            sprint_start_dt = None
            if sprint_start:
                try:
                    sprint_start_dt = datetime.fromisoformat(sprint_start.replace("Z", "+00:00"))
                except Exception:
                    pass

            for issue in issues:
                fields = issue["fields"]
                assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
                status_lower = (fields.get("status", {}).get("name", "") or "").lower()

                if _is_resolved(fields):
                    done += 1
                    assignee_done[assignee] = assignee_done.get(assignee, 0) + 1
                else:
                    not_done += 1
                    if status_lower in ("blocked", "on hold"):
                        blocked += 1

                # Scope creep: created after sprint started
                created = fields.get("created", "")
                if created and sprint_start_dt:
                    try:
                        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if created_dt > sprint_start_dt + timedelta(days=1):
                            scope_added += 1
                    except Exception:
                        pass

            total = done + not_done
            completion = round(done / total * 100) if total else 0

            sprint_data.append({
                "name": short_name,
                "total": total,
                "done": done,
                "not_done": not_done,
                "completion": completion,
                "spillovers": not_done,
                "blocked": blocked,
                "scope_added": scope_added,
                "assignee_done": assignee_done,
            })

        # Build burndown for the latest sprint using Jira's sprint report API
        import requests as _requests
        burndown_labels = []
        burndown_ideal = []
        burndown_actual = []
        latest_sprint = sprints[-1]
        ls_start = latest_sprint.get("startDate", "")
        ls_end = latest_sprint.get("endDate", "")
        latest_sid = latest_sprint["id"]

        # Find the board ID for this sprint
        board_id = latest_sprint.get("_board_project", "")
        boards = _get_boards_for_projects(jira, proj_list)
        bid = boards[0]["id"] if boards else None

        if ls_start and ls_end and bid:
            try:
                start_dt = datetime.fromisoformat(ls_start.replace("Z", "+00:00")).date()
                end_dt = datetime.fromisoformat(ls_end.replace("Z", "+00:00")).date()
                latest_total = sprint_data[-1]["total"]

                # Use Jira's burndown chart API
                resp = _requests.get(
                    f"{JIRA_URL}/rest/greenhopper/1.0/rapid/charts/scopechangeburndownchart",
                    params={"rapidViewId": bid, "sprintId": latest_sid},
                    auth=(JIRA_EMAIL, JIRA_API_TOKEN),
                    headers={"Accept": "application/json"},
                    timeout=(5, 15),
                )
                if resp.status_code == 200:
                    bd_data = resp.json()
                    changes = bd_data.get("changes", {})
                    bd_start_ts = bd_data.get("startTime", 0)

                    # Replay membership and done-status separately so we can:
                    #  - exclude tickets that were REMOVED from the sprint (descoped)
                    #  - ignore statC (estimate) noise that previously flipped state
                    #  - count remaining = currently in sprint AND not in a done column
                    issue_in_sprint = {}   # key -> bool
                    issue_done = {}        # key -> bool
                    sorted_entries = sorted(changes.items(), key=lambda x: int(x[0]))

                    daily_remaining = {}   # date -> latest end-of-event remaining count
                    initial_remaining = 0

                    for ts_str, items in sorted_entries:
                        ts_int = int(ts_str)
                        for item in items:
                            key = item.get("key", "")
                            if not key:
                                continue
                            # 'added' carries explicit True/False — handle removals too.
                            if "added" in item:
                                in_sprint = bool(item["added"])
                                issue_in_sprint[key] = in_sprint
                                if in_sprint:
                                    issue_done.setdefault(key, False)
                            # Column transitions are the authoritative status signal.
                            if "column" in item:
                                issue_done[key] = bool(item["column"].get("done"))
                                issue_in_sprint.setdefault(key, True)
                            # statC = story-point/estimate change. INTENTIONALLY ignored
                            # for state — it doesn't represent a status transition, and
                            # using its 'done' field caused divergence from Jira's
                            # native burndown.

                        remaining = sum(
                            1 for k, in_sprint in issue_in_sprint.items()
                            if in_sprint and not issue_done.get(k, False)
                        )
                        day = datetime.fromtimestamp(ts_int / 1000, tz=timezone.utc).date()
                        if ts_int <= bd_start_ts:
                            initial_remaining = remaining
                        else:
                            # Latest event on `day` wins — that's the end-of-day snapshot.
                            daily_remaining[day] = remaining

                    # Working days for the chart x-axis
                    current = start_dt
                    working_days = []
                    while current <= end_dt:
                        if current.weekday() < 5:
                            working_days.append(current)
                        current += timedelta(days=1)

                    total_work_days = len(working_days) or 1
                    # Don't silently switch to the post-creep total — that produces a
                    # wrong baseline. Keep the API's view; fall back only if missing.
                    scope = initial_remaining if initial_remaining > 0 else latest_total

                    # Day 0
                    burndown_labels.append(start_dt.strftime("%b %d"))
                    burndown_ideal.append(float(scope))
                    burndown_actual.append(scope)

                    # "As-of" lookup so weekend events propagate forward to Monday.
                    sorted_event_days = sorted(daily_remaining.keys())
                    last_remaining = scope
                    event_idx = 0
                    for i, day in enumerate(working_days):
                        # Advance through every event-day with date <= this working day.
                        while event_idx < len(sorted_event_days) and sorted_event_days[event_idx] <= day:
                            last_remaining = daily_remaining[sorted_event_days[event_idx]]
                            event_idx += 1
                        burndown_labels.append(day.strftime("%b %d"))
                        burndown_ideal.append(round(scope * (1 - (i + 1) / total_work_days), 1))
                        burndown_actual.append(last_remaining)
            except Exception:
                pass

        # Build chart data markers (frontend parses these)
        labels = [s["name"] for s in sprint_data]
        completions = [s["completion"] for s in sprint_data]
        spillovers = [s["spillovers"] for s in sprint_data]
        committed = [s["total"] for s in sprint_data]
        completed = [s["done"] for s in sprint_data]
        scope_creep = [s["scope_added"] for s in sprint_data]

        chart_json = json.dumps({
            "labels": labels,
            "completion": completions,
            "spillovers": spillovers,
            "committed": committed,
            "completed": completed,
            "scope_creep": scope_creep,
            "burndown_labels": burndown_labels,
            "burndown_ideal": burndown_ideal,
            "burndown_actual": burndown_actual,
        })

        lines = [f"<!--SPRINTCHART:{chart_json}-->"]
        lines.append("")

        # Summary table
        lines.append("| Sprint | Committed | Completed | Spillovers | Completion | Scope Creep |")
        lines.append("|--------|-----------|-----------|------------|------------|-------------|")
        for s in sprint_data:
            lines.append(f"| {s['name']} | {s['total']} | {s['done']} | {s['spillovers']} | {s['completion']}% | {s['scope_added']} |")

        # WINS section — compare latest sprint vs previous
        lines.append("")
        lines.append("🏆 WINS & TRENDS:")
        if len(sprint_data) >= 2:
            latest = sprint_data[-1]
            prev = sprint_data[-2]

            # Completion trend
            if latest["completion"] > prev["completion"]:
                diff = latest["completion"] - prev["completion"]
                lines.append(f"  ✅ Completion rate improved: {prev['completion']}% → {latest['completion']}% (+{diff}%)")
            elif latest["completion"] < prev["completion"]:
                diff = prev["completion"] - latest["completion"]
                lines.append(f"  ⚠️ Completion rate dropped: {prev['completion']}% → {latest['completion']}% (-{diff}%)")
            else:
                lines.append(f"  ➡️ Completion rate steady at {latest['completion']}%")

            # Spillover trend
            if latest["spillovers"] < prev["spillovers"]:
                diff = prev["spillovers"] - latest["spillovers"]
                lines.append(f"  ✅ Fewer spillovers: {prev['spillovers']} → {latest['spillovers']} ({diff} fewer)")
            elif latest["spillovers"] > prev["spillovers"]:
                diff = latest["spillovers"] - prev["spillovers"]
                lines.append(f"  ⚠️ More spillovers: {prev['spillovers']} → {latest['spillovers']} ({diff} more)")

            # Scope creep trend
            if latest["scope_added"] < prev["scope_added"]:
                lines.append(f"  ✅ Less scope creep: {prev['scope_added']} → {latest['scope_added']} tickets added mid-sprint")
            elif latest["scope_added"] > prev["scope_added"]:
                lines.append(f"  ⚠️ More scope creep: {prev['scope_added']} → {latest['scope_added']} tickets added mid-sprint")

            # Throughput trend
            if latest["done"] > prev["done"]:
                lines.append(f"  ✅ Higher throughput: completed {latest['done']} vs {prev['done']} last sprint")

            # Top contributor in latest sprint
            if latest["assignee_done"]:
                top = max(latest["assignee_done"].items(), key=lambda x: x[1])
                lines.append(f"  🌟 {top[0]} completed the most tickets ({top[1]}) in the latest sprint")

        # Multi-sprint trends
        if len(sprint_data) >= 3:
            avg_completion = round(sum(s["completion"] for s in sprint_data) / len(sprint_data))
            avg_spillover = round(sum(s["spillovers"] for s in sprint_data) / len(sprint_data), 1)
            lines.append(f"  📊 Average across {len(sprint_data)} sprints: {avg_completion}% completion, {avg_spillover} spillovers/sprint")

            # Is the trend improving?
            first_half = sprint_data[:len(sprint_data)//2]
            second_half = sprint_data[len(sprint_data)//2:]
            avg_first = sum(s["completion"] for s in first_half) / len(first_half)
            avg_second = sum(s["completion"] for s in second_half) / len(second_half)
            if avg_second > avg_first + 3:
                lines.append(f"  📈 Upward trend: completion improving over the last {len(sprint_data)} sprints")
            elif avg_first > avg_second + 3:
                lines.append(f"  📉 Downward trend: completion declining — worth discussing in retro")

        return "\n".join(lines)

    except Exception as e:
        return f"Error generating sprint charts: {e}"


# ---------------------------------------------------------------------------
# Google Chat Integration
# ---------------------------------------------------------------------------

def _ago(when) -> str:
    """Compact relative-time formatter for PR engagement timestamps."""
    if not when:
        return ""
    now = datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    secs = int((now - when).total_seconds())
    if secs < 60:   return "just now"
    if secs < 3600: return f"{secs // 60}m ago"
    if secs < 86400: return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _enrich_pr_with_reviews(pr_url: str) -> dict:
    """Fetch full reviewer engagement for one PR from GitHub. Designed to run
    inside a ThreadPoolExecutor so we can enrich every open sprint PR in parallel.

    Returns: {
      "by_user": {login: {"name", "state", "comments", "last_activity"}},
      "approved_overall": bool,   # at least one approval, no outstanding changes
      "needs_changes": bool,      # at least one CHANGES_REQUESTED still standing
      "author": login,            # PR author (excluded from engagement)
      "error": Optional[str],
    }
    Review states: APPROVED, CHANGES_REQUESTED, COMMENTED. Latest review per
    user wins; any inline/general comment also bumps the comment counter and
    falls back to COMMENTED if the user hasn't formally reviewed."""
    out = {"by_user": {}, "approved_overall": False, "needs_changes": False,
           "author": None, "last_commit_at": None, "stale_reviewers": [],
           "error": None}
    try:
        parts = pr_url.replace("https://github.com/", "").split("/")
        if len(parts) < 4:
            out["error"] = "bad PR url"
            return out
        gh = _get_github()
        gh_pr = gh.get_repo(f"{parts[0]}/{parts[1]}").get_pull(int(parts[3]))
        out["author"] = gh_pr.user.login if gh_pr.user else None

        users = {}

        def _bump(login, name, when, kind, state=None):
            if not login:
                return
            u = users.setdefault(login, {"name": name or login, "state": None,
                                         "comments": 0, "last_activity": None})
            if kind == "review" and state:
                u["state"] = state  # caller passes latest review per user already
            if kind in ("issue_comment", "review_comment"):
                u["comments"] += 1
                if not u["state"]:
                    u["state"] = "COMMENTED"
            if when and (u["last_activity"] is None or when > u["last_activity"]):
                u["last_activity"] = when

        # Reviews — keep only the LATEST per user (an approval after a request
        # for changes should win, etc.). Includes COMMENTED reviews.
        latest_review_per_user = {}
        for r in gh_pr.get_reviews():
            if not r.user:
                continue
            login = r.user.login
            t = r.submitted_at
            prev = latest_review_per_user.get(login)
            if not prev or (t and prev[1] and t > prev[1]) or (t and not prev[1]):
                latest_review_per_user[login] = (r.state, t, r.user.name)
        for login, (state, t, name) in latest_review_per_user.items():
            _bump(login, name, t, "review", state=state)

        # General conversation comments (PR-level)
        for c in gh_pr.get_issue_comments():
            if c.user:
                _bump(c.user.login, c.user.name, c.created_at, "issue_comment")

        # Inline code-review comments
        for c in gh_pr.get_review_comments():
            if c.user:
                _bump(c.user.login, c.user.name, c.created_at, "review_comment")

        # PR author doesn't review themselves — strip them from engagement
        if out["author"]:
            users.pop(out["author"], None)

        # Last commit timestamp — used to detect reviews that went stale because
        # the author pushed fixes after the reviewer's last engagement.
        last_commit_at = None
        try:
            for c in gh_pr.get_commits():
                d = c.commit.committer.date if c.commit and c.commit.committer else None
                if d is None:
                    continue
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                if last_commit_at is None or d > last_commit_at:
                    last_commit_at = d
        except Exception:
            pass
        out["last_commit_at"] = last_commit_at

        # Mark COMMENTED / CHANGES_REQUESTED reviewers whose last activity
        # predates the latest commit — they reviewed, code got updated since,
        # they haven't come back. Approvals don't go stale.
        if last_commit_at:
            for login, info in users.items():
                if info.get("state") in ("COMMENTED", "CHANGES_REQUESTED") and info.get("last_activity"):
                    la = info["last_activity"]
                    if la.tzinfo is None:
                        la = la.replace(tzinfo=timezone.utc)
                    if la < last_commit_at:
                        info["stale"] = True
                        out["stale_reviewers"].append(login)

        approved = [u for u, info in users.items() if info["state"] == "APPROVED"]
        changes  = [u for u, info in users.items() if info["state"] == "CHANGES_REQUESTED"]
        out["by_user"] = users
        out["approved_overall"] = bool(approved) and not changes
        out["needs_changes"] = bool(changes)
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def _format_engagement(by_user: dict) -> list:
    """Render the per-reviewer lines for a PR. Sorted by 'most-recent activity'."""
    icon = {"APPROVED": "✅", "CHANGES_REQUESTED": "❌", "COMMENTED": "💬"}
    label = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested",
             "COMMENTED": "commented"}
    ordered = sorted(by_user.items(),
                     key=lambda x: x[1].get("last_activity") or datetime.min.replace(tzinfo=timezone.utc),
                     reverse=True)
    out = []
    for login, info in ordered:
        st = info.get("state") or "COMMENTED"
        bits = [f"{icon.get(st, '💬')} {info.get('name') or login} — {label.get(st, 'engaged')}"]
        if info.get("last_activity"):
            bits.append(_ago(info["last_activity"]))
        if info.get("comments"):
            bits.append(f"{info['comments']} comment{'s' if info['comments'] != 1 else ''}")
        if info.get("stale"):
            # The author pushed fixes after this person's last review — they
            # haven't come back to look. Most actionable nudge for the EM.
            bits.append("⚠️ code updated since · awaiting re-review")
        out.append("    " + " · ".join(bits))
    return out


def get_pending_prs(project: str = "") -> str:
    """Get all open/pending PRs for the current sprint with rich per-reviewer
    engagement: who has approved, who has requested changes, who has commented,
    and — most actionably — which reviewers' last review is now stale because
    the author pushed fixes after them.

    Architecture:
      1. Jira dev-status gives the list of OPEN PRs + assigned reviewers (no
         GitHub creds needed for this step).
      2. GitHub is queried per PR IN PARALLEL for reviews + general comments +
         inline code comments + the latest commit timestamp.

    Args:
        project: Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all.
    """
    try:
        import requests as _req

        jira = _get_jira()
        proj_list = _resolve_projects(project)
        issues, sprint_label = _get_current_sprint_jql(jira, proj_list, "")
        if not issues:
            return f"No issues found in sprint for {', '.join(proj_list)}."

        # --- Pass 1: collect open PRs from Jira dev-panel (no GitHub calls) ---
        pr_records = []  # list of dicts
        for issue in issues:
            fields = issue["fields"]
            if _is_resolved(fields):
                continue
            issue_id = issue.get("id")
            try:
                resp = _req.get(
                    f"{JIRA_URL}/rest/dev-status/latest/issue/detail",
                    params={"issueId": issue_id, "applicationType": "GitHub", "dataType": "pullrequest"},
                    auth=(JIRA_EMAIL, JIRA_API_TOKEN),
                    headers={"Accept": "application/json"},
                    timeout=(5, 15),
                )
                if resp.status_code != 200:
                    continue
                for detail in resp.json().get("detail", []):
                    for pr in detail.get("pullRequests", []):
                        if pr.get("status", "") != "OPEN":
                            continue
                        pr_records.append({
                            "key": issue["key"],
                            "status": fields.get("status", {}).get("name", ""),
                            "assignee": (fields.get("assignee") or {}).get("displayName", "Unassigned"),
                            "pr_id": pr.get("id", "").lstrip("#"),
                            "pr_name": pr.get("name", "")[:80],
                            "pr_url": pr.get("url", ""),
                            "author": pr.get("author", {}).get("name", "Unknown"),
                            "assigned_reviewers": [r.get("name", "") for r in pr.get("reviewers", [])],
                        })
            except Exception:
                continue

        # --- Pass 2: parallel GitHub enrichment (skipped if GitHub is down) ---
        enrichments = {}  # pr_url -> enrichment dict
        github_ok = is_github_healthy()
        if github_ok and pr_records:
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {pool.submit(_enrich_pr_with_reviews, r["pr_url"]): r["pr_url"]
                           for r in pr_records if r["pr_url"]}
                for future in as_completed(futures):
                    pr_url = futures[future]
                    try:
                        enrichments[pr_url] = future.result()
                    except Exception:
                        enrichments[pr_url] = {"error": "enrichment failed"}

        # --- Pass 3: render ---
        lines = [f"=== PENDING PRs: {sprint_label} ===\n"]
        needs_review, approved_not_merged, awaiting_rereview = [], [], []
        pr_count = 0

        for rec in pr_records:
            enr = enrichments.get(rec["pr_url"], {})
            # Skip approved PRs from the main list — they show in their own callout.
            if enr.get("approved_overall"):
                approvers = [info.get("name") or login
                             for login, info in enr.get("by_user", {}).items()
                             if info.get("state") == "APPROVED"]
                approved_not_merged.append(
                    f"{rec['key']} PR #{rec['pr_id']} — approved by {', '.join(approvers) or '?'}"
                )
                continue

            pr_count += 1
            engagement_lines = _format_engagement(enr.get("by_user", {})) if enr.get("by_user") else []

            # Short header for this PR
            block = [
                f"📌 {rec['key']} [{rec['status']}] — {rec['assignee']}",
                f"  PR #{rec['pr_id']}: {rec['pr_name']}",
                f"  Author: {rec['author']}",
                f"  Assigned reviewers: {', '.join(rec['assigned_reviewers']) or 'none'}",
            ]
            if not github_ok:
                block.append("  _Review activity unavailable — GitHub temporarily offline._")
            elif enr.get("error"):
                block.append("  _Review activity unavailable for this PR._")
            elif engagement_lines:
                block.append("  Review activity:")
                block.extend(engagement_lines)
            else:
                block.append("  Review activity: ⏳ no reviews or comments yet")

            block.append(f"  URL: {rec['pr_url']}")
            lines.append("\n".join(block))
            lines.append("")

            # Categorise for the summary
            if enr.get("stale_reviewers"):
                stale_names = [enr["by_user"][u].get("name") or u for u in enr["stale_reviewers"]]
                awaiting_rereview.append(
                    f"{rec['key']} PR #{rec['pr_id']} — {rec['author']} pushed fixes; "
                    f"waiting on {', '.join(stale_names)} to re-review"
                )
            elif enr.get("needs_changes"):
                needs_review.append(f"{rec['key']} PR #{rec['pr_id']} ({rec['assignee']}) — changes requested")
            else:
                needs_review.append(f"{rec['key']} PR #{rec['pr_id']} ({rec['assignee']}) — awaiting first review")

        if pr_count == 0 and not approved_not_merged:
            lines.append("No open PRs found in this sprint.")
            return "\n".join(lines)

        lines.append(f"\n📊 SUMMARY: {pr_count + len(approved_not_merged)} open PRs across the sprint")
        if awaiting_rereview:
            # Most actionable: someone commented/asked for changes, fixes are in, ball is in their court.
            lines.append(f"🔁 AWAITING RE-REVIEW ({len(awaiting_rereview)}):")
            for ar in awaiting_rereview:
                lines.append(f"  {ar}")
        if needs_review:
            lines.append(f"⏳ NEEDS REVIEW ({len(needs_review)}):")
            for nr in needs_review:
                lines.append(f"  {nr}")
        if approved_not_merged:
            lines.append(f"✅ APPROVED BUT NOT MERGED ({len(approved_not_merged)}):")
            for am in approved_not_merged:
                lines.append(f"  {am}")

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching pending PRs: {e}"


def send_to_gchat(message: str) -> str:
    """Send a message to the team's Google Chat space via webhook.
    Use this when the manager asks to share a briefing, status update, or summary with the team.

    Args:
        message: The message text to send to Google Chat. Supports basic formatting.
    """
    import requests

    if not GCHAT_WEBHOOK_URL:
        return "Google Chat webhook not configured. Add GCHAT_WEBHOOK_URL to .env to enable this feature."

    try:
        # Google Chat webhook accepts simple text or card messages
        payload = {"text": message}
        resp = requests.post(
            GCHAT_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=(5, 15),
        )
        if resp.status_code == 200:
            return "Message sent to Google Chat successfully."
        else:
            return f"Failed to send to Google Chat: {resp.status_code} — {resp.text[:200]}"
    except Exception as e:
        return f"Error sending to Google Chat: {e}"


# ---------------------------------------------------------------------------
# Apply the resilience wrapper to read-only tools (write tools stay unwrapped
# so they always hit live and never silently serve a cached side effect).
# ---------------------------------------------------------------------------
for _name in (
    "get_jira_issue", "search_sprint", "get_github_prs", "get_sprint_history",
    "predict_spillovers", "get_morning_briefing", "get_person_summary",
    "get_person_github_activity", "get_sprint_retro_data", "get_team_availability",
    "get_sprint_charts", "get_pending_prs",
):
    globals()[_name] = _resilient(globals()[_name])
