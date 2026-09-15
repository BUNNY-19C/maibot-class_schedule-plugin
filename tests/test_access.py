"""会话身份提取与访问名单判定测试。

这一层的价值在于「拿不到群号也不能崩、也不能误判」：
不同适配器给的消息结构不一样，所以覆盖 dict / 对象 / 缺字段 / 空消息
四种形态。
"""

import unittest

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.access import (
    ChatIdentity,
    evaluate_access,
    evaluate_chat_scope,
    identity_from_kwargs,
    normalize_entry,
)


class FakeInfo:
    """模拟「带属性的消息对象」形态。"""

    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class TestIdentityFromKwargs(unittest.TestCase):
    def test_group_message_as_dict(self):
        identity = identity_from_kwargs(
            {
                "stream_id": "s1",
                "message": {
                    "chat_info": {
                        "group_info": {"group_id": "123456", "group_name": "高数三班"}
                    },
                    "user_info": {"user_id": "654321", "user_nickname": "小明"},
                },
            }
        )
        self.assertEqual(identity.stream_id, "s1")
        self.assertEqual(identity.group_id, "123456")
        self.assertEqual(identity.user_id, "654321")
        self.assertEqual(identity.chat_type, "group")
        self.assertEqual(identity.label, "高数三班")
        self.assertIn("群「高数三班」(123456)", identity.display)

    def test_group_message_as_object(self):
        group_info = FakeInfo(group_id="123456", group_name="高数三班")
        chat_info = FakeInfo(group_info=group_info)
        message = FakeInfo(
            chat_info=chat_info, user_info=FakeInfo(user_id="654321")
        )
        identity = identity_from_kwargs({"stream_id": "s1", "message": message})
        self.assertEqual(identity.group_id, "123456")
        self.assertEqual(identity.chat_type, "group")

    def test_private_message_has_no_group(self):
        identity = identity_from_kwargs(
            {
                "stream_id": "p1",
                "message": {"user_info": {"user_id": "654321", "user_nickname": "小明"}},
            }
        )
        self.assertEqual(identity.group_id, "")
        self.assertEqual(identity.chat_type, "private")
        self.assertIn("私聊「小明」", identity.display)

    def test_flat_message_dict(self):
        """有些适配器直接把字段摊平在消息里。"""
        identity = identity_from_kwargs(
            {"stream_id": "s1", "message": {"group_id": "111", "user_id": "222"}}
        )
        self.assertEqual(identity.group_id, "111")
        self.assertEqual(identity.user_id, "222")

    def test_missing_message_falls_back_to_stream_id(self):
        identity = identity_from_kwargs({"stream_id": "only-stream"})
        self.assertEqual(identity.stream_id, "only-stream")
        self.assertEqual(identity.identifiers, ["only-stream"])

    def test_empty_kwargs(self):
        identity = identity_from_kwargs({})
        self.assertEqual(identity.identifiers, [])
        self.assertEqual(identity.chat_type, "")
        self.assertEqual(identity.display, "未知会话")

    def test_none_kwargs(self):
        self.assertEqual(identity_from_kwargs(None).identifiers, [])

    def test_is_group_flag_used_when_group_id_missing(self):
        identity = identity_from_kwargs(
            {"stream_id": "s1", "message": {"is_group": True, "user_id": "222"}}
        )
        self.assertEqual(identity.chat_type, "group")

    def test_raw_message_used_as_fallback(self):
        identity = identity_from_kwargs(
            {"stream_id": "s1", "raw_message": {"message_base_info": {"group_id": "777"}}}
        )
        self.assertEqual(identity.group_id, "777")

    def test_raw_message_preferred_over_identity_less_message(self):
        """回归：message 结构陌生时不能就此放弃，raw_message 里的群号要用上。"""
        identity = identity_from_kwargs(
            {
                "stream_id": "s1",
                "message": {"something_unexpected": {"nested": "x"}},
                "raw_message": {"group_id": "888", "user_id": "999"},
            }
        )
        self.assertEqual(identity.group_id, "888")
        self.assertEqual(identity.user_id, "999")

    def test_message_with_ids_beats_raw_message(self):
        identity = identity_from_kwargs(
            {
                "stream_id": "s1",
                "message": {"group_info": {"group_id": "111"}},
                "raw_message": {"group_id": "222"},
            }
        )
        self.assertEqual(identity.group_id, "111")

    def test_top_level_kwargs_fields_used(self):
        """部分适配器把 group_id/user_id 直接摊在 kwargs 顶层。"""
        identity = identity_from_kwargs(
            {"stream_id": "s1", "group_id": "333", "user_id": "444", "is_group": True}
        )
        self.assertEqual(identity.group_id, "333")
        self.assertEqual(identity.user_id, "444")
        self.assertEqual(identity.chat_type, "group")

    def test_stream_id_kept_when_no_ids_anywhere(self):
        identity = identity_from_kwargs(
            {"stream_id": "s1", "message": {"chat_info": {"group_info": {}}}}
        )
        self.assertEqual(identity.stream_id, "s1")
        self.assertEqual(identity.group_id, "")
        self.assertEqual(identity.identifiers, ["s1"])

    def test_identifiers_exclude_empty_and_keep_order(self):
        identity = ChatIdentity(stream_id="s", group_id="g", user_id="u")
        self.assertEqual(identity.identifiers, ["g", "u", "s"])

    def test_identifiers_dedupe(self):
        identity = ChatIdentity(stream_id="same", group_id="same")
        self.assertEqual(identity.identifiers, ["same"])


class TestNormalizeEntry(unittest.TestCase):
    def test_plain_id(self):
        self.assertEqual(normalize_entry("123456"), ["123456"])

    def test_platform_prefixed(self):
        self.assertEqual(normalize_entry("qq:123456"), ["qq:123456", "123456"])

    def test_whitespace_trimmed(self):
        self.assertEqual(normalize_entry("  123456  "), ["123456"])

    def test_empty(self):
        self.assertEqual(normalize_entry(""), [])
        self.assertEqual(normalize_entry(None), [])

    def test_non_string(self):
        self.assertEqual(normalize_entry(123456), ["123456"])


class TestRealMaiBotPayload(unittest.TestCase):
    """按麦麦主机**实测**下发的结构构造用例。

    来源（本机麦麦 1.2.0 源码）：
    - ``src/plugin_runtime/component_query.py::_build_command_executor`` 组装的
      invoke_args：顶层带 ``stream_id`` / ``group_id`` / ``user_id`` / ``platform``
      / ``is_local_operator`` / ``matched_groups`` / ``message``；
    - ``src/plugin_runtime/host/message_utils.py::_session_message_to_dict`` +
      ``_message_info_to_dict`` 生成 ``message``：群号用户号在 ``message_info`` 下，
      且 ``message`` 自带 ``session_id``。

    这些字段名一旦被上游改动，名单匹配会静默失效，所以固定成测试。
    """

    #: 主机派发命令时真实下发的 kwargs（群聊）
    GROUP_COMMAND_KWARGS = {
        "text": "/课表",
        "stream_id": "session-abc123",
        "group_id": "123456789",
        "platform": "qq",
        "user_id": "987654321",
        "is_local_operator": False,
        "matched_groups": {},
        "message": {
            "message_id": "m1",
            "timestamp": "1750000000.0",
            "platform": "qq",
            "session_id": "session-abc123",
            "processed_plain_text": "/课表",
            "is_command": True,
            "message_info": {
                "user_info": {
                    "user_id": "987654321",
                    "user_nickname": "小明",
                    "user_cardname": "小明（高数三班）",
                },
                "group_info": {"group_id": "123456789", "group_name": "高数三班"},
                "additional_config": {},
            },
            "raw_message": [],
        },
    }

    #: 私聊时 ``group_info`` 为 None
    PRIVATE_COMMAND_KWARGS = {
        "stream_id": "session-private",
        "group_id": "",
        "platform": "qq",
        "user_id": "987654321",
        "message": {
            "session_id": "session-private",
            "message_info": {
                "user_info": {"user_id": "987654321", "user_nickname": "小明"},
                "group_info": None,
                "additional_config": {},
            },
        },
    }

    #: 工具调用只拿到 LLM 给的参数，没有任何会话信息
    TOOL_KWARGS = {"scope": "today"}

    def test_group_command_payload(self):
        identity = identity_from_kwargs(self.GROUP_COMMAND_KWARGS)
        self.assertEqual(identity.group_id, "123456789")
        self.assertEqual(identity.user_id, "987654321")
        self.assertEqual(identity.stream_id, "session-abc123")
        self.assertEqual(identity.chat_type, "group")
        # 群聊的展示名用群名（名片只对私聊有意义）
        self.assertEqual(identity.label, "高数三班")
        self.assertEqual(
            identity.identifiers, ["123456789", "987654321", "session-abc123"]
        )

    def test_private_label_prefers_cardname(self):
        """私聊里没有群名，展示名按 名片 → 昵称 的顺序取。"""
        identity = identity_from_kwargs(
            {
                "stream_id": "s",
                "message": {
                    "message_info": {
                        "user_info": {
                            "user_id": "1",
                            "user_nickname": "昵称",
                            "user_cardname": "名片",
                        },
                        "group_info": None,
                    }
                },
            }
        )
        self.assertEqual(identity.label, "名片")

    def test_private_label_falls_back_to_nickname(self):
        identity = identity_from_kwargs(
            {
                "stream_id": "s",
                "message": {
                    "message_info": {
                        "user_info": {"user_id": "1", "user_nickname": "昵称"},
                        "group_info": None,
                    }
                },
            }
        )
        self.assertEqual(identity.label, "昵称")

    def test_group_command_matched_by_group_number(self):
        identity = identity_from_kwargs(self.GROUP_COMMAND_KWARGS)
        for mode, expected in (("whitelist", True), ("blacklist", False)):
            with self.subTest(mode=mode):
                decision = evaluate_access(
                    identity, mode=mode, entries=["123456789"]
                )
                self.assertEqual(decision.allowed, expected)

    def test_group_command_matched_by_stream_id(self):
        identity = identity_from_kwargs(self.GROUP_COMMAND_KWARGS)
        self.assertTrue(
            evaluate_access(
                identity, mode="whitelist", entries=["session-abc123"]
            ).allowed
        )

    def test_private_command_payload(self):
        identity = identity_from_kwargs(self.PRIVATE_COMMAND_KWARGS)
        self.assertEqual(identity.group_id, "")
        self.assertEqual(identity.user_id, "987654321")
        self.assertEqual(identity.chat_type, "private")
        self.assertEqual(identity.stream_id, "session-private")

    def test_group_ids_resolved_from_message_alone(self):
        """只给 message、不给顶层字段时也要能取到（不依赖顶层兜底）。"""
        identity = identity_from_kwargs({"message": self.GROUP_COMMAND_KWARGS["message"]})
        self.assertEqual(identity.group_id, "123456789")
        self.assertEqual(identity.user_id, "987654321")
        self.assertEqual(identity.stream_id, "session-abc123")

    def test_tool_payload_has_no_identity(self):
        """工具调用拿不到会话信息——名单因此在工具路径上无效。"""
        identity = identity_from_kwargs(self.TOOL_KWARGS)
        self.assertEqual(identity.identifiers, [])
        # 白名单下必然拒绝、黑名单下必然放行；两种都说明"名单管不住工具"
        self.assertTrue(
            evaluate_access(identity, mode="whitelist", entries=["1"]).denied
        )
        self.assertTrue(
            evaluate_access(identity, mode="blacklist", entries=["1"]).allowed
        )


class TestSnowLumaAdapterPayload(unittest.TestCase):
    """按 SnowLuma 适配器**实测**构造的结构。

    来源：`MaiBot/plugins/snowluma-adapter/snowluma_adapter/core.py`
    （`_build_..._message_dict` 与 `@MessageGateway(route_type="duplex",
    platform="qq", protocol="snowluma")`）。要点：

    - ``message_info.user_info`` 有 ``user_id`` / ``user_nickname`` /
      ``user_cardname`` 三个字段；
    - **群聊才有** ``message_info.group_info``，私聊时该键不存在（不是 None）；
    - ``platform`` 就是 ``"qq"``，与 NapCat 相同；
    - 适配器自己填的 ``session_id`` 是空串，真正的会话 ID 由主机在执行命令时
      补到顶层 ``stream_id``——所以不能只依赖 message 里的 session_id。
    """

    GROUP_MESSAGE = {
        "message_id": "12345",
        "timestamp": "1750000000.0",
        "platform": "qq",
        "message_info": {
            "user_info": {
                "user_id": "987654321",
                "user_nickname": "小明",
                "user_cardname": "小明（高数三班）",
            },
            "additional_config": {
                "self_id": "10000",
                "snowluma_message_type": "group",
            },
            "group_info": {"group_id": "123456789", "group_name": "高数三班"},
        },
        "raw_message": [{"type": "text", "data": "/课表"}],
        "is_command": True,
        "is_notify": False,
        "session_id": "",
        "processed_plain_text": "/课表",
    }

    PRIVATE_MESSAGE = {
        "message_id": "12346",
        "platform": "qq",
        "message_info": {
            "user_info": {
                "user_id": "987654321",
                "user_nickname": "小明",
                "user_cardname": "",
            },
            "additional_config": {"snowluma_message_type": "private"},
        },
        "raw_message": [{"type": "text", "data": "/课表订阅"}],
        "is_command": True,
        "session_id": "",
        "processed_plain_text": "/课表订阅",
    }

    #: 主机在命令执行器里补上的顶层字段（见 component_query._build_command_executor）
    HOST_TOP_LEVEL = {
        "stream_id": "session-from-host",
        "group_id": "123456789",
        "user_id": "987654321",
        "platform": "qq",
    }

    def test_group_payload(self):
        identity = identity_from_kwargs(
            {**self.HOST_TOP_LEVEL, "message": self.GROUP_MESSAGE}
        )
        self.assertEqual(identity.group_id, "123456789")
        self.assertEqual(identity.user_id, "987654321")
        self.assertEqual(identity.chat_type, "group")
        self.assertEqual(identity.label, "高数三班")
        # 适配套件自己填的 session_id 是空的，必须用主机给的 stream_id
        self.assertEqual(identity.stream_id, "session-from-host")

    def test_private_payload_has_no_group_info_key(self):
        """私聊时 group_info 键根本不存在，不能因此崩或误判成群聊。"""
        identity = identity_from_kwargs(
            {
                "stream_id": "session-private",
                "user_id": "987654321",
                "message": self.PRIVATE_MESSAGE,
            }
        )
        self.assertEqual(identity.group_id, "")
        self.assertEqual(identity.chat_type, "private")
        self.assertEqual(identity.user_id, "987654321")
        # 名片为空时退回昵称
        self.assertEqual(identity.label, "小明")
        self.assertEqual(identity.stream_id, "session-private")

    def test_group_matchable_by_group_number_or_session_id(self):
        identity = identity_from_kwargs(
            {**self.HOST_TOP_LEVEL, "message": self.GROUP_MESSAGE}
        )
        for entry in ("123456789", "987654321", "session-from-host", "qq:123456789"):
            with self.subTest(entry=entry):
                self.assertTrue(
                    evaluate_access(identity, mode="whitelist", entries=[entry]).allowed
                )

    def test_reminder_recipient_identity_from_subscription(self):
        """投递时的身份来自订阅记录，字段就是/课表订阅时抓到的那些。"""
        store = __import__(
            "class_schedule.store", fromlist=["Subscription"]
        ).Subscription(
            stream_id="session-from-host",
            chat_type="group",
            group_id="123456789",
            user_id="987654321",
            label="高数三班",
        )
        self.assertEqual(
            store.identity.identifiers, ["123456789", "987654321", "session-from-host"]
        )


class TestEvaluateAccess(unittest.TestCase):
    GROUP = ChatIdentity(stream_id="s1", group_id="123456", user_id="654321")

    def test_off_allows_everything(self):
        decision = evaluate_access(self.GROUP, mode="off", entries=[])
        self.assertTrue(decision.allowed)

    def test_off_allows_even_blank_identity(self):
        decision = evaluate_access(ChatIdentity(), mode="off", entries=[])
        self.assertTrue(decision.allowed)

    def test_unknown_mode_treated_as_off(self):
        self.assertTrue(evaluate_access(ChatIdentity(), mode="bogus", entries=[]).allowed)

    def test_whitelist_allows_match_by_group_id(self):
        decision = evaluate_access(self.GROUP, mode="whitelist", entries=["123456"])
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.matched, "123456")

    def test_whitelist_allows_match_by_user_id(self):
        self.assertTrue(
            evaluate_access(self.GROUP, mode="whitelist", entries=["654321"]).allowed
        )

    def test_whitelist_allows_match_by_stream_id(self):
        self.assertTrue(
            evaluate_access(self.GROUP, mode="whitelist", entries=["s1"]).allowed
        )

    def test_whitelist_denies_non_member(self):
        decision = evaluate_access(self.GROUP, mode="whitelist", entries=["999"])
        self.assertTrue(decision.denied)
        self.assertIn("白名单", decision.reason)

    def test_whitelist_with_empty_list_denies_all(self):
        """开了白名单却没填内容，等于谁都不许用——这是刻意的失败关闭。"""
        self.assertTrue(
            evaluate_access(self.GROUP, mode="whitelist", entries=[]).denied
        )

    def test_whitelist_denies_when_identity_unknown(self):
        decision = evaluate_access(ChatIdentity(), mode="whitelist", entries=["123456"])
        self.assertTrue(decision.denied)
        self.assertIn("无法识别", decision.reason)

    def test_blacklist_denies_match(self):
        decision = evaluate_access(self.GROUP, mode="blacklist", entries=["123456"])
        self.assertTrue(decision.denied)
        self.assertIn("黑名单", decision.reason)
        self.assertEqual(decision.matched, "123456")

    def test_blacklist_allows_non_member(self):
        self.assertTrue(
            evaluate_access(self.GROUP, mode="blacklist", entries=["999"]).allowed
        )

    def test_blacklist_with_empty_list_allows_all(self):
        self.assertTrue(
            evaluate_access(self.GROUP, mode="blacklist", entries=[]).allowed
        )

    def test_blacklist_allows_unknown_identity(self):
        """白名单默认拒绝、黑名单默认放行——这是允许/拒绝名单的固有语义。"""
        self.assertTrue(
            evaluate_access(ChatIdentity(), mode="blacklist", entries=["123456"]).allowed
        )

    def test_platform_prefixed_entry_matches_bare_id(self):
        self.assertTrue(
            evaluate_access(self.GROUP, mode="whitelist", entries=["qq:123456"]).allowed
        )

    def test_matching_trims_spaces(self):
        decision = evaluate_access(self.GROUP, mode="whitelist", entries=["  123456 "])
        self.assertTrue(decision.allowed)

    def test_matching_is_case_sensitive_by_design(self):
        """会话 ID 可能区分大小写，放宽会变成越权入口，所以精确比较。"""
        identity = ChatIdentity(stream_id="My-Stream")
        self.assertTrue(
            evaluate_access(identity, mode="whitelist", entries=["My-Stream"]).allowed
        )
        self.assertTrue(
            evaluate_access(identity, mode="whitelist", entries=["my-stream"]).denied
        )

    def test_blank_entries_are_skipped(self):
        decision = evaluate_access(self.GROUP, mode="whitelist", entries=["   ", "123456"])
        self.assertTrue(decision.allowed)

    def test_mode_is_case_insensitive(self):
        self.assertTrue(
            evaluate_access(self.GROUP, mode="WHITELIST", entries=["123456"]).allowed
        )

    def test_decision_exposes_identifiers_for_logging(self):
        decision = evaluate_access(self.GROUP, mode="whitelist", entries=["999"])
        self.assertEqual(decision.identifiers, ["123456", "654321", "s1"])


class TestChatScope(unittest.TestCase):
    """适用范围判定：私聊/群聊 + 认不出类型时的两个方向。"""

    PRIVATE = ChatIdentity(stream_id="p1", user_id="654321", chat_type="private")
    GROUP = ChatIdentity(stream_id="g1", group_id="123456", chat_type="group")
    UNKNOWN = ChatIdentity(stream_id="x1")

    def test_both_allows_everything(self):
        for identity in (self.PRIVATE, self.GROUP, self.UNKNOWN):
            with self.subTest(identity=identity.chat_type):
                self.assertTrue(
                    evaluate_chat_scope(
                        identity, scope="both", unknown_allows=False
                    ).allowed
                )

    def test_private_scope(self):
        self.assertTrue(
            evaluate_chat_scope(
                self.PRIVATE, scope="private", unknown_allows=False
            ).allowed
        )
        denied = evaluate_chat_scope(self.GROUP, scope="private", unknown_allows=False)
        self.assertTrue(denied.denied)
        self.assertIn("私聊", denied.reason)

    def test_group_scope(self):
        self.assertTrue(
            evaluate_chat_scope(self.GROUP, scope="group", unknown_allows=False).allowed
        )
        denied = evaluate_chat_scope(self.PRIVATE, scope="group", unknown_allows=False)
        self.assertTrue(denied.denied)
        self.assertIn("群聊", denied.reason)
        self.assertIn("私聊", denied.reason)  # 说清当前是什么

    def test_unknown_is_denied_for_inbound(self):
        """入站：认不出类型就拒绝（宁可不答，也别把课表带进陌生会话）。"""
        decision = evaluate_chat_scope(
            self.UNKNOWN, scope="private", unknown_allows=False
        )
        self.assertTrue(decision.denied)
        self.assertIn("无法识别", decision.reason)

    def test_unknown_is_allowed_for_outbound(self):
        """出站：认不出类型仍投递（静默漏提醒是本插件最坏的失败方式）。"""
        self.assertTrue(
            evaluate_chat_scope(
                self.UNKNOWN, scope="private", unknown_allows=True
            ).allowed
        )

    def test_unknown_scope_value_is_permissive(self):
        """配置写错/为空时不做限制，避免因为一个错字把功能全锁死。"""
        for scope in ("", "  ", "PRIVATE-ONLY", "both"):
            with self.subTest(scope=scope):
                self.assertTrue(
                    evaluate_chat_scope(
                        self.GROUP, scope=scope, unknown_allows=False
                    ).allowed
                )

    def test_scope_is_case_and_space_insensitive(self):
        self.assertTrue(
            evaluate_chat_scope(
                self.PRIVATE, scope="  Private ", unknown_allows=False
            ).allowed
        )


if __name__ == "__main__":
    unittest.main()
