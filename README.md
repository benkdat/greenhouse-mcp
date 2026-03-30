# Greenhouse MCP Server

Claude-accessible MCP server for DAT's Greenhouse Harvest API. Enables live pipeline queries, candidate lookups, interview scheduling visibility, and stale pipeline reporting — all from Claude.ai.

---

## Tools

| Tool | What it does |
|---|---|
| `greenhouse_list_jobs` | List open/closed reqs with hiring team and status |
| `greenhouse_get_job` | Full details for a single req including interview stages |
| `greenhouse_get_candidate` | Candidate profile + all applications |
| `greenhouse_list_applications` | Applications filtered by job or status |
| `greenhouse_get_application` | Single application details |
| `greenhouse_get_scheduled_interviews` | Interviews with full panel + emails for calendar cross-ref |
| `greenhouse_get_scorecards` | Submitted scorecards by interviewer |
| `greenhouse_get_offer` | Offer status and compensation details |
| `greenhouse_pipeline_summary` | Stage-by-stage snapshot for a req (TalentWall replacement) |
| `greenhouse_list_users` | Greenhouse users with emails (for calendar lookups) |
| `greenhouse_stale_pipeline_report` | Active applications with no activity in 7+ days |

---

## Setup

### 1. Get your Greenhouse Harvest API key

1. Log into Greenhouse
2. Go to **Configure → Dev Center → API Credential Management**
3. Create a new Harvest API key
4. Under permissions, enable at minimum: **Jobs, Applications, Candidates, Scheduled Interviews, Scorecards, Offers, Users**

### 2. Deploy to Render

1. Push this folder to a GitHub repo
2. Go to [render.com](https://render.com) → New Web Service → connect your repo
3. Render will auto-detect `render.yaml`
4. In the Render dashboard, set the environment variable:
   ```
   GREENHOUSE_API_KEY=your_key_here
   ```
5. Deploy. Your MCP URL will be: `https://greenhouse-mcp.onrender.com/mcp`

### 3. Connect to Claude.ai

1. Go to **Claude.ai → Settings → Integrations**
2. Add a new MCP server
3. Enter your Render URL: `https://greenhouse-mcp.onrender.com/mcp`
4. Save — the tools will appear in Claude automatically

---

## Local Development

```bash
pip install -r requirements.txt
export GREENHOUSE_API_KEY=your_key_here
python server.py stdio   # stdio mode for local testing
```

Test with MCP Inspector:
```bash
npx @modelcontextprotocol/inspector python server.py stdio
```

---

## Rescheduling Workflow (with Google Calendar)

Once connected alongside the Google Calendar MCP already in Claude.ai:

1. "Who's on the interview panel for candidate X?" → `greenhouse_get_scheduled_interviews`
2. Claude gets all interviewer emails from Greenhouse
3. "Find a 45-minute slot next week that works for all of them" → `gcal_find_meeting_times`
4. Claude proposes options → you confirm → calendar invite drafted

---

## Security Notes

- Never commit your API key. Always set via environment variables.
- The Render free tier spins down after inactivity — first request may take ~30 seconds to wake.
- All tools are read-only. No write operations are included.
