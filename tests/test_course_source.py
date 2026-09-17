"""课表仓库测试：编码嗅探、文件名收敛、导入落盘、缓存与删除。"""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.course_source import (
    CourseRepository,
    import_filename,
    safe_filename,
)
from class_schedule.ics_parser import IcsParseError, decode_ics_bytes

ICS_TEMPLATE = (
    "BEGIN:VCALENDAR\n"
    "BEGIN:VEVENT\n"
    "UID:{uid}\n"
    "SUMMARY:{summary}\n"
    "DTSTART:20260901T080000\n"
    "DTEND:20260901T094000\n"
    "LOCATION:{location}\n"
    "END:VEVENT\n"
    "END:VCALENDAR\n"
)


def ics_text(
    summary: str = "高等数学", location: str = "教三-201", uid: str = "c1"
) -> str:
    """构造一份只有一个事件的 ics 文本。"""
    return ICS_TEMPLATE.format(uid=uid, summary=summary, location=location)


class TestSafeFilename(unittest.TestCase):
    def test_path_traversal_is_stripped(self):
        """来自 URL 的文件名不能带路径分隔符。"""
        for raw in ("../../etc/passwd", "..\\..\\win.ini", "a/b/c.ics"):
            with self.subTest(raw=raw):
                name = safe_filename(raw)
                self.assertNotIn("/", name)
                self.assertNotIn("\\", name)
                self.assertNotIn("..", name)
                self.assertTrue(name.endswith(".ics"))

    def test_extension_appended_when_missing(self):
        self.assertEqual(safe_filename("我的课表"), "我的课表.ics")

    def test_existing_extension_preserved(self):
        self.assertEqual(safe_filename("cal.ics"), "cal.ics")

    def test_chinese_and_digits_survive(self):
        self.assertEqual(safe_filename("2026秋-课表.ics"), "2026秋-课表.ics")

    def test_blank_falls_back(self):
        self.assertEqual(safe_filename("   "), "schedule.ics")
        self.assertEqual(safe_filename(""), "schedule.ics")

    def test_dangerous_chars_replaced(self):
        name = safe_filename('a<b>c:"d"|e?f*g.ics')
        self.assertTrue(name.endswith(".ics"))
        for char in '<>:"|?*':
            self.assertNotIn(char, name)

    def test_length_capped(self):
        self.assertLessEqual(len(safe_filename("x" * 500)), 84)


class TestEncodingSniffing(unittest.TestCase):
    def test_gbk_file_is_decoded_correctly(self):
        """手动放进目录的 GBK 课表不能读成乱码。"""
        with TemporaryDirectory() as tmp:
            ics_dir = Path(tmp)
            (ics_dir / "gbk.ics").write_bytes(
                ics_text(summary="高等数学", location="教三-201").encode("gb18030")
            )
            repo = CourseRepository(ics_dir)
            repo.refresh(force=True)

            self.assertEqual(repo.errors, [])
            self.assertEqual(len(repo.events), 1)
            self.assertEqual(repo.events[0].summary, "高等数学")
            self.assertEqual(repo.events[0].location, "教三-201")

    def test_utf8_file_still_works(self):
        with TemporaryDirectory() as tmp:
            ics_dir = Path(tmp)
            (ics_dir / "utf8.ics").write_text(ics_text(), encoding="utf-8")
            repo = CourseRepository(ics_dir)
            repo.refresh(force=True)
            self.assertEqual(repo.events[0].summary, "高等数学")

    def test_utf8_with_bom(self):
        """带 BOM 的 utf-8-sig 文件也要能解析。"""
        with TemporaryDirectory() as tmp:
            ics_dir = Path(tmp)
            (ics_dir / "bom.ics").write_text(ics_text(), encoding="utf-8-sig")
            repo = CourseRepository(ics_dir)
            repo.refresh(force=True)
            self.assertEqual(repo.events[0].summary, "高等数学")


class TestRefresh(unittest.TestCase):
    def test_bad_file_recorded_without_breaking_others(self):
        with TemporaryDirectory() as tmp:
            ics_dir = Path(tmp)
            (ics_dir / "good.ics").write_text(ics_text(), encoding="utf-8")
            (ics_dir / "bad.ics").write_text("这不是课表", encoding="utf-8")
            repo = CourseRepository(ics_dir)
            repo.refresh(force=True)

            self.assertEqual(len(repo.events), 1)
            self.assertEqual(len(repo.errors), 1)
            self.assertIn("bad.ics", repo.errors[0])

    def test_non_ics_files_ignored(self):
        with TemporaryDirectory() as tmp:
            ics_dir = Path(tmp)
            (ics_dir / "notes.txt").write_text(ics_text(), encoding="utf-8")
            (ics_dir / "readme.md").write_text("# hi", encoding="utf-8")
            repo = CourseRepository(ics_dir)
            repo.refresh(force=True)
            self.assertEqual(repo.file_count, 0)
            self.assertEqual(repo.events, [])

    def test_missing_directory_is_not_fatal(self):
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "nope")
            repo.refresh(force=True)  # 不应抛异常
            self.assertEqual(repo.events, [])

    def test_cache_hit_skips_reparse_but_force_does_not(self):
        """指纹不变时走缓存；force=True 必须绕过缓存重新解析。

        用「同长度改写 + 还原 mtime」伪造一个指纹不变的改动，
        才能确定性地验证两条分支（否则只能靠时间戳，而 Windows
        时钟粒度约 15ms，连续两次刷新可能拿到同一个值）。
        """
        with TemporaryDirectory() as tmp:
            ics_dir = Path(tmp)
            path = ics_dir / "a.ics"
            path.write_text(ics_text(summary="高等数学"), encoding="utf-8")
            original = path.stat()

            repo = CourseRepository(ics_dir, cache_seconds=300)
            repo.refresh(force=True)
            self.assertEqual(repo.events[0].summary, "高等数学")

            path.write_text(ics_text(summary="大学物理"), encoding="utf-8")
            os.utime(path, (original.st_atime, original.st_mtime))

            repo.refresh()
            self.assertEqual(repo.events[0].summary, "高等数学")  # 缓存命中，未重读

            repo.refresh(force=True)
            self.assertEqual(repo.events[0].summary, "大学物理")  # 强制重解析

    def test_new_file_invalidates_cache(self):
        with TemporaryDirectory() as tmp:
            ics_dir = Path(tmp)
            repo = CourseRepository(ics_dir, cache_seconds=300)
            repo.refresh(force=True)
            self.assertEqual(len(repo.events), 0)

            (ics_dir / "new.ics").write_text(ics_text(), encoding="utf-8")
            repo.refresh()  # 指纹变了，应当重新解析
            self.assertEqual(len(repo.events), 1)


class TestSaveIcs(unittest.TestCase):
    def test_save_and_parse(self):
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            result = repo.save_ics(ics_text(), "downloaded.ics")

            self.assertEqual(result.filename, "downloaded.ics")
            self.assertEqual(result.event_count, 1)
            self.assertFalse(result.replaced)
            self.assertTrue((Path(tmp) / "ics" / "downloaded.ics").exists())
            self.assertEqual(len(repo.events), 1)

    def test_invalid_content_is_rejected_without_writing(self):
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            repo.ensure_dir()
            with self.assertRaises(IcsParseError):
                repo.save_ics("这不是课表", "bad.ics")
            self.assertEqual(list((Path(tmp) / "ics").iterdir()), [])

    def test_warnings_do_not_include_other_files_errors(self):
        """回归：一次导入曾把**整个目录**的错误当成这次导入的警告。

        目录里另有一个坏文件时，用户会以为刚导入的课表有问题。
        """
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            repo.ensure_dir()
            (Path(tmp) / "ics" / "broken.ics").write_text("这不是课表", encoding="utf-8")

            result = repo.save_ics(ics_text(), "good.ics")

            self.assertEqual(result.event_count, 1)
            self.assertEqual(result.warnings, [])
            # 目录级的错误依然记录着，只是不算在这次导入头上
            self.assertTrue(any("broken.ics" in item for item in repo.errors))

    def test_warnings_report_skipped_lines_of_this_file(self):
        """这份文件里有几行没读进去，要能在回执里告诉用户。"""
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            text = (
                "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:c1\nSUMMARY:高等数学\n"
                "DTSTART:20260901T080000\nDTEND:20260901T094000\n"
                "这一行不是合法的 ICS 属性\n"
                "END:VEVENT\nEND:VCALENDAR\n"
            )
            result = repo.save_ics(text, "partial.ics")

            self.assertEqual(result.event_count, 1)
            self.assertTrue(any("1 处内容无法解析" in item for item in result.warnings))

    def test_same_name_replaces_and_reports(self):
        """同名导入是"更新"语义，不保留旧课表。"""
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            first = repo.save_ics(ics_text(summary="旧课表"), "cal.ics")
            second = repo.save_ics(ics_text(summary="新课表"), "cal.ics")

            self.assertFalse(first.replaced)
            self.assertTrue(second.replaced)
            self.assertEqual(repo.file_count, 1)
            self.assertEqual(repo.events[0].summary, "新课表")

    def test_traversal_filename_is_contained(self):
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            result = repo.save_ics(ics_text(), "../../escape.ics")
            written = (Path(tmp) / "ics" / result.filename).resolve()
            self.assertTrue(written.is_relative_to((Path(tmp) / "ics").resolve()))

    def test_gbk_download_is_normalized_to_utf8(self):
        """网址导入的 GBK 内容解码后以 UTF-8 落盘。"""
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            text = decode_ics_bytes(ics_text(summary="大学物理").encode("gb18030"))
            repo.save_ics(text, "gbk.ics")

            raw = (Path(tmp) / "ics" / "gbk.ics").read_bytes()
            self.assertIn("大学物理", raw.decode("utf-8"))

    def test_no_temp_file_left_after_import(self):
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            repo.save_ics(ics_text(), "cal.ics")
            leftovers = [
                item.name
                for item in (Path(tmp) / "ics").iterdir()
                if item.name != "cal.ics"
            ]
            self.assertEqual(leftovers, [])


class TestImportFilename(unittest.TestCase):
    def test_stable_for_same_url(self):
        """同一网址必须生成同一个文件名，否则旧课表会残留。"""
        url = "https://example.com/export/my-calendar.ics"
        self.assertEqual(import_filename(url), import_filename(url))

    def test_different_urls_do_not_collide(self):
        a = import_filename("https://a.example.com/calendar.ics")
        b = import_filename("https://b.example.com/calendar.ics")
        self.assertNotEqual(a, b)

    def test_readable_and_safe(self):
        name = import_filename("https://example.com/2026秋-课表.ics")
        self.assertTrue(name.startswith("imported-"))
        self.assertTrue(name.endswith(".ics"))
        self.assertIn("2026秋-课表", name)
        for char in '<>:"|?*\\/':
            self.assertNotIn(char, name)

    def test_would_not_collide_with_manual_file(self):
        """导入文件名带 imported- 前缀，不会覆盖用户手动放的文件。"""
        name = import_filename("https://example.com/cal.ics")
        self.assertNotEqual(name, safe_filename("cal.ics"))

    def test_query_string_does_not_change_name(self):
        base = "https://example.com/cal.ics"
        self.assertNotEqual(import_filename(base), import_filename(base + "?token=abc"))

    def test_hostless_url_still_produces_name(self):
        name = import_filename("https://example.com/")
        self.assertTrue(name.startswith("imported-"))
        self.assertTrue(name.endswith(".ics"))


class TestDeleteIcs(unittest.TestCase):
    def test_delete_removes_file_and_events(self):
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            repo.save_ics(ics_text(), "a.ics")
            self.assertEqual(len(repo.events), 1)

            self.assertTrue(repo.delete_ics("a.ics"))
            self.assertEqual(repo.events, [])

    def test_delete_missing_returns_false(self):
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            repo.ensure_dir()
            self.assertFalse(repo.delete_ics("nope.ics"))

    def test_delete_rejects_non_ics_and_paths(self):
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            repo.ensure_dir()
            (Path(tmp) / "ics" / "keep.txt").write_text("x", encoding="utf-8")
            self.assertFalse(repo.delete_ics("keep.txt"))
            self.assertFalse(repo.delete_ics("../state.json"))
            self.assertTrue((Path(tmp) / "ics" / "keep.txt").exists())


class TestUrlSourceMap(unittest.TestCase):
    """网址来源映射：自动刷新靠它知道去哪下载、内容有没有变。"""

    def test_save_with_source_url_records_mapping(self):
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            repo.save_ics(ics_text(), "imported-a-12345678.ics", source_url="https://x.test/a.ics")

            sources = repo.url_sources()
            self.assertEqual(list(sources), ["imported-a-12345678.ics"])
            self.assertEqual(sources["imported-a-12345678.ics"]["url"], "https://x.test/a.ics")
            self.assertTrue(sources["imported-a-12345678.ics"]["fingerprint"])

    def test_mapping_persists_to_new_repo_instance(self):
        """映射写在 ics 目录里，重建仓库对象后仍在（自动刷新跨重启可用）。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ics"
            CourseRepository(path).save_ics(
                ics_text(), "imported-a-12345678.ics", source_url="https://x.test/a.ics"
            )

            repo2 = CourseRepository(path)
            self.assertEqual(
                repo2.url_sources()["imported-a-12345678.ics"]["url"],
                "https://x.test/a.ics",
            )

    def test_save_without_source_url_records_nothing(self):
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            repo.save_ics(ics_text(), "manual.ics")
            self.assertEqual(repo.url_sources(), {})

    def test_delete_ics_removes_mapping(self):
        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            repo.save_ics(ics_text(), "imported-a-12345678.ics", source_url="https://x.test/a.ics")

            repo.delete_ics("imported-a-12345678.ics")

            self.assertEqual(repo.url_sources(), {})

    def test_corrupt_sources_file_is_tolerated(self):
        """来源映射坏掉只影响自动刷新，不能让仓库初始化失败。"""
        with TemporaryDirectory() as tmp:
            ics_dir = Path(tmp) / "ics"
            ics_dir.mkdir(parents=True)
            (ics_dir / ".sources.json").write_text("{ 不是 json", encoding="utf-8")

            repo = CourseRepository(ics_dir)
            self.assertEqual(repo.url_sources(), {})
            repo.save_ics(ics_text(), "a.ics")  # 照常可用
            self.assertEqual(len(repo.events), 1)

    def test_fingerprint_matches_content(self):
        from class_schedule.file_intake import content_fingerprint

        with TemporaryDirectory() as tmp:
            repo = CourseRepository(Path(tmp) / "ics")
            text = ics_text()
            repo.save_ics(text, "imported-a-12345678.ics", source_url="https://x.test/a.ics")
            self.assertEqual(
                repo.url_sources()["imported-a-12345678.ics"]["fingerprint"],
                content_fingerprint(text),
            )


if __name__ == "__main__":
    unittest.main()
