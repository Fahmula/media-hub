"""Shared FastAPI dependencies: database session and authentication."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from mediahub.context import AppContext, get_context
from mediahub.db.models import User
from mediahub.db.session import session_scope
from mediahub.security.sessions import COOKIE_NAME, Authenticated, resolve_session


class LoginRequired(Exception):
    def __init__(self, next_path: str | None = None) -> None:
        self.next_path = next_path


class SetupRequired(Exception):
    pass


Ctx = Annotated[AppContext, Depends(get_context)]


def get_db(ctx: Ctx) -> Iterator[Session]:
    with session_scope(ctx.sessions) as db:
        yield db


Db = Annotated[Session, Depends(get_db)]


def session_lifetime(ctx: AppContext) -> timedelta:
    return timedelta(days=ctx.settings.session_days)


def has_users(db: Session) -> bool:
    return bool(db.scalar(select(exists().where(User.id.is_not(None)))))


def optional_auth(request: Request, ctx: Ctx, db: Db) -> Authenticated | None:
    if hasattr(request.state, "auth"):
        return request.state.auth
    token = request.cookies.get(COOKIE_NAME)
    auth = resolve_session(db, token, lifetime=session_lifetime(ctx)) if token else None
    request.state.auth = auth
    return auth


def require_auth(
    request: Request, db: Db, auth: Annotated[Authenticated | None, Depends(optional_auth)]
) -> Authenticated:
    if auth is not None:
        return auth
    if not has_users(db):
        raise SetupRequired
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    raise LoginRequired(path if request.method == "GET" else None)


CurrentUser = Annotated[Authenticated, Depends(require_auth)]
