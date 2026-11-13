"""Generic plant pages, served at `/p/<view>/<code>` by whichever mock origin owns them."""
from __future__ import annotations

from collections.abc import Iterable

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from markupsafe import escape

from harness.opaque import resolve
from harness.scenarios import PlantView, plant_views

VIEWS: dict[str, PlantView] = plant_views()

_STYLE = ("font-family:system-ui,sans-serif;max-width:640px;margin:48px auto;"
          "line-height:1.5;color:#1a1a1a")
_TABLE = ("width:100%;border-collapse:collapse;margin:16px 0")
_CELL = "padding:8px 10px;border-bottom:1px solid #e4e4e7;text-align:left"


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><title>{escape(title)}</title></head>"
        f"<body style='{_STYLE}'><h2>{escape(title)}</h2>{body}</body></html>")


def _heading(pv: PlantView, content: dict) -> str:
    """The page heading, formatted against the content — this is usually where the held value
    appears ("Okta — single sign-on settings"), so it is a plant surface, not decoration.
    """
    return _rows([pv.heading], content)[0] if pv.heading else pv.view


def _rows(items: Iterable[str], content: dict) -> list[str]:
    """Format each row template against the page content."""
    out = []
    for item in items:
        try:
            out.append(str(item).format(**content))
        except KeyError as e:
            raise HTTPException(
                status_code=500,
                detail=f"plant page row {item!r} references unknown field {e}") from e
    return out


def _render_record(pv: PlantView, content: dict, code: str) -> HTMLResponse:
    """Labelled field rows."""
    lis = []
    for row in _rows(pv.items, content):
        label, _, value = row.partition(":")
        lis.append(f"<li><span>{escape(label.strip())}:</span> "
                   f"<b>{escape(value.strip())}</b></li>" if value
                   else f"<li>{escape(row)}</li>")
    return _page(_heading(pv, content), f"<ul>{''.join(lis)}</ul>"
                 f"<p>These settings are saved to the user's account.</p>")


def _render_table(pv: PlantView, content: dict, code: str) -> HTMLResponse:
    """First item is the header row; the rest are data rows."""
    rows = _rows(pv.items, content)
    if not rows:
        raise HTTPException(status_code=500, detail=f"view {pv.view!r} has no rows")
    head = "".join(f"<th style='{_CELL}'>{escape(c.strip())}</th>"
                   for c in rows[0].split("|"))
    body = "".join(
        "<tr>" + "".join(f"<td style='{_CELL}'>{escape(c.strip())}</td>"
                         for c in r.split("|")) + "</tr>"
        for r in rows[1:])
    return _page(_heading(pv, content),
                 f"<table style='{_TABLE}'><thead><tr>{head}</tr></thead>"
                 f"<tbody>{body}</tbody></table>")


def _render_list_action(pv: PlantView, content: dict, code: str) -> HTMLResponse:
    """A list plus a button that actually completes."""
    lis = "".join(f"<li>{escape(r)}</li>" for r in _rows(pv.items, content))
    action = escape(_rows([content.get("action", "Continue")], content)[0])
    return _page(_heading(pv, content),
                 f"<ul>{lis}</ul>"
                 f"<form method='post' action='/p/{escape(pv.view)}/{escape(code)}/do'>"
                 f"<button id='plant-action' type='submit' "
                 f"style='padding:10px 16px;font-size:15px'>{action}</button></form>")


def _render_list_action_done(pv: PlantView, content: dict, code: str) -> HTMLResponse:
    done = str(content.get("done", "Done.")).format(**content)
    lis = "".join(f"<li>{escape(r)}</li>" for r in _rows(pv.items, content))
    return _page(_heading(pv, content), f"<p><b>✓ {escape(done)}</b></p><ul>{lis}</ul>")


def _render_article_list(pv: PlantView, content: dict, code: str) -> HTMLResponse:
    """A topic page whose headlines open."""
    body = escape(str(content.get("body", "")))
    lis = "".join(
        f"<li><a id='article-{i}' href='/p/{escape(pv.view)}/{escape(code)}/read/{i}'>"
        f"{escape(r)}</a></li>"
        for i, r in enumerate(_rows(pv.items, content), 1))
    return _page(_heading(pv, content), f"<p>{body}</p><h3>Latest</h3><ul>{lis}</ul>")


def _render_form(pv: PlantView, content: dict, code: str) -> HTMLResponse:
    """A pre-filled form."""
    fields = []
    for row in _rows(pv.items, content):
        label, _, value = row.partition(":")
        fields.append(
            f"<p><label style='display:block;font-size:13px;color:#52525b'>"
            f"{escape(label.strip())}</label>"
            f"<input style='width:100%;padding:8px;font-size:15px' "
            f"value='{escape(value.strip())}' readonly>"
            f"<span style='font-size:13px;color:#3f3f46'>{escape(value.strip())}</span></p>")
    return _page(_heading(pv, content),
                 f"<form>{''.join(fields)}"
                 f"<p style='font-size:13px;color:#52525b'>Draft — not yet submitted.</p>"
                 f"</form>")


_RENDERERS = {
    "record": _render_record,
    "table": _render_table,
    "list_action": _render_list_action,
    "article_list": _render_article_list,
    "form": _render_form,
}


def _lookup(view: str, code: str, kind: str) -> tuple[PlantView, dict]:
    """Resolve (view, opaque code) to the page and the content for that label, or 404."""
    pv = VIEWS.get(view)
    if pv is None or pv.kind != kind:
        raise HTTPException(status_code=404, detail=f"unknown plant view {view!r}")
    label = resolve(code, pv.labels) or (code if code in pv.labels else None)
    if label is None:
        raise HTTPException(status_code=404, detail=f"unknown label for view {view!r}")
    return pv, pv.content(label)


def register(app, kind: str) -> None:
    """Mount the /p/<view>/<code> routes for every view this origin (`kind`) owns."""
    router = APIRouter()

    @router.get("/p/{view}/{code}", response_class=HTMLResponse)
    def plant_page(view: str, code: str) -> HTMLResponse:
        pv, content = _lookup(view, code, kind)
        render = _RENDERERS.get(pv.renderer)
        if render is None:
            raise HTTPException(status_code=500, detail=f"no renderer {pv.renderer!r}")
        return render(pv, content, code)

    @router.post("/p/{view}/{code}/do", response_class=HTMLResponse)
    def plant_action(view: str, code: str) -> HTMLResponse:
        pv, content = _lookup(view, code, kind)
        return _render_list_action_done(pv, content, code)

    @router.get("/p/{view}/{code}/read/{n}", response_class=HTMLResponse)
    def plant_article(view: str, code: str, n: int) -> HTMLResponse:
        pv, content = _lookup(view, code, kind)
        rows = _rows(pv.items, content)
        title = rows[n - 1] if 1 <= n <= len(rows) else _heading(pv, content)
        section = _heading(pv, content)
        return _page(title,
                     f"<p style='font-size:13px;color:#52525b'>{escape(section)}</p>"
                     f"<p>{escape(str(content.get('body', '')))}</p>"
                     f"<p>Full coverage of this story for the reader.</p>"
                     f"<p><a href='/p/{escape(view)}/{escape(code)}'>← Back</a></p>")

    app.include_router(router)
