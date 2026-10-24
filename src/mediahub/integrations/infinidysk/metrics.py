"""Minimal parser for InfiniDysk's Prometheus text exposition (``/metrics``).

Only ``nzbdav_*`` samples are kept. Label values are unescaped; NaN/Inf values are dropped.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

PREFIX = "nzbdav_"
_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
_ESCAPES = {"\\\\": "\\", '\\"': '"', "\\n": "\n"}


@dataclass(frozen=True)
class Sample:
    name: str
    labels: dict[str, str]
    value: float


def _unescape(value: str) -> str:
    return re.sub(r'\\[\\"n]', lambda m: _ESCAPES[m.group(0)], value)


def parse(text: str) -> list[Sample]:
    samples = []
    for line in text.splitlines():
        if not line.startswith(PREFIX):
            continue
        match = _SAMPLE.match(line)
        if match is None:
            continue
        name, raw_labels, raw_value = match.groups()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        labels = {k: _unescape(v) for k, v in _LABEL.findall(raw_labels or "")}
        samples.append(Sample(name, labels, value))
    return samples


class Metrics:
    def __init__(self, samples: list[Sample]) -> None:
        self._by_name: dict[str, list[Sample]] = {}
        for sample in samples:
            self._by_name.setdefault(sample.name, []).append(sample)

    @classmethod
    def from_text(cls, text: str) -> Metrics:
        return cls(parse(text))

    def value(self, name: str, **labels: str) -> float | None:
        for sample in self._by_name.get(name, []):
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
        return None

    def by_label(self, name: str, label: str, **filters: str) -> dict[str, float]:
        """``{label value: sample value}`` for samples of ``name`` matching ``filters``."""
        return {
            s.labels[label]: s.value
            for s in self._by_name.get(name, [])
            if label in s.labels and all(s.labels.get(k) == v for k, v in filters.items())
        }
