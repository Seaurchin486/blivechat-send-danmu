# -*- coding: utf-8 -*-
"""blivechat 插件消息处理。

插件通过 blcsdk 连上 blivechat 后，直播间消息会以 ``AddTextMsg`` 等形式推过来。
这里负责：

* 维护房间列表（自动回复要知道发到哪个房间）
* 收到弹幕 -> 交给 :class:`rules.AutoReplyEngine` 决策 -> 发出去
* 把“谁说了什么、触发了哪条规则”写进日志，方便控制台排查

blivechat 的插件协议要求用户在界面上开启“通过服务器转发消息”，否则收不到消息。
"""
import logging
from typing import *

import blcsdk
import blcsdk.models as sdk_models

import app as app_mod
from bilibili_live import errors
from rules import IncomingDanmaku
from watchdog import ConnectionWatchdog

logger = logging.getLogger('send-danmu.' + __name__)

_handler: Optional['MsgHandler'] = None


def init(app: app_mod.App, watchdog: ConnectionWatchdog):
    global _handler
    _handler = MsgHandler(app)
    # 用一层包装接收所有命令，一边转给插件处理器，一边给看门狗报活
    blcsdk.set_msg_handler(_WatchedHandler(_handler, watchdog))
    logger.info('消息处理器已注册')


def shut_down():
    blcsdk.set_msg_handler(None)


class _WatchedHandler(blcsdk.HandlerInterface):
    """把所有消息喂给看门狗，再转给真正的处理器"""

    def __init__(self, inner: 'MsgHandler', watchdog: ConnectionWatchdog):
        self._inner = inner
        self._watchdog = watchdog

    def handle(self, client: blcsdk.BlcPluginClient, command: dict):
        self._watchdog.touch()
        self._inner.handle(client, command)

    def on_client_stopped(self, client: blcsdk.BlcPluginClient, exception: Optional[Exception]):
        self._inner.on_client_stopped(client, exception)


class MsgHandler(blcsdk.BaseHandler):
    def __init__(self, app: app_mod.App):
        self._app = app

    # ------------------------------------------------------------ 生命周期

    def on_client_stopped(self, client: blcsdk.BlcPluginClient, exception: Optional[Exception]):
        if exception is not None:
            logger.error('与 blivechat 的连接异常断开：%r', exception)
        else:
            logger.info('与 blivechat 的连接已断开（blivechat 退出或插件被禁用）')
        # blivechat 没了，插件继续跑也没有意义，让主程序收尾退出
        import main as main_mod
        main_mod.start_shut_down()

    # ---------------------------------------------------------------- 房间

    def _on_add_room(
        self, client: blcsdk.BlcPluginClient, message: sdk_models.AddRoomMsg, extra: sdk_models.ExtraData
    ):
        if extra.is_from_plugin:
            return
        self._app.mark_room_init(extra.room_key, extra.room_id, is_success=False)
        logger.info('blivechat 添加了房间 %s（尚未初始化）', extra.room_key)

    def _on_room_init(
        self, client: blcsdk.BlcPluginClient, message: sdk_models.RoomInitMsg, extra: sdk_models.ExtraData
    ):
        if extra.is_from_plugin:
            return
        self._app.mark_room_init(extra.room_key, extra.room_id, is_success=message.is_success)
        if message.is_success:
            logger.info('房间 %s 初始化成功，room_id=%s', extra.room_key, extra.room_id)
        else:
            logger.warning(
                '房间 %s 初始化失败（room_id=%s）。请检查这个房间在 blivechat 里是否连接正常。',
                extra.room_key, extra.room_id,
            )

    def _on_del_room(
        self, client: blcsdk.BlcPluginClient, message: sdk_models.DelRoomMsg, extra: sdk_models.ExtraData
    ):
        if extra.is_from_plugin:
            return
        self._app.mark_room_removed(extra.room_key)
        logger.info('blivechat 删除了房间 %s', extra.room_key)

    # ---------------------------------------------------------------- 弹幕

    def _on_add_text(
        self, client: blcsdk.BlcPluginClient, message: sdk_models.AddTextMsg, extra: sdk_models.ExtraData
    ):
        if extra.is_from_plugin:
            # 插件自己产生的消息，直接忽略，避免自我循环
            return

        self._app.mark_room_init(extra.room_key, extra.room_id, is_success=True)

        incoming = IncomingDanmaku(
            content=message.content or '',
            author=message.author_name or '',
            uid=message.uid or '',
            timestamp=message.timestamp or 0,
            author_level=message.author_level,
            medal_level=message.medal_level,
            medal_name=message.medal_name or '',
            guard_level=message.privilege_type,
            message_id=message.id or '',
            is_mirror=message.is_mirror,
            is_from_plugin=extra.is_from_plugin,
            room_id=extra.room_id or 0,
        )

        # 表情弹幕没有文字内容，规则匹配没有意义
        if message.content_type == sdk_models.ContentType.EMOTICON:
            logger.debug('%s 发了个表情弹幕，跳过', message.author_name)
            return

        decision = self._app.engine.decide(incoming)
        if not decision.should_reply:
            logger.debug('[弹幕] %s：%s（不回复：%s）', message.author_name, message.content, decision.reason)
            return

        logger.info(
            '[自动回复] 规则「%s」命中「%s」，%s 的弹幕：%s -> 回复：%s',
            decision.rule_id, decision.keyword, message.author_name, message.content, decision.reply,
        )

        if not decision.reply:
            logger.warning('规则「%s」算出的回复是空的，跳过', decision.rule_id)
            return

        # 必须在事件循环里用 create_task，不能在处理器里 await：
        # 处理器和网络协程在同一个协程，阻塞它会导致收不到后续消息
        import asyncio

        asyncio.create_task(self._send_reply(decision, incoming))

    async def _send_reply(self, decision, incoming: IncomingDanmaku):
        try:
            result = await self._app.send_now(decision.reply, incoming.room_id or None, source='auto')
        except errors.DanmakuError as e:
            logger.error('自动回复发送失败：%s', e)
            if e.auth_error:
                logger.error(
                    '看起来是登录态问题。请在控制台点“重读Cookie”，'
                    '或重新用官方的“登录B站账号”插件登录后重启 blivechat。'
                )
            elif e.retryable:
                logger.warning('B站限流/风控，请调大 data/config.ini 里的 min_send_interval 和 cooldown')
            return
        except Exception:  # noqa: BLE001
            logger.exception('自动回复发送时出现意外错误：')
            return

        if not result.dry_run:
            logger.info('[自动回复] 已发送：%s', result.content)

    # -------------------------------------------------------- 其它消息类型

    def _on_add_gift(
        self, client: blcsdk.BlcPluginClient, message: sdk_models.AddGiftMsg, extra: sdk_models.ExtraData
    ):
        if extra.is_from_plugin:
            return
        logger.debug('[礼物] %s 赠送 %s x%d', message.author_name, message.gift_name, message.num)

    def _on_add_member(
        self, client: blcsdk.BlcPluginClient, message: sdk_models.AddMemberMsg, extra: sdk_models.ExtraData
    ):
        if extra.is_from_plugin:
            return
        logger.debug('[上舰] %s 购买了 %d%s', message.author_name, message.num, message.unit)

    def _on_add_super_chat(
        self, client: blcsdk.BlcPluginClient, message: sdk_models.AddSuperChatMsg, extra: sdk_models.ExtraData
    ):
        if extra.is_from_plugin:
            return
        logger.debug('[醒目留言] %s（%d元）：%s', message.author_name, message.price, message.content)

    # -------------------------------------------------------------- 管理界面

    def _on_open_plugin_admin_ui(
        self, client: blcsdk.BlcPluginClient, message: sdk_models.OpenPluginAdminUiMsg, extra: sdk_models.ExtraData
    ):
        """用户在 blivechat 插件列表里点了“管理”"""
        if not self._app.open_panel():
            url = self._app.panel.url if self._app.panel is not None else ''
            if url:
                logger.info('请手动在浏览器里打开控制台：%s', url)
            else:
                logger.warning('控制台没有启动，请检查 data/config.ini 的 [panel] 配置')
