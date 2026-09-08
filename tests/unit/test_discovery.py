"""存档候选位置探测接口的单元测试."""

from __future__ import annotations

import pytest

from archive_management.domain import Game
from archive_management.services.discovery import (
    LocationCandidate,
    NoopCandidateProbe,
)

pytestmark = [pytest.mark.discovery, pytest.mark.critical]


def test_noop_probe_returns_no_candidates() -> None:
    candidates = NoopCandidateProbe().probe(Game(name="Demo"))
    assert candidates == []


def test_location_candidate_defaults() -> None:
    candidate = LocationCandidate(path="/games/demo/save")
    assert candidate.source == "manual"
    assert candidate.confidence == 0.5
    assert candidate.reason_code == "auto"
