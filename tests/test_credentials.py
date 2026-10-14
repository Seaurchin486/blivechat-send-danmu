# -*- coding: utf-8 -*-
"""登录凭据读取的测试。

重点是真实地走一遍 blivechat 的流程：用 aiohttp 的 CookieJar 存一个 cookie jar
（官方“登录B站账号”插件就是这么存的），再读出来，确认能拿到 SESSDATA / bili_jct。
"""
import os
import pickle
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from bilibili_live import credentials as creds_mod

COOKIES = {
    'SESSDATA': 'abc123def456',
    'bili_jct': 'deadbeefcafe',
    'DedeUserID': '438160221',
    'buvid3': 'AAAA-BBBB-CCCC',
}


def write_jar(path, cookies):
    """按官方登录插件的方式，用 aiohttp.CookieJar 存一份 pickle。

    aiohttp 的 CookieJar 构造时要求有运行中的事件循环，所以这里包一层 asyncio.run。
    """
    import asyncio
    import http.cookies

    import aiohttp

    async def _write():
        jar = aiohttp.CookieJar()
        for name, value in cookies.items():
            simple = http.cookies.SimpleCookie()
            simple[name] = value
            simple[name]['domain'] = '.bilibili.com'
            simple[name]['path'] = '/'
            jar.update_cookies(simple)
        jar.save(path)

    asyncio.run(_write())


def test_load_real_cookie_jar(tmp_path):
    path = str(tmp_path / 'cookie_jar.pickle')
    write_jar(path, COOKIES)

    loaded = creds_mod.load_cookie_jar(path)
    assert loaded['SESSDATA'] == 'abc123def456'
    assert loaded['bili_jct'] == 'deadbeefcafe'
    assert loaded['DedeUserID'] == '438160221'


def test_load_missing_file_raises():
    with pytest.raises(creds_mod.CredentialsError):
        creds_mod.load_cookie_jar('Z:/definitely/does/not/exist.pickle')


def test_load_corrupt_file_raises(tmp_path):
    path = str(tmp_path / 'bad.pickle')
    with open(path, 'wb') as f:
        f.write(b'this is not a pickle at all')

    with pytest.raises(creds_mod.CredentialsError):
        creds_mod.load_cookie_jar(path)


def test_extract_plain_simple_cookie():
    import http.cookies

    simple = http.cookies.SimpleCookie()
    simple['SESSDATA'] = 'xyz'
    simple['bili_jct'] = 'qqq'

    assert creds_mod._extract_cookies(simple) == {'SESSDATA': 'xyz', 'bili_jct': 'qqq'}


def test_extract_new_aiohttp_json_format():
    """aiohttp >= 3.10 的 CookieJar.save() 写的是 JSON，不是 pickle"""
    data = {
        'bilibili.com|': {
            'SESSDATA': {'key': 'SESSDATA', 'value': 'sess-json',
                         'coded_value': 'sess-json', 'domain': 'bilibili.com', 'path': '/'},
            'bili_jct': {'key': 'bili_jct', 'value': 'jct-json',
                         'coded_value': 'jct-json', 'domain': 'bilibili.com', 'path': '/'},
        }
    }
    cookies = creds_mod._extract_cookies(data)
    assert cookies == {'SESSDATA': 'sess-json', 'bili_jct': 'jct-json'}


def test_load_json_cookie_jar_from_disk(tmp_path):
    """真的写一份 JSON 格式的 jar（就是新版本 aiohttp 的行为）再从磁盘读"""
    import json

    path = tmp_path / 'cookie_jar.pickle'
    path.write_text(json.dumps({
        'bilibili.com|': {
            'SESSDATA': {'key': 'SESSDATA', 'value': COOKIES['SESSDATA'], 'path': '/'},
            'bili_jct': {'key': 'bili_jct', 'value': COOKIES['bili_jct'], 'path': '/'},
        }
    }), encoding='utf-8')

    loaded = creds_mod.load_cookie_jar(str(path))
    assert loaded['SESSDATA'] == COOKIES['SESSDATA']
    assert loaded['bili_jct'] == COOKIES['bili_jct']


def test_load_pickle_cookie_jar_from_disk(tmp_path):
    """老版本 aiohttp 写的是 SimpleCookie 的 pickle，也要能读"""
    import http.cookies
    import pickle

    simple = http.cookies.SimpleCookie()
    simple['SESSDATA'] = 'old-sess'
    simple['bili_jct'] = 'old-jct'

    path = tmp_path / 'cookie_jar.pickle'
    with open(path, 'wb') as f:
        pickle.dump(simple, f)

    loaded = creds_mod.load_cookie_jar(str(path))
    assert loaded['SESSDATA'] == 'old-sess'
    assert loaded['bili_jct'] == 'old-jct'


def test_load_flat_json_cookie_jar(tmp_path):
    """也支持最朴素的 {'SESSDATA': ..., 'bili_jct': ...} 形式"""
    import json

    path = tmp_path / 'flat.json'
    path.write_text(json.dumps(dict(COOKIES)), encoding='utf-8')

    loaded = creds_mod.load_cookie_jar(str(path))
    assert loaded['SESSDATA'] == COOKIES['SESSDATA']


def test_load_empty_cookie_jar_raises(tmp_path):
    path = tmp_path / 'cookie_jar.pickle'
    path.write_bytes(b'')

    with pytest.raises(creds_mod.CredentialsError):
        creds_mod.load_cookie_jar(str(path))


def test_credentials_requires_both_fields():
    creds = creds_mod.Credentials({'SESSDATA': 'a'}, source='test')
    assert not creds.is_complete
    assert creds.missing() == ['bili_jct']

    with pytest.raises(creds_mod.CredentialsError) as info:
        creds_mod._check(creds)
    assert 'bili_jct' in str(info.value)


def test_credentials_masking_does_not_leak():
    """面板上展示的信息不能把完整 SESSDATA 暴露出来"""
    creds = creds_mod.Credentials(dict(COOKIES), source='test')
    info = creds.to_dict()

    assert 'SESSDATA' not in str(info.values())
    assert COOKIES['SESSDATA'] not in str(info)
    assert info['sessdataPreview'] == 'abc1...f456'
    assert info['complete'] is True


def test_cookie_header_format():
    creds = creds_mod.Credentials(dict(COOKIES))
    header = creds.cookie_header()
    assert 'SESSDATA=abc123def456' in header
    assert 'bili_jct=deadbeefcafe' in header


def test_filter_for_url_keeps_bilibili_cookies():
    filtered = creds_mod.filter_for_url(COOKIES, creds_mod.NAV_URL)
    # aiohttp 的域匹配至少不应该把关键 cookie 弄丢
    assert filtered.get('SESSDATA') == 'abc123def456'
    assert filtered.get('bili_jct') == 'deadbeefcafe'


def test_load_credentials_from_manual_config():
    c = config.AppConfig()
    c.cookies = {'SESSDATA': 'manual-sess', 'bili_jct': 'manual-jct'}

    creds = creds_mod.load_credentials(c)
    assert creds.sessdata == 'manual-sess'
    assert creds.csrf == 'manual-jct'
    assert creds.source == '手动配置[account]'


def test_load_credentials_from_cookie_file(tmp_path):
    path = str(tmp_path / 'cookie_jar.pickle')
    write_jar(path, COOKIES)

    c = config.AppConfig()
    c.cookie_file = path

    creds = creds_mod.load_credentials(c)
    assert creds.sessdata == COOKIES['SESSDATA']
    assert creds.csrf == COOKIES['bili_jct']


def test_load_credentials_manual_takes_priority(tmp_path):
    """配置里手写的 cookie 应该优先于 cookie 文件"""
    path = str(tmp_path / 'cookie_jar.pickle')
    write_jar(path, COOKIES)

    c = config.AppConfig()
    c.cookie_file = path
    c.cookies = {'SESSDATA': 'override', 'bili_jct': 'override'}

    creds = creds_mod.load_credentials(c)
    assert creds.sessdata == 'override'


def test_load_credentials_without_any_source_gives_actionable_error(tmp_path):
    c = config.AppConfig()
    # 指向一个不存在的路径，确保不会误读到真实环境里的 cookie
    c.cookie_file = str(tmp_path / 'nope.pickle')

    with pytest.raises(creds_mod.CredentialsError):
        creds_mod.load_credentials(c)


def test_cookie_file_watcher_detects_change(tmp_path):
    path = tmp_path / 'cookie_jar.pickle'
    path.write_bytes(b'x')

    watcher = creds_mod.CookieFileWatcher(str(path))
    assert not watcher.changed()

    # mtime 精度可能只有秒级，显式改一下时间戳
    os.utime(path, (0, 0))
    assert watcher.changed()
    assert not watcher.changed()
