"""
Sprint Scribe Memory Layer — SQLite persistent storage.

Stores sprint outcomes, predictions, 1:1 history, EM notes, and preferences.
All data is isolated per user_id.
"""

import sqlite3
import json
import os
from datetime import datetime, timezone

_DB_PATH = os.path.join(os.path.dirname(__file__) or ".", "data", "sprint_scribe.db")
_db = None


def get_db() -> sqlite3.Connection:
    """Get or create the SQLite connection (singleton, WAL mode for concurrent reads)."""
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        _db = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA foreign_keys=ON")
        init_db(_db)
    return _db


def init_db(conn: sqlite3.Connection):
    """Create tables if they don't exist. Idempotent."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sprint_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            sprint_name TEXT NOT NULL,
            project TEXT,
            completion_rate REAL,
            committed INTEGER,
            completed INTEGER,
            spillovers INTEGER,
            velocity REAL,
            decisions TEXT,
            predicted_spillovers TEXT,
            actual_spillovers TEXT,
            prediction_accuracy REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            prediction_type TEXT NOT NULL,
            prediction_content TEXT NOT NULL,
            context TEXT,
            outcome TEXT,
            accuracy REAL,
            created_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS oneone_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            person_name TEXT NOT NULL,
            topics TEXT,
            actions_committed TEXT,
            follow_ups TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS em_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS user_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT UNIQUE NOT NULL,
            jira_projects TEXT,
            github_repos TEXT,
            create_project TEXT,
            preferences_json TEXT DEFAULT '{}',
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_sprint_outcomes_user ON sprint_outcomes(user_id);
        CREATE INDEX IF NOT EXISTS idx_predictions_user ON predictions(user_id);
        CREATE INDEX IF NOT EXISTS idx_oneone_user_person ON oneone_history(user_id, person_name);
        CREATE INDEX IF NOT EXISTS idx_em_notes_user ON em_notes(user_id);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Sprint Outcomes
# ---------------------------------------------------------------------------

def save_sprint_outcome(user_id: str, sprint_name: str, project: str = "",
                        completion_rate: float = 0, committed: int = 0,
                        completed: int = 0, spillovers: int = 0,
                        velocity: float = 0, decisions: str = "",
                        predicted_spillovers: str = "", actual_spillovers: str = "",
                        prediction_accuracy: float = None) -> int:
    """Save a sprint outcome. Returns the row ID."""
    conn = get_db()
    # Upsert — update if sprint already closed for this user
    existing = conn.execute(
        "SELECT id FROM sprint_outcomes WHERE user_id = ? AND sprint_name = ?",
        (user_id, sprint_name)
    ).fetchone()
    if existing:
        conn.execute("""
            UPDATE sprint_outcomes SET project=?, completion_rate=?, committed=?,
            completed=?, spillovers=?, velocity=?, decisions=?,
            predicted_spillovers=?, actual_spillovers=?, prediction_accuracy=?,
            created_at=datetime('now')
            WHERE id=?
        """, (project, completion_rate, committed, completed, spillovers, velocity,
              decisions, predicted_spillovers, actual_spillovers, prediction_accuracy,
              existing["id"]))
        conn.commit()
        return existing["id"]
    else:
        cursor = conn.execute("""
            INSERT INTO sprint_outcomes (user_id, sprint_name, project, completion_rate,
            committed, completed, spillovers, velocity, decisions,
            predicted_spillovers, actual_spillovers, prediction_accuracy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, sprint_name, project, completion_rate, committed, completed,
              spillovers, velocity, decisions, predicted_spillovers, actual_spillovers,
              prediction_accuracy))
        conn.commit()
        return cursor.lastrowid


def get_recent_sprint_outcomes(user_id: str, limit: int = 3) -> list[dict]:
    """Get the most recent sprint outcomes for a user."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM sprint_outcomes WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------

def save_prediction(user_id: str, prediction_type: str, prediction_content: str,
                    context: str = "") -> int:
    """Save a prediction (spillover, risk, etc). Returns row ID."""
    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO predictions (user_id, prediction_type, prediction_content, context)
        VALUES (?, ?, ?, ?)
    """, (user_id, prediction_type, prediction_content, context))
    conn.commit()
    return cursor.lastrowid


def resolve_prediction(prediction_id: int, outcome: str, accuracy: float = None):
    """Mark a prediction as resolved with the actual outcome."""
    conn = get_db()
    conn.execute("""
        UPDATE predictions SET outcome=?, accuracy=?, resolved_at=datetime('now')
        WHERE id=?
    """, (outcome, accuracy, prediction_id))
    conn.commit()


def get_prediction_accuracy(user_id: str, limit: int = 10) -> dict:
    """Get prediction accuracy stats for a user."""
    conn = get_db()
    rows = conn.execute("""
        SELECT prediction_type, accuracy FROM predictions
        WHERE user_id = ? AND accuracy IS NOT NULL
        ORDER BY resolved_at DESC LIMIT ?
    """, (user_id, limit)).fetchall()
    if not rows:
        return {"total": 0, "avg_accuracy": None, "by_type": {}}
    accuracies = [r["accuracy"] for r in rows if r["accuracy"] is not None]
    avg = sum(accuracies) / len(accuracies) if accuracies else None
    by_type = {}
    for r in rows:
        t = r["prediction_type"]
        if t not in by_type:
            by_type[t] = []
        if r["accuracy"] is not None:
            by_type[t].append(r["accuracy"])
    return {
        "total": len(rows),
        "avg_accuracy": round(avg, 1) if avg is not None else None,
        "by_type": {k: round(sum(v)/len(v), 1) for k, v in by_type.items() if v},
    }


def get_unresolved_predictions(user_id: str) -> list[dict]:
    """Get predictions that haven't been resolved yet."""
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM predictions WHERE user_id = ? AND outcome IS NULL
        ORDER BY created_at DESC LIMIT 20
    """, (user_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 1:1 History
# ---------------------------------------------------------------------------

def save_oneone(user_id: str, person_name: str, topics: str = "",
                actions_committed: str = "", follow_ups: str = "",
                notes: str = "") -> int:
    """Save a 1:1 session record. Returns row ID."""
    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO oneone_history (user_id, person_name, topics, actions_committed,
        follow_ups, notes)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, person_name, topics, actions_committed, follow_ups, notes))
    conn.commit()
    return cursor.lastrowid


def get_person_history(user_id: str, person_name: str, limit: int = 3) -> list[dict]:
    """Get recent 1:1 history for a specific person."""
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM oneone_history
        WHERE user_id = ? AND person_name = ?
        ORDER BY created_at DESC LIMIT ?
    """, (user_id, person_name, limit)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# EM Notes
# ---------------------------------------------------------------------------

def save_note(user_id: str, content: str, category: str = "general") -> int:
    """Save an explicit EM note ("remember this"). Truncates at 5000 chars."""
    content = content[:5000] if len(content) > 5000 else content
    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO em_notes (user_id, content, category) VALUES (?, ?, ?)
    """, (user_id, content, category))
    conn.commit()
    return cursor.lastrowid


def get_recent_notes(user_id: str, limit: int = 5) -> list[dict]:
    """Get recent EM notes."""
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM em_notes WHERE user_id = ? ORDER BY created_at DESC LIMIT ?
    """, (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# User Preferences
# ---------------------------------------------------------------------------

def get_user_preferences(user_id: str) -> dict:
    """Get stored preferences for a user. Returns empty dict if none."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM user_preferences WHERE user_id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else {}


def save_user_preferences(user_id: str, jira_projects: str = "",
                          github_repos: str = "", create_project: str = "",
                          preferences_json: str = "{}"):
    """Save or update user preferences."""
    conn = get_db()
    conn.execute("""
        INSERT INTO user_preferences (user_id, jira_projects, github_repos,
        create_project, preferences_json, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
        jira_projects=excluded.jira_projects, github_repos=excluded.github_repos,
        create_project=excluded.create_project, preferences_json=excluded.preferences_json,
        updated_at=datetime('now')
    """, (user_id, jira_projects, github_repos, create_project, preferences_json))
    conn.commit()


# ---------------------------------------------------------------------------
# Memory Context Builder (for system prompt injection)
# ---------------------------------------------------------------------------

_MAX_MEMORY_CHARS = 2000

def get_memory_context(user_id: str) -> str:
    """Build a formatted memory context string for injection into the system prompt.

    Returns at most ~2000 chars of high-signal memory:
    - Last 2-3 sprint outcomes with decisions
    - Prediction accuracy trend
    - Pending 1:1 follow-ups
    - Recent EM notes

    IMPORTANT: This is context for the AI, not ground truth. The system prompt
    must instruct the AI to verify against live data before stating as fact.
    """
    sections = []

    # Sprint outcomes
    sprints = get_recent_sprint_outcomes(user_id, limit=3)
    if sprints:
        lines = ["Recent sprint outcomes:"]
        for s in sprints:
            line = f"- {s['sprint_name']}: {s['completion_rate']}% completion, {s['spillovers']} spillovers"
            if s.get("decisions"):
                line += f". Decisions: {s['decisions'][:150]}"
            if s.get("prediction_accuracy") is not None:
                line += f". Prediction accuracy: {s['prediction_accuracy']}%"
            lines.append(line)
        sections.append("\n".join(lines))

    # Prediction accuracy
    accuracy = get_prediction_accuracy(user_id)
    if accuracy["avg_accuracy"] is not None:
        sections.append(
            f"Your prediction track record: {accuracy['avg_accuracy']}% accurate "
            f"over {accuracy['total']} predictions."
        )

    # Pending 1:1 follow-ups (most recent per person, if they have follow-ups)
    conn = get_db()
    recent_oneones = conn.execute("""
        SELECT person_name, actions_committed, follow_ups, created_at
        FROM oneone_history WHERE user_id = ? AND (actions_committed != '' OR follow_ups != '')
        ORDER BY created_at DESC LIMIT 6
    """, (user_id,)).fetchall()
    if recent_oneones:
        seen = set()
        follow_up_lines = ["Pending 1:1 follow-ups:"]
        for o in recent_oneones:
            if o["person_name"] not in seen:
                seen.add(o["person_name"])
                parts = []
                if o["actions_committed"]:
                    parts.append(f"committed: {o['actions_committed'][:100]}")
                if o["follow_ups"]:
                    parts.append(f"follow up: {o['follow_ups'][:100]}")
                follow_up_lines.append(f"- {o['person_name']} ({o['created_at'][:10]}): {'; '.join(parts)}")
        if len(follow_up_lines) > 1:
            sections.append("\n".join(follow_up_lines))

    # Recent notes
    notes = get_recent_notes(user_id, limit=3)
    if notes:
        note_lines = ["Your notes:"]
        for n in notes:
            note_lines.append(f"- [{n['created_at'][:10]}] {n['content'][:150]}")
        sections.append("\n".join(note_lines))

    if not sections:
        return ""

    # Join and truncate
    memory = "\n\n".join(sections)
    if len(memory) > _MAX_MEMORY_CHARS:
        memory = memory[:_MAX_MEMORY_CHARS - 50] + "\n... (older memory truncated)"

    return memory
