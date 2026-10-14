# -*- coding: utf-8 -*-
"""端到端集成测试。

用假的 blivechat 服务跑真实的插件代码（真实的 blcsdk、真实的 ConfigStore、
真实的规则引擎、真实的控制台 HTTP 服务），只把「发到 B 站」这一步换成
``dry_run`` / 假的 aiohttp 响应，这样：

* 能验证插件真的连得上 blivechat、收得到弹幕
* 能验证自动回复的完整链路：收到弹幕 -> 规则命中 -> 发出去 -> 播报到前端
* 能验证「自己发出去的回声不会触发二次回复」这个最容易出事的地方
* 能真实验证控制台的手动发送 HTTP 接口
"""
import asyncio
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
# 插件自带的 blcsdk 在 vendor 下（main.py 启动时也会这么加）
sys.path.insert(0, os.path.join(_ROOT, 'vendor'))

from blcsdk.models import Command

from fake_blivechat import FakeBlivechat

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def reset_blcsdk():
    """把 blcsdk 的全局状态清干净。

    blcsdk 是"一个进程只能 init 一次"的设计（真实插件也只需要一次），
    所以集成测试之间必须显式复位，否则第二个测试会撞上
    ``InitError: Cannot call init() again``。

    另外顺手绕掉官方 SDK 里两处和现代 Python 不兼容的地方：
    ``CookieJar()`` 在没有运行中事件循环时会抛错，以及已废弃的 ``session.loop``
    断言。这两处在真实插件里跑在事件循环线程上，不会有问题。
    """
    import aiohttp

    import blcsdk
    import blcsdk.api as sdk_api

    # 关掉上一条连接
    client = sdk_api._plugin_client
    if client is not None:
        try:
            await client.stop_and_close()
        except Exception:  # noqa: BLE001 测试清理阶段不关心
            pass
    session = sdk_api._http_session
    if session is not None and not session.closed:
        await session.close()

    # 复位模块级单例状态
    sdk_api._plugin_client = None
    sdk_api._msg_handler = None
    sdk_api._msg_handler_wrapper = None
    sdk_api._init_msg = None
    sdk_api._http_session = None
    sdk_api._init_future = None
    sdk_api._blc_port = 0
    sdk_api._blc_base_url = ''
    sdk_api._token = ''

CONFIG_TEMPLATE = """[account]
cookie_file = {cookie_file}
room_id = 0

[danmaku]
min_send_interval = 0
max_length = 20
dry_run = {dry_run}
color = 16777215
fontsize = 25
mode = 1

[auto_reply]
enabled = true
cooldown = 0
template = {{content}}

[panel]
enabled = true
host = 127.0.0.1
port = {panel_port}
auto_open = false

[sdk]
broadcast = true
broadcast_author = [自动回复]

[rule-hi]
name = 打招呼
enabled = true
match_type = contains
keywords = 你好
reply = 你好呀
cooldown = 0
"""


class PluginHarness:
    """把插件按真实方式跑起来（同一个进程，避免子进程调试困难）"""

    def __init__(self, fake, tmp_path, *, dry_run=True, panel_port=0):
        self.fake = fake
        self.tmp_path = tmp_path
        self.dry_run = dry_run
        self.panel_port = panel_port
        self.app = None
        self.watchdog = None

    async def start(self):
        import config
        import app as app_mod
        import listener
        from watchdog import ConnectionWatchdog

        await reset_blcsdk()

        # 把插件的 data/log 指到临时目录，绝不碰真实环境
        base = self.tmp_path
        config.DATA_PATH = str(base / 'data')
        config.LOG_PATH = str(base / 'log')
        config.DEFAULT_CONFIG_PATH = os.path.join(config.DATA_PATH, 'config.ini')
        config.EXAMPLE_CONFIG_PATH = os.path.join(config.DATA_PATH, 'config.example.ini')
        config.BASE_PATH = str(base)
        os.makedirs(config.DATA_PATH, exist_ok=True)
        os.makedirs(config.LOG_PATH, exist_ok=True)

        # 造一份 cookie jar（两种格式都用真的写一遍）
        cookie_path = str(base / 'cookie_jar.pickle')
        _write_cookie_jar(cookie_path)

        port = self.panel_port
        with open(config.DEFAULT_CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write(CONFIG_TEMPLATE.format(
                cookie_file=cookie_path,
                dry_run='true' if self.dry_run else 'false',
                panel_port=port,
            ))

        config.init()
        store = config.get_store()

        import blcsdk
        os.environ['BLC_TOKEN'] = self.fake.token
        os.environ['BLC_PORT'] = str(self.fake.port)
        await blcsdk.init()
        assert blcsdk.is_sdk_version_compatible()

        self.app = app_mod.App(store)
        self.app.plugin_id = blcsdk.get_plugin_id()
        self.app.sdk_version = blcsdk.get_blc_sdk_version()
        self.app.blc_version = blcsdk.get_blc_version()
        app_mod.set_app(self.app)
        await self.app.start()

        # 看门狗超时设得很长，免得测试跑着跑着被它判定断开
        self.watchdog = ConnectionWatchdog(300.0, lambda: None)
        self.watchdog.start()
        listener.init(self.app, self.watchdog)
        return self

    async def stop(self):
        import listener

        listener.shut_down()
        if self.watchdog is not None:
            await self.watchdog.stop()
        if self.app is not None:
            await self.app.shut_down()
        await reset_blcsdk()


SESSDATA_VALUE = 'abcdefghijklmnop-sessdata-value'
JCT_VALUE = '0123456789abcdef0123456789abcdef'


def _write_cookie_jar(path):
    """写一份 JSON 格式的 cookie jar（新版 aiohttp 的格式）"""
    data = {
        'bilibili.com|': {
            'SESSDATA': {'key': 'SESSDATA', 'value': SESSDATA_VALUE, 'path': '/', 'domain': 'bilibili.com'},
            'bili_jct': {'key': 'bili_jct', 'value': JCT_VALUE, 'path': '/', 'domain': 'bilibili.com'},
        }
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)


@pytest.fixture
async def fake_blc():
    fake = FakeBlivechat(token='test-token-123')
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def plugin(fake_blc, tmp_path):
    harness = PluginHarness(fake_blc, tmp_path)
    await harness.start()
    try:
        yield harness
    finally:
        await harness.stop()


# ------------------------------------------------------------------ 基础连通

async def test_plugin_connects_and_reports_versions(plugin, fake_blc):
    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)

    assert plugin.app.blc_version == '1.12.0'
    assert plugin.app.sdk_version == '1.0.1'
    assert plugin.app.plugin_id == 'send-danmu-test'

    # 配置和凭据都读到了
    status = plugin.app.status()
    assert status['configLoaded'] is True
    assert status['credentials']['complete'] is True
    # 面板上只展示脱敏后的值，不能泄露完整 SESSDATA
    assert status['credentials']['sessdataPreview'] == 'abcd...alue'
    assert SESSDATA_VALUE not in json.dumps(status)
    assert status['autoReplyEnabled'] is True
    assert len(status['rules']) == 1


async def test_plugin_loads_existing_rooms_from_http_api(plugin, fake_blc):
    fake_blc.rooms = [{'roomId': 5440, 'roomKey': {'type': 1, 'value': 5440}}]
    # 重新拉一次，模拟启动时 get_rooms()
    import blcsdk

    rooms = await blcsdk.get_rooms()
    assert len(rooms) == 1
    assert rooms[0].room_id == 5440


# -------------------------------------------------------------- 自动回复链路

async def test_auto_reply_end_to_end(plugin, fake_blc):
    """收到「你好」-> 命中规则 -> 发弹幕 -> 播报到 blivechat"""
    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    await fake_blc.add_room(5440)
    await asyncio.sleep(0.2)

    await fake_blc.send_danmaku('你好呀主播', author='观众甲', uid='1001', message_id='m1')

    # 插件应该把「你好呀」播报回前端（dry_run 下不真的发 B站）
    texts = await fake_blc.wait_for(Command.ADD_TEXT_REQ, timeout=10)
    assert len(texts) == 1
    assert texts[0]['data']['content'] == '你好呀'

    # 日志里应该能看到命中了哪条规则
    await fake_blc.wait_for_log('打招呼')

    status = plugin.app.status()
    assert status['sendStats']['sent'] == 1


async def test_unmatched_danmaku_does_not_reply(plugin, fake_blc):
    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    await fake_blc.add_room(5440)
    await asyncio.sleep(0.2)

    await fake_blc.send_danmaku('完全无关的话', message_id='m1')
    await asyncio.sleep(0.5)

    assert fake_blc.messages_of(Command.ADD_TEXT_REQ) == []


async def test_duplicate_delivery_replies_once(plugin, fake_blc):
    """blivechat 重连时可能重放同一条弹幕，只能回一次"""
    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    await fake_blc.add_room(5440)
    await asyncio.sleep(0.2)

    await fake_blc.send_danmaku('你好', message_id='dup-1')
    await fake_blc.send_danmaku('你好', message_id='dup-1')
    await asyncio.sleep(0.5)

    assert len(fake_blc.messages_of(Command.ADD_TEXT_REQ)) == 1


async def test_own_echo_does_not_trigger_reply_loop(plugin, fake_blc):
    """最关键的一条：我们自己发出的弹幕会被 B站推回来，绝不能自己回自己。

    这里的做法：先把规则改成「命中『你好呀』就回『循环了』」，
    然后推一条我们自己刚发出的回复内容作为回声，应该被识别并忽略。
    """
    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    await fake_blc.add_room(5440)
    await asyncio.sleep(0.2)

    # 手动记一条“我们刚发了 你好呀”
    plugin.app.engine.note_our_message('你好呀', author='测试号')

    # 这条回声的文本正好也能被规则匹配（把规则关键词换成 你好呀 更直观）
    rule = plugin.app.config.rules[0]
    rule.keywords = ['你好呀']

    before = len(fake_blc.messages_of(Command.ADD_TEXT_REQ))
    await fake_blc.send_danmaku('你好呀', author='测试号', message_id='echo-1')
    await asyncio.sleep(0.5)

    after = len(fake_blc.messages_of(Command.ADD_TEXT_REQ))
    assert after == before, '自己的回声被当成了观众弹幕，会无限循环！'


async def test_mirror_danmaku_ignored(plugin, fake_blc):
    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    await fake_blc.add_room(5440)
    await asyncio.sleep(0.2)

    await fake_blc.send_danmaku('你好', is_mirror=True, message_id='mirror-1')
    await asyncio.sleep(0.5)

    assert fake_blc.messages_of(Command.ADD_TEXT_REQ) == []


async def test_emoticon_danmaku_ignored(plugin, fake_blc):
    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    await fake_blc.add_room(5440)
    await asyncio.sleep(0.2)

    # content_type=1 是表情弹幕
    await fake_blc.send_danmaku('你好', content_type=1, message_id='emoji-1')
    await asyncio.sleep(0.5)

    assert fake_blc.messages_of(Command.ADD_TEXT_REQ) == []


async def test_identity_code_room_uses_real_room_id_for_send(plugin, fake_blc):
    """用身份码连接时，extra.roomId 仍然是真实房间ID，发弹幕要用它"""
    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    await fake_blc.add_room_with_key(5440, {'type': 2, 'value': 'abcd1234'})
    await asyncio.sleep(0.2)

    await fake_blc.send_danmaku('你好', message_id='m1')

    texts = await fake_blc.wait_for(Command.ADD_TEXT_REQ, timeout=10)
    assert texts[0]['data']['content'] == '你好呀'
    # 播报的 roomKey 必须是身份码那个
    assert texts[0]['data']['roomKey']['type'] == 2


# ---------------------------------------------------------------- 房间管理

async def test_room_init_failure_is_reported(plugin, fake_blc):
    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)

    room_key = {'type': 1, 'value': 9999}
    await fake_blc._send(Command.ADD_ROOM, {}, {'roomId': None, 'roomKey': room_key})
    await fake_blc._send(Command.ROOM_INIT, {'isSuccess': False},
                         {'roomId': 9999, 'roomKey': room_key})

    await fake_blc.wait_for_log('初始化失败')
    status = plugin.app.status()
    assert status['rooms'][0]['initSuccess'] is False


async def test_removed_room_clears_primary(plugin, fake_blc):
    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    room_key = {'type': 1, 'value': 5440}
    await fake_blc.add_room(5440)
    await asyncio.sleep(0.2)
    assert plugin.app.rooms.primary_room_id == 5440

    await fake_blc._send(Command.DEL_ROOM, {}, {'roomId': 5440, 'roomKey': room_key})
    await asyncio.sleep(0.3)

    assert plugin.app.rooms.primary_room_id is None
    assert plugin.app.status()['rooms'] == []


# ---------------------------------------------------- 控制台手动发送（真 HTTP）

async def test_panel_manual_send(plugin, fake_blc, tmp_path):
    """真正打一遍控制台的 HTTP 接口"""
    import aiohttp

    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    await fake_blc.add_room(5440)
    await asyncio.sleep(0.2)

    assert plugin.app.panel is not None, '控制台没起来'
    base = plugin.app.panel.url.rstrip('/')

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        # 状态接口
        async with session.get(base + '/api/status') as res:
            assert res.status == 200
            status = await res.json()
            assert status['primaryRoomId'] == 5440
            assert status['credentials']['complete'] is True

        # 手动发送
        async with session.post(base + '/api/send', json={'content': '手动发的弹幕'}) as res:
            assert res.status == 200
            data = await res.json()
            assert data['ok'] is True, data
            assert data['result']['dryRun'] is True

        # 日志接口
        async with session.get(base + '/api/logs') as res:
            assert res.status == 200
            logs = await res.json()
            assert 'entries' in logs

    # 这个测试没有推送观众弹幕，所以自动回复不会触发，只应有手动这一条
    assert plugin.app.status()['sendStats']['sent'] == 1

    # 手动发送的内容也要显示在弹幕栏里
    broadcast = await fake_blc.wait_for(Command.ADD_TEXT_REQ, timeout=5)
    assert broadcast[0]['data']['content'] == '手动发的弹幕'
    assert broadcast[0]['data']['authorName'] == '[自动回复]'


async def test_panel_send_with_empty_content_is_rejected(plugin, fake_blc):
    import aiohttp

    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    base = plugin.app.panel.url.rstrip('/')

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.post(base + '/api/send', json={'content': '   '}) as res:
            assert res.status == 400
            data = await res.json()
            assert 'error' in data


async def test_panel_serves_html(plugin, fake_blc):
    import aiohttp

    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    base = plugin.app.panel.url.rstrip('/')

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(base + '/') as res:
            assert res.status == 200
            html = await res.text()
            assert 'blivechat' in html
            assert '手动发弹幕' in html


async def test_panel_rejects_path_traversal(plugin, fake_blc):
    """控制台只提供固定的几个接口，不应能读到任意文件"""
    import aiohttp

    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    base = plugin.app.panel.url.rstrip('/')

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(base + '/../../../../etc/passwd') as res:
            # 会被规范化或直接 404，总之不能返回文件内容
            assert res.status in (400, 404), f'状态码 {res.status}'
            body = await res.text()
            assert 'root:' not in body


async def test_open_admin_ui_does_not_crash(plugin, fake_blc):
    """用户点“管理”按钮：auto_open=false，所以只写日志，不该抛异常"""
    await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
    await fake_blc.open_admin_ui()
    await fake_blc.wait_for_log('控制台')


# ------------------------------------------------------------ 凭据缺失的处理

async def test_plugin_starts_even_without_credentials(fake_blc, tmp_path):
    """没有登录凭据时插件也要能起来，控制台要能看到原因"""
    import config
    import app as app_mod
    import listener
    import blcsdk
    from watchdog import ConnectionWatchdog

    await reset_blcsdk()

    base = tmp_path
    config.DATA_PATH = str(base / 'data')
    config.LOG_PATH = str(base / 'log')
    config.DEFAULT_CONFIG_PATH = os.path.join(config.DATA_PATH, 'config.ini')
    config.EXAMPLE_CONFIG_PATH = os.path.join(config.DATA_PATH, 'config.example.ini')
    config.BASE_PATH = str(base)
    os.makedirs(config.DATA_PATH, exist_ok=True)

    # 指向一个不存在的 cookie 文件
    with open(config.DEFAULT_CONFIG_PATH, 'w', encoding='utf-8') as f:
        f.write(CONFIG_TEMPLATE.format(
            cookie_file=str(base / 'missing.pickle'), dry_run='true', panel_port=0,
        ))

    config.init()
    os.environ['BLC_TOKEN'] = fake_blc.token
    os.environ['BLC_PORT'] = str(fake_blc.port)
    await blcsdk.init()

    app_ = app_mod.App(config.get_store())
    app_mod.set_app(app_)
    await app_.start()
    watchdog = ConnectionWatchdog(300.0, lambda: None)
    watchdog.start()
    listener.init(app_, watchdog)
    try:
        assert app_.status()['credentials'] is None

        # 自动回复会因为没凭据而失败，但绝不能把插件搞崩
        await asyncio.wait_for(fake_blc.connected.wait(), timeout=10)
        await fake_blc.add_room(5440)
        await asyncio.sleep(0.2)
        await fake_blc.send_danmaku('你好', message_id='m1')
        await fake_blc.wait_for_log('凭据')
    finally:
        listener.shut_down()
        await watchdog.stop()
        await app_.shut_down()
        await reset_blcsdk()
