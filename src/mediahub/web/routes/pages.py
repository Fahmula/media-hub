"""Monitoring pages and their polled partials.

Every handler here is ``async`` and reads only the in-memory snapshot store, so pages render
instantly regardless of how slow or unreachable the monitored services are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from mediahub.aggregation.dashboard import DashboardView, build_view
from mediahub.context import AppContext, get_context
from mediahub.models.common import ServiceType
from mediahub.models.downloads import ActivityEvent, DownloadItem
from mediahub.models.infinidysk import ActiveRead
from mediahub.models.playback import PlaybackSession
from mediahub.models.snapshot import ServiceSnapshot
from mediahub.security.csrf import verify_csrf
from mediahub.web.deps import require_auth
from mediahub.web.scope import remember_scope, scope_context
from mediahub.web.templating import render

router = APIRouter(dependencies=[Depends(require_auth), Depends(verify_csrf)])

DASHBOARD_SECTIONS = frozenset(
    {"attention", "summary", "streams", "downloads", "status", "infinidysk"}
)
ActivityTab = Literal["playback", "downloads", "history", "streams"]


def _view(request: Request, ctx: AppContext) -> tuple[DashboardView, dict[str, object]]:
    ctx.monitor.demand.touch()
    scoped = scope_context(request, ctx)
    view = build_view(ctx.monitor.store, str(scoped["scope"]))
    return view, {"view": view, **scoped}


def _page(
    request: Request, ctx: AppContext, template: str, extra: dict[str, object]
) -> HTMLResponse:
    _, base = _view(request, ctx)
    response = render(request, template, {**base, **extra})
    remember_scope(request, response, str(base["scope"]))
    return response


# ----- dashboard ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    return _page(request, ctx, "pages/dashboard.html", {"page_title": "Dashboard"})


@router.get("/partials/dashboard/{section}", response_class=HTMLResponse)
async def dashboard_partial(
    section: str, request: Request, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    if section not in DASHBOARD_SECTIONS:
        raise HTTPException(404)
    _, base = _view(request, ctx)
    return render(request, f"partials/dashboard/{section}.html", base)


@router.get("/servers", response_class=HTMLResponse)
async def servers(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    ctx.monitor.demand.touch()
    view = build_view(ctx.monitor.store, "all")
    return render(request, "pages/servers.html", {"view": view, "page_title": "Servers"})


@router.get("/partials/servers", response_class=HTMLResponse)
async def servers_partial(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    ctx.monitor.demand.touch()
    view = build_view(ctx.monitor.store, "all")
    return render(request, "partials/servers.html", {"view": view})


@router.get("/attention", response_class=HTMLResponse)
async def attention(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    return _page(request, ctx, "pages/attention.html", {"page_title": "Needs Attention"})


@router.get("/partials/attention", response_class=HTMLResponse)
async def attention_partial(
    request: Request, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    _, base = _view(request, ctx)
    return render(request, "partials/attention_list.html", base)


# ----- activity / service pages ------------------------------------------------------------
@dataclass
class FilteredView:
    """A dashboard view narrowed to one service type and optionally one instance."""

    service_type: ServiceType | None
    instance: str | None
    instances: list[ServiceSnapshot] = field(default_factory=list)
    sessions: list[PlaybackSession] = field(default_factory=list)
    downloads: list[DownloadItem] = field(default_factory=list)
    history: list[ActivityEvent] = field(default_factory=list)
    snapshots: list[ServiceSnapshot] = field(default_factory=list)
    reads: list[tuple[ActiveRead, ServiceSnapshot]] = field(default_factory=list)


def filter_view(
    view: DashboardView, service_type: ServiceType | None, instance: str | None
) -> FilteredView:
    out = FilteredView(service_type=service_type, instance=instance)
    out.instances = [
        s for s in view.snapshots if service_type is None or s.source.service_type is service_type
    ]
    if instance and not any(s.source.instance_id == instance for s in out.instances):
        instance = out.instance = None
    out.snapshots = [
        s for s in out.instances if instance is None or s.source.instance_id == instance
    ]
    ids = {s.source.key for s in out.snapshots}
    out.sessions = [s for s in view.sessions if s.source.key in ids]
    out.downloads = [d for d in view.downloads if d.source.key in ids]
    out.history = [e for e in view.history if e.source.key in ids]
    out.reads = [
        (r, snap) for snap in out.snapshots if snap.infinidysk for r in snap.infinidysk.reads
    ]
    return out


SERVICE_PAGES: dict[str, ServiceType] = {t.value: t for t in ServiceType}


def _service_context(
    request: Request, ctx: AppContext, service_type: ServiceType | None
) -> tuple[DashboardView, dict[str, object]]:
    view, base = _view(request, ctx)
    instance = request.query_params.get("instance")
    filtered = filter_view(view, service_type, instance)
    return view, {**base, "fv": filtered, "service_type": service_type}


@router.get("/activity", response_class=HTMLResponse)
async def activity(
    request: Request,
    ctx: AppContext = Depends(get_context),
    tab: ActivityTab = "playback",
) -> HTMLResponse:
    _, base = _service_context(request, ctx, None)
    response = render(
        request, "pages/activity.html", {**base, "tab": tab, "page_title": "Activity"}
    )
    remember_scope(request, response, str(base["scope"]))
    return response


@router.get("/partials/activity/{tab}", response_class=HTMLResponse)
async def activity_partial(
    tab: ActivityTab, request: Request, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    _, base = _service_context(request, ctx, None)
    return render(request, "partials/activity_list.html", {**base, "tab": tab})


def _service_page_handler(service_type: ServiceType):  # type: ignore[no-untyped-def]
    async def handler(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
        _, base = _service_context(request, ctx, service_type)
        response = render(
            request,
            "pages/service.html",
            {**base, "page_title": service_type.label, "back_href": "/"},
        )
        remember_scope(request, response, str(base["scope"]))
        return response

    return handler


# Explicit paths (not a "/{service}" catch-all, which would shadow /servers and /settings).
for _service_type in ServiceType:
    router.add_api_route(
        f"/{_service_type.value}",
        _service_page_handler(_service_type),
        methods=["GET"],
        response_class=HTMLResponse,
    )


@router.get("/partials/service/{service}", response_class=HTMLResponse)
async def service_partial(
    service: str, request: Request, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    service_type = SERVICE_PAGES.get(service)
    if service_type is None:
        raise HTTPException(404)
    _, base = _service_context(request, ctx, service_type)
    return render(request, "partials/service_body.html", base)
