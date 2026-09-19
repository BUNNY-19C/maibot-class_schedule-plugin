"""云端客户端与笔记数据库测试（阶段 1 基础设施）。"""

import json
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.llm_client import (
    CloudError,
    NotConfiguredError,
    SiliconFlowClient,
)
from class_schedule.notes_db import NotesDatabase, cosine


class FakeTransport:
    """记录请求并返回预置响应；可配置前 N 次失败以测退避重试。"""

    def __init__(self, responses, fail_first=0):
        self.calls = []
        self._responses = list(responses)
        self._fail_first = fail_first

    def __call__(self, url, body, headers):
        self.calls.append((url, json.loads(body), dict(headers)))
        if self._fail_first > 0:
            self._fail_first -= 1
            raise OSError("模拟网络抖动")
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return json.dumps(self._responses[index]).encode("utf-8")


class TestClientGating(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_degrades_not_crashes(self):
        client = SiliconFlowClient("")
        self.assertFalse(client.configured)
        with self.assertRaises(NotConfiguredError):
            await client.chat(model="m", messages=[{"role": "user", "content": "x"}])

    async def test_private_api_base_rejected(self):
        """SSRF 纪律对 API 地址同样生效：内网 base 直接拒绝。"""
        client = SiliconFlowClient("k", base_url="https://127.0.0.1:9/v1")
        with self.assertRaises(CloudError):
            await client.chat(model="m", messages=[{"role": "user", "content": "x"}])

    async def test_key_never_in_errors(self):
        client = SiliconFlowClient(
            "sk-secret-abcdef",
            transport=FakeTransport([{"error": "boom"}]),
        )
        client._max_retries = 0  # 直达失败路径

        class _Boom:
            def __call__(self, url, body, headers):
                raise OSError("connection reset")

        client._transport = _Boom()
        with self.assertRaises(CloudError) as ctx:
            await client.chat(model="m", messages=[{"role": "user", "content": "x"}])
        self.assertNotIn("sk-secret-abcdef", str(ctx.exception))


class TestClientCalls(unittest.IsolatedAsyncioTestCase):
    async def test_chat_success_and_usage(self):
        transport = FakeTransport(
            [{"choices": [{"message": {"content": "hello"}}],
              "usage": {"prompt_tokens": 3, "completion_tokens": 5}}]
        )
        client = SiliconFlowClient("k", transport=transport)
        result = await client.chat(model="m", messages=[{"role": "user", "content": "x"}])
        self.assertEqual(result["text"], "hello")
        self.assertEqual(result["prompt_tokens"], 3)
        self.assertEqual(transport.calls[0][2]["Authorization"], "Bearer k")

    async def test_vision_embeds_image_as_data_url(self):
        transport = FakeTransport(
            [{"choices": [{"message": {"content": "ok"}}], "usage": {}}]
        )
        client = SiliconFlowClient("k", transport=transport)
        await client.vision(model="vlm", image_base64="QUJD", prompt="识别公式")
        content = transport.calls[0][1]["messages"][0]["content"]
        kinds = [part.get("type") for part in content]
        self.assertEqual(kinds, ["image_url", "text"])
        self.assertTrue(
            content[0]["image_url"]["url"].startswith("data:image/png;base64,")
        )

    async def test_retry_then_give_up(self):
        transport = FakeTransport(
            [{"choices": [{"message": {"content": "ok"}}], "usage": {}}],
            fail_first=2,  # 前两次网络失败，第三次成功
        )
        client = SiliconFlowClient(
            "k", transport=transport, max_retries=2
        )
        # 退避会 sleep，测试里直接跑完：1s + 2s 可接受
        result = await client.chat(model="m", messages=[{"role": "user", "content": "x"}])
        self.assertEqual(result["text"], "ok")
        self.assertEqual(len(transport.calls), 3)

    async def test_permanent_4xx_no_retry(self):
        class _Http400:
            def __init__(self):
                self.count = 0

            def __call__(self, url, body, headers):
                self.count += 1
                raise CloudError("HTTP 400 bad model")

        fake = _Http400()
        client = SiliconFlowClient("k", transport=fake, max_retries=2)
        with self.assertRaises(CloudError):
            await client.chat(model="m", messages=[{"role": "user", "content": "x"}])
        self.assertEqual(fake.count, 1)  # 参数错不重试

    async def test_embed_and_rerank_shapes(self):
        transport = FakeTransport(
            [
                {"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
                {"results": [{"index": 1, "relevance_score": 0.9}]},
            ]
        )
        client = SiliconFlowClient("k", transport=transport)
        vectors = await client.embed(model="e", texts=["a"])
        self.assertEqual(vectors, [[0.1, 0.2]])
        results = await client.rerank(model="r", query="q", documents=["d1", "d2"])
        self.assertEqual(results[0]["index"], 1)


class TestNotesDatabase(unittest.TestCase):
    def _tmpdir(self) -> Path:
        """临时目录经 addCleanup 删除；cleanup 是 LIFO——先关库再删目录。"""
        import shutil, tempfile

        path = Path(tempfile.mkdtemp(prefix="notesdb-"))
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def _db(self, root: Path) -> NotesDatabase:
        db = NotesDatabase(root / "notes.db")
        db.initialize()
        self.addCleanup(db.close)  # 后注册先执行：确保在 rmtree 之前关闭连接
        return db

    def test_initialize_idempotent(self):
        tmp = self._tmpdir()
        root = Path(tmp)
        self._db(root)
        self._db(root)  # 二次建表不报错

    def test_add_note_and_search(self):
        tmp = self._tmpdir()
        db = self._db(Path(tmp))
        note_id = db.add_note(
            source_type="文字", raw_content="欧拉公式 e^iπ+1=0 第三章要考",
            course="高等数学", message_id="m-1",
        )
        self.assertGreater(note_id, 0)
        hits = db.search_text("欧拉")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["course"], "高等数学")
        # 空查询不报错、全命中受 course 过滤约束
        self.assertEqual(db.search_text(""), [])
        self.assertEqual(db.search_text("欧拉", course="机械设计"), [])

    def test_formula_dedup_by_fingerprint(self):
        tmp = self._tmpdir()
        db = self._db(Path(tmp))
        first, first_created = db.upsert_formula({"fingerprint": "fp-1", "name": "欧拉公式"})
        second, second_created = db.upsert_formula({"fingerprint": "fp-1", "name": "改名也一样"})
        self.assertEqual(first, second)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(db.search_formulas("欧拉")[0]["name"], "欧拉公式")
        with self.assertRaises(ValueError):
            db.upsert_formula({"fingerprint": "  "})

    def test_upsert_updates_image_hash_for_new_bytes(self):
        """同一公式（同指纹）换一张图重拍：最新图片的 hash 要进缓存，否则每拍一次付一次钱。"""
        tmp = self._tmpdir()
        db = self._db(Path(tmp))
        formula_id, created = db.upsert_formula(
            {"fingerprint": "fp-1", "name": "欧拉公式", "image_hash": "hash-A"}
        )
        self.assertTrue(created)
        same_id, created = db.upsert_formula(
            {"fingerprint": "fp-1", "name": "欧拉公式", "image_hash": "hash-B"}
        )
        self.assertFalse(created)
        self.assertEqual(same_id, formula_id)
        self.assertEqual(db.formula_by_image_hash("hash-B")["id"], formula_id)
        # 旧字节不再是缓存键（为简化不再留多份，详见 upsert_formula 的说明）
        self.assertIsNone(db.formula_by_image_hash("hash-A"))

    def test_attach_tag_reuses_tag_rows(self):
        tmp = self._tmpdir()
        db = self._db(Path(tmp))
        note = db.add_note(source_type="文字", raw_content="x")
        db.attach_tag("note", note, "#公式")
        db.attach_tag("note", note, "#公式", source="LLM")
        rows = db._connection().execute(
            "SELECT COUNT(*) c FROM tags WHERE name='#公式'"
        ).fetchone()
        self.assertEqual(rows["c"], 1)
        with self.assertRaises(ValueError):
            db.attach_tag("unknown-table", 1, "#x")

    def test_embedding_roundtrip_and_cosine(self):
        tmp = self._tmpdir()
        db = self._db(Path(tmp))
        note = db.add_note(source_type="文字", raw_content="x")
        db.store_embedding(note, "m1", [1.0, 0.0, 0.5])
        stored = db.all_embeddings()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0][0], note)
        self.assertAlmostEqual(stored[0][1][2], 0.5, places=5)
        self.assertAlmostEqual(cosine([1, 0], [1, 0]), 1.0)
        self.assertEqual(cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(cosine([], [1]), 0.0)

    def test_fts_flag_reflects_capability(self):
        """探测结果要么 True 要么 False——不允许停在未初始化状态被使用。"""
        tmp = self._tmpdir()
        db = self._db(Path(tmp))
        self.assertIsInstance(db.fts_enabled, bool)


if __name__ == "__main__":
    unittest.main()
