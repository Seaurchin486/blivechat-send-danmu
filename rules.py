# -*- coding: utf-8 -*-
"""自动回复规则引擎。

这里只做纯决策：收到一条弹幕，决定要不要回、回什么。真正发送由
``bilibili_live.danmaku.DanmakuClient`` 负责，这样决策逻辑可以单独测试。

关键的去重/防自触发设计：

* blivechat 会把插件自己产生的消息也转发回来（``extra.is_from_plugin``），
  所以先按 ``is_from_plugin`` 过滤；
* 我们在 B 站真实发出去的弹幕，会作为普通观众弹幕从弹幕流回到
  blivechat，这一条必须被忽略，否则就会自己回自己。这里用
  ``expected_echoes`` 记录“我们自己发出去的内容指纹”，命中即忽略。
"""
import collections
import hashlib
import logging
import random
import re
import time
from typing import *

logger = logging.getLogger('send-danmu.' + __name__)

# 回复模板里可用的变量
TEMPLATE_VARS = (
    'content', 'author', 'uid', 'level', 'medal_level', 'medal_name',
    'guard_name', 'keyword', 'room_id', 'time',
)

COMMAND_PREFIXES = ('/', '!', '！')


class SafeDict(dict):
    """模板里出现未知变量时保留原样，而不是抛 KeyError"""

    def __missing__(self, key):
        return '{' + key + '}'


class IncomingDanmaku:
    """一条待决策的弹幕"""

    def __init__(
        self,
        content: str,
        author: str = '',
        uid: str = '',
        *,
        timestamp: int = 0,
        author_level: int = 0,
        medal_level: int = 0,
        medal_name: str = '',
        guard_level: int = 0,
        message_id: str = '',
        is_mirror: bool = False,
        is_from_plugin: bool = False,
        room_id: int = 0,
    ):
        self.content = content
        self.author = author
        self.uid = uid
        self.timestamp = timestamp or int(time.time())
        self.author_level = author_level
        self.medal_level = medal_level
        self.medal_name = medal_name
        self.guard_level = guard_level
        self.message_id = message_id
        self.is_mirror = is_mirror
        self.is_from_plugin = is_from_plugin
        self.room_id = room_id

    @property
    def guard_name(self) -> str:
        return {1: '总督', 2: '提督', 3: '舰长'}.get(self.guard_level, '')

    def event_key(self) -> str:
        """事件指纹，用于同一事件的重复投递去重"""
        if self.message_id:
            return f'id:{self.message_id}'
        raw = f'{self.author}\x00{self.content}\x00{self.timestamp}'
        return 'h:' + hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            'content': self.content,
            'author': self.author,
            'uid': self.uid,
            'timestamp': self.timestamp,
            'authorLevel': self.author_level,
            'medalLevel': self.medal_level,
            'medalName': self.medal_name,
            'guardLevel': self.guard_level,
            'isMirror': self.is_mirror,
        }


class Decision:
    """决策结果"""

    def __init__(
        self,
        should_reply: bool,
        *,
        reply: str = '',
        rule_id: str = '',
        reason: str = '',
        keyword: str = '',
    ):
        self.should_reply = should_reply
        self.reply = reply
        self.rule_id = rule_id
        self.reason = reason
        self.keyword = keyword

    def __repr__(self):
        if self.should_reply:
            return f'<Decision reply rule={self.rule_id!r} {self.reply!r}>'
        return f'<Decision skip reason={self.reason!r}>'


class AutoReplyEngine:
    """规则匹配 + 冷却 + 去重"""

    def __init__(self, config_):
        self.reload(config_)

    def reload(self, config_):
        self._config = config_
        # 按优先级降序。配置加载时已经排过一遍，这里再排一次，
        # 这样直接用 AppConfig 构造出来的配置也能保证匹配顺序正确。
        self._rules = sorted(
            (r for r in config_.rules if r.enabled),
            key=lambda r: r.priority,
            reverse=True,
        )

        self._own_author = ''
        """我们自己的 B 站昵称，决定回声匹配精度"""
        # 规则ID -> [(关键词原文, 编译后的正则)]，只在 match_type 为 regex 时有内容
        self._compiled: Dict[str, List[Tuple[str, re.Pattern]]] = {}
        for rule in self._rules:
            if rule.match_type == 'regex':
                patterns: List[Tuple[str, re.Pattern]] = []
                for keyword in rule.keywords:
                    try:
                        patterns.append((keyword, re.compile(keyword, re.IGNORECASE)))
                    except re.error as e:
                        logger.warning('Rule %s has bad regex %r: %s', rule.id, keyword, e)
                self._compiled[rule.id] = patterns

        # 冷却状态
        self._global_last_reply = 0.0
        self._rule_last_reply: Dict[str, float] = {}
        self._user_rule_last_reply: Dict[Tuple[str, str], float] = {}
        self._reply_times: Deque[float] = collections.deque()

        # 去重状态
        self._seen_events: Dict[str, float] = {}
        self._expected_echoes: Dict[str, float] = {}
        self._sequence_counter: Dict[str, int] = {}

    # ------------------------------------------------------------ 状态维护

    def note_our_message(self, content: str, *, author: str = '', ttl: float = 30.0):
        """记录“我们刚发出去了这条弹幕”，它稍后会从弹幕流回来，必须忽略。

        匹配精度取决于我们是否知道自己的昵称：

        * 知道自己是谁 —— 只按“作者 + 内容”匹配。精确，不会误伤别人发的相同内容。
        * 不知道 —— 退化成“只看内容”的通配匹配。宁可 30 秒内对同内容的弹幕少回
          一次，也不能漏认自己的回声，否则会自己回自己、无限循环（B 站会把我们
          发出的弹幕原样推回来）。
        """
        expires = time.monotonic() + ttl
        if author:
            self._expected_echoes[self._echo_key(content, author)] = expires
        if not author or not self._own_author:
            # 不知道自己昵称时，注册通配指纹兜底
            self._expected_echoes[self._echo_key(content, '')] = expires

    def set_own_author(self, author: str):
        """告诉引擎我们自己的 B 站昵称，用于精确识别回声"""
        self._own_author = author or ''
        if self._own_author:
            # 已经有昵称了，把之前注册的通配指纹清掉，避免继续误伤
            self._expected_echoes = {
                key: exp for key, exp in self._expected_echoes.items()
                if not key.startswith('wildcard:')
            }

    @staticmethod
    def _echo_key(content: str, author: str) -> str:
        prefix = 'wildcard:' if not author else 'pair:'
        raw = f'{author}\x00{content}'
        return prefix + hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()

    def _is_our_echo(self, msg: IncomingDanmaku) -> bool:
        now = time.monotonic()
        # 顺便清理过期项
        for key in [k for k, exp in self._expected_echoes.items() if exp < now]:
            del self._expected_echoes[key]

        if self._echo_key(msg.content, msg.author) in self._expected_echoes:
            return True
        # 不知道昵称时才用通配指纹兜底
        if not self._own_author:
            return self._echo_key(msg.content, '') in self._expected_echoes
        return False

    def _is_duplicate(self, msg: IncomingDanmaku) -> bool:
        now = time.monotonic()
        # 5 分钟窗口足够覆盖 blivechat 的重连重放
        window = 300.0
        for key in [k for k, exp in self._seen_events.items() if exp < now]:
            del self._seen_events[key]

        key = msg.event_key()
        if key in self._seen_events:
            return True
        self._seen_events[key] = now + window
        return False

    # --------------------------------------------------------------- 决策

    def decide(self, msg: IncomingDanmaku) -> Decision:
        config_ = self._config

        if not config_.auto_reply_enabled:
            return Decision(False, reason='自动回复已关闭')
        if msg.is_from_plugin:
            return Decision(False, reason='插件自己产生的消息')
        if config_.ignore_mirror and msg.is_mirror:
            return Decision(False, reason='跨房弹幕')
        if self._is_our_echo(msg):
            return Decision(False, reason='自己发出的弹幕回声')
        if self._is_duplicate(msg):
            return Decision(False, reason='重复投递的消息')
        if not self._passes_filters(msg):
            return Decision(False, reason='未通过全局过滤')
        if not self._within_rate_limit():
            return Decision(False, reason='已达每分钟回复上限')

        keyword, matched_rule = self._match(msg)
        if matched_rule is None:
            return Decision(False, reason='没有规则匹配')

        now = time.monotonic()

        # 全局冷却
        elapsed = now - self._global_last_reply
        if self._global_last_reply > 0 and elapsed < config_.auto_reply_cooldown:
            return Decision(False, reason=f'全局冷却中（还需 {config_.auto_reply_cooldown - elapsed:.1f}s）')

        # 规则冷却
        cooldown = matched_rule.cooldown or config_.auto_reply_cooldown
        rule_last = self._rule_last_reply.get(matched_rule.id, 0.0)
        if rule_last > 0 and now - rule_last < cooldown:
            return Decision(False, reason=f'规则 {matched_rule.id} 冷却中')

        # 同一用户对同一规则的冷却
        if matched_rule.per_user_cooldown > 0:
            user_key = (matched_rule.id, msg.uid or msg.author)
            user_last = self._user_rule_last_reply.get(user_key, 0.0)
            if user_last > 0 and now - user_last < matched_rule.per_user_cooldown:
                return Decision(False, reason=f'用户 {msg.author} 触发规则 {matched_rule.id} 过于频繁')

        reply = self._render(matched_rule, msg, keyword, config_)

        # 占用冷却，防止同一批消息里连续命中
        self._global_last_reply = now
        self._rule_last_reply[matched_rule.id] = now
        if matched_rule.per_user_cooldown > 0:
            self._user_rule_last_reply[(matched_rule.id, msg.uid or msg.author)] = now
        self._reply_times.append(now)

        return Decision(True, reply=reply, rule_id=matched_rule.id, keyword=keyword)

    # ------------------------------------------------------------ 判定细节

    def _passes_filters(self, msg: IncomingDanmaku) -> bool:
        config_ = self._config

        if config_.allow_users:
            if not _matches_user_list(msg, config_.allow_users):
                logger.debug('Ignored %s: not in allow list', msg.author)
                return False
        if config_.block_users and _matches_user_list(msg, config_.block_users):
            logger.debug('Ignored %s: in block list', msg.author)
            return False
        if config_.ignore_medal_level_below > 0 and msg.medal_level < config_.ignore_medal_level_below:
            # 没戴当前房间勋章（0级）的也一并忽略
            return False
        if config_.min_author_level > 0 and msg.author_level < config_.min_author_level:
            return False
        return True

    def _within_rate_limit(self) -> bool:
        limit = self._config.auto_reply_max_per_minute
        if limit <= 0:
            return True
        now = time.monotonic()
        reply_times = self._reply_times
        while reply_times and now - reply_times[0] > 60.0:
            reply_times.popleft()
        return len(reply_times) < limit

    def _match(self, msg: IncomingDanmaku) -> Tuple[str, Optional[Any]]:
        """返回 (命中的关键词, 规则)"""
        content = msg.content
        stripped = content.strip()
        command_target = stripped
        is_command = False
        for prefix in COMMAND_PREFIXES:
            if stripped.startswith(prefix):
                command_target = stripped[len(prefix):].strip()
                is_command = True
                break

        for rule in self._rules:  # 已按 priority 降序排列
            if is_command and not rule.command:
                continue
            if not is_command and not rule.chat_trigger:
                continue

            target = command_target if is_command else content
            keyword = self._match_rule(rule, target, msg)
            if keyword is not None:
                return keyword, rule

            # 指令模式下额外允许“/关键词 参数”的写法
            if is_command and rule.match_type in ('equals', 'contains'):
                for kw in rule.keywords:
                    if command_target == kw or command_target.startswith(kw + ' ') or command_target.startswith(kw + '　'):
                        return kw, rule
        return '', None

    def _match_rule(self, rule, target: str, msg: IncomingDanmaku) -> Optional[str]:
        # 用户条件不满足时直接跳过
        if rule.min_author_level > 0 and msg.author_level < rule.min_author_level:
            return None
        if rule.min_medal_level > 0 and msg.medal_level < rule.min_medal_level:
            return None
        if rule.require_guard and msg.guard_level <= 0:
            return None

        for keyword in rule.keywords:
            if rule.match_type == 'contains':
                if keyword.lower() in target.lower():
                    return keyword
            elif rule.match_type == 'equals':
                if target.strip().lower() == keyword.lower():
                    return keyword
            elif rule.match_type == 'prefix':
                if target.lower().startswith(keyword.lower()):
                    return keyword
            elif rule.match_type == 'suffix':
                if target.lower().endswith(keyword.lower()):
                    return keyword
        # regex 单独一轮，保证同一规则内按关键词顺序返回命中的那一个
        if rule.match_type == 'regex':
            for keyword, pattern in self._compiled.get(rule.id, []):
                if pattern.search(target):
                    return keyword
        return None

    def _render(self, rule, msg: IncomingDanmaku, keyword: str, config_) -> str:
        reply = self._pick_reply(rule)
        template = config_.auto_reply_template or '{content}'

        mapping = SafeDict(
            content=reply,
            author=msg.author,
            uid=msg.uid,
            level=msg.author_level,
            medal_level=msg.medal_level,
            medal_name=msg.medal_name,
            guard_name=msg.guard_name,
            keyword=keyword,
            room_id=msg.room_id,
            time=time.strftime('%H:%M:%S', time.localtime(msg.timestamp)),
        )
        try:
            text = template.format_map(mapping)
        except (ValueError, IndexError) as e:
            logger.warning('Bad auto_reply template %r: %s', template, e)
            text = reply

        # 模板里没用到 {content} 时把回复内容接在后面，避免只回一句无意义的话
        if '{content}' not in template and reply and reply not in text:
            text = f'{text} {reply}'
        return text.strip()

    def _pick_reply(self, rule) -> str:
        replies = rule.replies
        if not replies:
            return ''
        if len(replies) == 1 or rule.reply_mode == 'first':
            return replies[0]
        if rule.reply_mode == 'random':
            return random.choice(replies)
        # sequence：轮流取
        index = self._sequence_counter.get(rule.id, 0)
        self._sequence_counter[rule.id] = (index + 1) % len(replies)
        return replies[index]

    # ----------------------------------------------------------- 供面板显示

    def describe_rules(self) -> List[Dict[str, Any]]:
        return [
            {
                'id': r.id,
                'enabled': r.enabled,
                'matchType': r.match_type,
                'keywords': r.keywords,
                'replies': r.replies,
                'replyMode': r.reply_mode,
                'cooldown': r.cooldown,
                'perUserCooldown': r.per_user_cooldown,
                'priority': r.priority,
                'command': r.command,
                'chatTrigger': r.chat_trigger,
                'requireGuard': r.require_guard,
                'minAuthorLevel': r.min_author_level,
                'minMedalLevel': r.min_medal_level,
                'regexOk': all(
                    _is_valid_regex(k) for k in r.keywords
                ) if r.match_type == 'regex' else True,
            }
            for r in self._rules
        ]


def _is_valid_regex(pattern: str) -> bool:
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


def _matches_user_list(msg: IncomingDanmaku, entries: List[str]) -> bool:
    for entry in entries:
        if not entry:
            continue
        if entry == msg.author or entry == msg.uid:
            return True
        if entry.startswith('uid:') and entry[4:] == msg.uid:
            return True
        if entry.startswith('name:') and entry[5:] == msg.author:
            return True
    return False
