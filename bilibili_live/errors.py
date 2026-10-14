# -*- coding: utf-8 -*-
"""B站直播接口错误码 -> 人话映射。

错误码来源：bilibili-API-collect 文档（发送直播弹幕一节）以及社区长期实践中
稳定出现的返回值。这里只做“能不能救、怎么救”的判断，不改变原始返回值。
"""
from typing import *

# 可以在等待后重试的错误码
RETRYABLE_CODES: Set[int] = {
    -412,    # 请求被风控拦截
    -509,    # 超出限制
    -799,    # 请求过于频繁，请稍后再试
    -500,    # 服务器内部错误
    10031,   # 发送频率过快
}

# 需要用户重新登录或换账号的错误码
AUTH_CODES: Set[int] = {
    -101,    # 账号未登录
    -111,    # csrf 校验失败
}

# code == 0 但 message 是这些值时，其实并没有发送成功，而是被限流/拒绝了。
# 来源：BanqiJane/Bilibili_Danmuji 对 msg/send 返回值的处理
SOFT_FAILURE_MESSAGES: Set[str] = {
    'msg in 1s',   # 发得太快
    'msg repeat',  # 内容重复
}

_CODE_MESSAGES: Dict[int, str] = {
    0: '成功',

    # 通用
    -101: '账号未登录：cookie 里的 SESSDATA 无效或已过期，请重新登录 B 站',
    -102: '账号被封停',
    -111: 'csrf 校验失败：cookie 里的 bili_jct 与请求中的 csrf 不一致，请重新读取 cookie',
    -400: '请求错误：缺少必要参数或参数不合法',
    -403: '访问权限不足：账号可能被限制或该房间不允许发言',
    -404: '接口不存在：B 站接口可能已变更',
    -412: '请求被风控拦截：发送过于频繁或 IP 被暂时限制，请等几分钟再试',
    -500: '服务器内部错误：B 站服务异常，稍后重试',
    -509: '超出限制：请降低发送频率',
    -799: '请求过于频繁：请稍后再试',
    -352: '风控校验失败：请求头或签名被判定为异常，通常需要在网页端正常发一条弹幕',

    # 发送弹幕相关
    10031: '发送频率过快：请等待几秒后再发',
    1003212: '弹幕超出限制长度：请缩短内容',
    11000: '弹幕包含被屏蔽的关键字，或弹幕颜色/模式该房间不支持',
    10065107: '弹幕颜色不存在：请改用该房间允许的颜色',
}


def describe(code: int) -> str:
    """把错误码翻译成人话，未知的码保留原始信息"""
    return _CODE_MESSAGES.get(code, f'未知错误码 {code}')


def is_soft_failure(message: str) -> bool:
    """code == 0 但 message 表示其实没发成功（限流/重复）"""
    return _normalize_message(message) in SOFT_FAILURE_MESSAGES


def _normalize_message(message: str) -> str:
    return (message or '').strip().lower()


def is_success(code: int) -> bool:
    return code == 0


def is_retryable(code: int) -> bool:
    return code in RETRYABLE_CODES


def is_auth_error(code: int) -> bool:
    return code in AUTH_CODES


class DanmakuError(Exception):
    """发送弹幕失败"""

    def __init__(self, code: int, message: str = '', *, endpoint: str = ''):
        self.code = code
        self.message = message
        self.endpoint = endpoint
        super().__init__(self.describe())

    def describe(self) -> str:
        detail = describe(self.code)
        if self.message and str(self.code) not in self.message:
            detail = f'{detail}（B站返回：{self.message}）'
        return detail

    @property
    def retryable(self) -> bool:
        return is_retryable(self.code)

    @property
    def auth_error(self) -> bool:
        return is_auth_error(self.code)

    def __str__(self):
        return f'[{self.code}] {self.describe()}'
