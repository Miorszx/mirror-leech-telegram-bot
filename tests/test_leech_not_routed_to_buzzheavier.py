"""Regression: ``DEFAULT_UPLOAD=bh`` (or ``gf``) must not hijack *leech* commands.

History
-------
``before_start`` decided the backend from ``DEFAULT_UPLOAD`` *before* checking
``is_leech``.  With ``DEFAULT_UPLOAD="bh"`` a plain ``/l`` task therefore got
``is_buzzheavier=True``, which skipped the leech branch below — the branch that
computes ``split_size`` (and thumbnail / as_doc / ...).  ``split_size`` stayed
``0`` and the task crashed later in ``proceed_split``::

    parts = -(-f_size // self.split_size)
    ZeroDivisionError: division by zero

The help text documents the contract: *"DEFAULT_UPLOAD doesn't affect on leech
cmds."*  This test pins that contract at the ``before_start`` level: after a
leech task starts, ``split_size`` must be a positive number and neither
``is_buzzheavier`` nor ``is_gofile`` may be set — while a *mirror* task still
routes to the configured backend.

Needs the real bot runtime (pyrogram / aiofiles), so run it *inside* the
container::

    docker exec -e PYTHONPATH=/app mirrorbot-app-1 \\
        /app/mltbenv/bin/python /app/tests/test_leech_not_routed_to_buzzheavier.py
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace

sys.argv = ["x"]

from bot.core.config_manager import Config  # noqa: E402
from bot.core.startup import update_variables  # noqa: E402
from bot.modules import mirror_leech as ml  # noqa: E402

MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=t"


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


class LeechNotRoutedToBuzzHeavierTest(unittest.IsolatedAsyncioTestCase):
    _ready = False

    async def asyncSetUp(self):
        if not self.__class__._ready:
            Config.load()
            # ``update_variables`` mirrors what a live bot does at startup:
            # with LEECH_SPLIT_SIZE unset (0) it normalises to
            # TgClient.MAX_SPLIT_SIZE. ``before_start`` reads
            # Config.LEECH_SPLIT_SIZE, so without this the split_size assertion
            # below would be comparing against 0 by design.
            await update_variables()
            setattr(self.__class__, "_orig_default_upload", Config.DEFAULT_UPLOAD)
            setattr(self.__class__, "_orig_split", Config.LEECH_SPLIT_SIZE)
            self.__class__._ready = True
        Config.DEFAULT_UPLOAD = "bh"

    async def asyncTearDown(self):
        Config.DEFAULT_UPLOAD = getattr(self.__class__, "_orig_default_upload")
        Config.LEECH_SPLIT_SIZE = getattr(self.__class__, "_orig_split")

    async def _before_start(self, text, **kwargs):
        msg = _make_message(text)
        task = ml.Mirror(client=SimpleNamespace(), message=msg, **kwargs)
        # ``thumb`` is normally derived from user settings before before_start;
        # an empty string keeps this unit-level call off the network.
        task.thumb = ""
        await task.before_start()
        return task

    async def test_leech_default_bh_keeps_leech_route_and_split_size(self):
        task = await self._before_start(f"/l {MAGNET}", is_leech=True)
        self.assertTrue(task.is_leech)
        self.assertFalse(task.is_buzzheavier)
        self.assertFalse(task.is_gofile)
        self.assertNotEqual(task.split_size, 0)
        self.assertGreater(task.split_size, 0)

    async def test_leech_explicit_up_bh_keeps_leech_route(self):
        task = await self._before_start(f"/l {MAGNET} -up bh", is_leech=True)
        self.assertTrue(task.is_leech)
        self.assertFalse(task.is_buzzheavier)
        self.assertGreater(task.split_size, 0)

    async def test_leech_explicit_up_gf_keeps_leech_route(self):
        task = await self._before_start(f"/l {MAGNET} -up gf", is_leech=True)
        self.assertTrue(task.is_leech)
        self.assertFalse(task.is_gofile)
        self.assertFalse(task.is_buzzheavier)
        self.assertGreater(task.split_size, 0)

    async def test_mirror_default_bh_still_routes_to_buzzheavier(self):
        # The fix must NOT stop mirrors from honouring DEFAULT_UPLOAD=bh.
        task = await self._before_start(f"/m {MAGNET}", is_leech=False)
        self.assertFalse(task.is_leech)
        self.assertTrue(task.is_buzzheavier)

    async def test_split_size_avoids_proceed_split_division(self):
        # Mirror the exact expression from proceed_split on the computed size:
        # it must not raise ZeroDivisionError.
        task = await self._before_start(f"/l {MAGNET}", is_leech=True)
        try:
            _ = -(-12345678 // task.split_size)
        except ZeroDivisionError as exc:  # pragma: no cover - regression guard
            self.fail(f"proceed_split would divide by zero: {exc}")


if __name__ == "__main__":
    unittest.main()
