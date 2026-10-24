"""Errors raised by integrations. Messages are user-facing and must never contain secrets."""

from __future__ import annotations

from enum import StrEnum


class ErrorKind(StrEnum):
    UNREACHABLE = "unreachable"
    AUTH = "auth"
    PROTOCOL = "protocol"


class IntegrationError(Exception):
    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
