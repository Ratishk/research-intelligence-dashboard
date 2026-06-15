# research-dashboard MCP server

`mcp_server.py` is a **stdio MCP server** that exposes the Research Intelligence
Dashboard's highest-value data as LLM tools. It is a thin, read-only proxy: it
makes HTTP `GET` requests against the dashboard's REST API (FastAPI on `:8000`)
and reshapes the responses into compact, JSON-serialisable dicts. It never
touches the database or scheduler, and `main.py` is not modified.

Server name: **`research-dashboard`**.

## Tools

| Tool | Backing endpoint | What it returns |
|------|------------------|-----------------|
| `get_signals(limit=25, direction="")` | `GET /api/signals` | Recent classified signals, optionally filtered by direction, trimmed to `{id, signal_type, direction, confidence, summary, tickers, industry, url, created_at}`. |
| `get_conviction()` | `GET /api/conviction` | The conviction evidence packet (macro regime, smart-money divergences, insider/politician/institutional trades, unusual volume, rising themes). Empty sections omitted. |
| `get_smart_money(days=14)` | `GET /api/smart-money?days=N` | Politician + insider + institutional trades, net flow, and divergent names. |
| `get_ticker_dossier(symbol)` | `GET /api/ticker/{SYM}` | Full per-ticker view: price, fundamentals, smart-money flow, recent signals, theses, holding info. |
| `get_macro()` | `GET /api/macro` | Macro indicators, composite outlook, and regime. |
| `get_material_events(days=14)` | `GET /api/events8k?days=N` | 8-K material events grouped by item code. *(endpoint added by a concurrent agent; returns a clear "not available yet — restart the dashboard" error until deployed.)* |
| `get_form144(days=30)` | `GET /api/form144?days=N` | Planned insider sales (Form 144). *(same concurrent-endpoint note.)* |

Every tool returns a JSON-serialisable dict and degrades to `{"error": "..."}`
on failure (dashboard offline, timeout, HTTP error) instead of raising — so the
calling LLM always gets a clean, interpretable result. All HTTP calls use a 15s
timeout.

## Configuration

The dashboard base URL comes from the `DASHBOARD_API_URL` environment variable
and defaults to `http://127.0.0.1:8000`.

## Install & run

> **Important — use a separate virtualenv.** The `mcp` SDK pulls in a newer
> `starlette`/`pydantic` than the dashboard's FastAPI app pins. Installing `mcp`
> into the dashboard's own environment breaks the dashboard's imports. Keep the
> MCP server in its own venv. A gitignored `.venv` already exists in this repo
> for exactly this purpose:

```bash
# from the dashboard repo root
cd /Users/ratishkorrapati/research-intelligence-dashboard

# one-time setup (the repo already ships this .venv; recreate only if missing)
/opt/anaconda3/bin/python3 -m venv .venv
.venv/bin/python -m pip install mcp requests

# run the MCP server (stdio)
.venv/bin/python mcp_server.py
```

The dashboard API itself stays on its original interpreter — do not reinstall
its requirements into `.venv`, and do not install `mcp` into the dashboard's
runtime env.

## Claude / `.mcp.json` config snippet

Add this to your MCP client config (e.g. Claude Desktop `claude_desktop_config.json`
or a project `.mcp.json`). Paths are absolute and machine-specific:

```json
{
  "mcpServers": {
    "research-dashboard": {
      "command": "/Users/ratishkorrapati/research-intelligence-dashboard/.venv/bin/python",
      "args": ["/Users/ratishkorrapati/research-intelligence-dashboard/mcp_server.py"],
      "env": {
        "DASHBOARD_API_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

Make sure the dashboard is running (`python run.py` in this repo) before the
client connects, otherwise every tool returns the offline error.

## Smoke test

```bash
# headless: call the tool functions directly against the live API
.venv/bin/python -c "import mcp_server as m; print(m.get_macro())"

# protocol: initialize + tools/list over stdio (expects 7 tools)
.venv/bin/python - <<'PY'
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    p = StdioServerParameters(command=".venv/bin/python", args=["mcp_server.py"])
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print([t.name for t in tools.tools])

asyncio.run(main())
PY
```
