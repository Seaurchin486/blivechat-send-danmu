# -*- coding: utf-8 -*-
"""规则引擎的单元测试。

重点覆盖几个容易出错的地方：
* 自己发出去的弹幕回声必须被忽略（否则会无限自我回复）
* blivechat 重复投递的同一条消息只处理一次
* 冷却与每分钟上限
* 指令模式 / 普通聊天模式的区分
* 模板变量渲染
"""
import sys
import os
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from rules import AutoReplyEngine, IncomingDanmaku


def make_config(**overrides):
    c = config.AppConfig()
    c.auto_reply_enabled = True
    c.auto_reply_cooldown = 0.0
    c.auto_reply_template = '{content}'
    c.auto_reply_max_per_minute = 0
    c.ignore_mirror = True
    c.ignore_medal_level_below = 0
    c.min_author_level = 0
    c.block_users = []
    c.allow_users = []
    c.rules = []
    for key, value in overrides.items():
        setattr(c, key, value)
    return c


def make_rule(rule_id='r1', **overrides):
    r = config.RuleConfig()
    r.id = rule_id
    r.enabled = True
    r.match_type = 'contains'
    r.keywords = ['你好']
    r.replies = ['你好呀']
    r.reply_mode = 'first'
    r.cooldown = 0.0
    r.per_user_cooldown = 0.0
    r.priority = 0
    r.min_author_level = 0
    r.min_medal_level = 0
    r.require_guard = False
    r.command = True
    r.chat_trigger = True
    for key, value in overrides.items():
        setattr(r, key, value)
    return r


def dm(content, author='观众A', uid='1001', **kwargs):
    # 默认给一个唯一的消息ID：真实环境里 blivechat 会给每条弹幕一个ID，
    # 不传的话会被去重逻辑当成同一条消息的重复投递。
    if 'message_id' not in kwargs:
        kwargs['message_id'] = _next_message_id()
    return IncomingDanmaku(content, author, uid, **kwargs)


_message_counter = [0]


def _next_message_id():
    _message_counter[0] += 1
    return f'auto-{_message_counter[0]}'


# --------------------------------------------------------------------- 基础

def test_simple_keyword_match():
    engine = AutoReplyEngine(make_config(rules=[make_rule()]))
    d = engine.decide(dm('你好啊'))
    assert d.should_reply
    assert d.reply == '你好呀'
    assert d.rule_id == 'r1'


def test_no_match():
    engine = AutoReplyEngine(make_config(rules=[make_rule()]))
    d = engine.decide(dm('完全无关的内容'))
    assert not d.should_reply
    assert d.reason == '没有规则匹配'


def test_disabled_globally():
    engine = AutoReplyEngine(make_config(auto_reply_enabled=False, rules=[make_rule()]))
    assert not engine.decide(dm('你好')).should_reply


def test_disabled_rule_skipped():
    engine = AutoReplyEngine(make_config(rules=[make_rule(enabled=False)]))
    assert not engine.decide(dm('你好')).should_reply


def test_from_plugin_ignored():
    engine = AutoReplyEngine(make_config(rules=[make_rule()]))
    d = engine.decide(dm('你好', is_from_plugin=True))
    assert not d.should_reply
    assert '插件' in d.reason


def test_mirror_ignored():
    engine = AutoReplyEngine(make_config(rules=[make_rule()]))
    assert not engine.decide(dm('你好', is_mirror=True)).should_reply


def test_mirror_allowed_when_configured():
    engine = AutoReplyEngine(make_config(ignore_mirror=False, rules=[make_rule()]))
    assert engine.decide(dm('你好', is_mirror=True)).should_reply


# --------------------------------------------------- 自己的回声 & 重复投递

def test_own_echo_is_ignored():
    """在 B 站真发出去的弹幕会作为观众弹幕回来，必须不理它"""
    engine = AutoReplyEngine(make_config(rules=[make_rule(keywords=['哈哈'], replies=['嘿嘿'])]))
    engine.set_own_author('我的小号')
    engine.note_our_message('哈哈', author='我的小号')

    # 回声：作者名和内容都和我们发的一致
    d = engine.decide(dm('哈哈', author='我的小号'))
    assert not d.should_reply
    assert '回声' in d.reason

    # 别人说同样的话，应该正常回复
    assert engine.decide(dm('哈哈', author='别的观众')).should_reply


def test_own_echo_ignored_when_author_name_unknown():
    """本地完全拿不到用户名时，退化成按内容匹配，也要能认出自己的回声"""
    engine = AutoReplyEngine(make_config(rules=[make_rule(keywords=['哈哈'], replies=['嘿嘿'])]))
    engine.note_our_message('哈哈', author='')

    d = engine.decide(dm('哈哈', author='B站上显示的昵称'))
    assert not d.should_reply
    assert '回声' in d.reason


def test_known_own_author_does_not_block_other_viewers():
    """知道自己的昵称时，别的观众发同样内容也不能被误判成回声"""
    engine = AutoReplyEngine(make_config(rules=[make_rule(keywords=['哈哈'], replies=['嘿嘿'])]))
    engine.set_own_author('我的小号')
    engine.note_our_message('哈哈', author='我的小号')

    # 我们自己的回声被忽略
    assert not engine.decide(dm('哈哈', author='我的小号')).should_reply
    # 别的观众发一样的内容，正常回复
    assert engine.decide(dm('哈哈', author='别的观众')).should_reply


def test_duplicate_delivery_processed_once():
    engine = AutoReplyEngine(make_config(rules=[make_rule()]))
    first = engine.decide(dm('你好', message_id='abc123'))
    second = engine.decide(dm('你好', message_id='abc123'))
    assert first.should_reply
    assert not second.should_reply
    assert '重复' in second.reason


def test_same_content_different_users_not_duplicate():
    engine = AutoReplyEngine(make_config(rules=[make_rule()]))
    assert engine.decide(dm('你好', author='A', message_id='m1')).should_reply
    assert engine.decide(dm('你好', author='B', message_id='m2')).should_reply


# ------------------------------------------------------------------- 冷却

def test_global_cooldown():
    engine = AutoReplyEngine(make_config(
        auto_reply_cooldown=60.0,
        rules=[make_rule()],
    ))
    assert engine.decide(dm('你好', uid='1', message_id='a')).should_reply
    d = engine.decide(dm('你好', uid='2', message_id='b'))
    assert not d.should_reply
    assert '冷却' in d.reason


def test_per_user_cooldown_only_affects_that_user():
    engine = AutoReplyEngine(make_config(rules=[
        make_rule(per_user_cooldown=60.0),
    ]))
    assert engine.decide(dm('你好', uid='u1', message_id='a')).should_reply
    # 同一个用户被挡
    assert not engine.decide(dm('你好', uid='u1', message_id='b')).should_reply
    # 另一个用户不受影响
    assert engine.decide(dm('你好', uid='u2', message_id='c')).should_reply


def test_per_minute_limit():
    engine = AutoReplyEngine(make_config(
        auto_reply_max_per_minute=2,
        rules=[make_rule()],
    ))
    assert engine.decide(dm('你好', uid='1', message_id='a')).should_reply
    assert engine.decide(dm('你好', uid='2', message_id='b')).should_reply
    d = engine.decide(dm('你好', uid='3', message_id='c'))
    assert not d.should_reply
    assert '上限' in d.reason


def test_rule_cooldown_overrides_global():
    engine = AutoReplyEngine(make_config(
        auto_reply_cooldown=0.0,
        rules=[make_rule(cooldown=60.0)],
    ))
    assert engine.decide(dm('你好', uid='1', message_id='a')).should_reply
    assert not engine.decide(dm('你好', uid='2', message_id='b')).should_reply


# ------------------------------------------------------------------- 过滤

def test_block_list_by_name():
    engine = AutoReplyEngine(make_config(block_users=['坏蛋'], rules=[make_rule()]))
    assert not engine.decide(dm('你好', author='坏蛋')).should_reply
    assert engine.decide(dm('你好', author='好人')).should_reply


def test_block_list_by_uid_prefix():
    engine = AutoReplyEngine(make_config(block_users=['uid:9999'], rules=[make_rule()]))
    assert not engine.decide(dm('你好', uid='9999')).should_reply
    assert engine.decide(dm('你好', uid='1111')).should_reply


def test_allow_list_is_exclusive():
    engine = AutoReplyEngine(make_config(allow_users=['name:VIP'], rules=[make_rule()]))
    assert not engine.decide(dm('你好', author='路人')).should_reply
    assert engine.decide(dm('你好', author='VIP')).should_reply


def test_medal_level_filter():
    engine = AutoReplyEngine(make_config(ignore_medal_level_below=5, rules=[make_rule()]))
    assert not engine.decide(dm('你好', medal_level=3)).should_reply
    assert engine.decide(dm('你好', medal_level=7)).should_reply


def test_author_level_filter():
    engine = AutoReplyEngine(make_config(min_author_level=10, rules=[make_rule()]))
    assert not engine.decide(dm('你好', author_level=2)).should_reply
    assert engine.decide(dm('你好', author_level=20)).should_reply


# ------------------------------------------------------------- 匹配方式

@pytest.mark.parametrize('match_type,keyword,text,expected', [
    ('contains', '你好', 'xx你好xx', True),
    ('contains', '你好', 'xx再见xx', False),
    ('equals', '你好', '你好', True),
    ('equals', '你好', '你好啊', False),
    ('prefix', '你好', '你好啊', True),
    ('prefix', '你好', '我你好', False),
    ('suffix', '再见', '大家再见', True),
    ('suffix', '再见', '再见大家', False),
])
def test_match_types(match_type, keyword, text, expected):
    engine = AutoReplyEngine(make_config(rules=[
        make_rule(match_type=match_type, keywords=[keyword]),
    ]))
    assert engine.decide(dm(text)).should_reply is expected


def test_regex_match():
    engine = AutoReplyEngine(make_config(rules=[
        make_rule(match_type='regex', keywords=['^6{3,}$']),
    ]))
    assert engine.decide(dm('666')).should_reply
    assert engine.decide(dm('66666')).should_reply
    assert not engine.decide(dm('66')).should_reply
    assert not engine.decide(dm('6612')).should_reply


def test_case_insensitive_match():
    engine = AutoReplyEngine(make_config(rules=[
        make_rule(match_type='contains', keywords=['hello']),
    ]))
    assert engine.decide(dm('HELLO world')).should_reply


# --------------------------------------------------------------- 指令模式

def test_command_form_triggers():
    engine = AutoReplyEngine(make_config(rules=[make_rule(keywords=['点歌'])]))
    assert engine.decide(dm('/点歌')).should_reply


def test_command_form_with_argument():
    engine = AutoReplyEngine(make_config(rules=[make_rule(keywords=['点歌'])]))
    assert engine.decide(dm('/点歌 起风了')).should_reply


def test_command_only_rule_ignores_plain_chat():
    engine = AutoReplyEngine(make_config(rules=[
        make_rule(keywords=['点歌'], chat_trigger=False),
    ]))
    assert not engine.decide(dm('我想点歌')).should_reply
    assert engine.decide(dm('/点歌')).should_reply


def test_chat_only_rule_ignores_command():
    engine = AutoReplyEngine(make_config(rules=[
        make_rule(keywords=['你好'], command=False),
    ]))
    assert not engine.decide(dm('/你好')).should_reply
    assert engine.decide(dm('你好')).should_reply


def test_full_width_exclamation_command():
    engine = AutoReplyEngine(make_config(rules=[make_rule(keywords=['打卡'])]))
    assert engine.decide(dm('！打卡')).should_reply


# ----------------------------------------------------------------- 优先级

def test_priority_order():
    low = make_rule('low', keywords=['你好'], replies=['低优先级'], priority=0)
    high = make_rule('high', keywords=['你好'], replies=['高优先级'], priority=10)
    engine = AutoReplyEngine(make_config(rules=[low, high]))
    d = engine.decide(dm('你好'))
    assert d.rule_id == 'high'
    assert d.reply == '高优先级'


def test_rule_conditions_skip_to_next_rule():
    """高优先级规则的用户条件不满足时，应该落到下一条规则"""
    guard_rule = make_rule('guard', keywords=['你好'], replies=['舰长好'], require_guard=True, priority=10)
    normal_rule = make_rule('normal', keywords=['你好'], replies=['你好呀'], priority=0)
    engine = AutoReplyEngine(make_config(rules=[guard_rule, normal_rule]))

    d = engine.decide(dm('你好', guard_level=0, message_id='a'))
    assert d.rule_id == 'normal'

    engine2 = AutoReplyEngine(make_config(rules=[guard_rule, normal_rule]))
    d2 = engine2.decide(dm('你好', guard_level=3, message_id='b'))
    assert d2.rule_id == 'guard'


def test_min_medal_level_in_rule():
    rule = make_rule(min_medal_level=10)
    engine = AutoReplyEngine(make_config(rules=[rule]))
    assert not engine.decide(dm('你好', medal_level=1)).should_reply
    assert engine.decide(dm('你好', medal_level=12)).should_reply


# ----------------------------------------------------------- 回复选取/模板

def test_reply_mode_first():
    engine = AutoReplyEngine(make_config(rules=[
        make_rule(replies=['A', 'B'], reply_mode='first'),
    ]))
    assert engine.decide(dm('你好', message_id='a')).reply == 'A'


def test_reply_mode_sequence():
    engine = AutoReplyEngine(make_config(
        auto_reply_cooldown=0.0,
        rules=[make_rule(replies=['A', 'B', 'C'], reply_mode='sequence')],
    ))
    got = [engine.decide(dm('你好', uid=str(i), message_id=f'm{i}')).reply for i in range(3)]
    assert got == ['A', 'B', 'C']


def test_reply_mode_random_only_uses_given_replies():
    engine = AutoReplyEngine(make_config(rules=[
        make_rule(replies=['A', 'B'], reply_mode='random'),
    ]))
    for i in range(10):
        assert engine.decide(dm('你好', uid=str(i), message_id=f'm{i}')).reply in ('A', 'B')


def test_template_variables():
    engine = AutoReplyEngine(make_config(
        auto_reply_template='{author}({level}) 说 {content}',
        rules=[make_rule()],
    ))
    d = engine.decide(dm('你好', author='小明', author_level=7))
    assert d.reply == '小明(7) 说 你好呀'


def test_template_content_appended_when_missing():
    """模板里没写 {content} 时，规则回复要接在后面，不能丢"""
    engine = AutoReplyEngine(make_config(
        auto_reply_template='收到！',
        rules=[make_rule()],
    ))
    d = engine.decide(dm('你好'))
    assert d.reply == '收到！ 你好呀'


def test_template_unknown_variable_kept():
    engine = AutoReplyEngine(make_config(
        auto_reply_template='{unknown} {content}',
        rules=[make_rule()],
    ))
    d = engine.decide(dm('你好'))
    assert d.reply == '{unknown} 你好呀'


def test_template_medal_and_guard():
    engine = AutoReplyEngine(make_config(
        auto_reply_template='[{guard_name}][{medal_name}{medal_level}]{content}',
        rules=[make_rule()],
    ))
    d = engine.decide(dm('你好', guard_level=3, medal_name='粉丝', medal_level=21))
    assert d.reply == '[舰长][粉丝21]你好呀'


# ----------------------------------------------------------------- 其它

def test_engine_reload_picks_up_changes():
    c = make_config(rules=[make_rule()])
    engine = AutoReplyEngine(c)
    assert engine.decide(dm('你好', message_id='a')).should_reply

    c2 = make_config(rules=[make_rule(keywords=['再见'], replies=['拜拜'])])
    engine.reload(c2)
    assert not engine.decide(dm('你好', message_id='b')).should_reply
    assert engine.decide(dm('再见', message_id='c')).should_reply


def test_describe_rules_reports_bad_regex():
    rule = make_rule(match_type='regex', keywords=['[unclosed'])
    engine = AutoReplyEngine(make_config(rules=[rule]))
    described = engine.describe_rules()
    assert described[0]['regexOk'] is False


def test_bad_regex_does_not_crash():
    rule = make_rule(match_type='regex', keywords=['[unclosed'])
    engine = AutoReplyEngine(make_config(rules=[rule]))
    assert not engine.decide(dm('随便')).should_reply
