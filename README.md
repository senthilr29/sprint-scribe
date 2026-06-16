# Sprint Scribe

An AI colleague for engineering managers. Sprint Scribe cross-references **Jira** and **GitHub**
to give you sprint insight you'd otherwise have to dig for — a morning risk briefing, 1:1 prep,
sprint retro intelligence, PR review balancing, standup→Jira sync, and screenshot→bug.

It's built to be **multi-manager**: every manager points it at *their own* Jira projects,
Confluence space, and GitHub repos via a single config file. No code changes needed.

---

## What it does

- **Morning risk briefing** — stale tickets, "In Progress" with no PR, aging PRs without review, blocked items, workload imbalance.
- **1:1 prep** — per-person summary: tickets done vs carried over, PR activity, review patterns, suggested talking points.
- **Sprint retro intelligence** — completion rate, scope creep, carry-overs, cycle-time patterns.
- **PR review balancing** — suggests the least-loaded reviewers, never the author or someone who already reviewed.
- **Standup → Jira** — turn standup notes into ticket updates.
- **Screenshot → bug** — drop in a screenshot, get a drafted bug ticket.

---

## Setup (5 minutes)

### 1. Clone & install

```bash
git clone git@github.com:senthilr29/sprint-scribe.git
cd sprint-scribe
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Add your credentials

Copy the example env file and fill in your own tokens:

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | What it is |
|----------|-----------|
| `OPENAI_API_KEY` | Your OpenAI API key |
| `JIRA_URL` | `https://vendasta.jira.com` |
| `JIRA_EMAIL` | Your Vendasta email |
| `JIRA_API_TOKEN` | id.atlassian.com → Security → API tokens |
| `GITHUB_TOKEN` | GitHub → Settings → Developer settings → Personal access tokens (repo scope) |
| `GITHUB_ORG` | `vendasta` |
| `GITHUB_REPOS` | Comma-separated repos your teams work in, e.g. `reputation,meetings` |
| `JIRA_PROJECTS` | Your Jira project keys, e.g. `REP,VS` |
| `JIRA_CREATE_PROJECT` | Default project for new tickets |
| `GCHAT_WEBHOOK_URL` | (optional) Google Chat space webhook for digests |

> `.env` is gitignored — your tokens never get committed.

### 3. Configure your teams

Copy the config template and rename it to your name:

```bash
cp configs/example.yaml configs/<yourname>.yaml
```

Edit it to list your Jira projects and map each developer's **Jira display name → GitHub username**:

```yaml
name: "Your Name"
email: "you@vendasta.com"

jira_projects:
  - REP
create_project: REP

github_repos:
  - reputation

teams:
  REP:
    display_name: "Your Team Name"
    members:
      "Developer One": "github-id-1"
      "Developer Two": "github-id-2"

exclude_from_workload:
  - "Your Name"
```

> Config files (`configs/*.yaml`) are gitignored except the template — your roster stays private to your machine.

### 4. Run

```bash
python app.py
# or: uvicorn app:app --reload
```

Open http://localhost:8000.

---

## How multi-manager works

`user_config.py` auto-loads every `configs/*.yaml` at startup. Your team roster, project keys,
and display names all come from that file — the system prompt, PR allocation, and per-person
activity all read from the same single source. There are placeholder fallbacks in the code
(`Developer One`, etc.); these only appear if no config loads, so they're a signal that your
config didn't take.

The identity seam is `get_current_user()` in `user_config.py` — today it picks your single
config; swap it for OAuth/session later to host one shared instance for everyone.

---

## Notes

- **Never commit** `.env` or your `configs/<name>.yaml` — both are gitignored by default.
- Requires Python 3.10+ (uses `dict[str, dict]` / `str | None` type hints).
- Built at Vendasta. Questions → sramalingam@vendasta.com
