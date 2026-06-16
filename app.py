import os
import json
import base64
import asyncio
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

import tools as t
from tools import (
    get_jira_issue, create_jira_ticket, search_sprint, get_github_prs,
    update_jira_status, add_jira_comment, attach_to_jira,
    get_sprint_history, predict_spillovers, score_predictions, get_prediction_track_record,
    get_morning_briefing, get_person_summary, get_person_github_activity,
    get_sprint_retro_data, get_team_availability, get_sprint_charts, get_pending_prs, send_to_gchat,
)
from prompts import build_system_prompt
from user_config import get_current_user, list_users, get_team_roster_text
from db import get_memory_context, save_note, save_oneone, save_prediction, get_db, init_db, get_prediction_accuracy

MODELS = ["gpt-4o", "gpt-4o-mini"]

# --- OpenAI tool definitions (JSON schema) ---

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_jira_issue",
            "description": "Get full details, status, assignee, and recent comments for a Jira ticket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Jira issue key e.g. PROJ-1234"}
                },
                "required": ["issue_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_jira_ticket",
            "description": "Create a new Jira ticket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "One-line ticket title"},
                    "description": {"type": "string", "description": "Detailed description"},
                    "issue_type": {"type": "string", "description": "Bug, Story, Task, or Improvement. Default is Bug.", "default": "Bug"},
                    "project": {"type": "string", "description": "Jira project key e.g. PROJ1. Uses default if not specified.", "default": ""},
                },
                "required": ["summary", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_sprint",
            "description": "Search Jira issues using JQL. Use for sprint health, assignee workload, blocked items.",
            "parameters": {
                "type": "object",
                "properties": {
                    "jql": {"type": "string", "description": "Valid JQL string e.g. 'project in (PROJ1, PROJ2) AND sprint in openSprints()'"}
                },
                "required": ["jql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_github_prs",
            "description": "Find open or recently merged GitHub PRs related to a Jira ticket key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_key": {"type": "string", "description": "Jira issue key to search for e.g. PROJ-1234"}
                },
                "required": ["ticket_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_jira_status",
            "description": "Transition a Jira ticket to a new status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Jira issue key e.g. PROJ-1234"},
                    "new_status": {"type": "string", "description": "Target status name e.g. 'In Review', 'In Progress', 'Done', 'To Do'"},
                },
                "required": ["issue_key", "new_status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_jira_comment",
            "description": "Add a comment to a Jira ticket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Jira issue key e.g. PROJ-1234"},
                    "comment": {"type": "string", "description": "Comment text to add"},
                },
                "required": ["issue_key", "comment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "attach_to_jira",
            "description": "Attach a file to a Jira ticket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Jira issue key e.g. PROJ-567"},
                    "filename": {"type": "string", "description": "Filename for the attachment e.g. screenshot.png"},
                    "image_base64": {"type": "string", "description": "Base64-encoded image content"},
                },
                "required": ["issue_key", "filename", "image_base64"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sprint_history",
            "description": "Get historical sprint data for the last N closed sprints. Returns completion rates, spillover counts, and velocity per sprint.",
            "parameters": {
                "type": "object",
                "properties": {
                    "num_sprints": {"type": "integer", "description": "Number of past sprints to analyze. Default 5.", "default": 5},
                    "project": {"type": "string", "description": "Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all projects.", "default": ""}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "predict_spillovers",
            "description": "Analyze the current sprint and predict which tickets are at risk of spilling over.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all projects.", "default": ""}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "score_predictions",
            "description": "Grade past spillover predictions against what actually happened, for sprints that have since closed. Updates Sprint Scribe's accuracy track record. Run after a sprint closes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all.", "default": ""}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_prediction_track_record",
            "description": "Show how accurate Sprint Scribe's spillover predictions have been over time. Use when asked 'how accurate are you?', 'what's your track record?', or for proof the predictions are trustworthy.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_morning_briefing",
            "description": "Generate a morning risk briefing by cross-referencing Jira and GitHub. Surfaces stale tickets, In Progress with no PR, aging PRs without review, blocked items, and workload imbalance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all projects.", "default": ""}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_person_summary",
            "description": "Get a comprehensive summary of a team member's work for 1:1 prep. Shows current sprint tickets, completion rate, carried-over items.",
            "parameters": {
                "type": "object",
                "properties": {
                    "person_name": {"type": "string", "description": "Team member's display name as it appears in Jira (e.g. 'Alex Johnson')"},
                    "project": {"type": "string", "description": "Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all projects.", "default": ""}
                },
                "required": ["person_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_person_github_activity",
            "description": "Get a team member's recent GitHub activity: PRs authored, reviews given, and collaboration ratio. Requires the person to be mapped to a GitHub username in your team config (configs/<name>.yaml).",
            "parameters": {
                "type": "object",
                "properties": {
                    "person_name": {"type": "string", "description": "Team member's display name as it appears in Jira (e.g. 'Alex Johnson')"}
                },
                "required": ["person_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sprint_retro_data",
            "description": "Analyze a completed sprint for retrospective insights. Returns completion rate, scope creep, carry-overs, cycle time patterns, and suggested discussion topics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sprint_name": {"type": "string", "description": "Name of the sprint to analyze. Leave empty for the most recently closed sprint.", "default": ""},
                    "project": {"type": "string", "description": "Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all projects.", "default": ""}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_availability",
            "description": "Get team workload and availability to suggest who can pick up PR reviews or extra work. Shows ticket count, story points remaining, and pending PR reviews per person.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all projects.", "default": ""}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sprint_charts",
            "description": "Get sprint comparison data with charts for the last N sprints. Shows completion rates, spillovers, scope creep, and a Wins section highlighting improvements. Returns visual chart data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "num_sprints": {"type": "integer", "description": "Number of sprints to compare. Default 4.", "default": 4},
                    "project": {"type": "string", "description": "Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all.", "default": ""}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pending_prs",
            "description": "Get all open/pending PRs for the current sprint. Shows which tickets have PRs raised, review status (approved, changes requested, waiting), and who needs to review. Uses Jira's dev panel + GitHub review data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Jira project key e.g. 'PROJ1' or 'PROJ2'. Leave empty for all.", "default": ""}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_to_gchat",
            "description": "Send a message to the team's Google Chat space. Use when the manager asks to share a briefing, status, or summary with the team.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The message text to send to Google Chat. Format it nicely for the team to read."}
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "Save a note to memory that Sprint Scribe will remember in future conversations. Use when the manager says 'remember this', 'note that', or wants to save a decision or observation for later.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The note to remember. Keep it concise and actionable."},
                    "category": {"type": "string", "description": "Category: 'decision', 'observation', 'action', or 'general'. Default is 'general'.", "default": "general"},
                },
                "required": ["content"],
            },
        },
    },
]

TOOL_DISPATCH = {
    "get_jira_issue": get_jira_issue,
    "create_jira_ticket": create_jira_ticket,
    "search_sprint": search_sprint,
    "get_github_prs": get_github_prs,
    "update_jira_status": update_jira_status,
    "add_jira_comment": add_jira_comment,
    "attach_to_jira": attach_to_jira,
    "get_sprint_history": get_sprint_history,
    "predict_spillovers": predict_spillovers,
    "score_predictions": score_predictions,
    "get_prediction_track_record": get_prediction_track_record,
    "get_morning_briefing": get_morning_briefing,
    "get_person_summary": get_person_summary,
    "get_person_github_activity": get_person_github_activity,
    "get_sprint_retro_data": get_sprint_retro_data,
    "get_team_availability": get_team_availability,
    "get_sprint_charts": get_sprint_charts,
    "get_pending_prs": get_pending_prs,
    "send_to_gchat": send_to_gchat,
    "save_note": lambda content, category="general": _save_note_wrapper(content, category),
}


def _save_note_wrapper(content: str, category: str = "general") -> str:
    """Wrapper for save_note that injects the current user_id."""
    user_id = _current_user_id or "default"
    try:
        save_note(user_id, content, category)
        return f"Noted. I'll remember this in future conversations."
    except Exception as e:
        return f"Failed to save note: {e}"


# Track current user for tool calls that need it
_current_user_id = None

TOOL_DISPLAY_NAMES = {
    "get_jira_issue": "Fetching Jira issue",
    "create_jira_ticket": "Creating Jira ticket",
    "search_sprint": "Searching sprint",
    "get_github_prs": "Checking GitHub PRs",
    "update_jira_status": "Updating ticket status",
    "add_jira_comment": "Adding comment",
    "attach_to_jira": "Attaching file",
    "get_sprint_history": "Analyzing sprint history",
    "predict_spillovers": "Predicting spillovers",
    "score_predictions": "Grading past predictions",
    "get_prediction_track_record": "Checking prediction accuracy",
    "get_morning_briefing": "Generating morning briefing",
    "get_person_summary": "Preparing 1:1 summary",
    "get_person_github_activity": "Checking GitHub activity",
    "get_sprint_retro_data": "Analyzing sprint retro",
    "get_team_availability": "Checking team availability",
    "get_sprint_charts": "Building sprint charts",
    "get_pending_prs": "Checking pending PRs",
    "send_to_gchat": "Sending to Google Chat",
    "save_note": "Saving to memory",
}

TOOL_ICONS = {
    "get_jira_issue": "🎫",
    "create_jira_ticket": "📝",
    "search_sprint": "🔍",
    "get_github_prs": "🔗",
    "update_jira_status": "🔄",
    "add_jira_comment": "💬",
    "attach_to_jira": "📎",
    "get_sprint_history": "📈",
    "predict_spillovers": "🔮",
    "score_predictions": "🎯",
    "get_prediction_track_record": "📈",
    "get_morning_briefing": "☀️",
    "get_person_summary": "👤",
    "get_person_github_activity": "💻",
    "get_sprint_retro_data": "🔁",
    "get_team_availability": "📊",
    "get_sprint_charts": "📈",
    "get_pending_prs": "🔍",
    "send_to_gchat": "💬",
    "save_note": "🧠",
}

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

_client = None


def get_client():
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


# In-memory single-session store. Run as your own local instance (one manager per
# process); see the OAuth/session seam in user_config.get_current_user() to host
# a shared multi-user deployment.
session = {"history": []}

# Dynamic settings — can be changed from the UI
settings = {
    "jira_projects": os.getenv("JIRA_PROJECTS", ""),
    "github_repos": os.getenv("GITHUB_REPOS", ""),
    "create_project": os.getenv("JIRA_CREATE_PROJECT", ""),
}


def _call_openai(client, messages, tools):
    """Try models in fallback order. Returns the response message or error string."""
    for model in MODELS:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            return response.choices[0].message
        except Exception as e:
            err = str(e)
            if any(k in err for k in ("429", "503", "rate_limit", "capacity", "overloaded")):
                continue
            return f"Error ({model}): {err}"
    return "All models rate-limited. Wait ~30s and try again."


def _get_suggestions(response_text: str, user_message: str) -> list:
    """Return empty list — let the AI suggest follow-ups naturally in conversation."""
    return []


# Max number of recent messages to keep in full (system prompt always kept)
_MAX_RECENT_MESSAGES = 20
# Max characters for a single tool result before truncation
_MAX_TOOL_RESULT_CHARS = 3000
# Max file upload size (10MB)
_MAX_IMAGE_SIZE = 10 * 1024 * 1024
# Valid image signatures: magic bytes -> MIME type
_IMAGE_SIGNATURES = {
    b'\x89PNG': 'image/png',
    b'\xff\xd8\xff': 'image/jpeg',
    b'GIF8': 'image/gif',
    # WebP: RIFF header (4 bytes) + size (4 bytes) + WEBP signature (4 bytes)
    # Checked separately below since we need to verify bytes 8-11
}


def _compact_history(history: list) -> list:
    """Keep conversation history manageable to avoid hitting OpenAI context limits.
    Keeps the system prompt and the most recent messages. Truncates large tool results
    in older messages."""
    if len(history) <= _MAX_RECENT_MESSAGES + 1:
        return history

    system = [history[0]] if history and history[0].get("role") == "system" else []
    rest = history[1:] if system else history

    # Keep recent messages in full
    old = rest[:-_MAX_RECENT_MESSAGES]
    recent = rest[-_MAX_RECENT_MESSAGES:]

    # Truncate large tool results in older messages
    compacted = []
    for msg in old:
        if msg.get("role") == "tool" and len(msg.get("content", "")) > _MAX_TOOL_RESULT_CHARS:
            compacted.append({
                **msg,
                "content": msg["content"][:_MAX_TOOL_RESULT_CHARS] + "\n... (truncated for context)"
            })
        else:
            compacted.append(msg)

    return system + compacted + recent


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html") as f:
        return f.read()


@app.get("/api/users")
async def get_users():
    """List available EM profiles for the user selector."""
    return list_users()


@app.get("/api/team")
async def get_team(user: str = ""):
    """Team roster (name + team) from the active EM config — single source for the
    1:1 picker so it never drifts from configs/<your>.yaml."""
    user_config = get_current_user(user or None)
    members = []
    if user_config:
        for team, data in (user_config.get("teams") or {}).items():
            for name in (data.get("members") or {}).keys():
                members.append({"name": name, "team": team})
    return members


@app.get("/api/health/integrations")
async def integration_health():
    """Per-integration status for the header dots. Cached briefly inside tools.py
    so this endpoint is cheap to poll."""
    return await asyncio.to_thread(t.get_integration_health)


@app.get("/api/track-record")
async def track_record(user: str = ""):
    """Spillover prediction accuracy for the header badge."""
    user_config = get_current_user(user or None)
    uid = user_config["user_id"] if user_config else "default"
    acc = get_prediction_accuracy(uid)
    return {"avg_accuracy": acc.get("avg_accuracy"), "total": acc.get("total", 0)}


@app.post("/api/clear")
async def clear_chat():
    session["history"] = []
    return {"status": "ok"}


@app.get("/api/settings")
async def get_settings():
    return settings


@app.get("/api/jira-projects")
async def list_jira_projects():
    """Fetch all Jira projects the user has access to."""
    try:
        from tools import _get_jira
        jira = _get_jira()
        projects = jira.projects()
        return [{"key": p["key"], "name": p.get("name", p["key"])} for p in projects]
    except Exception:
        return {"error": "Failed to load Jira projects. Check server configuration."}


import re as _re

def _sanitize_project_key(s: str) -> str:
    """Allow only alphanumeric, commas, spaces, hyphens — prevent prompt injection."""
    return _re.sub(r'[^A-Za-z0-9,\s\-]', '', s).strip().upper()


@app.post("/api/settings")
async def save_settings(
    jira_projects: str = Form(...),
    github_repos: str = Form(""),
    create_project: str = Form(""),
):
    sanitized_projects = _sanitize_project_key(jira_projects)
    if not sanitized_projects:
        return {"error": "At least one valid Jira project key is required."}
    settings["jira_projects"] = sanitized_projects
    settings["github_repos"] = _re.sub(r'[^A-Za-z0-9,\-/\s]', '', github_repos).strip()
    settings["create_project"] = _sanitize_project_key(create_project) or sanitized_projects.split(",")[0].strip()

    import tools as t
    t.JIRA_PROJECTS = [p.strip() for p in settings["jira_projects"].split(",") if p.strip()]
    t.GITHUB_REPOS = [r.strip() for r in settings["github_repos"].split(",") if r.strip()]
    t.JIRA_CREATE_PROJECT = settings["create_project"]

    session["history"] = []
    return {"status": "ok"}


@app.post("/api/digest/run")
async def run_digest_endpoint(project: str = Form(""), send: str = Form("true"), user: str = Form("")):
    """Fire the proactive daily digest on demand (for live demos or manual sends).
    The scheduled path is digest.py's main(); this is the same logic over HTTP."""
    import digest
    user_config = get_current_user(user or None)
    t.CURRENT_USER_ID = user_config["user_id"] if user_config else "default"
    do_send = str(send).lower() in ("true", "1", "yes")
    return await asyncio.to_thread(digest.run_digest, project, do_send)


@app.post("/api/chat")
async def chat(message: str = Form(...), image: UploadFile = File(None), user: str = Form("")):
    """Stream SSE events: tool steps, then final answer."""

    async def event_stream():
        global _current_user_id
        client = get_client()
        history = session["history"]

        # Load user config and set current user for tool calls
        user_config = get_current_user(user or None)
        _current_user_id = user_config["user_id"] if user_config else "default"
        # Attribute memory writes inside tools (predictions, outcomes) to this EM.
        t.CURRENT_USER_ID = _current_user_id

        # Build user message content
        content_parts = [{"type": "text", "text": message}]
        image_b64 = None

        if image and image.filename:
            img_bytes = await image.read()
            if len(img_bytes) > _MAX_IMAGE_SIZE:
                yield f"data: {json.dumps({'type': 'answer', 'content': 'Image too large. Maximum size is 10MB.'})}\n\n"
                return
            # Validate image type by magic bytes
            detected_mime = None
            for sig, mime_type in _IMAGE_SIGNATURES.items():
                if img_bytes[:len(sig)] == sig:
                    detected_mime = mime_type
                    break
            # WebP check: RIFF header + WEBP at bytes 8-11
            if not detected_mime and len(img_bytes) >= 12:
                if img_bytes[:4] == b'RIFF' and img_bytes[8:12] == b'WEBP':
                    detected_mime = 'image/webp'
            if not detected_mime:
                yield f"data: {json.dumps({'type': 'answer', 'content': 'Unsupported file type. Please upload a PNG, JPEG, GIF, or WebP image.'})}\n\n"
                return
            image_b64 = base64.b64encode(img_bytes).decode("utf-8")
            mime = detected_mime
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{image_b64}"},
            })
            # Store in session so attach_to_jira can use it
            session["pending_image"] = {"b64": image_b64, "filename": image.filename, "mime": mime}

        # Add system prompt if this is the first message — with memory context
        if not history:
            # Build roster and memory from user config
            roster = get_team_roster_text(user_config) if user_config else None
            memory = get_memory_context(_current_user_id)
            projects = settings["jira_projects"]
            create_proj = settings["create_project"]
            # Override from user config if available
            if user_config:
                projects = ",".join(user_config.get("jira_projects", [projects]))
                create_proj = user_config.get("create_project", create_proj)
            history.append({
                "role": "system",
                "content": build_system_prompt(projects, create_proj, roster, memory or None),
            })

        history.append({"role": "user", "content": content_parts})

        max_iterations = 10
        for _ in range(max_iterations):
            # Compact history before each OpenAI call to stay within context limits
            msgs_to_send = _compact_history(history)
            response_msg = await asyncio.to_thread(_call_openai, client, msgs_to_send, OPENAI_TOOLS)

            # Error string returned
            if isinstance(response_msg, str):
                yield f"data: {json.dumps({'type': 'answer', 'content': f'⚠️ {response_msg}'})}\n\n"
                return

            # No tool calls — final text response
            if not response_msg.tool_calls:
                text = response_msg.content or "(no response)"
                history.append({"role": "assistant", "content": text})
                suggestions = _get_suggestions(text, message)
                yield f"data: {json.dumps({'type': 'answer', 'content': text, 'suggestions': suggestions})}\n\n"
                return

            # Has tool calls — execute them
            # If model also returned text (greeting/acknowledgment), send it first
            if response_msg.content and response_msg.content.strip():
                yield f"data: {json.dumps({'type': 'answer', 'content': response_msg.content.strip()})}\n\n"

            # Add assistant message with tool calls to history
            history.append({
                "role": "assistant",
                "content": response_msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in response_msg.tool_calls
                ],
            })

            for tc in response_msg.tool_calls:
                tool_name = tc.function.name
                display_name = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                icon = TOOL_ICONS.get(tool_name, "⚡")

                # Parse args defensively — malformed model JSON shouldn't break the loop.
                try:
                    tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except Exception:
                    tool_args = {}

                yield f"data: {json.dumps({'type': 'tool_start', 'name': display_name, 'icon': icon, 'args': tool_args})}\n\n"

                # Execute the tool. Anything that raises here becomes the tool's
                # "result" string so the assistant's tool_call ALWAYS gets a
                # paired tool response in history. Without this, an unhandled
                # exception leaves an orphan tool_call_id and OpenAI rejects the
                # next request with HTTP 400 ("must be followed by tool messages").
                try:
                    tool_fn = TOOL_DISPATCH.get(tool_name)
                    if tool_fn:
                        result = await asyncio.to_thread(tool_fn, **tool_args)
                    else:
                        result = f"Unknown tool: {tool_name}"

                    # Extract chart data from tool result and send as separate event
                    import re
                    result_str = str(result)
                    chart_match = re.search(r'<!--SPRINTCHART:(.*?)-->', result_str)
                    if chart_match:
                        try:
                            chart_data = json.loads(chart_match.group(1))
                            yield f"data: {json.dumps({'type': 'chart', 'data': chart_data})}\n\n"
                        except Exception:
                            pass
                        result = re.sub(r'<!--SPRINTCHART:.*?-->\n?', '', result_str)

                    person_match = re.search(r'<!--PERSONCHART:(.*?)-->', str(result))
                    if person_match:
                        try:
                            person_chart = json.loads(person_match.group(1))
                            yield f"data: {json.dumps({'type': 'person_chart', 'data': person_chart})}\n\n"
                        except Exception:
                            pass
                        result = re.sub(r'<!--PERSONCHART:.*?-->\n?', '', str(result))
                except Exception as e:
                    result = f"Tool '{tool_name}' failed: {type(e).__name__}: {e}"

                yield f"data: {json.dumps({'type': 'tool_done', 'name': display_name, 'icon': icon})}\n\n"

                # Always append a paired tool message for this tool_call_id.
                history.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result),
                })

        yield f"data: {json.dumps({'type': 'answer', 'content': 'Max tool iterations reached.'})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "127.0.0.1")  # 0.0.0.0 for container/Cloud Run
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
