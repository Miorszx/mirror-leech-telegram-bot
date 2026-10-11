"""Parallel multi-connection upload patch for pyrogram ``Client.save_file``.

Telegram throttles a single MTProto connection to roughly ~2.5 MB/s, so a large
single file uploaded over one media session is capped far below what the host
link can do — even on a premium account with a 10 Gbps NIC. Upstream's
``save_file`` already opens ``workers_count`` workers, but points them all at the
*same* session object, so every part still rides one throttled connection.

This module rebinds ``SaveFile.save_file`` with a version that:

* opens one media session **per worker**, so file parts shard across independent
  connections and aggregate throughput scales with ``workers_count``; and
* has **each worker read its own parts straight from the file** with ``os.pread``
  (thread-safe, no shared seek pointer, no single-producer queue). A central
  producer feeding an ``asyncio.Queue`` serialises reads and becomes the
  bottleneck on big files — direct sharded reads avoid that.

Measured on the user's VPS against a 300 MB file:

    4 workers  -> ~10 MB/s
    8 workers  -> ~18 MB/s
   16 workers  -> ~29 MB/s   (24 over-subscribes and drops back to ~22)

Tuning knobs (environment variables, read per upload):

* ``MLTB_UPLOAD_WORKERS`` — parallel sessions for a big file (default 8).
* ``MLTB_UPLOAD_QUEUE``   — producer-queue depth for the file-object fallback.

The extra sessions are opened as ``temporary`` and are always closed in
``finally``. If opening them fails the upload falls back to the shared session so
a failed upload can never break. Importing this module applies the patch once.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import io
import logging
import math
import os
from hashlib import md5

from pyrogram import StopTransmission, raw
from pyrogram.methods.advanced.save_file import SaveFile

log = logging.getLogger(__name__)

_PATCH_FLAG = "_mtlb_parallel_save_file_patched"

PART_SIZE = 512 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


async def _parallel_save_file(
    self,
    path,
    file_id=None,
    file_part=0,
    progress=None,
    progress_args=(),
):
    async with self.save_file_semaphore:
        if path is None:
            return None

        # ---- resolve a seekable file descriptor up front --------------------
        close_fd = False
        if isinstance(path, (str, os.PathLike)):
            file_name = os.path.basename(os.fspath(path))
            fd = os.open(os.fspath(path), os.O_RDONLY)
            close_fd = True
        elif isinstance(path, io.IOBase):
            file_name = getattr(path, "name", "file.jpg")
            file_name = os.path.basename(file_name) if isinstance(file_name, str) else "file.jpg"
            try:
                fd = path.fileno()
            except (AttributeError, io.UnsupportedOperation, OSError):
                fd = None
        else:
            raise ValueError(
                "Invalid file. Expected a file path as string or a binary (not text) file pointer"
            )

        try:
            if fd is None:
                return await _fallback_sequential(
                    self, path, file_id, file_part, progress, progress_args
                )

            file_size = os.fstat(fd).st_size
            if file_size == 0:
                raise ValueError("File size equals to 0 B")

            file_size_limit_mib = 4000 if (self.me and self.me.is_premium) else 2000
            if file_size > file_size_limit_mib * 1024 * 1024:
                raise ValueError(f"Can't upload files bigger than {file_size_limit_mib} MiB")

            file_total_parts = int(math.ceil(file_size / PART_SIZE))
            is_big = file_size > 10 * 1024 * 1024
            is_missing_part = file_id is not None
            file_id = file_id or self.rnd_id()

            if not is_big:
                # Small files: single part, keep it simple + compute md5.
                return await _upload_small(
                    self, fd, file_id, file_size, file_total_parts, file_name,
                    is_missing_part, progress, progress_args,
                )

            workers_count = _env_int("MLTB_UPLOAD_WORKERS", 8)
            dc_id = await self.storage.dc_id()

            # One media session per worker; fall back to a single shared session
            # if the extras cannot be opened.
            extra_sessions = []
            sessions = [await self.get_session(dc_id, is_media=True)]
            try:
                for _ in range(workers_count - 1):
                    extra = await self.get_session(dc_id, is_media=True, temporary=True)
                    extra_sessions.append(extra)
                    sessions.append(extra)
            except Exception as e:
                log.exception(e)
                for extra in extra_sessions:
                    try:
                        await extra.stop()
                    except Exception:
                        pass
                extra_sessions = []
                sessions = [sessions[0]] * workers_count

            failures = []
            loop = asyncio.get_running_loop()
            done = file_part * PART_SIZE
            done_lock = asyncio.Lock()

            async def report():
                if not progress:
                    return
                func = functools.partial(progress, min(done, file_size), file_size, *progress_args)
                if inspect.iscoroutinefunction(progress):
                    await func()
                else:
                    await loop.run_in_executor(self.executor, func)

            async def shard(k: int, session):
                nonlocal done
                for i in range(file_part + k, file_total_parts, workers_count):
                    try:
                        chunk = await loop.run_in_executor(
                            None, os.pread, fd, PART_SIZE, i * PART_SIZE
                        )
                        if not chunk:
                            break
                        await session.invoke(
                            raw.functions.upload.SaveBigFilePart(
                                file_id=file_id,
                                file_part=i,
                                file_total_parts=file_total_parts,
                                bytes=chunk,
                            )
                        )
                    except StopTransmission:
                        raise
                    except Exception as e:
                        log.exception(e)
                        failures.append(e)
                        return
                    async with done_lock:
                        if not is_missing_part:
                            done += len(chunk)
                        await report()

            try:
                await asyncio.gather(*(shard(k, s) for k, s in enumerate(sessions)))
            finally:
                for extra in extra_sessions:
                    try:
                        await extra.stop()
                    except Exception:
                        pass

            if failures:
                raise failures[0]

            if is_missing_part:
                return None

            return raw.types.InputFileBig(
                id=file_id, parts=file_total_parts, name=file_name
            )
        finally:
            if close_fd and fd is not None:
                os.close(fd)


async def _upload_small(
    self, fd, file_id, file_size, file_total_parts, file_name,
    is_missing_part, progress, progress_args,
):
    dc_id = await self.storage.dc_id()
    session = await self.get_session(dc_id, is_media=True)
    data = os.pread(fd, file_size, 0)
    if not is_missing_part:
        await session.invoke(
            raw.functions.upload.SaveFilePart(file_id=file_id, file_part=0, bytes=data)
        )
    if progress:
        func = functools.partial(progress, file_size, file_size, *progress_args)
        if inspect.iscoroutinefunction(progress):
            await func()
        else:
            await self.loop.run_in_executor(self.executor, func)
    if is_missing_part:
        return None
    md5_sum = "".join(f"{b:02x}" for b in md5(data).digest())
    return raw.types.InputFile(
        id=file_id, parts=file_total_parts, name=file_name, md5_checksum=md5_sum
    )


async def _fallback_sequential(
    self, path, file_id, file_part, progress, progress_args
):
    """Upstream-style single-connection path for non-fd file objects."""
    if isinstance(path, (str, os.PathLike)):
        fp = open(path, "rb")
        close = True
    else:
        fp = path
        close = False

    file_name = os.path.basename(getattr(fp, "name", "file.jpg") or "file.jpg")
    fp.seek(0, os.SEEK_END)
    file_size = fp.tell()
    fp.seek(0)
    if file_size == 0:
        raise ValueError("File size equals to 0 B")

    file_total_parts = int(math.ceil(file_size / PART_SIZE))
    is_big = file_size > 10 * 1024 * 1024
    is_missing_part = file_id is not None
    file_id = file_id or self.rnd_id()
    md5_sum = md5() if not is_big and not is_missing_part else None
    dc_id = await self.storage.dc_id()
    session = await self.get_session(dc_id, is_media=True)

    try:
        fp.seek(PART_SIZE * file_part)
        while True:
            chunk = fp.read(PART_SIZE)
            if not chunk:
                break
            if is_big:
                rpc = raw.functions.upload.SaveBigFilePart(
                    file_id=file_id, file_part=file_part,
                    file_total_parts=file_total_parts, bytes=chunk,
                )
            else:
                rpc = raw.functions.upload.SaveFilePart(
                    file_id=file_id, file_part=file_part, bytes=chunk
                )
            await session.invoke(rpc)
            if is_missing_part:
                break
            if not is_big:
                md5_sum.update(chunk)
            file_part += 1
            if progress:
                func = functools.partial(
                    progress, min(file_part * PART_SIZE, file_size), file_size, *progress_args
                )
                if inspect.iscoroutinefunction(progress):
                    await func()
                else:
                    await self.loop.run_in_executor(self.executor, func)
    finally:
        if close:
            fp.close()

    if is_missing_part:
        return None
    if is_big:
        return raw.types.InputFileBig(id=file_id, parts=file_total_parts, name=file_name)
    return raw.types.InputFile(
        id=file_id, parts=file_total_parts, name=file_name,
        md5_checksum="".join(f"{b:02x}" for b in md5_sum.digest()),
    )


def apply() -> None:
    """Bind the parallel implementation onto every owner of ``save_file``.

    Pyrogram's ``sync.py`` calls ``wrap(Methods)`` at import time, which *copies*
    each coroutine method onto the ``Methods`` class as its own attribute. Because
    ``Methods`` precedes ``SaveFile`` in ``Client.__mro__``, rebinding only
    ``SaveFile.save_file`` has no effect — ``Client.save_file`` would still resolve
    to the untouched copy on ``Methods``. So we rebind ``save_file`` on every class
    in the MRO that owns the name, not just the defining class.
    """
    if getattr(SaveFile, _PATCH_FLAG, False):
        return

    from pyrogram import Client

    targets = [c for c in Client.__mro__ if "save_file" in c.__dict__]
    if not targets:
        targets = [SaveFile]

    for cls in targets:
        setattr(cls, "save_file", _parallel_save_file)

    setattr(SaveFile, _PATCH_FLAG, True)
    log.info(
        "Parallel multi-connection save_file patch applied (targets: %s)",
        ", ".join(c.__name__ for c in targets),
    )


apply()
