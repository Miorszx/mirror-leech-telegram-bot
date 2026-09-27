"""Tests for the BuzzHeavier Telegraph index builder.

Focus: episode detection must not mistake a resolution string (``1440x1080``)
for a ``SxxExx`` / ``1x01`` marker. That regression collapsed every file of a
season into "Season 40 / E10" on the Telegraph page (2026-09-27).
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest


@pytest.fixture
def ti(monkeypatch):
    """Import telegraph_index with a stubbed bot package."""
    from pathlib import Path

    bot_pkg = ModuleType("bot")
    bot_pkg.__path__ = []
    bot_helper = ModuleType("bot.helper")
    bot_helper.__path__ = []
    bot_ext = ModuleType("bot.helper.ext_utils")
    # Real on-disk path so importlib can locate ``telegraph_index``.
    bot_ext.__path__ = [
        str(Path(__file__).resolve().parent.parent / "bot" / "helper" / "ext_utils")
    ]

    monkeypatch.setitem(sys.modules, "bot", bot_pkg)
    monkeypatch.setitem(sys.modules, "bot.helper", bot_helper)
    monkeypatch.setitem(sys.modules, "bot.helper.ext_utils", bot_ext)

    sys.modules.pop("bot.helper.ext_utils.telegraph_index", None)
    return importlib.import_module("bot.helper.ext_utils.telegraph_index")


# ── regression: resolution must not be read as season x episode ────────────

def test_resolution_is_not_season_x_episode(ti):
    """``1440x1080`` must not yield season 40 / episode 10."""
    assert ti._detect_episode(
        "[Group] Example Series - 005 (BD 1440x1080 x265-10Bit Flac).mkv"
    ) == (None, 5)


def test_resolution_never_matches_se_regex(ti):
    for fname in (
        "Show (BD 1920x1080 x264).mkv",
        "Show (BD 1440x1080 x265-10Bit Flac).mkv",
        "Show (1280x720 HEVC).mkv",
    ):
        assert ti._RE_SE.search(fname) is None


def test_series_grouping_is_per_episode(ti):
    """Files of one season must land in distinct episodes, not all in E10."""
    files = [
        (f"[Group] Example Series - {n:03d} (BD 1440x1080 x265-10Bit Flac).mkv",
         f"https://example.invalid/{n}", 500_000_000)
        for n in range(1, 21)
    ]
    html = ti.build_telegraph_index_html(files)
    assert "Season 40" not in html
    assert html.count("E10 —") == 1  # only the real episode 10
    assert "E01 —" in html and "E20 —" in html


# ── canonical forms still parse ────────────────────────────────────────────

@pytest.mark.parametrize(
    "fname,expected",
    [
        ("Show.S02E07.1080p.mkv", (2, 7)),
        ("Show s2e7.mkv", (2, 7)),
        ("Show 1x07.mkv", (1, 7)),
        ("Show 01x07.mkv", (1, 7)),
        ("Show.E07.1080p.mkv", (None, 7)),
        ("Show Ep07.mkv", (None, 7)),
        ("Show Episode.07.mkv", (None, 7)),
    ],
)
def test_canonical_episode_forms(ti, fname, expected):
    assert ti._detect_episode(fname) == expected


# ── bare " - 001" numbering used by BD releases ────────────────────────────

@pytest.mark.parametrize(
    "fname,expected",
    [
        ("[Group] Show - 001 (BD 1440x1080).mkv", (None, 1)),
        ("[Group] Show - 142 END (BD 1440x1080).mkv", (None, 142)),
        ("[Group] Show - 080.5 [Special] (BD).mkv", (None, 80)),
        ("[Group] Show - 07 v2.mkv", (None, 7)),
    ],
)
def test_bare_numbering(ti, fname, expected):
    assert ti._detect_episode(fname) == expected


def test_codec_and_year_are_not_episodes(ti):
    assert ti._detect_episode("[Group] Show x265-10Bit Flac.mkv") is None
    assert ti._detect_episode("[Group] Show 2023 (BD 1920x1080).mkv") is None


def test_specials_fall_through_to_other_files(ti):
    files = [
        ("EXTRA/[Group] Show [SP01] NCOP (BD 1440x1080).mkv", "https://example.invalid/a", 1),
        ("EXTRA/[Group] Show [SP02] NCED (BD 1440x1080).mkv", "https://example.invalid/b", 1),
        ("[Group] Show - 001 (BD 1440x1080).mkv", "https://example.invalid/c", 1),
        ("[Group] Show - 002 (BD 1440x1080).mkv", "https://example.invalid/d", 1),
        ("[Group] Show - 003 (BD 1440x1080).mkv", "https://example.invalid/e", 1),
    ]
    html = ti.build_telegraph_index_html(files)
    assert "📁 Other Files" in html
    assert "SP01" in html and "NCED" in html
