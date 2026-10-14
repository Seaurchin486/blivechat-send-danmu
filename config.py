# -*- coding: utf-8 -*-
"""配置加载。

配置文件是 INI 格式，首次启动时会把 ``data/config.example.ini`` 复制成
``data/config.ini``，用户改完由控制台点“重载配置”生效（不用重启 blivechat）。
"""
import configparser
import logging
import os
import shutil
import sys
import threading
from typing import *

logger = logging.getLogger('send-danmu.' + __name__)

BASE_PATH = os.path.dirname(os.path.realpath(__file__))
DATA_PATH = os.path.join(BASE_PATH, 'data')
LOG_PATH = os.path.join(BASE_PATH, 'log')
DEFAULT_CONFIG_PATH = os.path.join(DATA_PATH, 'config.ini')
EXAMPLE_CONFIG_PATH = os.path.join(DATA_PATH, 'config.example.ini')

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
)


class RuleConfig:
    """一条自动回复规则"""

    def __init__(self):
        self.id = ''
        """规则名，用于日志和去重"""

        self.enabled = True

        self.match_type = 'contains'
        """匹配方式：contains / equals / prefix / suffix / regex"""

        self.keywords: List[str] = []
        """关键词列表"""

        self.replies: List[str] = []
        """回复内容列表。多个时按 reply_mode 选取"""

        self.reply_mode = 'first'
        """多回复时的选取方式：first / random / sequence"""

        self.cooldown = 0.0
        """本条规则的冷却时间（秒），0 表示用全局冷却"""

        self.per_user_cooldown = 0.0
        """同一用户触发本条规则的冷却时间（秒），0 表示不限制"""

        self.priority = 0
        """优先级，越大越先匹配"""

        self.min_author_level = 0
        """触发所需的最低用户等级"""

        self.min_medal_level = 0
        """触发所需的当前房间最低勋章等级"""

        self.require_guard = False
        """是否只允许舰队用户触发"""

        self.command = True
        """是否允许通过弹幕指令（/关键词）触发"""

        self.chat_trigger = True
        """是否允许通过普通弹幕内容触发"""

    def __repr__(self):
        return f'<RuleConfig {self.id!r} {self.match_type} {self.keywords!r}>'


class AppConfig:
    def __init__(self):
        # [account]
        self.cookie_file = ''
        """cookie jar 路径，留空则自动探测 blivechat 的 data/cookie_jar.pickle"""

        self.cookies = {}
        """手动指定的 cookie，形如 {'SESSDATA': '...', 'bili_jct': '...'}，优先级高于 cookie_file"""

        self.room_id = 0
        """手动指定房间ID。0 表示使用 blivechat 当前连接的房间"""

        self.auth_code = ''
        """身份码。填写后将自动查找对应的房间（需在 blivechat 里先添加该身份码的房间）。优先级高于 room_id"""

        # [danmaku]
        self.min_send_interval = 2.0
        """两条弹幕之间的最小间隔（秒）"""

        self.max_send_queue = 50
        """发送队列上限，超过则丢弃最旧的任务"""

        self.max_length = 20
        """弹幕最大长度，超出则截断"""

        self.send_timeout = 10.0
        """发送请求超时（秒）"""

        self.danmaku_color = 16777215
        """弹幕颜色，十进制 RGB"""

        self.danmaku_fontsize = 25
        """弹幕字号"""

        self.danmaku_mode = 1
        """弹幕模式，1 为滚动弹幕"""

        self.dry_run = False
        """试运行：只记录不发真实弹幕"""

        # [auto_reply]
        self.auto_reply_enabled = True
        self.auto_reply_cooldown = 5.0
        """全局冷却（秒），任意一条自动回复之后至少等这么久"""

        self.auto_reply_template = '{content}'
        """回复模板，可用变量见 README"""

        self.auto_reply_max_per_minute = 0
        """每分钟最多自动回复几条，0 表示不限制"""

        self.ignore_mirror = True
        """忽略跨房弹幕"""

        self.ignore_medal_level_below = 0
        """忽略勋章等级低于该值的弹幕，0 表示不过滤"""

        self.min_author_level = 0
        """忽略用户等级低于该值的弹幕，0 表示不过滤"""

        self.block_users: List[str] = []
        """黑名单，按用户名或 uid 匹配（uid 支持 Open ID 前缀）"""

        self.allow_users: List[str] = []
        """白名单，非空时只处理名单内的用户"""

        # [panel]
        self.panel_enabled = True
        self.panel_host = '127.0.0.1'
        self.panel_port = 12452
        self.panel_auto_open = True
        """点击 blivechat 插件“管理”按钮时自动打开控制台"""

        # [sdk]
        self.plugin_broadcast = True
        """自动回复的内容是否也显示在 blivechat 弹幕栏"""

        self.broadcast_author = '[自动回复]'
        """播报到弹幕栏时显示的作者名"""

        self.connection_timeout = 35.0
        """多久收不到 blivechat 的消息就认为它已经退出（秒）。

        blivechat 每 30 秒发一次心跳，所以 35 秒没动静基本可以确定它没了。
        这个兜底是为了防止 blivechat 被强杀时插件变成僵尸进程。
        """

        self.rules: List[RuleConfig] = []

    # ------------------------------------------------------------------ 加载

    def load(self, path: str) -> bool:
        if not os.path.exists(path):
            logger.warning('Config file not found: %s', path)
            return False

        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            parser.read(path, encoding='utf-8-sig')
        except (configparser.Error, OSError):
            logger.exception('Failed to parse config:')
            return False

        try:
            self._load_account(parser)
            self._load_danmaku(parser)
            self._load_auto_reply(parser)
            self._load_panel(parser)
            self._load_sdk(parser)
            self._load_rules(parser)
        except Exception:  # noqa: BLE001 配置错误不应该让插件挂掉
            logger.exception('Failed to load config:')
            return False
        return True

    @staticmethod
    def _section_get(section: configparser.SectionProxy, key: str, default):
        """按类型取配置项，取不到时返回默认值"""
        try:
            if isinstance(default, bool):
                return section.getboolean(key, default)
            if isinstance(default, int):
                return section.getint(key, default)
            if isinstance(default, float):
                return section.getfloat(key, default)
            return section.get(key, default)
        except (ValueError, configparser.Error):
            logger.warning('Bad config value: [%s] %s, using default %r', section.name, key, default)
            return default

    def _load_account(self, parser):
        if not parser.has_section('account'):
            return
        s = parser['account']
        self.cookie_file = self._section_get(s, 'cookie_file', '').strip()
        self.room_id = self._section_get(s, 'room_id', 0)
        self.auth_code = self._section_get(s, 'auth_code', '').strip()

        # 也允许 SESSDATA 之类的直接写在 [account] 里
        known = ('cookie_file', 'room_id', 'auth_code')
        for name, value in s.items():
            if name in known:
                continue
            value = value.strip()
            if value:
                self.cookies[name] = value

    def _load_danmaku(self, parser):
        if not parser.has_section('danmaku'):
            return
        s = parser['danmaku']
        self.min_send_interval = max(0.0, self._section_get(s, 'min_send_interval', 2.0))
        self.max_send_queue = max(1, self._section_get(s, 'max_send_queue', 50))
        self.max_length = max(1, self._section_get(s, 'max_length', 20))
        self.send_timeout = max(1.0, self._section_get(s, 'send_timeout', 10.0))
        self.danmaku_color = self._section_get(s, 'color', 16777215)
        self.danmaku_fontsize = self._section_get(s, 'fontsize', 25)
        self.danmaku_mode = self._section_get(s, 'mode', 1)
        self.dry_run = self._section_get(s, 'dry_run', False)

    def _load_auto_reply(self, parser):
        if not parser.has_section('auto_reply'):
            return
        s = parser['auto_reply']
        self.auto_reply_enabled = self._section_get(s, 'enabled', True)
        self.auto_reply_cooldown = max(0.0, self._section_get(s, 'cooldown', 5.0))
        self.auto_reply_template = self._section_get(s, 'template', '{content}')
        self.auto_reply_max_per_minute = max(0, self._section_get(s, 'max_per_minute', 0))
        self.ignore_mirror = self._section_get(s, 'ignore_mirror', True)
        self.ignore_medal_level_below = self._section_get(s, 'ignore_medal_level_below', 0)
        self.min_author_level = self._section_get(s, 'min_author_level', 0)
        self.block_users = _split_list(self._section_get(s, 'block_users', ''))
        self.allow_users = _split_list(self._section_get(s, 'allow_users', ''))

    def _load_panel(self, parser):
        if not parser.has_section('panel'):
            return
        s = parser['panel']
        self.panel_enabled = self._section_get(s, 'enabled', True)
        self.panel_host = self._section_get(s, 'host', '127.0.0.1').strip() or '127.0.0.1'
        self.panel_port = min(65535, max(1, self._section_get(s, 'port', 12452)))
        self.panel_auto_open = self._section_get(s, 'auto_open', True)

    def _load_sdk(self, parser):
        if not parser.has_section('sdk'):
            return
        s = parser['sdk']
        self.plugin_broadcast = self._section_get(s, 'broadcast', True)
        self.broadcast_author = self._section_get(s, 'broadcast_author', '[自动回复]').strip() or '[自动回复]'
        self.connection_timeout = max(10.0, self._section_get(s, 'connection_timeout', 35.0))

    def _load_rules(self, parser):
        rules: List[RuleConfig] = []
        for section_name in parser.sections():
            if not section_name.startswith('rule'):
                continue

            s = parser[section_name]
            rule = RuleConfig()
            rule.id = self._section_get(s, 'name', '').strip() or section_name
            rule.enabled = self._section_get(s, 'enabled', True)
            rule.match_type = self._section_get(s, 'match_type', 'contains').strip().lower()
            # 正则里逗号是有意义的（比如 ^6{3,}$ ），所以正则只能按换行分隔
            raw_keywords = self._section_get(s, 'keywords', '')
            if rule.match_type == 'regex':
                rule.keywords = _split_lines(raw_keywords)
            else:
                rule.keywords = _split_list(raw_keywords)
            rule.replies = _split_replies(self._section_get(s, 'reply', ''))
            rule.reply_mode = self._section_get(s, 'reply_mode', 'first').strip().lower()
            rule.cooldown = max(0.0, self._section_get(s, 'cooldown', 0.0))
            rule.per_user_cooldown = max(0.0, self._section_get(s, 'per_user_cooldown', 0.0))
            rule.priority = self._section_get(s, 'priority', 0)
            rule.min_author_level = self._section_get(s, 'min_author_level', 0)
            rule.min_medal_level = self._section_get(s, 'min_medal_level', 0)
            rule.require_guard = self._section_get(s, 'require_guard', False)
            rule.command = self._section_get(s, 'command', True)
            rule.chat_trigger = self._section_get(s, 'chat_trigger', True)

            if not rule.keywords:
                logger.warning('Rule %s has no keywords, skipped', rule.id)
                continue
            if not rule.replies:
                logger.warning('Rule %s has no reply, skipped', rule.id)
                continue
            if rule.match_type not in ('contains', 'equals', 'prefix', 'suffix', 'regex'):
                logger.warning('Rule %s has bad match_type %r, using contains', rule.id, rule.match_type)
                rule.match_type = 'contains'
            if rule.reply_mode not in ('first', 'random', 'sequence'):
                rule.reply_mode = 'first'

            rules.append(rule)

        # 优先级高的先匹配
        rules.sort(key=lambda r: r.priority, reverse=True)
        self.rules = rules


def _split_list(value: str) -> List[str]:
    """按换行/逗号/中文逗号切分，去掉空项"""
    if not value:
        return []
    normalized = value.replace('，', ',').replace('\r\n', '\n').replace('\r', '\n')
    parts: List[str] = []
    for line in normalized.split('\n'):
        for item in line.split(','):
            item = item.strip()
            if item:
                parts.append(item)
    return parts


def _split_lines(value: str) -> List[str]:
    """按换行切分，保留整行（回复里可以带逗号），去掉空行"""
    if not value:
        return []
    normalized = value.replace('\r\n', '\n').replace('\r', '\n')
    return [line.strip() for line in normalized.split('\n') if line.strip()]


def _split_replies(value: str) -> List[str]:
    """解析 reply 字段。

    写多行时一行一条回复；同一行用逗号分隔也算多条（方便写 ``回复A, 回复B``）。
    需要回复内容本身带逗号时，请写多行（一行一条）。
    """
    if not value:
        return []
    normalized = value.replace('，', ',').replace('\r\n', '\n').replace('\r', '\n')
    replies: List[str] = []
    for line in normalized.split('\n'):
        for item in line.split(','):
            item = item.strip()
            if item:
                replies.append(item)
    return replies


class ConfigStore:
    """线程安全的配置容器。

    控制台线程会随时读取配置，而重载发生在事件循环里，所以这里用“整体替换”
    而不是逐字段修改：新配置先在局部构造完，再一次性换掉引用。这样读配置的
    地方永远看到一份自洽的配置。
    """

    def __init__(self, path: Optional[str] = None):
        # 注意：不要用 DEFAULT_CONFIG_PATH 当默认参数值。默认参数在函数定义时
        # 就求值了，之后改模块常量不会生效（测试里就是这么踩坑的）。
        self._path = path or DEFAULT_CONFIG_PATH
        self._lock = threading.Lock()
        self._config = AppConfig()
        self._loaded = False
        self._error = ''
        self.reload()

    def get(self) -> AppConfig:
        return self._config

    @property
    def path(self) -> str:
        return self._path

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def error(self) -> str:
        return self._error

    def reload(self) -> bool:
        """重读配置文件。失败时保留上一份可用配置并返回 False"""
        config = AppConfig()
        ok = config.load(self._path)
        if not ok:
            self._error = f'读取配置失败：{self._path}'
            logger.warning('Config reload failed, keeping the previous config')

        with self._lock:
            if ok:
                self._config = config
                self._error = ''
            self._loaded = ok
        return ok


# 全局单例：插件运行时都通过它取配置
STORE: Optional[ConfigStore] = None


def get_store() -> ConfigStore:
    global STORE
    if STORE is None:
        STORE = ConfigStore(DEFAULT_CONFIG_PATH)
    return STORE


def set_store(store: 'ConfigStore'):
    global STORE
    STORE = store


def init():
    """加载配置。找不到配置文件时用默认值并给出警告"""
    os.makedirs(DATA_PATH, exist_ok=True)
    os.makedirs(LOG_PATH, exist_ok=True)

    if not os.path.exists(DEFAULT_CONFIG_PATH) and os.path.exists(EXAMPLE_CONFIG_PATH):
        try:
            shutil.copyfile(EXAMPLE_CONFIG_PATH, DEFAULT_CONFIG_PATH)
            logger.info('Created default config: %s', DEFAULT_CONFIG_PATH)
        except OSError:
            logger.exception('Failed to create default config:')

    # 每次都按当前路径重建，保证路径相关的模块常量改了之后能生效
    set_store(ConfigStore(DEFAULT_CONFIG_PATH))
    if not STORE.loaded:
        logger.warning('Failed to load config, using built-in defaults')


def reload() -> bool:
    """重新加载配置。成功返回 True"""
    return get_store().reload()


def get_config() -> AppConfig:
    store = get_store()
    return store.get()


def resolve_cookie_file(config_: 'AppConfig') -> Optional[str]:
    """定位 blivechat 的 cookie jar。

    插件的工作目录是 ``<blivechat>/data/plugins/<插件ID>``，所以 blivechat 的
    ``data/cookie_jar.pickle`` 在 ``../../cookie_jar.pickle``。

    探测路径一律以插件自身的位置为基准，不用当前工作目录——否则在开发环境下
    会意外读到仓库里的 cookie，导致“看起来登录了”但其实是别的环境。
    """
    candidates: List[str] = []

    if config_.cookie_file:
        path = config_.cookie_file
        if not os.path.isabs(path):
            path = os.path.join(BASE_PATH, path)
        candidates.append(path)

    # 插件源码在 <blivechat>/data/plugins/send-danmu 时的位置
    candidates.append(os.path.realpath(os.path.join(BASE_PATH, '..', '..', 'cookie_jar.pickle')))
    # 开发时插件源码在 <repo>/plugins/send-danmu 时的位置
    candidates.append(os.path.realpath(os.path.join(BASE_PATH, '..', '..', 'data', 'cookie_jar.pickle')))

    # 被 PyInstaller 打包后 __file__ 在临时解包目录里，不能用来定位 data
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.realpath(
            os.path.join(os.path.dirname(sys.executable), '..', '..', 'cookie_jar.pickle')
        ))

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None
