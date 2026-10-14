# -*- coding: utf-8 -*-
"""手动发送的排队与线程安全桥接。

插件的控制台是标准库的 ``http.server``，跑在独立线程里；而 aiohttp 的会话
必须只在拥有它的事件循环里使用。所以控制台的请求不能直接 await，而是：

1. 控制台线程调用 :meth:`DanmakuDispatcher.submit_threadsafe`，
   用 ``asyncio.run_coroutine_threadsafe`` 把协程丢进事件循环；
2. :class:`DanmakuDispatcher` 内部只有一个消费者任务，按顺序取出请求发送；
3. 发送结果通过 ``concurrent.futures.Future`` 回传，控制台线程 ``result()`` 等结果。

这样既保证了发送串行（配合 ``min_send_interval`` 限速），又不会跨线程用会话。
"""
import asyncio
import collections
import concurrent.futures
import logging
import time
from typing import *

from bilibili_live import errors

logger = logging.getLogger('send-danmu.' + __name__)

_loop: Optional[asyncio.AbstractEventLoop] = None
"""插件的事件循环。控制台线程要用它来跨线程提交任务。

注意不能在控制台线程里调用 ``asyncio.get_event_loop()``：非主线程没有默认
事件循环，会直接抛 RuntimeError。所以在事件循环里启动时把引用存下来。
"""


def set_loop(loop: asyncio.AbstractEventLoop):
    global _loop
    _loop = loop


def get_loop() -> Optional[asyncio.AbstractEventLoop]:
    if _loop is None or _loop.is_closed():
        return None
    return _loop


class SendRequest:
    def __init__(self, room_id: int, content: str, *, source: str = 'manual'):
        self.room_id = room_id
        self.content = content
        self.source = source
        self.created_at = time.time()
        self.future: concurrent.futures.Future = concurrent.futures.Future()

    def to_dict(self) -> Dict[str, Any]:
        return {
            'roomId': self.room_id,
            'content': self.content,
            'source': self.source,
            'createdAt': self.created_at,
        }


class DanmakuDispatcher:
    """串行的弹幕发送队列"""

    def __init__(self, app):
        self._app = app
        self._queue: asyncio.Queue = asyncio.Queue()
        self._consumer: Optional[asyncio.Task] = None
        self._dropped = 0

    async def start(self):
        set_loop(asyncio.get_running_loop())
        self._queue = asyncio.Queue()
        self._consumer = asyncio.create_task(self._consume(), name='danmaku-dispatcher')

    async def stop(self):
        if self._consumer is not None:
            self._consumer.cancel()
            try:
                await self._consumer
            except asyncio.CancelledError:
                pass
            self._consumer = None

        # 队列里没处理完的请求统一失败，避免控制台一直等
        while not self._queue.empty():
            request: SendRequest = self._queue.get_nowait()
            if not request.future.done():
                request.future.set_exception(RuntimeError('插件正在关闭'))

    # ------------------------------------------------------------- 提交请求

    def submit_threadsafe(self, room_id: int, content: str, *, source: str = 'manual'):
        """给控制台线程用的提交入口，返回 concurrent.futures.Future"""
        future: concurrent.futures.Future = concurrent.futures.Future()

        loop = get_loop()
        if loop is None:
            future.set_exception(RuntimeError('插件事件循环未运行'))
            return future

        max_queue = self._app.config.max_send_queue
        if self._queue.qsize() >= max_queue:
            self._dropped += 1
            future.set_exception(RuntimeError(f'发送队列已满（{max_queue}），请稍后再试'))
            return future

        request = SendRequest(room_id, content, source=source)
        request.future = future

        try:
            loop.call_soon_threadsafe(self._queue.put_nowait, request)
        except RuntimeError as e:  # 事件循环已经停了
            future.set_exception(RuntimeError(f'无法提交发送请求：{e}'))
        return future

    async def submit(self, room_id: int, content: str, *, source: str = 'auto'):
        """给事件循环内部用的提交入口，走同一条串行通道，返回 SendResult"""
        request = SendRequest(room_id, content, source=source)
        await self._queue.put(request)
        return await asyncio.shield(asyncio.wrap_future(request.future))

    # --------------------------------------------------------------- 消费者

    async def _consume(self):
        while True:
            request: SendRequest = await self._queue.get()
            try:
                result = await self._app.do_send(request.room_id, request.content)
                if not request.future.done():
                    request.future.set_result(result)
            except errors.DanmakuError as e:
                logger.warning('Send failed (room=%s): %s', request.room_id, e)
                if not request.future.done():
                    request.future.set_exception(e)
            except asyncio.CancelledError:
                if not request.future.done():
                    request.future.set_exception(RuntimeError('插件正在关闭'))
                raise
            except Exception as e:  # noqa: BLE001 单条失败不能拖垮消费者
                logger.exception('Unexpected error while sending:')
                if not request.future.done():
                    request.future.set_exception(e)
            finally:
                self._queue.task_done()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        return self._dropped
