# -*- coding: utf-8 -*-
"""发送直播弹幕。

接口：``POST https://api.live.bilibili.com/msg/send``
（form-urlencoded，认证靠 cookie 里的 SESSDATA，csfr 靠 cookie 里的 bili_jct）

这个模块只负责“把一条弹幕发出去”以及串行化/限速，不做任何自动回复决策。
"""
import asyncio
import logging
import time
from typing import *

import aiohttp

from . import credentials as creds_mod
from . import errors

logger = logging.getLogger('send-danmu.' + __name__)

SEND_URL = 'https://api.live.bilibili.com/msg/send'
ROOM_INIT_URL = 'https://api.live.bilibili.com/room/v1/Room/room_init'
DM_CONFIG_URL = 'https://api.live.bilibili.com/xlive/web-room/v1/dM/GetDMConfigByGroup'

# 弹幕模式
MODE_ROLLING = 1

# 内容长度按字符数限制（B 站按字符算，一个汉字算一个字符）
DEFAULT_MAX_LENGTH = 20


class SendResult:
    """一次发送的结果"""

    def __init__(self, ok: bool, content: str, room_id: int, *, dry_run: bool = False, error: str = ''):
        self.ok = ok
        self.content = content
        self.room_id = room_id
        self.dry_run = dry_run
        self.error = error
        self.timestamp = time.time()

    def __repr__(self):
        status = 'DRY-RUN' if self.dry_run else ('OK' if self.ok else 'FAIL')
        return f'<SendResult {status} room={self.room_id} {self.content!r} {self.error}>'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'ok': self.ok,
            'roomId': self.room_id,
            'content': self.content,
            'dryRun': self.dry_run,
            'error': self.error,
        }


def truncate(content: str, max_length: int) -> str:
    """按字符截断，并且去掉换行（弹幕里换行会导致发送失败）"""
    content = content.replace('\r', ' ').replace('\n', ' ').strip()
    if max_length > 0 and len(content) > max_length:
        content = content[:max_length]
    return content


class DanmakuClient:
    """带限速的弹幕发送客户端。

    所有发送都会排队串行执行，保证两条弹幕之间至少间隔
    ``config.min_send_interval`` 秒，避免触发 B 站的频率限制。
    """

    def __init__(self, config, *, session: Optional[aiohttp.ClientSession] = None):
        """
        :param config: 配置。可以传 ``AppConfig``，也可以传 ``config.ConfigStore``
            以便配置热重载后自动生效（这里用 duck typing 判断，避免循环导入）
        """
        self._config_or_store = config
        self._session = session
        self._own_session = session is None

        self._credentials: Optional[creds_mod.Credentials] = None
        self._lock = asyncio.Lock()
        self._last_send_time = 0.0

        self._sent_count = 0
        self._failed_count = 0
        self._last_result: Optional[SendResult] = None

    @property
    def _config(self):
        """总是取最新的配置，这样控制台改完配置不用重建客户端"""
        store = self._config_or_store
        get = getattr(store, 'get', None)
        if callable(get):
            return get()
        return store

    # ------------------------------------------------------------- 生命周期

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_exc):
        await self.close()
        return False

    async def start(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._config.send_timeout),
            )
        await self.reload_credentials()

    async def close(self):
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def reload_credentials(self):
        """重新从 cookie 文件加载凭据"""
        self._credentials = creds_mod.load_credentials(self._config)
        logger.info('Credentials loaded: %s', self._credentials)

        assert self._session is not None
        ok = await creds_mod.fetch_user_info(self._session, self._credentials, self._user_agent)
        if ok:
            logger.info('Logged in as %s (uid=%d)', self._credentials.user_name, self._credentials.uid)
        else:
            logger.warning(
                'Failed to verify login for %s; sending will most likely fail with -101',
                self._credentials.source,
            )
        return self._credentials

    # ----------------------------------------------------------------- 属性

    @property
    def credentials(self) -> Optional[creds_mod.Credentials]:
        return self._credentials

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            'sent': self._sent_count,
            'failed': self._failed_count,
            'lastSendTime': self._last_send_time,
        }

    @property
    def _user_agent(self) -> str:
        import config as config_mod

        return config_mod.USER_AGENT

    def _headers(self, room_id: int = 0) -> Dict[str, str]:
        headers = {
            'User-Agent': self._user_agent,
            'Origin': 'https://live.bilibili.com',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }
        if room_id:
            # B站前端发弹幕时 Referer 是具体房间页，带上更接近真实请求
            headers['Referer'] = f'https://live.bilibili.com/{room_id}'
        else:
            headers['Referer'] = 'https://live.bilibili.com/'
        return headers

    # --------------------------------------------------------------- 发送弹幕

    async def send(
        self,
        room_id: int,
        content: str,
        *,
        color: Optional[int] = None,
        fontsize: Optional[int] = None,
        mode: Optional[int] = None,
        reply_mid: int = 0,
        reply_uname: str = '',
    ) -> SendResult:
        """发一条弹幕。失败抛 ``errors.DanmakuError``"""
        content = truncate(content, self._config.max_length)
        if not content:
            raise ValueError('弹幕内容为空')

        # 限速段：用锁保证串行，并且按上一次发送时间补足间隔
        async with self._lock:
            interval = self._config.min_send_interval
            elapsed = time.monotonic() - self._last_send_time
            if self._last_send_time > 0 and elapsed < interval:
                wait = interval - elapsed
                logger.debug('Rate limiting: sleeping %.2fs', wait)
                await asyncio.sleep(wait)

            try:
                result = await self._send_once(
                    room_id, content, color=color, fontsize=fontsize, mode=mode,
                    reply_mid=reply_mid, reply_uname=reply_uname,
                )
            except errors.DanmakuError:
                # 抛出异常也是失败，计数不能漏
                self._failed_count += 1
                raise
            finally:
                self._last_send_time = time.monotonic()

        self._last_result = result
        if result.ok:
            self._sent_count += 1
        else:
            self._failed_count += 1
        return result

    async def _send_once(
        self, room_id: int, content: str, *, color, fontsize, mode, reply_mid, reply_uname
    ) -> SendResult:
        config_ = self._config

        if config_.dry_run:
            logger.info('[DRY-RUN] room=%d would send: %s', room_id, content)
            return SendResult(True, content, room_id, dry_run=True)

        creds = self._credentials
        if creds is None or not creds.is_complete:
            raise errors.DanmakuError(-101, '本地没有可用的 SESSDATA / bili_jct')

        # 参数名和取值来源：
        #   1. bilibili-API-collect 的“发送直播弹幕”一节（参数与错误码）
        #   2. live.bilibili.com 官方前端 webpack 产物里 axios 拦截器的明文实现
        #      —— 官方对 api.live.bilibili.com 的所有 POST 都会补上
        #      csrf / csrf_token / visit_id，其中 csrf 的值就是 cookie 里的 bili_jct
        form = {
            'bubble': '0',
            'msg': content,
            'color': str(config_.danmaku_color if color is None else color),
            'mode': str(config_.danmaku_mode if mode is None else mode),
            'fontsize': str(config_.danmaku_fontsize if fontsize is None else fontsize),
            'rnd': str(int(time.time())),
            'roomid': str(room_id),
            'csrf': creds.csrf,
            'csrf_token': creds.csrf,
            'visit_id': '',
            'jumpfrom': '0',
            'reply_mid': str(reply_mid),
            'reply_attr': '0',
            'replay_dmid': '',
            'statistics': '{"appId":100,"platform":5}',
            'room_type': '0',
        }
        if reply_mid and reply_uname:
            form['reply_uname'] = reply_uname

        cookies = creds_mod.filter_for_url(creds.cookies, SEND_URL)

        # 重试策略：B站限流相关的错误短时间重试通常有用；参数类错误重试没意义
        assert self._session is not None
        deadline = time.monotonic() + self._SEND_TIMEOUT_SECONDS
        backoff = self._RETRY_BASE_DELAY

        for attempt in range(1, self._RETRY_COUNT + 1):
            try:
                async with self._session.post(
                    SEND_URL,
                    data=form,
                    cookies=cookies,
                    headers=self._headers(room_id),
                ) as res:
                    # 每次重试都要刷新 rnd，它按约定是当前秒级时间戳
                    form['rnd'] = str(int(time.time()))

                    body = await res.text()
                    if res.status == 405:
                        raise errors.DanmakuError(
                            -400, 'B站返回 405：发送接口只接受 POST', endpoint=SEND_URL
                        )
                    if res.status != 200:
                        backoff = await self._retry_or_raise(
                            errors.DanmakuError(-500, f'HTTP {res.status} {res.reason}', endpoint=SEND_URL),
                            attempt, backoff, deadline,
                        )
                        continue
                    data = _parse_json(body)
            except aiohttp.ClientError as e:
                backoff = await self._retry_or_raise(
                    errors.DanmakuError(-500, f'网络错误：{e!r}', endpoint=SEND_URL),
                    attempt, backoff, deadline,
                )
                continue
            except asyncio.TimeoutError:
                backoff = await self._retry_or_raise(
                    errors.DanmakuError(-500, '请求超时', endpoint=SEND_URL),
                    attempt, backoff, deadline,
                )
                continue

            code = int(data.get('code', -500))
            message = str(data.get('message') or data.get('msg') or '')

            if not errors.is_success(code):
                backoff = await self._retry_or_raise(
                    errors.DanmakuError(code, message, endpoint=SEND_URL),
                    attempt, backoff, deadline,
                )
                continue

            # code == 0 不代表一定发出去了：B站会在 message 里返回 "msg in 1s" /
            # "msg repeat" 这类软失败。只重试前一种，内容重复重试也没用。
            if errors.is_soft_failure(message):
                # 10031「发送频率过快」是可重试码，借用它统一判断
                too_fast = errors.DanmakuError(10031)
                if message.strip().lower() == 'msg in 1s' and self._should_retry(attempt, deadline, too_fast):
                    logger.warning('B站提示发送过快（%s），%.1fs 后重试', message, backoff)
                    backoff = await self._backoff(backoff, deadline)
                    continue
                logger.warning('B站未接受这条弹幕：message=%r', message)
                return SendResult(False, content, room_id, error=message)

            logger.info('Sent danmaku to room %d: %s', room_id, content)
            return SendResult(True, content, room_id)

        raise errors.DanmakuError(-500, '发送失败（已重试多次）', endpoint=SEND_URL)

    # 重试相关常量
    _RETRY_COUNT = 3
    _RETRY_BASE_DELAY = 2.0
    _RETRY_MAX_DELAY = 20.0
    _SEND_TIMEOUT_SECONDS = 60.0

    def _should_retry(self, attempt: int, deadline: float, error: errors.DanmakuError) -> bool:
        return attempt < self._RETRY_COUNT and error.retryable and time.monotonic() < deadline

    async def _backoff(self, delay: float, deadline: float) -> float:
        """睡一会儿再重试，返回下一次的退避时长"""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return delay
        await asyncio.sleep(min(delay, remaining))
        return min(delay * 2, self._RETRY_MAX_DELAY)

    async def _retry_or_raise(self, error: errors.DanmakuError, attempt: int, backoff: float,
                              deadline: float) -> float:
        """需要重试就睡一觉并返回新的退避时长，否则抛出异常"""
        if not self._should_retry(attempt, deadline, error):
            raise error
        logger.warning('%s，%.1fs 后重试（第 %d 次）', error, backoff, attempt)
        return await self._backoff(backoff, deadline)

    # ------------------------------------------------------------- 房间信息

    async def get_real_room_id(self, room_id: int) -> int:
        """把短ID解析成真实房间ID"""
        assert self._session is not None
        try:
            async with self._session.get(
                ROOM_INIT_URL,
                params={'id': room_id},
                headers=self._headers(),
                cookies=creds_mod.filter_for_url(self._credentials.cookies, ROOM_INIT_URL)
                if self._credentials else None,
            ) as res:
                data = await res.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            logger.warning('room_init failed for %s: %r', room_id, e)
            return room_id

        if data.get('code') != 0:
            logger.warning('room_init failed for %s: %s', room_id, data.get('message'))
            return room_id

        real_id = int((data.get('data') or {}).get('room_id') or room_id)
        if real_id != room_id:
            logger.info('Resolved short room id %s -> %s', room_id, real_id)
        return real_id

    async def get_danmaku_config(self, room_id: int) -> Dict[str, Any]:
        """取该房间允许的弹幕颜色和模式（未登录只有白色/滚动）"""
        assert self._session is not None
        try:
            async with self._session.get(
                DM_CONFIG_URL,
                params={'room_id': room_id},
                headers=self._headers(),
                cookies=creds_mod.filter_for_url(self._credentials.cookies, DM_CONFIG_URL)
                if self._credentials else None,
            ) as res:
                data = await res.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            logger.warning('GetDMConfigByGroup failed for %s: %r', room_id, e)
            return {'colors': [], 'modes': [], 'error': repr(e)}

        if data.get('code') != 0:
            return {'colors': [], 'modes': [], 'error': str(data.get('message'))}

        payload = data.get('data') or {}
        colors: List[Dict[str, Any]] = []
        for group in payload.get('group') or []:
            for color in group.get('color') or []:
                if color.get('status') == 1:
                    colors.append({
                        'name': color.get('name'),
                        'color': int(color.get('color') or 0),
                        'hex': color.get('color_hex'),
                    })
        modes = [
            {'name': m.get('name'), 'mode': m.get('mode'), 'status': m.get('status')}
            for m in payload.get('mode') or []
        ]
        return {'colors': colors, 'modes': modes}


def _parse_json(body: str) -> Dict[str, Any]:
    import json

    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        raise errors.DanmakuError(-500, f'返回内容不是 JSON：{body[:200]!r}') from None
    if not isinstance(data, dict):
        raise errors.DanmakuError(-500, f'返回内容格式异常：{body[:200]!r}')
    return data
