"""AI HomeDesign — Image Optimization MCP server.

Exposes the AI HomeDesign V3 API (https://api.aihomedesign.com/v3) as Model
Context Protocol tools over streamable HTTP, so any MCP client (Claude, etc.)
can virtually stage, redesign, enhance, declutter and convert real-estate
photos in natural language.

Transport: streamable HTTP at /mcp  (plus GET /health).
Auth:      MULTI-TENANT. The bearer token on each request (except /health) IS the
           caller's AI HomeDesign x-api-key — every job runs with that key. In
           production the reverse proxy (Caddy) captures the key from the URL path
           (/aihd-mcp/<key>/mcp) and injects it as the bearer, so the MCP client
           only needs the URL. For backward compatibility, a bearer equal to the
           legacy MCP_AUTH_TOKEN falls back to the server-wide AIHD_API_KEY.

Env vars:
    AIHD_API_KEY   default/fallback AI HomeDesign x-api-key  (optional now)
    MCP_AUTH_TOKEN legacy shared bearer -> uses AIHD_API_KEY (optional)
    AIHD_API_BASE  override API base   (default https://api.aihomedesign.com/v3)
    PORT           listen port         (default 8080)
"""
from __future__ import annotations

import base64
import binascii
import contextvars
import functools
import os
import pathlib
import secrets
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

import db
from catalog import (AREAS, CATEGORIES, ROOM_LABELS, ROOMS, STYLE_INFO, STYLES,
                     TOOLS, UNAVAILABLE_TOOLS, item_slug)

API_KEY = os.environ.get("AIHD_API_KEY", "").strip()
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
API_BASE = os.environ.get("AIHD_API_BASE", "https://api.aihomedesign.com/v3").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))

# Base used to build the connector link returned by /token. On the dedicated
# subdomain it is the clean form e.g. "https://mcp.aihomedesign.com" -> the link
# becomes "<base>/<token>/mcp". If unset, falls back to "<PUBLIC_BASE>/aihd-mcp"
# (the legacy path layout on the marketing domain).
CONNECTOR_BASE = os.environ.get("MCP_CONNECTOR_BASE", "").rstrip("/")

# Optional upload hosting: when configured, POST /upload saves an image to
# UPLOAD_DIR and returns PUBLIC_BASE + UPLOAD_URL_PREFIX + /<name> (served by Caddy).
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "").strip()
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "").rstrip("/")
UPLOAD_URL_PREFIX = os.environ.get("UPLOAD_URL_PREFIX", "/u").rstrip("/")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
UPLOAD_PAGE = f"{PUBLIC_BASE}/mcp-test/upload/" if PUBLIC_BASE else ""


def _need_image(helper: str) -> dict:
    """Friendly 'please give me the photo' reply, shown inline in the chat when a
    helper is called with no image. Keeps the local-file case in-conversation."""
    return {"ok": True, "action_required": "provide_image", "tool": helper,
            "message": "Please share the photo. Either paste an image URL, or — if it's "
                       "on your computer — upload it at the link below and paste the link back.",
            "upload_url": UPLOAD_PAGE,
            "note": "Tip: if the photo is already online (e.g. a listing photo), just paste its URL "
                    "and I'll handle everything here."}

# Per-request AI HomeDesign key, bound by KeyAuthMiddleware from the bearer token.
# Each user brings their own key; the env AIHD_API_KEY is only a fallback.
_REQUEST_KEY: contextvars.ContextVar[str] = contextvars.ContextVar("aihd_request_key", default="")
# The opaque connector token (aihd_...) and a short key fingerprint for this request,
# used by the usage log to attribute calls to a user without storing the raw key.
_REQUEST_TOKEN: contextvars.ContextVar[str] = contextvars.ContextVar("aihd_request_token", default="")
_REQUEST_FP: contextvars.ContextVar[str] = contextvars.ContextVar("aihd_request_fp", default="")


def logged_tool():
    """Decorator: register a function as an MCP tool AND record every call in the
    usage log (tool name, ok/error, duration, truncated args). `functools.wraps`
    preserves the original signature + docstring so FastMCP still builds the correct
    input schema and tool description. Logging is best-effort and never blocks."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.time()
            status, error = "ok", None
            try:
                result = fn(*args, **kwargs)
                if isinstance(result, dict) and result.get("ok") is False:
                    status = "error"
                    error = str(result.get("status") or result.get("error") or "error")[:300]
                return result
            except Exception as e:
                status, error = "error", repr(e)[:300]
                raise
            finally:
                try:
                    db.log_event(
                        token=_REQUEST_TOKEN.get() or None,
                        key_fp=_REQUEST_FP.get() or None,
                        tool=fn.__name__,
                        status=status,
                        duration_ms=int((time.time() - start) * 1000),
                        error=error,
                        args=kwargs,
                    )
                except Exception:
                    pass

        return mcp.tool()(wrapper)

    return deco


def _current_key() -> str:
    """The AIHD x-api-key to use for the current request: the per-request key
    bound by the middleware, falling back to the server-wide AIHD_API_KEY."""
    return _REQUEST_KEY.get() or API_KEY


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build request headers for an AIHD call using the current caller's key."""
    h = {"x-api-key": _current_key()}
    if extra:
        h.update(extra)
    return h


HTTP_TIMEOUT = httpx.Timeout(60.0, connect=15.0)

# In-memory store of webhook results, keyed by process_id. Populated by the
# /webhook route when AI HomeDesign POSTs a completed process; read by
# get_process / wait_for_result so results arrive without polling.
WEBHOOK_RESULTS: dict[str, dict] = {}

mcp = FastMCP(
    "AI HomeDesign — Image Optimization",
    instructions=(
        "Tools to virtually stage, redesign, enhance, declutter and convert "
        "real-estate photos via the AI HomeDesign API. Typical flow: call "
        "`list_capabilities` to see the tools, then a high-level helper such as "
        "`virtual_staging`, `enhance_image`, `remove_items`, `interior_design` "
        "or `day_to_dusk` with a public image URL. For full control use "
        "`create_order` -> `run_process` -> `get_process`. Results come back as "
        "downloadable image URLs.\n\n"
        "PRESENTATION RULES — always obey a tool's `present_instructions` and "
        "`action_required` fields when present:\n"
        "• Style choice: when a tool returns `need_style` with a `styles` list, do NOT use "
        "an interactive dropdown/picker — write your own message listing EVERY style "
        "(title + description) as cards, tag 2-3 best fits from the photo, and ask the "
        "user to reply. Use only those styles — never invent or substitute names (no "
        "'Mid-Century Modern', 'Coastal', etc.).\n"
        "• Results: present each finished image as a downloadable image FILE (a file/"
        "preview card), not as a raw text link."
    ),
    stateless_http=True,
    json_response=False,
    host="0.0.0.0",
    port=PORT,
)


# --------------------------------------------------------------------------- #
# Low-level HTTP helpers against the AI HomeDesign V3 API
# --------------------------------------------------------------------------- #
def _api_error(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except Exception:
        body = {"error": resp.text[:500]}
    return {"ok": False, "status": resp.status_code, **body}


def _ok(body: Any) -> dict[str, Any]:
    """Normalise the API's `{ "data": ..., "message": "ok" }` envelope into a flat
    dict with `ok: true`. The live V3 API wraps successful payloads in `data`."""
    out: dict[str, Any] = {"ok": True}
    if isinstance(body, dict):
        data = body.get("data", None)
        if isinstance(data, dict):
            out.update(data)
            if "pagination" in body:
                out["pagination"] = body["pagination"]
        elif isinstance(data, list):
            out["data"] = data
            if "pagination" in body:
                out["pagination"] = body["pagination"]
        else:
            out.update({k: v for k, v in body.items() if k != "message"})
    return out


def _get(path: str, params: dict | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=HTTP_TIMEOUT) as c:
        r = c.get(f"{API_BASE}{path}", headers=_headers(), params=params)
    if r.status_code >= 400:
        return _api_error(r)
    return _ok(r.json() if r.content else {})


def _post_json(path: str, payload: dict) -> dict[str, Any]:
    with httpx.Client(timeout=HTTP_TIMEOUT) as c:
        r = c.post(f"{API_BASE}{path}", headers=_headers({"Content-Type": "application/json"}), json=payload)
    if r.status_code >= 400:
        return _api_error(r)
    return _ok(r.json() if r.content else {})


_CT_BY_EXT = {".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg",
              ".jpeg": "image/jpeg"}


def _download(url: str) -> tuple[bytes, str, str]:
    """Fetch image bytes from a public URL; return (bytes, filename, content_type)."""
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        data = r.content
    ctype = r.headers.get("content-type", "").split(";")[0].strip()
    name = os.path.basename(urlparse(url).path) or "image.jpg"
    if "." not in name:
        name += {"image/png": ".png", "image/webp": ".webp"}.get(ctype, ".jpg")
    if not ctype.startswith("image/"):
        ctype = _CT_BY_EXT.get(os.path.splitext(name)[1].lower(), "image/jpeg")
    return data, name, ctype


def _encode_multipart(fields: list[tuple[str, str]],
                      files: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    """Build a multipart/form-data body manually so that field names can repeat
    (`asset_file`/`asset_role` once per image), which the order endpoint requires."""
    boundary = "----aihdmcp" + os.urandom(16).hex()
    crlf = b"\r\n"
    b = bytearray()
    for name, value in fields:
        b += b"--" + boundary.encode() + crlf
        b += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        b += str(value).encode() + crlf
    for name, filename, content, ctype in files:
        b += b"--" + boundary.encode() + crlf
        b += f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode() + crlf
        b += f"Content-Type: {ctype}".encode() + crlf + crlf
        b += content + crlf
    b += b"--" + boundary.encode() + b"--" + crlf
    return bytes(b), f"multipart/form-data; boundary={boundary}"


def _ct_from_bytes(data: bytes) -> str:
    if data[:8].startswith(b"\x89PNG"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _resolve_image(image_url: str = "", image_base64: str = "") -> tuple[bytes, str, str]:
    """Turn an image URL OR a base64 string into (bytes, filename, content_type).
    `image_base64` may be a raw base64 string or a full `data:image/...;base64,...` URL."""
    if image_base64:
        s = image_base64.strip()
        if s.startswith("data:"):
            s = s.split(",", 1)[-1]
        try:
            data = base64.b64decode(s, validate=False)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"invalid base64 image data: {e}")
        ct = _ct_from_bytes(data)
        ext = {"image/png": ".png", "image/webp": ".webp"}.get(ct, ".jpg")
        return data, "upload" + ext, ct
    return _download(image_url)


def _create_order(parts: list[tuple[bytes, str, str]], project_id: str | None,
                  roles: list[str] | None) -> dict[str, Any]:
    """Build and submit an order from already-resolved image parts (bytes,name,ctype).
    The live API uses SINGULAR multipart field names `asset_file`/`asset_role`."""
    fields: list[tuple[str, str]] = []
    files: list[tuple[str, str, bytes, str]] = []
    if project_id:
        fields.append(("project_id", project_id))
    for i, (data, fname, ctype) in enumerate(parts):
        files.append(("asset_file", fname, data, ctype))
        role = roles[i] if (roles and i < len(roles)) else "primary_angle"
        fields.append(("asset_role", role))
    body, content_type = _encode_multipart(fields, files)
    with httpx.Client(timeout=HTTP_TIMEOUT) as c:
        r = c.post(f"{API_BASE}/order", headers=_headers({"Content-Type": content_type}), content=body)
    if r.status_code >= 400:
        return _api_error(r)
    return _ok(r.json())


def _upload_order(image_urls: list[str], project_id: str | None,
                  asset_roles: list[str] | None) -> dict[str, Any]:
    parts = [_resolve_image(image_url=u) for u in image_urls]
    return _create_order(parts, project_id, asset_roles)


def _build_widgets(tool_slug: str, selections: dict[str, Any]) -> list[dict]:
    """Map short {purpose_value} selections onto the tool's real widget/item slugs.

    `selections` keys are widget slugs (or the short purpose part) -> value or list."""
    spec = TOOLS.get(tool_slug, {})
    out = []
    by_slug = {w["slug"]: w for w in spec.get("widgets", [])}
    for key, value in selections.items():
        if value in (None, "", [], {}):
            continue
        wslug = key if key in by_slug else f"widget-{key}-{tool_slug}"
        w = by_slug.get(wslug)
        if not w:
            # tolerate unknown widget slug; pass through as-is
            wslug = key
        vals = value if isinstance(value, list) else [value]
        out.append({"slug": wslug, "item_slugs": [item_slug(wslug, v) for v in vals]})
    return out


# --------------------------------------------------------------------------- #
# Discovery tools
# --------------------------------------------------------------------------- #
@logged_tool()
def list_capabilities() -> dict:
    """List every AI HomeDesign tool this MCP can run, grouped by category.

    Returns each tool's slug, display name, description and whether it needs
    one or two input images. Start here to discover what is available."""
    grouped: dict[str, list] = {label: [] for label in CATEGORIES.values()}
    for slug, t in TOOLS.items():
        grouped[CATEGORIES[t["category"]]].append({
            "slug": slug,
            "name": t["name"],
            "description": t["desc"],
            "input_images": len(t["slots"]),
            "available": slug not in UNAVAILABLE_TOOLS,
        })
    return {"ok": True, "total_tools": len(TOOLS), "categories": grouped,
            "high_level_helpers": [
                "virtual_staging", "virtual_restaging", "interior_design",
                "enhance_image", "remove_items", "day_to_dusk", "change_wall",
                "change_floor", "change_ceiling"],
            "usage_note": ("Every helper accepts image_url OR image_base64. For the "
                           "style services (staging/restaging/interior design) call the "
                           "helper WITHOUT a style first: it returns a pick-list of styles "
                           "to show the user — never pick a style yourself. Detect the "
                           "room type from the image and pass it as `space`."),
            "api_base": API_BASE}


def _validate(field: str, value: str, allowed: list[str]) -> dict | None:
    """Return an error dict if `value` is set but not in `allowed`, else None."""
    if value and value not in allowed:
        return {"ok": False, "error": f"invalid {field} '{value}'",
                "allowed_values": allowed,
                "hint": "call list_styles to see every accepted value"}
    return None


def _style_options() -> list[dict]:
    return [{"choose": s, "title": STYLE_INFO[s][0], "description": STYLE_INFO[s][1]}
            for s in STYLES]


def _room_options() -> list[dict]:
    return [{"choose": r, "title": ROOM_LABELS[r]} for r in ROOMS]


def _ask_choice(tool_name: str, message: str, field: str, options: list[dict],
                extra: dict | None = None) -> dict:
    """Return a structured 'please choose' response. The assistant should present
    `options` to the user as a friendly pick-list, then call `tool_name` again with
    the chosen value. This is how the MCP suggests styles the user may not know exist."""
    out = {"ok": True, "action_required": "choose",
           "tool": tool_name, "field": field, "message": message,
           "options": options,
           "how_to_proceed": f"Call {tool_name} again with {field}=<the value the user picked>."}
    if extra:
        out.update(extra)
    return out


# Instruction the assistant must follow when presenting the style list. We deliberately
# do NOT shape the response as an `action_required: choose` / `options` payload, because
# that structure nudges the Claude client into its native single-select dropdown widget,
# which truncates the list. Instead we ask the model to write the list itself, inline.
def _style_present(verb: str) -> str:
    return (
        "Do NOT use an interactive single-select picker / dropdown UI for this. Instead, "
        "write your OWN chat message that lists EVERY style in `styles` below — all "
        f"{len(STYLES)} of them, none omitted, none hidden behind 'also available' — as a "
        "short card-style list, each line showing the style's title and one-line description. "
        "These are the ONLY valid styles: never invent, rename, merge or substitute any "
        "(there is no 'Mid-Century Modern', 'Coastal', etc.). First look at the photo, detect "
        "the room type and its existing materials/lighting, and mark the 2-3 styles that best "
        f"fit it as a 'good fit' recommendation for the {verb}. Then ask the user to reply "
        "with the style they want (any listed one, or their own description)."
    )


def _ask_style(helper: str, verb: str) -> dict:
    """The style list response. Shaped as plain content (not an action_required/choose
    payload) so the Claude client writes it inline as cards instead of collapsing it into
    its native dropdown picker that only shows a few options."""
    return {
        "ok": True,
        "need_style": True,
        "message": f"Need a style before I can run the {verb}.",
        "styles": _style_options(),
        "assistant_instructions": _style_present(verb),
        "room_options": _room_options(),
        "note": ("After the user picks a style, also set `space` to the room type you can "
                 "see in the photo (e.g. kitchen, living-room) — detect it yourself."),
        "how_to_proceed": (f"Call {helper} again with style=<the style the user picked> "
                           "and space=<the room type you detected>."),
    }


@logged_tool()
def list_styles(tool_slug: str = "") -> dict:
    """List the allowed design styles, room types and widget options. With no
    argument, returns the global style/room/area lists. With a tool_slug, returns
    that tool's widgets and their allowed values. The staging/design helpers
    ONLY accept these values — anything else is rejected before calling the API."""
    if not tool_slug:
        return {"ok": True, "styles": STYLES, "room_types": ROOMS, "areas": AREAS}
    t = TOOLS.get(tool_slug)
    if not t:
        return {"ok": False, "error": f"unknown tool '{tool_slug}'",
                "known_tools": list(TOOLS)}
    return {"ok": True, "tool": tool_slug, "name": t["name"],
            "widgets": [{"slug": w["slug"], "purpose": w["purpose"],
                         "select": w["select"], "required": w["required"],
                         "allowed_values": w["items"]} for w in t["widgets"]]}


@logged_tool()
def describe_tool(tool_slug: str) -> dict:
    """Show the input slots, widgets and selectable options for one tool, plus a
    ready-to-use example payload for `run_process`. Use this before `run_process`
    when you need the exact widget/item slugs for a tool."""
    t = TOOLS.get(tool_slug)
    if not t:
        return {"ok": False, "error": f"unknown tool '{tool_slug}'",
                "known_tools": list(TOOLS.keys())}
    example_widgets = []
    for w in t["widgets"]:
        first = w["items"][0] if w["items"] else "option"
        vals = w["items"][:2] if w["select"] == "multi" else [first]
        example_widgets.append({"slug": w["slug"],
                                "item_slugs": [item_slug(w["slug"], v) for v in vals]})
    return {
        "ok": True,
        "slug": tool_slug,
        "name": t["name"],
        "category": CATEGORIES[t["category"]],
        "description": t["desc"],
        "input_slots": t["slots"],
        "widgets": [{"slug": w["slug"], "purpose": w["purpose"],
                     "select": w["select"], "required": w["required"],
                     "example_values": w["items"]} for w in t["widgets"]],
        "example_process_payload": {
            "order_id": "<order_id>",
            "tool": tool_slug,
            "asset_map": {slot: "<asset_id>" for slot in t["slots"]},
            "widgets": example_widgets,
        },
    }


# --------------------------------------------------------------------------- #
# Project / order / process primitives (full API coverage)
# --------------------------------------------------------------------------- #
@logged_tool()
def create_project(address: str) -> dict:
    """Create a project (a container that groups all work for one property).
    Returns the new project's `id`."""
    return _post_json("/project", {"address": address})


@logged_tool()
def list_projects(page: int = 1, limit: int = 10, search: str = "") -> dict:
    """List your projects (paginated). Optional free-text `search` on address."""
    params = {"page": page, "limit": min(limit, 30)}
    if search:
        params["s"] = search
    return _get("/project", params)


@logged_tool()
def get_project(project_id: str) -> dict:
    """Fetch a single project by id."""
    return _get(f"/project/{project_id}")


@logged_tool()
def create_order(image_urls: list[str] | None = None,
                 image_base64: list[str] | None = None, project_id: str = "",
                 asset_roles: list[str] | None = None) -> dict:
    """Upload one or more images and create an order. Provide `image_urls` (public
    URLs) and/or `image_base64` (raw base64 or data: URLs) — you can use either.

    Returns `order_id` and an `assets` list whose `id`s you map to a tool's input
    slot when running a process. For two-image tools pass roles like
    ['primary_angle','secondary_angle'] (or 'mask' for mask tools)."""
    if isinstance(image_urls, str):
        image_urls = [image_urls]
    if isinstance(image_base64, str):
        image_base64 = [image_base64]
    try:
        parts = [_resolve_image(image_url=u) for u in (image_urls or [])]
        parts += [_resolve_image(image_base64=b) for b in (image_base64 or [])]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if not parts:
        return {"ok": False, "error": "Provide image_urls or image_base64."}
    return _create_order(parts, project_id or None, asset_roles)


@logged_tool()
def get_order(order_id: str) -> dict:
    """Fetch an order, including its input assets and any processes run on it."""
    return _get(f"/order/{order_id}")


@logged_tool()
def run_process(order_id: str, tool: str, asset_map: dict,
                widgets: list | None = None, process_group_id: str = "") -> dict:
    """Start an AI job on an existing order (full control over every tool).

    `asset_map` maps each of the tool's input-slot keys to an asset id from the
    order. `widgets` is the raw list of {slug, item_slugs}. Use `describe_tool`
    to get the exact slugs. Returns `process_ids` to poll with `get_process`."""
    if tool in UNAVAILABLE_TOOLS:
        return {"ok": False, "unavailable": True,
                "error": f"{tool} is currently unavailable on the provider (reports "
                         "'tool not found'). Please use a different tool."}
    payload: dict[str, Any] = {"order_id": order_id, "tool": tool, "asset_map": asset_map}
    if widgets:
        payload["widgets"] = widgets
    if process_group_id:
        payload["process_group_id"] = process_group_id
    return _post_json("/process", payload)


@logged_tool()
def get_process(process_id: str) -> dict:
    """Fetch a process: its `status` (pending/processing/done/failed) and, when
    done, the `final_assets` with downloadable result image URLs. If a webhook
    result has already arrived for this process, it is returned immediately."""
    cached = WEBHOOK_RESULTS.get(process_id)
    if cached:
        return {"ok": True, "source": "webhook", "status": cached.get("status"),
                "results": _result_urls(cached), "present_instructions": _PRESENT_RESULT,
                "raw": cached}
    return _get(f"/process/{process_id}")


@logged_tool()
def wait_for_result(process_id: str, timeout_seconds: int = 180,
                    poll_seconds: int = 6) -> dict:
    """Poll a process until it is `done` or `failed` (or until timeout), then
    return the final result image URLs. Respects the API's 5s minimum poll
    interval."""
    poll = max(poll_seconds, 5)
    deadline = time.time() + max(timeout_seconds, poll)
    last: dict[str, Any] = {}
    while time.time() < deadline:
        cached = WEBHOOK_RESULTS.get(process_id)
        if cached:
            return {"ok": True, "source": "webhook", "status": cached.get("status"),
                    "process_id": process_id, "results": _result_urls(cached),
                    "present_instructions": _PRESENT_RESULT, "raw": cached}
        last = _get(f"/process/{process_id}")
        if not last.get("ok"):
            return last
        status = last.get("status")
        if status in ("done", "failed"):
            return {"ok": True, "status": status, "process_id": process_id,
                    "results": _result_urls(last),
                    "present_instructions": _PRESENT_RESULT if status == "done" else None,
                    "raw": last}
        time.sleep(poll)
    return {"ok": False, "status": last.get("status", "timeout"),
            "error": "timed out waiting for result", "process_id": process_id}


# Instruction the assistant must follow when handing back a finished result. Forces
# the "show it as a downloadable image FILE" behaviour rather than pasting a raw link.
_PRESENT_RESULT = (
    "Present each result image to the user as a downloadable image FILE (a file / image "
    "preview card they can download or save) — do NOT just paste the raw URL as text. "
    "Then offer to run another style or tool on the same photo."
)


def _result_urls(process: dict) -> list[dict]:
    out = []
    for a in (process.get("final_assets") or []):
        out.append({"asset_id": a.get("asset_id"),
                    "url": a.get("src") or a.get("temp_src"),
                    "thumbnail": a.get("thumbnail_src") or a.get("temp_thumbnail_src")})
    return out


# --------------------------------------------------------------------------- #
# High-level one-shot helpers (upload + process [+ wait])
# --------------------------------------------------------------------------- #
def _one_shot(tool: str, image_url: str, selections: dict, wait: bool,
              project_id: str = "", image_base64: str = "",
              extra_images: dict | None = None,
              roles: list[str] | None = None) -> dict:
    if tool in UNAVAILABLE_TOOLS:
        return {"ok": False, "unavailable": True,
                "error": f"{tool} is currently unavailable on the provider."}
    if not image_url and not image_base64:
        return _need_image(tool)
    spec = TOOLS[tool]
    try:
        parts = [_resolve_image(image_url, image_base64)]
        if extra_images:
            parts += [_resolve_image(image_url=u) for u in extra_images.values()]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "step": "image", "error": str(e)}
    order = _create_order(parts, project_id or None, roles)
    if not order.get("ok"):
        return {"step": "upload", **order}
    assets = order.get("assets") or []
    if not assets:
        return {"ok": False, "step": "upload", "order_id": order.get("order_id"),
                "error": "Image uploaded but the API returned no asset IDs. Check the "
                         "image URL is a reachable image, that the plan has active "
                         "credits, or contact support@aihomedesign.com."}
    asset_map = {spec["slots"][0]: assets[0]["id"]}
    if extra_images:
        for i, slot in enumerate(extra_images.keys(), start=1):
            if i < len(assets):
                asset_map[slot] = assets[i]["id"]
    widgets = _build_widgets(tool, selections)
    proc = _post_json("/process", {"order_id": order["order_id"], "tool": tool,
                                   "asset_map": asset_map, "widgets": widgets})
    if not proc.get("ok"):
        return {"step": "process", "order_id": order["order_id"], **proc}
    pid = (proc.get("process_ids") or [None])[0]
    result = {"ok": True, "tool": tool, "order_id": order["order_id"],
              "process_id": pid, "status": "pending"}
    if wait and pid:
        return {**result, **wait_for_result(pid)}
    return result


def _style_service(tool: str, helper: str, verb: str, image_url: str,
                   image_base64: str, style: str, space: str, wait: bool,
                   project_id: str) -> dict:
    """Shared flow for every style-based service (staging, restaging, interior
    design, under-construction). Order of operations:
      1. If no style -> return the style pick-list for the user to choose (we NEVER
         pick a style automatically).
      2. If no room type -> the assistant should detect it from the image; if it
         genuinely cannot, return the room-type pick-list.
      3. Run the tool and return the result image."""
    if not image_url and not image_base64:
        return _need_image(helper)
    err = _validate("style", style, STYLES) or _validate("space", space, ROOMS)
    if err:
        return err
    if not style:
        return _ask_style(helper, verb)
    if not space:
        return _ask_choice(helper,
            "What type of room is in this photo? If you can tell from the image, just "
            "pass it as `space`; otherwise ask the user to choose:",
            "space", _room_options())
    return _one_shot(tool, image_url, {"space": space, "style": style},
                     wait, project_id, image_base64=image_base64)


@logged_tool()
def virtual_staging(image_url: str = "", style: str = "", space: str = "",
                    image_base64: str = "", wait: bool = True,
                    project_id: str = "") -> dict:
    """Furnish an EMPTY room photo with AI furniture in a chosen design style.

    Provide the photo as `image_url` OR `image_base64` (raw base64 or a data: URL).
    WORKFLOW — follow exactly:
    • Do NOT choose a `style` yourself. Call this first with style empty; it returns
      the list of design styles. Present them to the user and let THEM pick, then call
      again with their chosen style.
    • Detect `space` (room type) from the image yourself and pass it (bedroom, kitchen,
      living-room, ...). Only if you truly cannot tell, leave it empty and the tool
      will ask the user."""
    return _style_service("tool-virtual-staging", "virtual_staging", "staging",
                          image_url, image_base64, style, space, wait, project_id)


@logged_tool()
def interior_design(image_url: str = "", style: str = "", space: str = "",
                    image_base64: str = "", wait: bool = True,
                    project_id: str = "") -> dict:
    """Fully redesign a FURNISHED room (walls, floors, furniture, decor) in a style.
    Same workflow as virtual_staging: never pick the style yourself — ask the user;
    detect the room type from the image. Provide image_url OR image_base64."""
    return _style_service("tool-interior-design", "interior_design", "redesign",
                          image_url, image_base64, style, space, wait, project_id)


@logged_tool()
def virtual_restaging(image_url: str = "", style: str = "", space: str = "",
                      image_base64: str = "", wait: bool = True,
                      project_id: str = "") -> dict:
    """Replace the furniture in an ALREADY-FURNISHED room with a new design style.
    Same workflow as virtual_staging: ask the user for the style, detect the room
    type from the image. Provide image_url OR image_base64."""
    return _style_service("tool-virtual-restaging", "virtual_restaging", "restaging",
                          image_url, image_base64, style, space, wait, project_id)


@logged_tool()
def enhance_image(image_url: str = "", area: str = "indoor",
                  options: list[str] | None = None, image_base64: str = "",
                  wait: bool = True, project_id: str = "") -> dict:
    """Enhance a photo: better lighting, sharpness and colour balance. No style
    needed. Provide image_url OR image_base64.

    area: indoor | outdoor (detect from the image). options (multi, optional):
    add-fire-to-fireplace, add-screen-to-tv."""
    err = _validate("area", area, AREAS)
    if err:
        return err
    sel: dict[str, Any] = {"area": area}
    if options:
        sel["enhancement-options"] = options
    return _one_shot("tool-image-enhancement", image_url, sel, wait, project_id,
                     image_base64=image_base64)


@logged_tool()
def remove_items(image_url: str = "", image_base64: str = "", wait: bool = True,
                 project_id: str = "") -> dict:
    """Auto-detect and remove furniture/clutter from a room photo (decluttering),
    leaving a clean empty space. No style needed. Provide image_url OR image_base64."""
    return _one_shot("tool-item-removal", image_url, {}, wait, project_id,
                     image_base64=image_base64)


@logged_tool()
def day_to_dusk(image_url: str = "", sky_style: str = "",
                options: list[str] | None = None, image_base64: str = "",
                wait: bool = True, project_id: str = "") -> dict:
    """Convert a daytime EXTERIOR photo into a dusk / twilight scene. No style needed.
    Provide image_url OR image_base64.

    sky_style: leave empty to use the default sky (recommended — the 30+ named sky
    slugs are not publicly listed). options (multi, optional): shadow-removal,
    lawn-touch-up."""
    sel: dict[str, Any] = {}
    if sky_style:
        sel["sky-style"] = sky_style
    if options:
        sel["day-to-dusk-options"] = options
    return _one_shot("tool-day-to-dusk", image_url, sel, wait, project_id,
                     image_base64=image_base64)


def _renovate(tool: str, image_url: str, image_base64: str, space: str,
              wait: bool, project_id: str) -> dict:
    """Shared body for the wall/floor/ceiling renovation helpers. Sends only the
    (validated) room-type widget; the material is left to the API default because
    the provider does not publish valid material item slugs."""
    if space:
        err = _validate("space", space, ROOMS)
        if err:
            return err
    sel = {"space": space} if space else {}
    return _one_shot(tool, image_url, sel, wait, project_id, image_base64=image_base64)


@logged_tool()
def change_wall(image_url: str = "", space: str = "", image_base64: str = "",
                wait: bool = True, project_id: str = "") -> dict:
    """Change the wall finish/colour in a room photo (AI applies a tasteful default
    finish). Detect `space` (room type) from the image. Provide image_url OR image_base64."""
    return _renovate("tool-wall-change", image_url, image_base64, space, wait, project_id)


@logged_tool()
def change_floor(image_url: str = "", space: str = "", image_base64: str = "",
                 wait: bool = True, project_id: str = "") -> dict:
    """Change the floor material in a room photo (AI applies a tasteful default
    material). Detect `space` from the image. Provide image_url OR image_base64."""
    return _renovate("tool-floor-change", image_url, image_base64, space, wait, project_id)


@logged_tool()
def change_ceiling(image_url: str = "", space: str = "", image_base64: str = "",
                   wait: bool = True, project_id: str = "") -> dict:
    """Change the ceiling finish in a room photo (AI applies a tasteful default).
    Detect `space` from the image. Provide image_url OR image_base64."""
    return _renovate("tool-ceiling-change", image_url, image_base64, space, wait, project_id)


# --------------------------------------------------------------------------- #
# HTTP app: bearer auth middleware + health route, then streamable-http MCP
# --------------------------------------------------------------------------- #
@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request):  # noqa: ANN201
    return JSONResponse({"status": "ok", "service": "aihd-mcp",
                         "tools": len(TOOLS), "api_base": API_BASE,
                         "webhook_cached": len(WEBHOOK_RESULTS)})


@mcp.custom_route("/webhook", methods=["POST"])
async def webhook(request: Request):  # noqa: ANN201
    """Receive AI HomeDesign `process.completed` callbacks and cache them by
    process_id so get_process / wait_for_result can return without polling.
    Must answer 2xx within 10s. The reverse proxy injects the bearer token, so
    only calls through the secret URL path reach here."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    pid = payload.get("process_id")
    if pid:
        WEBHOOK_RESULTS[pid] = payload
        if len(WEBHOOK_RESULTS) > 1000:  # simple cap; drop oldest 200
            for k in list(WEBHOOK_RESULTS)[:200]:
                WEBHOOK_RESULTS.pop(k, None)
    return JSONResponse({"ok": True, "received": bool(pid)})


def _is_image(data: bytes) -> bool:
    return (data[:3] == b"\xff\xd8\xff" or data[:8].startswith(b"\x89PNG")
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


@mcp.custom_route("/upload", methods=["POST", "OPTIONS"])
async def upload(request: Request):  # noqa: ANN201
    """Accept a single image (multipart field `file`) and host it, returning a
    public URL the user can paste into Claude. Lets people use a photo from their
    computer without first putting it online."""
    cors = {"Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "*"}
    if request.method == "OPTIONS":
        return PlainTextResponse("", status_code=204, headers=cors)
    if not UPLOAD_DIR or not PUBLIC_BASE:
        return JSONResponse({"ok": False, "error": "upload not configured"},
                            status_code=501, headers=cors)
    form = await request.form()
    f = form.get("file") or form.get("image") or form.get("asset_file")
    if f is None or not hasattr(f, "read"):
        return JSONResponse({"ok": False, "error": "send a multipart 'file' field"},
                            status_code=400, headers=cors)
    data = await f.read()
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse({"ok": False, "error": "image too large (max 20MB)"},
                            status_code=413, headers=cors)
    if not _is_image(data):
        return JSONResponse({"ok": False, "error": "file is not a JPG/PNG/WebP image"},
                            status_code=400, headers=cors)
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(_ct_from_bytes(data), ".jpg")
    name = secrets.token_hex(16) + ext
    pathlib.Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    with open(os.path.join(UPLOAD_DIR, name), "wb") as out:
        out.write(data)
    return JSONResponse({"ok": True, "url": f"{PUBLIC_BASE}{UPLOAD_URL_PREFIX}/{name}",
                         "bytes": len(data)}, headers=cors)


def _validate_aihd_key(key: str) -> tuple[bool, str]:
    """Check a key against the AIHD API with a cheap authenticated GET. Returns
    (ok, message). 2xx -> valid; 401/403 -> bad key; anything else -> can't tell."""
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0)) as c:
            r = c.get(f"{API_BASE}/project", headers={"x-api-key": key},
                      params={"page": 1, "limit": 1})
    except Exception as e:
        return False, f"could not reach AI HomeDesign to verify the key ({e!r})"
    if r.status_code < 400:
        return True, "ok"
    if r.status_code in (401, 403):
        return False, "that AI HomeDesign API key was rejected — check it and try again"
    return False, f"unexpected response while verifying the key (HTTP {r.status_code})"


@mcp.custom_route("/token", methods=["POST", "OPTIONS"])
async def token(request: Request):  # noqa: ANN201
    """Public endpoint behind the landing page. Takes a user's AI HomeDesign key,
    VALIDATES it against the API, and — only if valid — mints an opaque connector
    token and returns the ready connector URL. The raw key never appears in that URL;
    it is stored encrypted server-side and swapped back in per request."""
    cors = {"Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type"}
    if request.method == "OPTIONS":
        return PlainTextResponse("", status_code=204, headers=cors)
    try:
        body = await request.json()
    except Exception:
        body = {}
    key = (body.get("key") or "").strip()
    if not key:
        return JSONResponse({"ok": False, "error": "Enter your AI HomeDesign API key."},
                            status_code=400, headers=cors)
    ok, msg = _validate_aihd_key(key)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400, headers=cors)
    tok = db.mint_token(key)
    if not tok:
        return JSONResponse(
            {"ok": False, "error": "Link service is temporarily unavailable. Try again shortly."},
            status_code=503, headers=cors)
    base = CONNECTOR_BASE or (f"{PUBLIC_BASE}/aihd-mcp" if PUBLIC_BASE else "")
    url = f"{base}/{tok}/mcp"
    return JSONResponse({"ok": True, "token": tok, "url": url}, headers=cors)


async def _send_401(send) -> None:  # noqa: ANN001
    await send({"type": "http.response.start", "status": 401,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
    await send({"type": "http.response.body", "body": b"Unauthorized"})


class KeyAuthMiddleware:
    """Pure-ASGI middleware: take the per-user AIHD key from the bearer token
    (Caddy injects it from the /aihd-mcp/<key>/... URL path) and bind it for the
    duration of the request so every AIHD call uses the caller's own key.

    Backward compatible: a bearer equal to the legacy MCP_AUTH_TOKEN resolves to
    the server-wide AIHD_API_KEY instead, so old shared-token links keep working.
    /health needs no key. Pure ASGI (not BaseHTTPMiddleware) so the ContextVar set
    here propagates into the FastMCP handler and the threaded sync tools."""

    def __init__(self, app):  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "").rstrip("/")
        # /health and /token are public: /health needs no key; /token receives the
        # key in its POST body and mints a connector link, so it has no bearer yet.
        if path.endswith("/health") or path.endswith("/token"):
            await self.app(scope, receive, send)
            return
        bearer = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                v = value.decode("latin-1")
                if v.lower().startswith("bearer "):
                    bearer = v[7:].strip()
                break
        opaque = ""
        if AUTH_TOKEN and bearer == AUTH_TOKEN:
            effective = API_KEY            # legacy shared token -> server key
        elif bearer.startswith(db.TOKEN_PREFIX):
            # New opaque connector token: swap it for the real key via the DB.
            resolved = db.resolve_token(bearer)
            if not resolved:
                await _send_401(send)
                return
            effective = resolved
            opaque = bearer
        else:
            effective = bearer or API_KEY  # legacy raw key (or env fallback)
        if not effective:
            await _send_401(send)
            return
        t_key = _REQUEST_KEY.set(effective)
        t_tok = _REQUEST_TOKEN.set(opaque)
        t_fp = _REQUEST_FP.set(db.key_fingerprint(effective))
        try:
            await self.app(scope, receive, send)
        finally:
            _REQUEST_KEY.reset(t_key)
            _REQUEST_TOKEN.reset(t_tok)
            _REQUEST_FP.reset(t_fp)


def main() -> None:
    db.init()  # token map + usage log; degrades gracefully if the DB is unreachable
    app = mcp.streamable_http_app()
    app.add_middleware(KeyAuthMiddleware)

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
