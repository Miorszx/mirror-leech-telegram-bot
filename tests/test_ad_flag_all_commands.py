"""End-to-end ``-ad`` (AllDebrid) flag routing across every download command.

Every download command (``/m /l /qm /ql /jm /jl /nm /nl``) is served by the
same ``Mirror`` class, so ``-ad`` is parsed once and shared. This test drives
the real ``Mirror.new_event`` for each command and asserts the resolver route
that actually gets taken, which is the part a unit test of ``arg_parser``
cannot prove.

It needs the bot runtime (pyrogram / aiofiles), so run it *inside* the bot
container::

    docker exec -e PYTHONPATH=/app mirrorbot-app-1 \\
        /app/mltbenv/bin/python -m unittest \\
        tests.test_ad_flag_all_commands -v

``tests/run_test.py`` covers the host-python path for modules without heavy
deps; this file is the container counterpart for the Telegram stack.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace

sys.argv = ["x"]

from bot.modules import mirror_leech as ml  # noqa: E402

MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=t"
DIRECT = "https://example.com/movie.mkv"
TORRENT_URL = "https://example.com/file.torrent"

# (command, extra constructor kwargs) — mirrors how the handlers instantiate.
COMMANDS = [
    ("/m", {}),
    ("/l", {"is_leech": True}),
    ("/qm", {"is_qbit": True}),
    ("/ql", {"is_qbit": True, "is_leech": True}),
    ("/jm", {"is_jd": True}),
    ("/jl", {"is_jd": True, "is_leech": True}),
    ("/nm", {"is_nzb": True}),
    ("/nl", {"is_nzb": True, "is_leech": True}),
]

# Commands whose native downloader takes a direct link as-is (so -ad only
# rewrites the link for the aria2/direct path).
NATIVE_DISPATCH = {
    "/qm": "add_qb_torrent",
    "/ql": "add_qb_torrent",
    "/jm": "add_jd_download",
    "/jl": "add_jd_download",
    "/nm": "add_nzb",
    "/nl": "add_nzb",
}

CALLS: dict = {}


async def _noop(self, *a, **k):
    return None


async def _fake_send_message(message, text, *a, **k):
    CALLS.setdefault("send_message", []).append(str(text)[:120])


async def _fake_get_content_type(link):
    return "video/mp4"


async def _fake_resolve_magnet(link, is_cancelled=None):
    CALLS["route"] = "alldebrid_resolve_magnet"
    return {"magnet_id": 111, "files": []}


async def _fake_resolve_torrent(torrent_bytes, name, is_cancelled=None):
    CALLS["route"] = "alldebrid_resolve_torrent"
    return {"magnet_id": 222, "files": []}


async def _fake_resolve(link):
    CALLS["route"] = "alldebrid_resolve"
    return "https://alldebrid.example/direct/file"


async def _fake_fetch_url_bytes(url):
    return b"d8:announce4:infod4:name4:testee"


def _dispatcher(name):
    async def _f(*a, **k):
        CALLS["dispatch"] = name

    return _f


def _make_message(text):
    user = SimpleNamespace(id=111, username="tester", mention=None, title=None)
    chat_type = SimpleNamespace(name="PRIVATE")
    chat = SimpleNamespace(id=-100123, type=chat_type)
    return SimpleNamespace(
        id=42,
        text=text,
        rich_message=None,
        reply_to_message=None,
        reply_to_message_id=None,
        chat=chat,
        from_user=user,
        sender_chat=None,
        link=None,
    )


class ADAllCommandsTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Stub everything that would touch Telegram / the DB / the network.
        ml.Mirror.before_start = _noop
        ml.Mirror.run_multi = _noop
        ml.Mirror.get_tag = _noop
        ml.Mirror.remove_from_same_dir = _noop
        ml.send_message = _fake_send_message
        ml.get_content_type = _fake_get_content_type
        ml.alldebrid_resolve_magnet = _fake_resolve_magnet
        ml.alldebrid_resolve_torrent = _fake_resolve_torrent
        ml.alldebrid_resolve = _fake_resolve
        ml.fetch_url_bytes = _fake_fetch_url_bytes
        for nm in (
            "add_direct_download",
            "add_aria2_download",
            "add_qb_torrent",
            "add_jd_download",
            "add_nzb",
            "add_gd_download",
            "add_tldv_download",
            "add_rclone_download",
        ):
            setattr(ml, nm, _dispatcher(nm))

    async def _run(self, command, kwargs, link, with_ad):
        CALLS.clear()
        suffix = " -ad" if with_ad else ""
        msg = _make_message(f"{command} {link}{suffix}")
        task = ml.Mirror(client=SimpleNamespace(), message=msg, **kwargs)
        await task.new_event()
        return task

    async def test_ad_magnet_routes_alldebrid_for_every_command(self):
        for command, kwargs in COMMANDS:
            with self.subTest(command=command):
                task = await self._run(command, kwargs, MAGNET, with_ad=True)
                self.assertTrue(task.is_alldebrid)
                self.assertEqual(CALLS.get("route"), "alldebrid_resolve_magnet")
                self.assertEqual(CALLS.get("dispatch"), "add_direct_download")

    async def test_ad_torrent_url_routes_alldebrid_for_every_command(self):
        for command, kwargs in COMMANDS:
            with self.subTest(command=command):
                task = await self._run(
                    command, kwargs, TORRENT_URL, with_ad=True
                )
                self.assertTrue(task.is_alldebrid)
                self.assertEqual(CALLS.get("route"), "alldebrid_resolve_torrent")
                self.assertEqual(CALLS.get("dispatch"), "add_direct_download")

    async def test_ad_direct_url_resolves_then_dispatches_per_command(self):
        for command, kwargs in COMMANDS:
            with self.subTest(command=command):
                task = await self._run(command, kwargs, DIRECT, with_ad=True)
                self.assertTrue(task.is_alldebrid)
                native = NATIVE_DISPATCH.get(command)
                if native is None:
                    # /m and /l go through the AllDebrid unlock endpoint.
                    self.assertEqual(CALLS.get("route"), "alldebrid_resolve")
                    self.assertEqual(
                        CALLS.get("dispatch"), "add_aria2_download"
                    )
                else:
                    # qbit / jd / nzb downloaders consume the link directly.
                    self.assertNotIn("route", CALLS)
                    self.assertEqual(CALLS.get("dispatch"), native)

    async def test_without_ad_never_touches_alldebrid(self):
        for command, kwargs in COMMANDS:
            with self.subTest(command=command):
                task = await self._run(command, kwargs, MAGNET, with_ad=False)
                self.assertFalse(task.is_alldebrid)
                self.assertNotIn("route", CALLS)

    async def test_ad_flag_position_requires_link_first(self):
        # Upstream arg_parser only treats tokens before the first flag as the
        # link, so "-ad <link>" drops the link for *every* flag, not just -ad.
        # The supported form is "<link> -ad".
        good = await self._run("/l", {"is_leech": True}, MAGNET, True)
        self.assertTrue(good.is_alldebrid)
        self.assertEqual(CALLS.get("route"), "alldebrid_resolve_magnet")

        CALLS.clear()
        msg = _make_message(f"/l -ad {MAGNET}")
        bad = ml.Mirror(client=SimpleNamespace(), message=msg, is_leech=True)
        with self.assertRaises(KeyError):
            await bad.new_event()
        self.assertTrue(bad.is_alldebrid)
        self.assertEqual(bad.link, "")

    async def test_ad_combines_with_other_flags(self):
        cases = [
            f"/l {MAGNET} -ad -z",
            f"/l {MAGNET} -z -ad",
            f"/l {MAGNET} -ad -up bh",
            f"/l {MAGNET} -up bh -ad",
        ]
        for text in cases:
            with self.subTest(text=text):
                CALLS.clear()
                msg = _make_message(text)
                task = ml.Mirror(
                    client=SimpleNamespace(), message=msg, is_leech=True
                )
                await task.new_event()
                self.assertTrue(task.is_alldebrid)
                self.assertEqual(
                    CALLS.get("route"), "alldebrid_resolve_magnet"
                )


if __name__ == "__main__":
    unittest.main()
