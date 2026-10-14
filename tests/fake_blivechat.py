# -*- coding: utf-8 -*-
"""测试用的假 blivechat 插件服务。

按 blivechat 的插件协议实现最小服务端：

* ``/api/plugin/websocket`` 接受带 ``Authorization: Bearer <token>`` 的 WebSocket
* 连上后立刻发 ``BLC_INIT``（插件才算启动完成）
* ``GET /api/plugin/rooms`` 返回房间列表（插件启动时会拉一次）
* 可以主动把直播间消息推给插件
* 收集插件发回来的消息，供测试断言

协议细节来自 blivechat 的 ``services/plugin.py`` 和 ``blcsdk``。
"""
import asyncio
import json
import logging
from typing import *

from aiohttp import web

# 复用 blivechat 官方的协议常量，避免自己猜数字
from blcsdk.models import Command

logger = logging.getLogger('fake-blc')

PLUGIN_ID = 'send-danmu-test'


class FakeBlivechat:
    def __init__(self, token: str, *, blc_version: str = '1.12.0', sdk_version: str = '1.0.1'):
        self.token = token
        self.blc_version = blc_version
        self.sdk_version = sdk_version

        self.received: List[dict] = []
        """插件发回来的所有消息"""
        self.connected = asyncio.Event()
        self._ws = None
        self._app = web.Application()
        self._app.router.add_get('/api/plugin/websocket', self._handle_ws)
        self._app.router.add_get('/api/plugin/rooms', self._handle_rooms)
        self._runner = None
        self._site = None
        self.port = 0

        self.rooms: List[dict] = []

    # ------------------------------------------------------------ 生命周期

    async def start(self):
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, '127.0.0.1', 0)
        await self._site.start()
        self.port = self._site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._ws is not None:
            await self._ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    # ---------------------------------------------------------------- 处理

    async def _handle_rooms(self, request: web.Request):
        auth = request.headers.get('Authorization', '')
        if auth != f'Bearer {self.token}':
            return web.json_response({'error': 'bad token'}, status=403)
        return web.json_response({'rooms': self.rooms})

    async def _handle_ws(self, request: web.Request):
        auth = request.headers.get('Authorization', '')
        if auth != f'Bearer {self.token}':
            return web.json_response({'error': 'bad token'}, status=403)

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws = ws

        # 连上就先发初始化消息
        await self._send(Command.BLC_INIT, {
            'blcVersion': self.blc_version,
            'sdkVersion': self.sdk_version,
            'pluginId': PLUGIN_ID,
        })
        self.connected.set()

        try:
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    continue
                try:
                    self.received.append(json.loads(message.data))
                except ValueError:
                    logger.warning('Bad JSON from plugin: %r', message.data)
        finally:
            self._ws = None
        return ws

    # ------------------------------------------------------------ 推送消息

    async def _send(self, cmd, data, extra=None):
        if self._ws is None:
            raise RuntimeError('插件还没连上来')
        body = {'cmd': int(cmd), 'data': data}
        if extra is not None:
            body['extra'] = extra
        await self._ws.send_json(body)

    async def add_room(self, room_id: int):
        room_key = {'type': 1, 'value': room_id}
        self.rooms = [{'roomId': room_id, 'roomKey': room_key}]
        await self._send(Command.ADD_ROOM, {}, {'roomId': None, 'roomKey': room_key})
        await self._send(Command.ROOM_INIT, {'isSuccess': True},
                         {'roomId': room_id, 'roomKey': room_key})

    async def add_room_with_key(self, room_id: int, room_key: dict):
        self.rooms = [{'roomId': room_id, 'roomKey': room_key}]
        await self._send(Command.ADD_ROOM, {}, {'roomId': None, 'roomKey': room_key})
        await self._send(Command.ROOM_INIT, {'isSuccess': True},
                         {'roomId': room_id, 'roomKey': room_key})

    async def send_danmaku(self, content: str, author: str = '观众甲', uid: str = '1001', *,
                           room_id: int = 5440, message_id: str = 'm1',
                           author_level: int = 10, medal_level: int = 0,
                           medal_name: str = '', guard_level: int = 0,
                           is_mirror: bool = False, content_type: int = 0):
        """按 AddTextMsg 的数组协议推送一条弹幕"""
        # 数组顺序见 blcsdk.models.AddTextMsg.from_command
        data = [
            '',                       # 0 头像
            1700000000,               # 1 时间戳
            author,                   # 2 用户名
            0,                        # 3 用户类型
            content,                  # 4 内容
            guard_level,              # 5 舰队等级
            0,                        # 6 是否礼物弹幕
            author_level,             # 7 用户等级
            True,                     # 8 是否正式会员
            True,                     # 9 是否绑定手机
            medal_level,              # 10 勋章等级
            message_id,               # 11 消息ID
            '',                       # 12 翻译
            content_type,             # 13 内容类型
            [''],                     # 14 内容参数
            {},                       # 15 内部
            uid,                      # 16 uid
            medal_name,               # 17 勋章名
            is_mirror,                # 18 是否跨房
        ]
        room_key = {'type': 1, 'value': room_id}
        await self._send(Command.ADD_TEXT, data, {'roomId': room_id, 'roomKey': room_key})

    async def open_admin_ui(self):
        await self._send(Command.OPEN_PLUGIN_ADMIN_UI, {}, {'roomId': None, 'roomKey': None})

    # -------------------------------------------------------------- 等待工具

    def messages_of(self, cmd: Command) -> List[dict]:
        return [m for m in self.received if m.get('cmd') == int(cmd)]

    async def wait_for(self, cmd: Command, *, count: int = 1, timeout: float = 10.0) -> List[dict]:
        """等插件发回 count 条指定类型的消息"""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            got = self.messages_of(cmd)
            if len(got) >= count:
                return got
            await asyncio.sleep(0.05)
        raise AssertionError(
            f'等 {cmd.name} 超时（期望 {count} 条，实际 {len(self.messages_of(cmd))} 条）。'
            f'已收到：{self.received}'
        )

    async def wait_for_log(self, substring: str, *, timeout: float = 15.0) -> dict:
        """等一条包含指定文字的日志消息"""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for m in self.messages_of(Command.LOG_REQ):
                if substring in str(m.get('data', {}).get('msg', '')):
                    return m
            await asyncio.sleep(0.05)

        logs = [m.get('data', {}).get('msg') for m in self.messages_of(Command.LOG_REQ)]
        # 诊断用：顺便把插件内存里的日志缓冲也带上，便于判断是"没记录"还是"没转发"
        try:
            from logbuf import LOG_BUFFER

            buffer_entries = [e.get('message') for e in LOG_BUFFER.snapshot(limit=40)['entries']]
        except Exception as e:  # noqa: BLE001
            buffer_entries = [f'<读取日志缓冲失败：{e!r}>']

        raise AssertionError(
            f'等日志 {substring!r} 超时。\n'
            f'已转发到 blivechat 的日志（{len(logs)} 条）：{logs[-30:]}\n'
            f'插件内存日志缓冲：{buffer_entries[-30:]}'
        )
