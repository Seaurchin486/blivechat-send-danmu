# -*- coding: utf-8 -*-
"""与 blivechat 的连接看门狗。

正常情况下 blivechat 每 30 秒会发一次心跳，如果它退出了，WebSocket 会被关掉，
``BlcPluginClient`` 自然就会触发 ``on_client_stopped``，插件跟着退出。

但有两种情况下收不到关闭通知，插件会变成一个没人管的僵尸进程：

* 网络被中途掐断（没有正常的 close 帧）
* blivechat 进程被强杀

所以这里再加一道保险：如果长时间收不到 blivechat 的任何消息（包括心跳），就认为
连接已经死了，主动走退出流程。阈值参考 blivechat 自己判断插件死亡的
``HEARTBEAT_INTERVAL + 5``（见 blivechat 的 ``api/plugin.py``）。
"""
import asyncio
import logging
from typing import *

logger = logging.getLogger('send-danmu.' + __name__)


class ConnectionWatchdog:
    def __init__(self, timeout: float, on_dead):
        """
        :param timeout: 多久收不到消息就认为连接死了（秒）
        :param on_dead: 判定死亡时调用的无参回调
        """
        self._timeout = max(5.0, timeout)
        self._on_dead = on_dead
        self._last_seen = 0.0
        self._task: Optional[asyncio.Task] = None
        self._fired = False

    def start(self):
        self._last_seen = asyncio.get_running_loop().time()
        self._fired = False
        self._task = asyncio.create_task(self._run(), name='connection-watchdog')
        logger.debug('连接看门狗已启动，超时 %.0f 秒', self._timeout)

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def touch(self):
        """每收到一条 blivechat 的消息就调一次"""
        self._last_seen = asyncio.get_running_loop().time()

    async def _run(self):
        # 检查间隔取超时的四分之一，最多 5 秒，保证及时又不浪费
        interval = max(1.0, min(5.0, self._timeout / 4))
        while True:
            await asyncio.sleep(interval)
            idle = asyncio.get_running_loop().time() - self._last_seen
            if idle >= self._timeout:
                self._fired = True
                logger.warning(
                    '已经 %.0f 秒没有收到 blivechat 的任何消息，认为连接已断开，插件即将退出',
                    idle,
                )
                try:
                    self._on_dead()
                except Exception:  # noqa: BLE001 回调出错也不能让看门狗崩掉
                    logger.exception('连接看门狗回调出错：')
                return

    @property
    def fired(self) -> bool:
        return self._fired
