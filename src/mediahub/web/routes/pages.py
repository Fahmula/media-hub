"""Top-level HTML pages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from mediahub.security.csrf import verify_csrf
from mediahub.web.deps import CurrentUser, require_auth
from mediahub.web.templating import render

router = APIRouter(dependencies=[Depends(require_auth), Depends(verify_csrf)])


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, auth: CurrentUser) -> HTMLResponse:
    return render(request, "pages/dashboard.html", {"page_title": "Dashboard"})
