"""状态持久化测试：v2 结构往返、v1 迁移、损坏容错、过期清理。"""

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.access import ChatIdentity
from class_schedule.store import (
    FIRED_MAX_ENTRIES,
    PluginState,
    Subscription,
)

NOW = datetime(2026, 9, 1, 8, 0)


def state_path(tmp: str) -> Path:
    return Path(tmp) / "state.json"


class TestSubscription(unittest.TestCase):
    def test_round_trip(self):
        record = Subscription(
            stream_id="s1",
            chat_type="group",
            group_id="123456",
            user_id="654321",
            label="高数三班",
            lead_minutes=30,
            added_at="2026-09-01T08:00:00",
        )
        revived = Subscription.from_dict(record.to_dict())
        self.assertEqual(revived, record)

    def test_from_dict_requires_stream_id(self):
        self.assertIsNone(Subscription.from_dict({}))
        self.assertIsNone(Subscription.from_dict({"stream_id": "  "}))
        self.assertIsNone(Subscription.from_dict("不是字典"))

    def test_unknown_chat_type_discarded(self):
        record = Subscription.from_dict({"stream_id": "s", "chat_type": "weird"})
        self.assertEqual(record.chat_type, "")

    def test_effective_lead_uses_override_then_default(self):
        self.assertEqual(Subscription("s", lead_minutes=45).effective_lead(20), 45)
        self.assertEqual(Subscription("s").effective_lead(20), 20)
        self.assertEqual(Subscription("s", lead_minutes=0).effective_lead(20), 0)

    def test_lead_display_marks_source(self):
        self.assertIn("单独设置", Subscription("s", lead_minutes=45).lead_display(20))
        self.assertIn("跟随配置", Subscription("s").lead_display(20))

    def test_type_label(self):
        self.assertEqual(Subscription("s", chat_type="group").type_label, "群聊")
        self.assertEqual(Subscription("s", chat_type="private").type_label, "私聊")
        self.assertEqual(Subscription("s").type_label, "会话")

    def test_identity_carries_ids_for_access_check(self):
        record = Subscription("s", group_id="g", user_id="u", chat_type="group")
        self.assertEqual(record.identity.identifiers, ["g", "u", "s"])


class TestRoundTrip(unittest.TestCase):
    def test_save_and_load(self):
        with TemporaryDirectory() as tmp:
            path = state_path(tmp)
            state = PluginState(
                subscriptions=[
                    Subscription("s1", chat_type="group", group_id="1", lead_minutes=30),
                    Subscription("p1", chat_type="private", user_id="2"),
                ],
                fired={"k1": NOW.isoformat()},
            )
            state.save(path)
            restored = PluginState.load(path)

            self.assertEqual([s.stream_id for s in restored.subscriptions], ["s1", "p1"])
            self.assertEqual(restored.subscriptions[0].lead_minutes, 30)
            self.assertEqual(restored.subscriptions[0].group_id, "1")
            self.assertIsNone(restored.subscriptions[1].lead_minutes)
            self.assertEqual(restored.fired, {"k1": NOW.isoformat()})

    def test_saved_file_is_v2_and_readable(self):
        with TemporaryDirectory() as tmp:
            path = state_path(tmp)
            PluginState(subscriptions=[Subscription("群-1")]).save(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["version"], 2)
            self.assertEqual(raw["subscriptions"][0]["stream_id"], "群-1")

    def test_save_creates_parent_directory(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "deep" / "state.json"
            PluginState().save(path)
            self.assertTrue(path.exists())

    def test_no_temp_file_left_behind(self):
        with TemporaryDirectory() as tmp:
            path = state_path(tmp)
            PluginState(subscriptions=[Subscription("a")]).save(path)
            leftovers = [item.name for item in Path(tmp).iterdir() if item.name != "state.json"]
            self.assertEqual(leftovers, [])


class TestLoadTolerance(unittest.TestCase):
    def test_missing_file_returns_empty_state(self):
        with TemporaryDirectory() as tmp:
            state = PluginState.load(Path(tmp) / "nope.json")
            self.assertEqual(state.subscriptions, [])
            self.assertEqual(state.fired, {})

    def test_corrupted_json_returns_empty_state(self):
        with TemporaryDirectory() as tmp:
            path = state_path(tmp)
            path.write_text("{ 这不是 json", encoding="utf-8")
            self.assertEqual(PluginState.load(path).subscriptions, [])

    def test_non_list_subscriptions_is_reported(self):
        """回归：subscriptions 字段存在却不是列表时曾经静默清空。

        与本文件既有的纪律一致——"文件存在却读不出来必须留痕"，
        否则用户只看到"没有提醒对象"，排查不到根因。
        """
        with TemporaryDirectory() as tmp:
            path = state_path(tmp)
            path.write_text(
                json.dumps({"version": 2, "subscriptions": None, "fired": {}}),
                encoding="utf-8",
            )
            with self.assertLogs("class_schedule.store", level="WARNING") as captured:
                state = PluginState.load(path)
            self.assertEqual(state.subscriptions, [])
            self.assertTrue(any("subscriptions" in line for line in captured.output))

    def test_missing_subscriptions_does_not_warn(self):
        """字段本身缺失（v1 或首次启动）是正常情况，不该告警。"""
        with TemporaryDirectory() as tmp:
            path = state_path(tmp)
            path.write_text(
                json.dumps({"version": 2, "targets": ["s1"]}), encoding="utf-8"
            )
            state = PluginState.load(path)
            self.assertEqual([s.stream_id for s in state.subscriptions], ["s1"])

    def test_wrong_types_are_discarded(self):
        with TemporaryDirectory() as tmp:
            path = state_path(tmp)
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "subscriptions": [
                            {"stream_id": "ok"},
                            {"stream_id": ""},
                            "垃圾",
                            None,
                            {"stream_id": "bad-lead", "lead_minutes": -5},
                        ],
                        "fired": {"k": "2026-09-01T08:00:00", "bad": 42},
                    }
                ),
                encoding="utf-8",
            )
            state = PluginState.load(path)
            self.assertEqual([s.stream_id for s in state.subscriptions], ["ok", "bad-lead"])
            self.assertIsNone(state.subscriptions[1].lead_minutes)
            self.assertEqual(state.fired, {"k": "2026-09-01T08:00:00"})

    def test_bool_lead_rejected(self):
        """True 是 int 的子类，但不能当成分钟数。"""
        with TemporaryDirectory() as tmp:
            path = state_path(tmp)
            path.write_text(
                json.dumps({"subscriptions": [{"stream_id": "s", "lead_minutes": True}]}),
                encoding="utf-8",
            )
            self.assertIsNone(PluginState.load(path).subscriptions[0].lead_minutes)

    def test_lead_above_upper_bound_rejected(self):
        with TemporaryDirectory() as tmp:
            path = state_path(tmp)
            path.write_text(
                json.dumps({"subscriptions": [{"stream_id": "s", "lead_minutes": 99999}]}),
                encoding="utf-8",
            )
            self.assertIsNone(PluginState.load(path).subscriptions[0].lead_minutes)

    def test_duplicate_stream_ids_collapsed(self):
        with TemporaryDirectory() as tmp:
            path = state_path(tmp)
            path.write_text(
                json.dumps(
                    {
                        "subscriptions": [
                            {"stream_id": "s", "lead_minutes": 10},
                            {"stream_id": "s", "lead_minutes": 20},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state = PluginState.load(path)
            self.assertEqual(len(state.subscriptions), 1)
            self.assertEqual(state.subscriptions[0].lead_minutes, 10)


class TestV1Migration(unittest.TestCase):
    """v1 是「裸 stream_id 列表 + 一个全局提前量」，要能平滑升上来。"""

    def _write_v1(self, tmp: str, payload: dict) -> Path:
        path = state_path(tmp)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_targets_become_subscriptions(self):
        with TemporaryDirectory() as tmp:
            path = self._write_v1(
                tmp, {"version": 1, "targets": ["s1", "s2"], "fired": {}}
            )
            state = PluginState.load(path)
            self.assertEqual([s.stream_id for s in state.subscriptions], ["s1", "s2"])
            self.assertTrue(all(s.lead_minutes is None for s in state.subscriptions))

    def test_global_lead_spread_to_existing_subscriptions(self):
        """旧的全局提前量体现用户意图，迁移时平摊下去而不是丢掉。"""
        with TemporaryDirectory() as tmp:
            path = self._write_v1(
                tmp,
                {
                    "version": 1,
                    "targets": ["s1", "s2"],
                    "remind_before_minutes": 35,
                    "fired": {"k": "2026-09-01T08:00:00"},
                },
            )
            state = PluginState.load(path)
            self.assertEqual([s.lead_minutes for s in state.subscriptions], [35, 35])
            self.assertEqual(state.fired, {"k": "2026-09-01T08:00:00"})

    def test_invalid_legacy_lead_dropped(self):
        with TemporaryDirectory() as tmp:
            path = self._write_v1(
                tmp, {"version": 1, "targets": ["s1"], "remind_before_minutes": -3}
            )
            self.assertIsNone(PluginState.load(path).subscriptions[0].lead_minutes)

    def test_legacy_junk_entries_skipped(self):
        with TemporaryDirectory() as tmp:
            path = self._write_v1(
                tmp, {"version": 1, "targets": ["ok", "", None, True, "ok"]}
            )
            state = PluginState.load(path)
            self.assertEqual([s.stream_id for s in state.subscriptions], ["ok"])

    def test_migrated_state_saves_as_v2(self):
        with TemporaryDirectory() as tmp:
            path = self._write_v1(tmp, {"version": 1, "targets": ["s1"]})
            state = PluginState.load(path)
            state.save(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["version"], 2)
            self.assertIn("subscriptions", raw)
            self.assertNotIn("targets", raw)

    def test_no_targets_key_yields_empty(self):
        with TemporaryDirectory() as tmp:
            path = self._write_v1(tmp, {"version": 1, "fired": {}})
            self.assertEqual(PluginState.load(path).subscriptions, [])


class TestSubscriptions(unittest.TestCase):
    def test_add_returns_created_flag(self):
        state = PluginState()
        _, created = state.add_subscription(ChatIdentity(stream_id="s1"))
        self.assertTrue(created)
        _, created_again = state.add_subscription(ChatIdentity(stream_id="s1"))
        self.assertFalse(created_again)
        self.assertEqual(len(state.subscriptions), 1)

    def test_add_requires_stream_id(self):
        state = PluginState()
        with self.assertRaises(ValueError):
            state.add_subscription(ChatIdentity())

    def test_add_records_identity_and_timestamp(self):
        state = PluginState()
        record, _ = state.add_subscription(
            ChatIdentity(
                stream_id="s1", group_id="g", user_id="u", chat_type="group", label="班"
            ),
            now=NOW,
        )
        self.assertEqual(record.group_id, "g")
        self.assertEqual(record.user_id, "u")
        self.assertEqual(record.chat_type, "group")
        self.assertEqual(record.added_at, NOW.isoformat(timespec="seconds"))

    def test_readd_refreshes_identity_keeps_lead(self):
        state = PluginState()
        state.add_subscription(ChatIdentity(stream_id="s1", label="旧名"))
        state.set_lead("s1", 44)
        state.add_subscription(ChatIdentity(stream_id="s1", label="新名"))
        record = state.find("s1")
        self.assertEqual(record.label, "新名")
        self.assertEqual(record.lead_minutes, 44)

    def test_remove(self):
        state = PluginState()
        state.add_subscription(ChatIdentity(stream_id="s1"))
        self.assertTrue(state.remove_subscription("s1"))
        self.assertFalse(state.remove_subscription("s1"))
        self.assertEqual(state.subscriptions, [])

    def test_find_and_set_lead_on_missing(self):
        state = PluginState()
        self.assertIsNone(state.find("nope"))
        self.assertFalse(state.set_lead("nope", 10))

    def test_set_lead_rejects_out_of_range(self):
        state = PluginState()
        state.add_subscription(ChatIdentity(stream_id="s1"))
        state.set_lead("s1", 99999)
        self.assertIsNone(state.find("s1").lead_minutes)
        state.set_lead("s1", 5)
        state.set_lead("s1", None)
        self.assertIsNone(state.find("s1").lead_minutes)


class TestPruneFired(unittest.TestCase):
    def test_old_entries_removed(self):
        state = PluginState(
            fired={
                "recent": NOW.isoformat(),
                "old": (NOW - timedelta(days=10)).isoformat(),
            }
        )
        self.assertEqual(state.prune_fired(NOW), 1)
        self.assertEqual(list(state.fired), ["recent"])

    def test_malformed_timestamp_removed(self):
        state = PluginState(fired={"junk": "not-a-date"})
        self.assertEqual(state.prune_fired(NOW), 1)
        self.assertEqual(state.fired, {})

    def test_oversized_map_trimmed(self):
        state = PluginState()
        for index in range(FIRED_MAX_ENTRIES + 50):
            state.fired[f"k{index}"] = (NOW - timedelta(seconds=index)).isoformat()
        state.prune_fired(NOW)
        self.assertEqual(len(state.fired), FIRED_MAX_ENTRIES)
        # 保留的应该是最新的那批
        self.assertIn("k0", state.fired)


if __name__ == "__main__":
    unittest.main()
