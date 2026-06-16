"""
Sprint Scribe — Per-EM user configuration.

Loads YAML configs from configs/ directory. Provides get_current_user()
which is the single identity function — swap this for OAuth later.
"""

import os
import glob

try:
    import yaml
except ImportError:
    yaml = None

_CONFIGS_DIR = os.path.join(os.path.dirname(__file__) or ".", "configs")
_configs_cache = {}


def _load_yaml(path: str) -> dict:
    """Load a YAML file. Falls back to basic parsing if pyyaml not installed."""
    if not yaml:
        raise ImportError("pyyaml is required. Install with: pip install pyyaml")
    with open(path) as f:
        return yaml.safe_load(f)


def load_all_users() -> dict[str, dict]:
    """Load all user configs from configs/ directory. Returns {user_id: config}."""
    global _configs_cache
    if _configs_cache:
        return _configs_cache

    configs = {}
    pattern = os.path.join(_CONFIGS_DIR, "*.yaml")
    for path in glob.glob(pattern):
        filename = os.path.basename(path)
        if filename == "example.yaml":
            continue
        user_id = filename.replace(".yaml", "")
        try:
            config = _load_yaml(path)
            config["user_id"] = user_id
            # Flatten team members into a single dict for backward compat
            all_members = {}
            team_names = {}
            for team_key, team_data in config.get("teams", {}).items():
                for jira_name, gh_name in team_data.get("members", {}).items():
                    all_members[jira_name] = gh_name
                    team_names[jira_name] = team_key
            config["_all_members"] = all_members
            config["_member_teams"] = team_names
            configs[user_id] = config
        except Exception as e:
            print(f"Warning: Failed to load config {path}: {e}")

    _configs_cache = configs
    return configs


def get_user_config(user_id: str) -> dict | None:
    """Get a specific user's config. Returns None if not found."""
    users = load_all_users()
    return users.get(user_id)


def get_current_user(user_id_param: str = None) -> dict | None:
    """THE identity function. All user-dependent code calls this.

    For v1: reads user_id from a parameter (query param, header, etc.)
    For v2: swap this to read from OAuth token / session cookie.

    Returns the user config dict, or None if not found.
    """
    if not user_id_param:
        # Default: if only one config exists, use it
        users = load_all_users()
        if len(users) == 1:
            return list(users.values())[0]
        return None
    return get_user_config(user_id_param)


def list_users() -> list[dict]:
    """List available user profiles (for the UI dropdown)."""
    users = load_all_users()
    return [
        {"user_id": uid, "name": u.get("name", uid), "email": u.get("email", "")}
        for uid, u in users.items()
    ]


def get_team_roster_text(config: dict) -> str:
    """Generate the team roster section for the system prompt from a user's config.
    Uses the team's display_name when set (e.g. 'Team Apollo') and keeps the project
    key visible so the AI can map between the two."""
    lines = []
    for team_key, team_data in config.get("teams", {}).items():
        members = list(team_data.get("members", {}).keys())
        if not members:
            continue
        display = team_data.get("display_name")
        label = f"{display} ({team_key})" if display and display != team_key else team_key
        lines.append(f"{label} devs: {', '.join(members)}")
    return "\n".join(lines)


def get_team_display_names(config: dict) -> dict:
    """Map project key -> friendly display name (falls back to the key itself)."""
    out = {}
    for team_key, team_data in (config.get("teams") or {}).items():
        out[team_key] = team_data.get("display_name") or team_key
    return out


def get_members_by_team(config: dict) -> dict:
    """Map project key -> list of current Jira display names on that team."""
    out = {}
    for team_key, team_data in (config.get("teams") or {}).items():
        out[team_key] = list((team_data.get("members") or {}).keys())
    return out


def invalidate_cache():
    """Clear the config cache (useful after config changes)."""
    global _configs_cache
    _configs_cache = {}
