"""Shared writer cancellation handling and isolated repository read views."""

import asyncio
from copy import copy
from functools import wraps


async def rollback_before_release(connection):
    # A second cancellation must not release the writer while rollback is queued.
    task = asyncio.create_task(connection.rollback())
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    task.result()


def serialized_write(method):
    @wraps(method)
    async def wrapper(self, *args, **kwargs):
        async with self.write_lock:
            writer = self
            if self.db is not self._writer_db:
                writer = copy(self)
                writer.db = self._writer_db
            try:
                return await method(writer, *args, **kwargs)
            except BaseException:
                await rollback_before_release(writer.db)
                raise

    return wrapper


def snapshot_read(method):
    @wraps(method)
    async def wrapper(self, *args, **kwargs):
        if self.reader is not None:
            view = copy(self)
            view.db = self.reader
            return await method(view, *args, **kwargs)
        if self.database is None:
            return await method(self, *args, **kwargs)
        async with self.database.read_connection() as reader:
            view = copy(self)
            view.db = view.reader = reader
            return await method(view, *args, **kwargs)

    return wrapper
