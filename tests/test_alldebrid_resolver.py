"""Tests for the AllDebrid resolver."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from unittest.mock import patch

import httpx
import pytest


@pytest.fixture
def alldebrid_module(monkeypatch):
    """Import ``alldebrid_resolver`` with the bot package stubs in place.

    The real ``bot/__init__.py`` performs side effects (reads env, opens
    sockets) we do not want during unit tests. We pre-register a
    minimal stub so the resolver module imports cleanly.
    """
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent

    # Stub the top-level ``bot`` package and its sub-modules used by the
    # resolver. This keeps tests free of Telegram/Pyrogram side effects.
    bot_pkg = ModuleType("bot")
    bot_pkg.__path__ = []

    class _Logger:
        @staticmethod
        def info(msg):
            pass

    bot_pkg.LOGGER = _Logger()

    config_pkg = ModuleType("bot.core")
    config_pkg.__path__ = []
    config_manager = ModuleType("bot.core.config_manager")

    class Config:
        ALLDEBRID_API_KEY = "test-key"

    config_manager.Config = Config

    helper_pkg = ModuleType("bot.helper")
    helper_pkg.__path__ = []
    ext_utils_pkg = ModuleType("bot.helper.ext_utils")
    ext_utils_pkg.__path__ = []
    exceptions_mod = ModuleType("bot.helper.ext_utils.exceptions")

    class DirectDownloadLinkException(Exception):
        pass

    exceptions_mod.DirectDownloadLinkException = DirectDownloadLinkException

    mlu_pkg = ModuleType("bot.helper.mirror_leech_utils")
    mlu_pkg.__path__ = []
    download_utils_pkg = ModuleType(
        "bot.helper.mirror_leech_utils.download_utils"
    )
    download_utils_pkg.__path__ = [
        str(
            project_root
            / "bot"
            / "helper"
            / "mirror_leech_utils"
            / "download_utils"
        )
    ]

    monkeypatch.setitem(sys.modules, "bot", bot_pkg)
    monkeypatch.setitem(sys.modules, "bot.core", config_pkg)
    monkeypatch.setitem(sys.modules, "bot.core.config_manager", config_manager)
    monkeypatch.setitem(sys.modules, "bot.helper", helper_pkg)
    monkeypatch.setitem(sys.modules, "bot.helper.ext_utils", ext_utils_pkg)
    monkeypatch.setitem(
        sys.modules,
        "bot.helper.ext_utils.exceptions",
        exceptions_mod,
    )
    monkeypatch.setitem(
        sys.modules, "bot.helper.mirror_leech_utils", mlu_pkg
    )
    monkeypatch.setitem(
        sys.modules,
        "bot.helper.mirror_leech_utils.download_utils",
        download_utils_pkg,
    )

    sys.modules.pop(
        "bot.helper.mirror_leech_utils.download_utils.alldebrid_resolver",
        None,
    )
    return importlib.import_module(
        "bot.helper.mirror_leech_utils.download_utils.alldebrid_resolver"
    )


@pytest.mark.asyncio
async def test_resolve_returns_unrestricted_url(alldebrid_module, monkeypatch):
    async def fake_call(method, url, *, params=None, data=None):
        assert "unlock" in url
        assert params["link"] == "https://1fichier.com/?abc"
        return {
            "link": "https://cdn.alldebrid.com/abc/file.bin",
            "filename": "file.bin",
            "filesize": 1024,
        }

    monkeypatch.setattr(alldebrid_module, "_call_api", fake_call)
    out = await alldebrid_module.alldebrid_resolve("https://1fichier.com/?abc")
    assert out == "https://cdn.alldebrid.com/abc/file.bin"


@pytest.mark.asyncio
async def test_resolve_returns_streams_dict(alldebrid_module, monkeypatch):
    async def fake_call(method, url, *, params=None, data=None):
        return {
            "filename": "folder",
            "filesize": 0,
            "streams": [
                {
                    "link": "https://cdn/a.mkv",
                    "filename": "a.mkv",
                    "filesize": 100,
                },
                {
                    "link": "https://cdn/b.mkv",
                    "filename": "b.mkv",
                    "filesize": 200,
                },
            ],
        }

    monkeypatch.setattr(alldebrid_module, "_call_api", fake_call)
    out = await alldebrid_module.alldebrid_resolve("https://mega.nz/folder/x")
    assert isinstance(out, dict)
    assert out["title"] == "folder"
    assert len(out["contents"]) == 2
    assert out["total_size"] == 300
    assert out["contents"][0]["url"] == "https://cdn/a.mkv"


@pytest.mark.asyncio
async def test_resolve_no_link_no_streams_raises(alldebrid_module, monkeypatch):
    async def fake_call(method, url, *, params=None, data=None):
        return {"filename": "thing", "filesize": 0}

    monkeypatch.setattr(alldebrid_module, "_call_api", fake_call)
    with pytest.raises(Exception) as exc_info:
        await alldebrid_module.alldebrid_resolve("https://x.example/file")
    assert "could not find a download link" in str(exc_info.value)


@pytest.mark.asyncio
async def test_resolve_requires_api_key(alldebrid_module, monkeypatch):
    monkeypatch.setattr(alldebrid_module.Config, "ALLDEBRID_API_KEY", "")
    with pytest.raises(Exception) as exc_info:
        await alldebrid_module.alldebrid_resolve(
            "https://1fichier.com/?abc"
        )
    assert "ALLDEBRID_API_KEY" in str(exc_info.value)


@pytest.mark.asyncio
async def test_check_supported_handles_missing_api_key(
    alldebrid_module, monkeypatch
):
    monkeypatch.setattr(alldebrid_module.Config, "ALLDEBRID_API_KEY", "")
    assert await alldebrid_module.alldebrid_check_supported(
        "https://1fichier.com/?abc"
    ) is False


@pytest.mark.asyncio
async def test_check_supported_matches_active_host(
    alldebrid_module, monkeypatch
):
    async def fake_call(method, url, *, params=None, data=None):
        return {
            "hosts": {
                "1fichier": {
                    "name": "1fichier",
                    "domains": ["1fichier.com"],
                    "status": True,
                },
                "old": {
                    "name": "old",
                    "domains": ["dead.example.com"],
                    "status": False,
                },
            }
        }

    monkeypatch.setattr(alldebrid_module, "_call_api", fake_call)
    assert await alldebrid_module.alldebrid_check_supported(
        "https://1fichier.com/?abc"
    )
    assert await alldebrid_module.alldebrid_check_supported(
        "https://www.1fichier.com/?abc"
    )
    assert not await alldebrid_module.alldebrid_check_supported(
        "https://random-example-xyz.com/x"
    )
    # Inactive host not eligible.
    assert not await alldebrid_module.alldebrid_check_supported(
        "https://dead.example.com/x"
    )


_TORRENT_BYTES = b"d8:announce25:http://tracker.example/ann4:infod4:name4:xee"


def _patch_transport(monkeypatch, module, handler):
    """Route the resolver's own ``AsyncClient`` through a mock transport."""

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(*args, **kwargs)

    monkeypatch.setattr(module, "AsyncClient", factory)
    monkeypatch.setattr(module, "_URL_FETCH_BACKOFF_S", 0.0)


@pytest.mark.asyncio
async def test_fetch_url_bytes_returns_torrent(alldebrid_module, monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, content=_TORRENT_BYTES)

    _patch_transport(monkeypatch, alldebrid_module, handler)
    out = await alldebrid_module.fetch_url_bytes(
        "https://tracker.example/download/1.torrent"
    )
    assert out == _TORRENT_BYTES
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_fetch_url_bytes_retries_transient_404(
    alldebrid_module, monkeypatch
):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(404, content=b"not found")
        return httpx.Response(200, content=_TORRENT_BYTES)

    _patch_transport(monkeypatch, alldebrid_module, handler)
    out = await alldebrid_module.fetch_url_bytes(
        "https://tracker.example/download/1.torrent"
    )
    assert out == _TORRENT_BYTES
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_fetch_url_bytes_gives_up_after_retries(
    alldebrid_module, monkeypatch
):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, content=b"nope")

    _patch_transport(monkeypatch, alldebrid_module, handler)
    with pytest.raises(Exception) as exc_info:
        await alldebrid_module.fetch_url_bytes(
            "https://tracker.example/download/dead.torrent"
        )
    assert "404 Not Found" in str(exc_info.value)
    assert calls["n"] == alldebrid_module._URL_FETCH_ATTEMPTS


@pytest.mark.asyncio
async def test_fetch_url_bytes_reports_rate_limit(
    alldebrid_module, monkeypatch
):
    def handler(request):
        return httpx.Response(429, content=b"slow down")

    _patch_transport(monkeypatch, alldebrid_module, handler)
    with pytest.raises(Exception) as exc_info:
        await alldebrid_module.fetch_url_bytes(
            "https://tracker.example/download/1.torrent"
        )
    assert "rate-limiting" in str(exc_info.value)


@pytest.mark.asyncio
async def test_fetch_url_bytes_rejects_web_page(
    alldebrid_module, monkeypatch
):
    def handler(request):
        return httpx.Response(
            200, content=b"<!DOCTYPE html><html><body>login</body></html>"
        )

    _patch_transport(monkeypatch, alldebrid_module, handler)
    with pytest.raises(Exception) as exc_info:
        await alldebrid_module.fetch_url_bytes(
            "https://tracker.example/download/1.torrent"
        )
    assert "web page" in str(exc_info.value)


@pytest.mark.asyncio
async def test_fetch_url_bytes_rejects_empty(alldebrid_module, monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"")

    _patch_transport(monkeypatch, alldebrid_module, handler)
    with pytest.raises(Exception) as exc_info:
        await alldebrid_module.fetch_url_bytes(
            "https://tracker.example/download/1.torrent"
        )
    assert "empty response" in str(exc_info.value)
