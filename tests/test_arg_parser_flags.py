"""Parsing tests for the ``-ad`` (AllDebrid) boolean flag.

These exercise ``arg_parser`` in isolation, so they run on any python
without the bot runtime (pyrogram / aiofiles). The end-to-end coverage that
drives ``Mirror.new_event`` lives in ``test_ad_flag_all_commands.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_arg_parser(monkeypatch):
    # Stub the package chain so bot_utils can be sliced out of source without
    # importing the Telegram / DB stack.
    for name in ("bot", "bot.helper", "bot.helper.ext_utils"):
        pkg = ModuleType(name)
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, name, pkg)

    src_path = (
        Path(__file__).resolve().parent.parent
        / "bot"
        / "helper"
        / "ext_utils"
        / "bot_utils.py"
    )
    src = src_path.read_text(encoding="utf-8")
    start = src.find("def arg_parser(")
    end = src.find("\ndef ", start + 1)
    snippet = src[start : end if end != -1 else len(src)]

    namespace: dict = {"loads": __import__("ast").literal_eval}
    exec(snippet, namespace)  # noqa: S102 - test-only controlled exec
    return namespace["arg_parser"]


@pytest.fixture
def arg_parser(monkeypatch):
    return _load_arg_parser(monkeypatch)


def _base(**extra):
    args = {"-ad": False, "-z": False, "-d": False, "link": ""}
    args.update(extra)
    return args


def test_ad_after_link(arg_parser):
    args = _base()
    arg_parser(["http://x", "-ad"], args)
    assert args["-ad"] is True
    assert args["link"] == "http://x"


def test_ad_alone_is_true(arg_parser):
    args = _base()
    arg_parser(["-ad"], args)
    assert args["-ad"] is True


def test_ad_does_not_swallow_link_as_value(arg_parser):
    # -ad is boolean: a following magnet must NOT be consumed as its value.
    args = _base()
    arg_parser(["magnet:?xt=urn:btih:abcd", "-ad"], args)
    assert args["-ad"] is True
    assert args["link"] == "magnet:?xt=urn:btih:abcd"


def test_ad_with_value_flag_after(arg_parser):
    args = _base()
    arg_parser(["http://x", "-ad", "-z", "pw"], args)
    assert args["-ad"] is True
    assert args["-z"] == "pw"
    assert args["link"] == "http://x"


def test_ad_before_value_flag(arg_parser):
    args = _base()
    arg_parser(["http://x", "-z", "pw", "-ad"], args)
    assert args["-ad"] is True
    assert args["-z"] == "pw"


def test_ad_with_reply_no_link(arg_parser):
    args = {"-ad": False, "link": ""}
    arg_parser(["-ad"], args)
    assert args["-ad"] is True
    assert args["link"] == ""


def test_unknown_flag_is_not_treated_as_ad(arg_parser):
    # Unknown tokens are not flags, so they stay glued to the link text and
    # must not flip -ad.
    args = _base()
    arg_parser(["http://x", "-nope"], args)
    assert args["-ad"] is False
    assert args["link"] == "http://x -nope"


def test_link_before_every_flag_is_required_upstream(arg_parser):
    # Documents upstream behaviour: only tokens *before* the first flag become
    # the link, so a flag placed in front of the link drops it. This is true
    # for every flag (-ad, -z, -d, -up ...), not specific to -ad.
    args = _base()
    arg_parser(["-ad", "http://x"], args)
    assert args["-ad"] is True
    assert args["link"] == ""
