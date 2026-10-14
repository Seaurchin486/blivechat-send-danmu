# -*- coding: utf-8 -*-
"""插件的核心对象：把 SDK、B站弹幕发送、规则引擎、控制台串起来。

所有会影响 aiohttp 会话的操作都在事件循环线程里做；控制台线程只通过
:class:`sender.DanmakuDispatcher` 提交请求，再通过:class:`rooms.RoomRegistry`
和:class:`config.ConfigStore` 读取状态。
"""
import asyncio
import concurrent.futures
import logging
import webbrowser
from typing import *

import aiohttp

import config
import logbuf
import sender
from bilibili_live import credentials as creds_mod
from bilibili_live import danmaku as danmaku_mod
from bilibili_live import errors
from rooms import RoomRegistry
from rules import AutoReplyEngine
from sender import DanmakuDispatcher

logger = logging.getLogger('send-danmu.' + __name__)

_app: Optional['App'] = None


def get_app() -> Optional['App']:
    return _app


def set_app(app: 'App'):
    global _app
    _app = app


class App:
    def __init__(self, config_store: config.ConfigStore):
        self.config_store = config_store

        self.session: Optional[aiohttp.ClientSession] = None
        self.client: Optional[danmaku_mod.DanmakuClient] = None
        self.dispatcher = DanmakuDispatcher(self)
        self.engine = AutoReplyEngine(config_store.get())
        self.rooms = RoomRegistry()

        self.panel = None
        self.sdk_version = ''
        self.blc_version = ''
        self.plugin_id = ''
        self._started = False
        self._main_room_id: Optional[int] = None
        """已经解析过真实ID的主房间"""

        self._log_handler: Optional[logbuf.QueueLogHandler] = None
        self._buffer_handler: Optional[logbuf.BufferLogHandler] = None
        self._log_task: Optional[asyncio.Task] = None

    @property
    def config(self) -> config.AppConfig:
        return self.config_store.get()

    # ------------------------------------------------------------- 生命周期

    async def start(self):
        if self._started:
            return
        self._started = True

        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.send_timeout),
        )
        self.client = danmaku_mod.DanmakuClient(self.config_store, session=self.session)

        # 先把日志处理器挂上，这样下面读凭据的日志也能进控制台和 blivechat
        self._start_log_forwarding()

        try:
            await self.client.reload_credentials()
        except creds_mod.CredentialsError as e:
            # 凭据问题不应该让插件直接退出：控制台还能打开，用户可以看到原因并修好
            logger.error('无法加载登录凭据，自动回复和手动发送都会被拒绝：\n%s', e)

        self._sync_own_author()

        await self.dispatcher.start()

        if self.config.panel_enabled:
            await self._start_panel()

        logger.info('插件已启动')

    # ------------------------------------------------------- 日志转发到 blivechat

    def _start_log_forwarding(self):
        """挂上日志处理器。

        * 内存缓冲：控制台的"日志"面板要能看到最近发生了什么
        * 转发到 blivechat：让 blivechat 的日志文件里也有插件的日志
        """
        self._log_handler = logbuf.QueueLogHandler()
        self._log_handler.setFormatter(logging.Formatter('{message}', style='{'))

        # 内存缓冲的格式带上时间戳，控制台上看起来更清楚
        self._buffer_handler = logbuf.BufferLogHandler()
        self._buffer_handler.setFormatter(logging.Formatter('{message}', style='{'))

        root_logger = logging.getLogger()
        # 确保 INFO 级别的日志也会被处理。库默认是 WARNING，如果不显式设置，
        # 我们自己的 logger.info(...) 根本不会产生日志记录。
        if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
            root_logger.setLevel(logging.INFO)
        root_logger.addHandler(self._log_handler)
        root_logger.addHandler(self._buffer_handler)

        self._log_task = asyncio.create_task(self._forward_logs(), name='log-forwarder')

    async def _forward_logs(self):
        import blcsdk

        while True:
            try:
                if self._log_handler is not None and self._log_handler.pending > 0:
                    # 有待发日志就尽快刷出去，别让用户等
                    await asyncio.sleep(0)
                else:
                    await asyncio.sleep(0.5)

                assert self._log_handler is not None
                for level, message in self._log_handler.drain():
                    try:
                        await blcsdk.log(message, level)
                    except Exception:  # noqa: BLE001 blivechat 断开时不该影响插件
                        break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 转发失败不能拖垮插件
                logger.debug('日志转发失败', exc_info=True)
                await asyncio.sleep(2)

    def _stop_log_forwarding(self):
        if self._log_task is not None:
            self._log_task.cancel()
            self._log_task = None

        root_logger = logging.getLogger()
        if self._log_handler is not None:
            root_logger.removeHandler(self._log_handler)
            self._log_handler = None
        if self._buffer_handler is not None:
            root_logger.removeHandler(self._buffer_handler)
            self._buffer_handler = None

    def _sync_own_author(self):
        """把自己的昵称告诉规则引擎，用于精确识别自己弹幕的回声"""
        if self.client is None or self.client.credentials is None:
            return
        self.engine.set_own_author(self.client.credentials.user_name)

    async def shut_down(self):
        if not self._started:
            return
        self._started = False

        self._stop_log_forwarding()

        await self.dispatcher.stop()

        if self.panel is not None:
            self.panel.stop()
            self.panel = None

        if self.client is not None:
            await self.client.close()
            self.client = None

        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None

        logger.info('插件已停止')

    async def _start_panel(self):
        try:
            from panel import PanelServer

            self.panel = PanelServer(self)
            self.panel.start()
        except OSError as e:
            logger.error(
                '控制台启动失败（端口 %s 可能被占用）：%s。可以把 [panel] port 改成别的端口，'
                '或设置 [panel] enabled = false 关掉控制台。',
                self.config.panel_port, e,
            )
            self.panel = None

    # --------------------------------------------------------------- 发送

    async def resolve_room_id(self, room_id: Optional[int] = None) -> int:
        """决定这条弹幕发给哪个房间"""
        if room_id:
            return room_id
        if self.config.auth_code:
            # 优先使用身份码查找房间
            resolved = await self._resolve_room_by_auth_code(self.config.auth_code)
            if resolved:
                return resolved
        if self.config.room_id:
            return self.config.room_id

        room_id = self.rooms.primary_room_id
        if room_id is None:
            # 还没收到任何房间消息时，允许手动指定；否则只能报错
            raise errors.DanmakuError(
                -400,
                '还不知道要发到哪个房间。请先在 blivechat 里连接一个房间，'
                '或在 data/config.ini 的 [account] room_id 或 auth_code 里手动指定。',
            )

        # blivechat 给的 room_id 已经是真实房间ID，这里只做一次兜底解析
        if self._main_room_id != room_id:
            resolved = await self.client.get_real_room_id(room_id)
            self._main_room_id = resolved
            self.rooms.set_primary(resolved)
        return self._main_room_id

    async def _resolve_room_by_auth_code(self, auth_code: str) -> Optional[int]:
        """通过身份码查找对应的真实房间ID"""
        # 先在已知房间列表里找
        for info in self.rooms._rooms.values():
            if info.room_key is not None and info.room_key.type == 2:  # AUTH_CODE
                if str(info.room_key.value) == auth_code:
                    if info.room_id is not None:
                        return info.room_id
                    # 如果 room_id 还没初始化，尝试解析
                    break

        # 如果已知列表里没找到，尝试通过 blcsdk 查询
        import blcsdk
        try:
            rooms = await blcsdk.get_rooms()
            for r in rooms:
                if r.room_key.type == blcsdk.models.RoomKeyType.AUTH_CODE:
                    if str(r.room_key.value) == auth_code:
                        if r.room_id is not None:
                            return r.room_id
        except Exception:
            pass

        return None

    async def do_send(self, room_id: int, content: str) -> danmaku_mod.SendResult:
        """真正发出去。由 dispatcher 的消费者任务调用"""
        assert self.client is not None, 'App is not started'

        content = danmaku_mod.truncate(content, self.config.max_length)
        if not content:
            raise errors.DanmakuError(-400, '弹幕内容为空')

        result = await self.client.send(room_id, content)

        if not result.ok:
            # code==0 但 B站用 message 告诉我们其实没发出去（限流/重复）。
            # 这种情况不能当成功，否则规则引擎会以为自己发过而被回声干扰
            raise errors.DanmakuError(
                10031 if 'msg in 1s' in (result.error or '').lower() else -400,
                f'B站未接受这条弹幕：{result.error or "未知原因"}',
            )

        if not result.dry_run:
            # 告诉规则引擎“这条是我们发的”，避免回声被当成观众弹幕再回一次
            self._sync_own_author()
            author = ''
            if self.client.credentials is not None:
                author = self.client.credentials.user_name
            self.engine.note_our_message(content, author=author)

        # 送到 blivechat 弹幕栏。放在 note_our_message 之后，双保险防回环。
        await self.broadcast_text(content, room_id=room_id)

        return result

    async def send_now(self, content: str, room_id: Optional[int] = None, *,
                       source: str = 'auto') -> danmaku_mod.SendResult:
        """给事件循环内部调用（自动回复路径）"""
        target_room = await self.resolve_room_id(room_id)
        return await self.dispatcher.submit(target_room, content, source=source)

    # --------------------------------------------------- 控制台线程用的入口

    def send_threadsafe(self, content: str, room_id: Optional[int] = None) -> concurrent.futures.Future:
        """给控制台线程调用，返回 concurrent.futures.Future"""
        future: concurrent.futures.Future = concurrent.futures.Future()

        loop = sender.get_loop()
        if loop is None:
            future.set_exception(RuntimeError('插件未运行'))
            return future

        async def _send():
            target_room = await self.resolve_room_id(room_id)
            return await self.dispatcher.submit(target_room, content, source='manual')

        # run_coroutine_threadsafe 返回的 future 就是我们要回传的那个
        return asyncio.run_coroutine_threadsafe(_send(), loop)

    def reload_config_threadsafe(self) -> concurrent.futures.Future:
        """给控制台线程调用：重新读取配置文件"""
        loop = sender.get_loop()
        if loop is None:
            future: concurrent.futures.Future = concurrent.futures.Future()
            future.set_exception(RuntimeError('插件未运行'))
            return future

        async def _reload():
            return await self.reload_config()

        return asyncio.run_coroutine_threadsafe(_reload(), loop)

    def reload_credentials_threadsafe(self) -> concurrent.futures.Future:
        loop = sender.get_loop()
        if loop is None:
            future: concurrent.futures.Future = concurrent.futures.Future()
            future.set_exception(RuntimeError('插件未运行'))
            return future

        async def _reload():
            assert self.client is not None
            return await self.client.reload_credentials()

        return asyncio.run_coroutine_threadsafe(_reload(), loop)

    # ------------------------------------------------------- 播报到 blivechat

    async def broadcast_text(self, content: str, *, room_id: Optional[int] = None,
                             author_name: str = '') -> bool:
        """把我们发出的内容也显示在 blivechat 弹幕栏里。

        用的是 SDK 的 ``send_text``，会以插件身份显示，和真实观众弹幕可以靠作者名
        区分。插件自己也会收到这条消息（``isFromPlugin=True``），规则引擎会直接
        忽略它，所以不会自我循环。
        """
        if not self.config.plugin_broadcast:
            return False

        import blcsdk

        room_key = self.rooms.find_room_key(room_id)
        if room_key is None and room_id is None:
            room_key = self.rooms.primary_room_key()

        try:
            await blcsdk.send_text(
                content,
                author_name or self.config.broadcast_author,
                room_key=room_key,
            )
            return True
        except Exception as e:  # noqa: BLE001 只是显示不出来，不该影响发送本身
            logger.warning('把消息显示到 blivechat 弹幕栏失败：%r', e)
            return False

    def broadcast_text_threadsafe(self, content: str, *, room_id: Optional[int] = None,
                                  author_name: str = '') -> concurrent.futures.Future:
        """给控制台线程用的播报入口"""
        loop = sender.get_loop()
        if loop is None:
            future: concurrent.futures.Future = concurrent.futures.Future()
            future.set_exception(RuntimeError('插件未运行'))
            return future

        async def _broadcast():
            return await self.broadcast_text(content, room_id=room_id, author_name=author_name)

        return asyncio.run_coroutine_threadsafe(_broadcast(), loop)

    # ------------------------------------------------------------ 配置热重载

    async def reload_config(self) -> Dict[str, Any]:
        ok = self.config_store.reload()
        if not ok:
            return {'ok': False, 'error': self.config_store.error}

        self.engine.reload(self.config)
        logger.info('配置已重载：%d 条规则', len(self.config.rules))

        # cookie 文件可能被登录插件换过，顺便重新读一次
        try:
            assert self.client is not None
            await self.client.reload_credentials()
            self._sync_own_author()
        except creds_mod.CredentialsError as e:
            logger.warning('重载配置后读取凭据失败：%s', e)

        if self.config.panel_enabled and self.panel is None:
            await self._start_panel()

        return {'ok': True, 'rules': len(self.config.rules)}

    # --------------------------------------------------------------- 状态

    def mark_room_init(self, room_key, room_id: Optional[int], is_success: bool):
        if is_success and room_id is not None:
            self.rooms.add(room_key, room_id, init_success=True)
            if self._main_room_id is None:
                self._main_room_id = room_id
                self.rooms.set_primary(room_id)
        else:
            self.rooms.add(room_key, room_id, init_success=False)

    def mark_room_removed(self, room_key):
        self.rooms.remove(room_key)

    def status(self) -> Dict[str, Any]:
        creds = self.client.credentials if self.client is not None else None
        panel_url = self.panel.url if self.panel is not None else ''
        return {
            'pluginId': self.plugin_id,
            'sdkVersion': self.sdk_version,
            'blcVersion': self.blc_version,
            'configPath': self.config_store.path,
            'configLoaded': self.config_store.loaded,
            'configError': self.config_store.error,
            'credentials': creds.to_dict() if creds is not None else None,
            'rooms': self.rooms.list(),
            'primaryRoomId': self.rooms.primary_room_id,
            'configuredRoomId': self.config.room_id,
            'dryRun': self.config.dry_run,
            'autoReplyEnabled': self.config.auto_reply_enabled,
            'rules': self.engine.describe_rules(),
            'sendStats': self.client.stats if self.client is not None else {},
            'queuePending': self.dispatcher.pending,
            'queueDropped': self.dispatcher.dropped,
            'panelUrl': panel_url,
            'minSendInterval': self.config.min_send_interval,
            'maxLength': self.config.max_length,
        }

    def open_panel(self):
        """用户点了 blivechat 插件列表里的“管理”按钮"""
        if self.panel is None:
            logger.warning('控制台未启用，无法打开。请检查 [panel] enabled / port 配置')
            return False

        url = self.panel.url
        logger.info('打开控制台：%s', url)
        if not self.config.panel_auto_open:
            return False
        try:
            webbrowser.open(url)
            return True
        except Exception as e:  # noqa: BLE001 打不开浏览器不算致命
            logger.warning('打开浏览器失败：%r，请手动访问 %s', e, url)
            return False
