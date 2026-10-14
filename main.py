#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""blivechat「发弹幕」插件入口。

被 blivechat 启动后：

1. 读配置、起日志
2. 连上 blivechat（blcsdk）
3. 加载 B 站登录 cookie，建立弹幕发送客户端
4. 打开本地控制台（默认 http://127.0.0.1:12452）
5. 等信号，收到就收尾退出
"""
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from typing import *

# 先把随插件附带的 blcsdk 加进搜索路径，这样不用 pip install 也能跑
_VENDOR_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'vendor')
if os.path.isdir(_VENDOR_PATH) and _VENDOR_PATH not in sys.path:
    sys.path.insert(0, _VENDOR_PATH)

import blcsdk
import blcsdk.models as sdk_models

import app as app_mod
import config
import listener
from logbuf import BufferLogHandler
from watchdog import ConnectionWatchdog

logger = logging.getLogger('send-danmu')

shut_down_event: Optional[asyncio.Event] = None
_app: Optional[app_mod.App] = None
_watchdog: Optional[ConnectionWatchdog] = None
"""blivechat 突然断开时的兜底退出机制"""


async def main():
    try:
        await init()
        await run()
    except blcsdk.InitError as e:
        logger.error('无法连接 blivechat：%s', e)
        logger.error('这个插件必须由 blivechat 启动（需要 BLC_TOKEN / BLC_PORT 环境变量）。')
        return 1
    except Exception:  # noqa: BLE001
        logger.exception('插件启动失败：')
        return 1
    finally:
        await shut_down()
    return 0


async def init():
    init_signal_handlers()
    init_logging()

    # 配置要在起 App 之前读好，App 会立刻用它
    config.init()

    await blcsdk.init()
    if not blcsdk.is_sdk_version_compatible():
        raise RuntimeError(
            f'SDK 版本不兼容（插件自带 {blcsdk.__version__}，blivechat 用的是 '
            f'{blcsdk.get_blc_sdk_version()}）。请更新插件。'
        )

    await start_app()

    listener.init(_app, _start_watchdog())

    logger.info(
        '已启动。blivechat 版本 %s，插件ID %s。别忘了在 blivechat 里开启“通过服务器转发消息”，'
        '否则收不到弹幕。',
        blcsdk.get_blc_version(), blcsdk.get_plugin_id(),
    )


def _start_watchdog() -> ConnectionWatchdog:
    global _watchdog
    _watchdog = ConnectionWatchdog(config.get_store().get().connection_timeout, start_shut_down)
    _watchdog.start()
    return _watchdog


async def start_app():
    global _app
    _app = app_mod.App(config.get_store())
    _app.plugin_id = blcsdk.get_plugin_id()
    _app.sdk_version = blcsdk.get_blc_sdk_version()
    _app.blc_version = blcsdk.get_blc_version()
    app_mod.set_app(_app)
    await _app.start()
    return _app


def init_signal_handlers():
    global shut_down_event
    shut_down_event = asyncio.Event()

    signums = (signal.SIGINT, signal.SIGTERM)
    try:
        loop = asyncio.get_running_loop()
        for signum in signums:
            loop.add_signal_handler(signum, start_shut_down)
    except (NotImplementedError, RuntimeError):
        # Windows 上不支持 add_signal_handler，只能用 signal.signal
        for signum in signums:
            try:
                signal.signal(signum, _signal_handler)
            except (ValueError, OSError):
                pass


def _signal_handler(*_args):
    start_shut_down()


def start_shut_down():
    """可以被任意线程调用"""
    if shut_down_event is not None and not shut_down_event.is_set():
        shut_down_event.set()


def init_logging():
    os.makedirs(config.LOG_PATH, exist_ok=True)
    filename = os.path.join(config.LOG_PATH, 'send-danmu.log')

    formatter = logging.Formatter(
        '{asctime} {levelname} [{name}]: {message}', style='{'
    )

    stream_handler = logging.StreamHandler()
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename, encoding='utf-8', when='midnight', backupCount=7, delay=True
    )
    # 控制台要能看到日志，所以额外挂一个内存缓冲
    buffer_handler = BufferLogHandler()

    logging.basicConfig(
        format='{asctime} {levelname} [{name}]: {message}',
        style='{',
        level=logging.INFO,
        handlers=[stream_handler, file_handler, buffer_handler],
    )

    # aiohttp 的访问日志太吵
    logging.getLogger('aiohttp').setLevel(logging.WARNING)
    logging.getLogger('blcsdk').setLevel(logging.INFO)


async def run():
    logger.info('插件运行中，按 Ctrl+C 退出')
    await shut_down_event.wait()
    logger.info('开始退出…')


async def shut_down():
    listener.shut_down()

    if _watchdog is not None:
        await _watchdog.stop()

    if _app is not None:
        await _app.shut_down()

    try:
        await blcsdk.shut_down()
    except Exception:  # noqa: BLE001
        logger.debug('blcsdk shutdown failed', exc_info=True)


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
