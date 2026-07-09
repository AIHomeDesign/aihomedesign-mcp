# AI HomeDesign MCP Server — Virtual Staging & Real‑Estate Photo AI for Claude

> An open‑source **Model Context Protocol (MCP) server** that turns the
> [AI HomeDesign](https://www.aihomedesign.com) real‑estate photo API into
> natural‑language tools. Connect it to **Claude** (or any MCP client) and
> **virtually stage, redesign, enhance, declutter, and day‑to‑dusk** property
> photos — just by asking.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Model Context Protocol](https://img.shields.io/badge/MCP-compatible-blue)](https://modelcontextprotocol.io)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB)](https://www.python.org)

---

## What is this?

**AI HomeDesign MCP Server** exposes the [AI HomeDesign V3 API](https://www.aihomedesign.com)
as a set of [Model Context Protocol](https://modelcontextprotocol.io) tools over
streamable HTTP. Any MCP‑compatible AI client — **Claude Desktop, Claude in the
browser, Cursor, and more** — can then run professional **real‑estate photo editing**
in plain English:

- 🛋️ **Virtual staging** — furnish an empty room with photorealistic AI furniture in a chosen design style.
- 🎨 **Virtual restaging & interior design** — replace existing furniture or fully redesign a furnished room.
- ✨ **Image enhancement** — fix lighting, sharpness, and color balance for listing‑ready photos.
- 🧹 **Item removal / decluttering** — automatically remove furniture and clutter to reveal a clean space.
- 🌆 **Day to dusk** — convert a daytime exterior into a warm twilight / dusk scene.
- 🧱 **Wall, floor & ceiling changes** — restyle surfaces with a tasteful AI finish.

Because it speaks MCP, there is **no SDK to learn** — you describe what you want and
the model calls the right tool with the right parameters.

## Why virtual staging over MCP?

Real‑estate agents, photographers, and PropTech teams spend hours in editing tools.
This server collapses that into a conversation: drop in a listing photo, pick a
style, and get back a downloadable, market‑ready image. It's **multi‑tenant** (every
caller uses their own AI HomeDesign key), **stateless**, and **self‑hostable**, so
it fits both a single agent and a production PropTech product.

---

## Available tools

| Tool | What it does | Input |
| --- | --- | --- |
| `list_capabilities` | List every tool, grouped by category | — |
| `list_styles` | List allowed design styles, room types & widget options | — |
| `describe_tool` | Show a tool's inputs + a ready‑to‑use example payload | tool slug |
| `virtual_staging` | Furnish an **empty** room in a design style | image + style + room |
| `virtual_restaging` | Swap furniture in an **already‑furnished** room | image + style + room |
| `interior_design` | Fully redesign a furnished room | image + style + room |
| `enhance_image` | Improve lighting, sharpness & color | image |
| `remove_items` | Declutter / remove furniture | image |
| `day_to_dusk` | Turn a daytime exterior into dusk | image |
| `change_wall` / `change_floor` / `change_ceiling` | Restyle a surface | image + room |
| `create_project` · `create_order` · `run_process` · `get_process` · `wait_for_result` | Low‑level primitives for full control | see `describe_tool` |

Every helper accepts an **image URL** or a **base64 image**, and returns
downloadable result‑image URLs.

**Supported design styles:** `prime`, `modern`, `farmhouse`, `scandinavian`,
`hampton`, `industrial`, `traditional`, `contemporary`.
**Supported room types:** bedroom, living‑room, kitchen, bathroom, dining‑room,
home‑office, outdoor, nursery.

---

## Quick start

### Option A — Connect to the hosted server (no setup)

1. Get your API key from [aihomedesign.com](https://www.aihomedesign.com).
2. Generate your personal connector link (your key is validated, then stored
   **encrypted** — it never appears in the URL).
3. Add the connector URL to Claude as a custom MCP/connector and start editing photos in chat.

### Option B — Self‑host with Docker

```bash
git clone https://github.com/AIHomeDesign/aihomedesign-mcp.git
cd aihomedesign-mcp
cp .env.example .env      # add your AIHD_API_KEY (and DB_DSN/TOKEN_SECRET for multi-tenant)

docker build -t aihomedesign-mcp .
docker run -p 8080:8080 --env-file .env aihomedesign-mcp
```

The MCP endpoint is served at `http://localhost:8080/mcp` (streamable HTTP), with a
health check at `GET /health`.

### Option C — Run locally with Python

```bash
pip install -r requirements.txt
export AIHD_API_KEY=your_key_here
python app/server.py
```

---

## Configuration

All configuration is via environment variables — see [`.env.example`](.env.example).

| Variable | Purpose | Default |
| --- | --- | --- |
| `AIHD_API_KEY` | Default/fallback AI HomeDesign x‑api‑key | — |
| `AIHD_API_BASE` | API base URL | `https://api.aihomedesign.com/v3` |
| `PORT` | Listen port | `8080` |
| `DB_DSN` | Postgres DSN for token map + usage log (optional) | — |
| `TOKEN_SECRET` | Fernet key to encrypt user keys at rest (optional) | — |
| `MCP_CONNECTOR_BASE` | Base used to build connector links | — |
| `UPLOAD_DIR` / `PUBLIC_BASE` | Enable the built‑in image‑upload endpoint | — |

### Security & multi‑tenant design

- **Bring‑your‑own‑key:** each request carries the caller's own AI HomeDesign key; jobs run under that key.
- **Keys never sit in the URL:** the landing page validates a key, then mints an opaque
  `aihd_…` connector token; the raw key is stored **encrypted at rest** (Fernet) and
  swapped back in per request.
- **Usage logging** records the tool, status, and timing per call — never the raw key
  (only a short, non‑reversible fingerprint).
- **Graceful degradation:** if the database is unreachable, the server keeps serving.

---

## How it works

```
MCP client (Claude, Cursor, …)
        │  streamable HTTP  /mcp
        ▼
AI HomeDesign MCP server  ──►  AI HomeDesign V3 API  ──►  result image URLs
   (this repo)                  (api.aihomedesign.com)
```

The server is built on [FastMCP](https://modelcontextprotocol.io) and Starlette/uvicorn,
with a pure‑ASGI auth middleware that binds the per‑request key.

## Tech stack

Python 3.12 · [MCP](https://modelcontextprotocol.io) (FastMCP) · Starlette · uvicorn ·
httpx · PostgreSQL (optional) · Docker.

## Learn more

- 🌐 Website: [aihomedesign.com](https://www.aihomedesign.com)
- 📖 Model Context Protocol: [modelcontextprotocol.io](https://modelcontextprotocol.io)

## Contributing

Issues and pull requests are welcome. Please open an issue to discuss substantial
changes first.

## License

Released under the [MIT License](LICENSE).

---

<sub>Keywords: AI HomeDesign MCP server · virtual staging API · virtual staging MCP ·
real estate photo editing AI · interior design AI · virtual restaging · day to dusk ·
image enhancement · declutter / item removal · Model Context Protocol · Claude MCP
server · PropTech · real estate photography automation.</sub>
