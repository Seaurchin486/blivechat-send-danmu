# -*- coding: utf-8 -*-
"""弹幕发送客户端的测试。

用假的 aiohttp 会话断言「发出去的请求长什么样」，并单独用真实网络做一次
契约探测（只需无 cookie 的请求，B站会回 -101，足以证明端点和请求格式正确）。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from bilibili_live import danmaku as danmaku_mod
from bilibili_live import errors


# --------------------------------------------------------------- 假 aiohttp

class FakeResponse:
    def __init__(self, payload, status=200, reason='OK', raw_body=None):
        self._payload = payload
        self.status = status
        self.reason = reason
        self._raw_body = raw_body

    async def text(self):
        if self._raw_body is not None:
            return self._raw_body
        import json

        return json.dumps(self._payload)

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """记录所有请求，按预设顺序返回响应"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append(('POST', url, kwargs))
        if not self.responses:
            raise AssertionError('FakeSession ran out of canned responses')
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.requests.append(('GET', url, kwargs))
        if not self.responses:
            raise AssertionError('FakeSession ran out of canned responses')
        return self.responses.pop(0)

    async def close(self):
        pass


class FakeCredentials:
    def __init__(self):
        self.cookies = {
            'SESSDATA': 'sess-value',
            'bili_jct': 'jct-value',
        }
        self.source = 'fake'
        self.uid = 123
        self.user_name = '测试号'

    @property
    def is_complete(self):
        return True

    @property
    def csrf(self):
        return self.cookies['bili_jct']


def make_client(responses, *, dry_run=False, credentials=None):
    c = config.AppConfig()
    c.dry_run = dry_run
    c.min_send_interval = 0.0
    c.max_length = 20
    c.danmaku_color = 16777215
    c.danmaku_fontsize = 25
    c.danmaku_mode = 1

    session = FakeSession(responses)
    client = danmaku_mod.DanmakuClient(c, session=session)
    client._credentials = credentials or FakeCredentials()
    return client, session


OK_PAYLOAD = {'code': 0, 'data': {'mode_info': {'mode': 0}}, 'message': '', 'msg': ''}


# ------------------------------------------------------------------- 请求构造

def test_send_uses_post_with_expected_form_and_headers():
    client, session = make_client([FakeResponse(OK_PAYLOAD)])
    result = asyncio.run(client.send(1899237171, 'QwQ'))

    assert result.ok
    assert result.content == 'QwQ'

    method, url, kwargs = session.requests[0]
    assert method == 'POST'
    assert url == 'https://api.live.bilibili.com/msg/send'

    form = kwargs['data']
    # 必需字段
    assert form['roomid'] == '1899237171'
    assert form['msg'] == 'QwQ'
    assert form['fontsize'] == '25'
    assert form['color'] == '16777215'
    # csrf 必须等于 cookie 里的 bili_jct
    assert form['csrf'] == 'jct-value'
    assert form['csrf_token'] == 'jct-value'
    # rnd 是秒级时间戳
    import time

    assert abs(int(form['rnd']) - int(time.time())) <= 5
    # 拼写是 replay_dmid（B站前端就是这样拼错的），不要"修正"
    assert 'replay_dmid' in form
    assert 'reply_dmid' not in form
    assert 'statistics' in form
    assert 'statistic' not in form

    headers = kwargs['headers']
    assert 'python' not in headers['User-Agent'].lower(), 'UA 里带 python 会被 B站风控'
    assert headers['Origin'] == 'https://live.bilibili.com'
    assert headers['Referer'] == 'https://live.bilibili.com/1899237171'


def test_send_passes_cookies():
    client, session = make_client([FakeResponse(OK_PAYLOAD)])
    asyncio.run(client.send(123, 'hi'))

    _, _, kwargs = session.requests[0]
    assert kwargs['cookies'].get('SESSDATA') == 'sess-value'


# ------------------------------------------------------------------- 错误处理

def test_not_logged_in_raises_auth_error():
    client, _ = make_client([FakeResponse({'code': -101, 'message': '账号未登录', 'ttl': 1})])

    with pytest.raises(errors.DanmakuError) as info:
        asyncio.run(client.send(1, 'hi'))

    assert info.value.code == -101
    assert info.value.auth_error
    assert not info.value.retryable
    assert 'SESSDATA' in str(info.value)


def test_csrf_failure_is_auth_error():
    client, _ = make_client([FakeResponse({'code': -111, 'message': 'csrf 校验失败'})])

    with pytest.raises(errors.DanmakuError) as info:
        asyncio.run(client.send(1, 'hi'))
    assert info.value.code == -111
    assert info.value.auth_error


def test_length_error_not_retried():
    client, session = make_client([FakeResponse({'code': 1003212, 'message': '超出限制长度'})])

    with pytest.raises(errors.DanmakuError) as info:
        asyncio.run(client.send(1, 'x' * 30))

    assert info.value.code == 1003212
    assert not info.value.retryable
    # 不可重试 -> 只请求了一次
    assert len(session.requests) == 1


def test_rate_limit_is_retried_then_succeeds():
    client, session = make_client([
        FakeResponse({'code': 10031, 'message': '发送频率过快'}),
        FakeResponse(OK_PAYLOAD),
    ])
    client._RETRY_BASE_DELAY = 0.0

    result = asyncio.run(client.send(1, 'hi'))

    assert result.ok
    assert len(session.requests) == 2, '应该重试了一次'


def test_rate_limit_exhausts_retries():
    client, session = make_client([
        FakeResponse({'code': 10031, 'message': '发送频率过快'}),
        FakeResponse({'code': 10031, 'message': '发送频率过快'}),
        FakeResponse({'code': 10031, 'message': '发送频率过快'}),
    ])
    client._RETRY_BASE_DELAY = 0.0

    with pytest.raises(errors.DanmakuError) as info:
        asyncio.run(client.send(1, 'hi'))

    assert info.value.code == 10031
    assert len(session.requests) == 3


def test_soft_failure_msg_in_1s_is_retried():
    """code==0 但 message 是 "msg in 1s" 时其实没发出去，要重试"""
    client, session = make_client([
        FakeResponse({'code': 0, 'message': 'msg in 1s', 'data': {}}),
        FakeResponse(OK_PAYLOAD),
    ])
    client._RETRY_BASE_DELAY = 0.0

    result = asyncio.run(client.send(1, 'hi'))
    assert result.ok
    assert len(session.requests) == 2


def test_soft_failure_msg_repeat_is_not_reported_as_success():
    """code==0 但 message 是 "msg repeat" 时不能当成功，重试也没用"""
    client, session = make_client([FakeResponse({'code': 0, 'message': 'msg repeat', 'data': {}})])

    result = asyncio.run(client.send(1, 'hi'))

    assert not result.ok
    assert result.error == 'msg repeat'
    assert len(session.requests) == 1, '内容重复没必要重试'


def test_http_405_gives_clear_error():
    client, _ = make_client([FakeResponse(None, status=405, reason='Method Not Allowed')])

    with pytest.raises(errors.DanmakuError) as info:
        asyncio.run(client.send(1, 'hi'))
    assert 'POST' in str(info.value)


def test_non_json_body_gives_clear_error():
    client, _ = make_client([FakeResponse(None, raw_body='<html>403</html>')])

    with pytest.raises(errors.DanmakuError) as info:
        asyncio.run(client.send(1, 'hi'))
    assert 'JSON' in str(info.value)


# --------------------------------------------------------------- 截断/校验

@pytest.mark.parametrize('raw,expected', [
    ('hello', 'hello'),
    ('你好世界', '你好世界'),
    ('a' * 30, 'a' * 20),
    ('带换行\n的内容', '带换行 的内容'),
    ('  前后空格  ', '前后空格'),
])
def test_truncate(raw, expected):
    assert danmaku_mod.truncate(raw, 20) == expected


def test_truncate_counts_characters_not_bytes():
    """按字符截断，中文不能只留半个字"""
    result = danmaku_mod.truncate('一二三四五六七八九十', 5)
    assert result == '一二三四五'
    assert len(result) == 5


def test_empty_content_rejected():
    client, _ = make_client([])
    with pytest.raises(ValueError):
        asyncio.run(client.send(1, '   '))


def test_dry_run_does_not_use_network():
    client, session = make_client([], dry_run=True)
    result = asyncio.run(client.send(1, 'hi'))

    assert result.ok
    assert result.dry_run
    assert session.requests == []


# --------------------------------------------------------------------- 限速

def test_rate_limit_enforces_minimum_interval():
    """两次发送之间至少要等 min_send_interval 秒"""
    import time

    client, session = make_client([FakeResponse(OK_PAYLOAD), FakeResponse(OK_PAYLOAD)])
    client._config.min_send_interval = 0.3

    async def two_sends():
        start = time.monotonic()
        await client.send(1, 'first')
        await client.send(1, 'second')
        return time.monotonic() - start

    elapsed = asyncio.run(two_sends())
    assert elapsed >= 0.3, f'限速没生效，只用了 {elapsed:.3f}s'
    assert len(session.requests) == 2


def test_stats_count_success_and_failure():
    client, _ = make_client([
        FakeResponse(OK_PAYLOAD),
        FakeResponse({'code': -101, 'message': '未登录'}),
    ])

    asyncio.run(client.send(1, 'ok'))
    with pytest.raises(errors.DanmakuError):
        asyncio.run(client.send(1, 'fail'))

    assert client.stats['sent'] == 1
    assert client.stats['failed'] == 1


# ----------------------------------------------------------------- 错误码表

def test_error_descriptions_are_human_readable():
    assert 'SESSDATA' in errors.describe(-101)
    assert 'bili_jct' in errors.describe(-111)
    assert '风控' in errors.describe(-412)
    assert '长度' in errors.describe(1003212)
    assert '频率' in errors.describe(10031)
    assert '10030' not in str(errors.describe(10030)) or '未知' in errors.describe(10030)


def test_retryable_and_auth_classification():
    assert errors.is_retryable(-412)
    assert errors.is_retryable(10031)
    assert not errors.is_retryable(-101)
    assert not errors.is_retryable(1003212)

    assert errors.is_auth_error(-101)
    assert errors.is_auth_error(-111)
    assert not errors.is_auth_error(-400)


def test_soft_failure_detection():
    assert errors.is_soft_failure('msg in 1s')
    assert errors.is_soft_failure('MSG REPEAT')
    assert errors.is_soft_failure('  msg in 1s  ')
    assert not errors.is_soft_failure('')
    assert not errors.is_soft_failure('ok')


# ------------------------------------------------- 真实网络契约探测（无 cookie）

@pytest.mark.network
def test_real_endpoint_rejects_without_login():
    """对真实接口发一次无 cookie 的请求，验证端点和请求格式。

    预期拿到 -101（账号未登录）。这能证明：
    * 端点是 POST /msg/send（不是 405）
    * 我们构造的表单参数被 B站接受了（否则会是 -400 参数错误）
    这个测试需要网络，用 `-m "not network"` 可以跳过。
    """
    import aiohttp

    async def probe():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(
                danmaku_mod.SEND_URL,
                data={
                    'bubble': '0',
                    'msg': 'test',
                    'color': '16777215',
                    'mode': '1',
                    'fontsize': '25',
                    'rnd': '1700000000',
                    'roomid': '5440',
                    'csrf': 'x',
                },
                headers={
                    'User-Agent': config.USER_AGENT,
                    'Origin': 'https://live.bilibili.com',
                    'Referer': 'https://live.bilibili.com/5440',
                },
            ) as res:
                assert res.status == 200, f'期望 HTTP 200，实际 {res.status}'
                return await res.json(content_type=None)

    data = asyncio.run(probe())
    assert 'code' in data, f'返回里没有 code：{data}'
    assert data['code'] == -101, f'期望 -101 账号未登录，实际 {data}'


@pytest.mark.network
def test_real_endpoint_rejects_get():
    """GET 应该被拒（405），这是我们断言"必须用 POST"的依据"""
    import aiohttp

    async def probe():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(
                danmaku_mod.SEND_URL + '?roomid=5440&msg=test&csrf=x&rnd=1&fontsize=25&color=16777215',
                headers={'User-Agent': config.USER_AGENT},
            ) as res:
                return res.status

    assert asyncio.run(probe()) == 405


@pytest.mark.network
def test_real_room_init_resolves_short_id():
    """room_init 能把短号解析成真实房间ID（5440 的短号是 1）"""
    import aiohttp

    async def probe():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(
                danmaku_mod.ROOM_INIT_URL, params={'id': 5440},
                headers={'User-Agent': config.USER_AGENT},
            ) as res:
                return await res.json(content_type=None)

    data = asyncio.run(probe())
    assert data['code'] == 0
    assert data['data']['room_id'] == 5440
    assert data['data']['short_id'] == 1
