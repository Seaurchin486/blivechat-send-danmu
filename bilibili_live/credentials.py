# -*- coding: utf-8 -*-
"""B站登录凭据。

blivechat 的“登录B站账号”插件会把浏览器 cookie 存成 aiohttp 的 CookieJar
（``data/cookie_jar.pickle``）。这里把它读出来，并且同时支持手动在配置里填写
cookie，方便不想装登录插件的用户。

发送弹幕需要两个值：

* ``SESSDATA`` —— 登录态
* ``bili_jct`` —— 同时作为请求里的 ``csrf`` / ``csrf_token``
"""
import http.cookies
import logging
import os
import pickle
import time
from typing import *

import yarl

logger = logging.getLogger('send-danmu.' + __name__)

NAV_URL = 'https://api.bilibili.com/x/web-interface/nav'

REQUIRED_COOKIES = ('SESSDATA', 'bili_jct')

# 请求时一并带上，缺失也不影响发送，但带上更接近真实浏览器
OPTIONAL_COOKIES = ('DedeUserID', 'DedeUserID__ckMd5', 'buvid3', 'buvid4', 'b_nut', 'sid')


class CredentialsError(Exception):
    """凭据不可用"""


class Credentials:
    """一份可用的登录凭据"""

    def __init__(self, cookies: Dict[str, str], *, source: str = '', uid: int = 0, user_name: str = ''):
        self.cookies = cookies
        self.source = source
        """凭据来源，用于日志"""
        self.uid = uid
        """B站用户ID"""
        self.user_name = user_name
        """B站用户名"""

    @property
    def sessdata(self) -> str:
        return self.cookies.get('SESSDATA', '')

    @property
    def csrf(self) -> str:
        return self.cookies.get('bili_jct', '')

    @property
    def is_complete(self) -> bool:
        return all(self.cookies.get(name) for name in REQUIRED_COOKIES)

    def missing(self) -> List[str]:
        return [name for name in REQUIRED_COOKIES if not self.cookies.get(name)]

    def cookie_header(self) -> str:
        """拼成 Cookie 请求头的值"""
        return '; '.join(f'{k}={v}' for k, v in self.cookies.items() if v)

    def to_dict(self) -> Dict[str, Any]:
        """给控制台展示用的脱敏信息（不含完整 cookie 值）"""
        return {
            'source': self.source,
            'uid': self.uid,
            'userName': self.user_name,
            'hasSessdata': bool(self.sessdata),
            'hasCsrf': bool(self.csrf),
            'complete': self.is_complete,
            'missing': self.missing(),
            'sessdataPreview': _mask(self.sessdata),
        }

    def __repr__(self):
        return f'<Credentials source={self.source!r} uid={self.uid} complete={self.is_complete}>'


def _mask(value: str) -> str:
    if not value:
        return ''
    if len(value) <= 8:
        return '*' * len(value)
    return f'{value[:4]}...{value[-4:]}'


def load_credentials(config_) -> Credentials:
    """按配置加载凭据。

    优先用手动写在配置里的 cookie（更明确），否则读 cookie_file / 自动探测到的
    blivechat cookie jar。
    """
    if config_.cookies:
        cookies = {k: v for k, v in config_.cookies.items() if v}
        creds = Credentials(cookies, source='手动配置[account]')
        _check(creds)
        return creds

    from config import resolve_cookie_file  # 延迟导入避免循环

    path = resolve_cookie_file(config_)
    if path is None:
        raise CredentialsError(
            '找不到登录凭据。请任选一种方式：\n'
            '  1. 安装并启用官方的“登录B站账号”插件，登录后重启 blivechat；\n'
            '  2. 在 data/config.ini 的 [account] 里填写 cookie_file 指向 cookie jar；\n'
            '  3. 在 data/config.ini 的 [account] 里直接填写 SESSDATA 和 bili_jct。'
        )

    cookies = load_cookie_jar(path)
    if not cookies:
        raise CredentialsError(f'cookie 文件里没有可用 cookie：{path}')

    creds = Credentials(cookies, source=path)
    _check(creds)
    return creds


def _check(creds: Credentials):
    missing = creds.missing()
    if missing:
        raise CredentialsError(
            f'登录凭据不完整，缺少 {", ".join(missing)}（来源：{creds.source}）。\n'
            'SESSDATA 用于登录，bili_jct 用于 csrf 校验，两个都需要。\n'
            '如果只用浏览器复制 cookie，请把整个 Cookie 头里的这两项都填上。'
        )


def load_cookie_jar(path: str) -> Dict[str, str]:
    """读取 blivechat 的 cookie jar，返回 {name: value}。

    ⚠️ aiohttp 的 ``CookieJar.save()`` 在不同版本里写的格式不一样：

    * aiohttp < 3.10 左右：marshalled 的 ``http.cookies.SimpleCookie``（pickle 风格）
    * aiohttp >= 3.10：JSON，形如
      ``{"bilibili.com|": {"SESSDATA": {"key":..., "value":..., "domain":...}}}``

    blivechat 把文件命名为 ``cookie_jar.pickle``，但内容可能是 JSON。
    所以这里先按 JSON 试，失败再按 pickle 试，两种都要支持。
    """
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError as e:
        raise CredentialsError(f'读取 cookie 文件失败：{path}（{e}）') from e

    if not raw:
        raise CredentialsError(f'cookie 文件是空的：{path}')

    data = None
    # JSON 一定以 { 或 [ 开头；pickle 是二进制协议，不会
    if raw.lstrip()[:1] in (b'{', b'['):
        import json

        try:
            data = json.loads(raw.decode('utf-8-sig'))
        except (ValueError, UnicodeDecodeError):
            logger.debug('Cookie file is not valid JSON, will try pickle: %s', path)

    if data is None:
        try:
            data = pickle.loads(raw)
        except Exception as e:  # noqa: BLE001 pickle 可能抛任何东西
            raise CredentialsError(
                f'cookie 文件既不是 JSON 也不是合法的 pickle：{path}（{e}）'
            ) from e

    cookies = _extract_cookies(data)
    if not cookies:
        logger.warning('No cookies extracted from %s (type=%s)', path, type(data).__name__)
    return cookies


def _extract_cookies(data) -> Dict[str, str]:
    """从反序列化出来的对象里尽量榨出 cookie"""
    # 1) http.cookies.SimpleCookie（老版本 aiohttp CookieJar 的实际存储）
    if isinstance(data, http.cookies.SimpleCookie):
        return {name: morsel.value for name, morsel in data.items() if morsel.value}

    # 2) 新版本 aiohttp 的 JSON：{domain: {name: {key, value, coded_value, ...}}}
    if isinstance(data, dict):
        flat: Dict[str, str] = {}
        for key, value in data.items():
            if isinstance(value, str) and key in REQUIRED_COOKIES + OPTIONAL_COOKIES:
                # 形如 {'SESSDATA': 'xxx'}
                flat[key] = value
            elif isinstance(value, http.cookies.Morsel) and value.value:
                flat[key] = value.value
            elif isinstance(value, dict):
                # 形如 {'coded_value':..., 'key':..., 'value':...} 的单个 cookie 记录
                if 'value' in value or 'coded_value' in value:
                    name = value.get('key') or key
                    cookie_value = value.get('value')
                    if cookie_value is None:
                        cookie_value = value.get('coded_value')
                    if name and cookie_value:
                        flat[str(name)] = str(cookie_value)
                else:
                    # 形如 {domain: {name: {...}}}，继续往下挖
                    flat.update(_extract_cookies(value))
        if flat:
            return flat

    # 3) 带 _cookies 属性的对象（aiohttp.CookieJar 实例本身）
    inner = getattr(data, '_cookies', None)
    if inner is not None:
        cookies = _extract_cookies(inner)
        if cookies:
            return cookies

    # 4) 可迭代的 Morsel 序列
    if isinstance(data, (list, tuple, set)):
        flat = {}
        for item in data:
            if isinstance(item, http.cookies.Morsel) and item.value:
                flat[item.key] = item.value
            elif isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
                flat[item[0]] = str(item[1])
        if flat:
            return flat

    # 5) 真的读不出来时，直接扫描对象的 __dict__
    if hasattr(data, '__dict__'):
        return _extract_cookies(vars(data))
    return {}


def filter_for_url(cookies: Dict[str, str], url: str) -> Dict[str, str]:
    """只保留该 URL 需要的 cookie。

    直接用 aiohttp 的 CookieJar 做域匹配，这样 `buvid3` 之类只在
    ``.bilibili.com`` 下发的 cookie 也能正确带上。
    """
    try:
        jar = _make_jar(cookies)
        filtered = jar.filter_cookies(yarl.URL(url))
        result = {name: morsel.value for name, morsel in filtered.items() if morsel.value}
        if result:
            return result
    except Exception:  # noqa: BLE001 CookieJar 行为在不同版本有差异，失败就退回全量
        logger.debug('CookieJar domain filtering failed, sending all cookies', exc_info=True)
    return dict(cookies)


def _make_jar(cookies: Dict[str, str]):
    import aiohttp

    jar = aiohttp.CookieJar()
    for name, value in cookies.items():
        simple = http.cookies.SimpleCookie()
        simple[name] = value
        simple[name]['domain'] = '.bilibili.com'
        simple[name]['path'] = '/'
        jar.update_cookies(simple)
    return jar


async def fetch_user_info(session, creds: Credentials, user_agent: str) -> bool:
    """请求 nav 接口验证登录态，成功时把 uid / 用户名写进 creds。

    :return: 是否已登录
    """
    cookies = filter_for_url(creds.cookies, NAV_URL)
    try:
        async with session.get(
            NAV_URL,
            cookies=cookies,
            headers={'User-Agent': user_agent, 'Referer': 'https://www.bilibili.com/'},
        ) as res:
            if res.status != 200:
                logger.warning('nav returned HTTP %s', res.status)
                return False
            data = await res.json(content_type=None)
    except Exception as e:  # noqa: BLE001 网络问题不应该让插件崩掉
        logger.warning('Failed to fetch user info: %r', e)
        return False

    if data.get('code') != 0:
        logger.warning('nav failed: code=%s message=%s', data.get('code'), data.get('message'))
        return False

    payload = data.get('data') or {}
    if not payload.get('isLogin'):
        logger.warning('nav says not logged in')
        return False

    creds.uid = int(payload.get('mid') or 0)
    creds.user_name = str(payload.get('uname') or '')
    return True


class CookieFileWatcher:
    """cookie jar 文件的 mtime 监控，变了就说明用户重新登录过"""

    def __init__(self, path: Optional[str]):
        self._path = path
        self._mtime: Optional[float] = None
        self.refresh()

    def refresh(self):
        if not self._path:
            self._mtime = None
            return
        try:
            self._mtime = os.path.getmtime(self._path)
        except OSError:
            self._mtime = None

    def changed(self) -> bool:
        if not self._path:
            return False
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            return False
        if self._mtime is None or mtime != self._mtime:
            self._mtime = mtime
            return True
        return False

    @property
    def path(self) -> Optional[str]:
        return self._path
