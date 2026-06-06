"""Jinja2 page routes: `GET /` and `GET /changelog`."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["pages"])


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    """Serve the main SPA-style page with the connect form, progress, and result."""
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "version": request.app.state.version,
        },
    )


@router.get("/changelog", response_class=HTMLResponse, include_in_schema=False)
async def changelog(request: Request) -> HTMLResponse:
    """Serve the changelog page (loads CHANGELOG.md via /api/changelog from the JS)."""
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "changelog.html",
        {
            "version": request.app.state.version,
        },
    )
