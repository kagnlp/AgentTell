"""Attacker origin server (attacker.test)."""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from attacker import db

BASE = Path(__file__).resolve().parent
app = FastAPI(title="attacker-origin")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

db.init_db()


@app.get("/probe/{scenario}/{session_id}", response_class=HTMLResponse)
def probe_path(request: Request, scenario: str, session_id: str,
               variant: str = "A") -> HTMLResponse:
    """Path-based probe: scenario + session_id live in the URL path so routing survives even if the
    agent drops the query string when navigating.
    """
    return _render_probe(request, session_id, variant, scenario)


@app.get("/probe/{scenario}/{session_id}/{variant}", response_class=HTMLResponse)
def probe_path_variant(request: Request, scenario: str, session_id: str,
                       variant: str) -> HTMLResponse:
    """Same as `probe_path` but with `variant` in the PATH too."""
    return _render_probe(request, session_id, variant, scenario)


@app.get("/probe/{scenario}/{session_id}/{variant}/{plant}", response_class=HTMLResponse)
def probe_path_plant(request: Request, scenario: str, session_id: str, variant: str,
                     plant: str) -> HTMLResponse:
    """The surface-carrying form, for a scenario whose option names vary by plant."""
    return _render_probe(request, session_id, variant, scenario, plant)


def _render_probe(request: Request, session_id: str, variant: str,
                  scenario: str, plant: str = "") -> HTMLResponse:
    """Render a scenario's probe with its options in a per-session random order."""
    from harness.scenarios import get_scenario

    sc = get_scenario(scenario)
    if sc.surfaces and plant not in sc.surfaces:
        raise HTTPException(
            status_code=400,
            detail=(f"{scenario} varies its options by surface; the probe URL must carry a plant "
                    f"id from {sorted(sc.surfaces)}, got {plant!r}"))
    names = sc.names_for(plant)
    order_seed = int.from_bytes(
        hashlib.sha256(f"{session_id}:{variant}".encode()).digest()[:8], "big")
    db.log_event(
        session_id, "pageview", url=str(request.url),
        referrer=request.headers.get("referer"),
        extra={"variant": variant, "scenario": scenario, "order_seed": order_seed,
               "user_agent": request.headers.get("user-agent")},
    )
    cards = []
    for a in list(sc.affordances) + list(sc.filler):
        name = names.get(a.id, a.name)
        cta = (sc.cta_template.format(name=name)
               if sc.cta_template and name != a.name else a.cta)
        href = f"/go?session_id={session_id}&to={a.id}&scenario={scenario}"
        if plant:
            href += f"&plant={plant}"
        cards.append({"id": a.id, "name": name, "cta": cta, "blurb": a.blurb, "href": href})
    random.Random(order_seed).shuffle(cards)
    return templates.TemplateResponse(
        request,
        sc.probe_template,
        {"session_id": session_id, "variant": variant, "scenario": scenario,
         "title": sc.title, "lede": sc.lede, "cards": cards},
    )


@app.post("/beacon")
async def beacon(request: Request) -> JSONResponse:
    """Client-JS telemetry sink."""
    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    session_id = payload.get("session_id", "anon")
    for ev in payload.get("events", []):
        db.log_event(
            session_id,
            ev.get("type", "beacon"),
            client_ts=ev.get("client_ts"),
            target_id=ev.get("target_id"),
            x=ev.get("x"),
            y=ev.get("y"),
            dwell_ms=ev.get("dwell_ms"),
            url=ev.get("url"),
            extra=ev.get("extra"),
        )
    return JSONResponse({"ok": True, "n": len(payload.get("events", []))})


def _completion_html(scenario: str, to: str, plant: str = "") -> str:
    """A plausible 'done' page for the engaged option; it never depends on a session."""
    from markupsafe import escape
    try:
        from harness.scenarios import get_scenario
        sc = get_scenario(scenario) if scenario else None
    except Exception:
        sc = None
    names = ({a.id: a.name for a in sc.filler} | sc.names_for(plant)) if sc else {}
    neutral_ids = {a.id for a in sc.filler} if sc else set()
    name = names.get(to, to.replace("_", " ").title())
    if to in neutral_ids and sc.general_completion:
        body = sc.general_completion.format(name=name)
    elif to in neutral_ids:
        body = f"✓ Done — “{name}” selected. You may close this page."
    elif sc and sc.completion:
        body = sc.completion.format(name=name)
    else:
        body = f"✅ {name} — done. Your order has been placed. You may close this page."
    return (f"<!doctype html><html><body style='font-family:sans-serif;max-width:460px;"
            f"margin:60px auto'><h3>{escape(body)}</h3>"
            f"<span id='go-complete' data-to='{escape(to)}' style='display:none'>"
            f"{escape(to)}</span></body></html>")


@app.get("/go", response_class=HTMLResponse)
def outbound(request: Request, session_id: str = "anon", to: str = "",
             scenario: str = "", plant: str = "") -> HTMLResponse:
    """Outbound link tracker: records which option link the agent followed, then lands on a
    plausible per-scenario completion page (see _completion_html).
    """
    db.log_event(session_id, "nav", target_id=to, url=str(request.url),
                 referrer=request.headers.get("referer"))
    return HTMLResponse(_completion_html(scenario, to, plant))
