"""Parsing and validation of the service-instance form.

Secrets are write-only: the form never shows stored values. A blank secret input on edit
keeps the stored value; optional secrets can be removed with an explicit "clear" checkbox.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mediahub.instances.urls import InvalidURLError, normalize_base_url
from mediahub.integrations.base import FieldSpec, InstanceConfig, Integration
from mediahub.integrations.registry import integration_class
from mediahub.models.common import ServiceType

NAME_MAX = 64
SECRET_MAX = 512
MIN_INTERVAL_S = 2
MAX_INTERVAL_S = 3600
MAX_OPTION_MINUTES = 1440
MAX_OPTION_PERCENT = 95


@dataclass
class InstanceForm:
    service_type: ServiceType
    name: str = ""
    base_url: str = ""
    enabled: bool = True
    verify_tls: bool = True
    poll_interval_s: int | None = None
    options: dict[str, Any] = field(default_factory=dict)
    # Only secrets the user typed (or explicitly cleared) this time.
    new_secrets: dict[str, str] = field(default_factory=dict)
    cleared_secrets: set[str] = field(default_factory=set)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def integration(self) -> type[Integration]:
        return integration_class(self.service_type)

    @property
    def valid(self) -> bool:
        return not self.errors

    def merged_secrets(self, stored: Mapping[str, str] | None) -> dict[str, str]:
        merged = {k: v for k, v in (stored or {}).items() if not k.startswith("__")}
        for key in self.cleared_secrets:
            merged.pop(key, None)
        merged.update(self.new_secrets)
        return merged

    def to_config(self, instance_id: str, stored: Mapping[str, str] | None) -> InstanceConfig:
        return InstanceConfig(
            id=instance_id,
            service_type=self.service_type,
            name=self.name,
            base_url=self.base_url,
            secrets=self.merged_secrets(stored),
            options={**self.options, "verify_tls": self.verify_tls},
            enabled=self.enabled,
            poll_interval_s=self.poll_interval_s,
        )


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def parse_instance_form(
    data: Mapping[str, Any], *, stored_secrets: Mapping[str, str] | None = None
) -> InstanceForm:
    try:
        service_type = ServiceType(_text(data, "service_type"))
    except ValueError as exc:
        raise ValueError("unknown service type") from exc
    form = InstanceForm(service_type=service_type)
    integration = form.integration

    form.name = _text(data, "name")
    if not form.name:
        form.errors["name"] = "Give this instance a name, e.g. “Main” or “4K”."
    elif len(form.name) > NAME_MAX:
        form.errors["name"] = f"Keep the name under {NAME_MAX} characters."

    raw_url = _text(data, "base_url")
    form.base_url = raw_url
    try:
        form.base_url = normalize_base_url(raw_url)
    except InvalidURLError as exc:
        form.errors["base_url"] = str(exc)

    form.enabled = data.get("enabled") == "on"
    form.verify_tls = data.get("verify_tls") == "on"

    interval = _text(data, "poll_interval_s")
    if interval:
        try:
            value = int(interval)
            if not MIN_INTERVAL_S <= value <= MAX_INTERVAL_S:
                raise ValueError
            form.poll_interval_s = value
        except ValueError:
            form.errors["poll_interval_s"] = (
                f"Use a whole number of seconds between {MIN_INTERVAL_S} and {MAX_INTERVAL_S}."
            )

    for spec in integration.option_fields:
        _parse_option(form, data, spec)

    stored = stored_secrets or {}
    for spec in integration.secret_fields:
        value = data.get(f"secret_{spec.name}")
        value = value.strip() if isinstance(value, str) else ""
        if value:
            if len(value) > SECRET_MAX or any(c.isspace() for c in value):
                form.errors[f"secret_{spec.name}"] = f"{spec.label} looks malformed."
            else:
                form.new_secrets[spec.name] = value
        elif data.get(f"clear_{spec.name}") == "on" and not spec.required:
            form.cleared_secrets.add(spec.name)
        elif spec.required and not stored.get(spec.name):
            form.errors[f"secret_{spec.name}"] = f"{spec.label} is required."
    return form


def _parse_option(form: InstanceForm, data: Mapping[str, Any], spec: FieldSpec) -> None:
    key = f"option_{spec.name}"
    if spec.kind == "checkbox":
        form.options[spec.name] = data.get(key) == "on"
        return
    value = _text(data, key)
    if not value:
        if spec.required:
            form.errors[key] = f"{spec.label} is required."
        return
    if spec.kind == "minutes":
        if not value.isdigit() or int(value) > MAX_OPTION_MINUTES:
            form.errors[key] = f"Use a whole number of minutes up to {MAX_OPTION_MINUTES}."
            return
        form.options[spec.name] = int(value)
        return
    if spec.kind == "percent":
        if not value.isdigit() or int(value) > MAX_OPTION_PERCENT:
            form.errors[key] = f"Use a whole number from 0 to {MAX_OPTION_PERCENT}."
            return
        form.options[spec.name] = int(value)
        return
    if spec.kind == "url":
        try:
            value = normalize_base_url(value)
        except InvalidURLError as exc:
            form.errors[key] = str(exc)
    form.options[spec.name] = value
