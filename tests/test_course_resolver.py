"""CourseResolver 测试：精确/别名/唯一包含/fuzzy/歧义/未找到的解析契约。

这些场景是防误归档规则的回归锚点——课程名解析错一次，笔记就归错一门课，
所以每一档都按评估清单逐条固定，而不是只测"能解析"。
"""

import unittest

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.course_resolver import (
    CONTAINS_MIN_QUERY_LENGTH,
    FUZZY_SUGGEST_THRESHOLD,
    CourseResolver,
    MatchKind,
    normalize_course_name,
    similarity,
)

#: 评估清单里的标准课程集：一门含"自动控制"的课 + 两门无关课
COURSES = ["航空自动控制基础", "高等数学", "大学英语"]
#: 三门都含"自动控制"的课程集：必须歧义，绝不能自动挑
AMBIGUOUS_COURSES = ["航空自动控制基础", "自动控制原理", "自动控制系统设计"]


class TestNormalizeCourseName(unittest.TestCase):
    def test_variants_converge(self):
        """空格/连接符/全角/大小写的各种写法都收敛到同一个比较形态。"""
        variants = [
            "航空自动控制基础",
            " 航空 自动控制基础 ",
            "航空-自动控制基础",
            "航空 自动控制基础",
            "航空—自动控制基础",
        ]
        full = normalize_course_name("航空自动控制基础")
        for variant in variants:
            self.assertEqual(normalize_course_name(variant), full, variant)
        self.assertEqual(normalize_course_name("ＡＢＣ 航空"), "abc航空")

    def test_section_suffix_is_not_normalized_away(self):
        """（一）/（下）这类班次后缀是**不同的课**，归一绝不能把它们抹掉。"""
        self.assertNotEqual(
            normalize_course_name("航空自动控制基础"),
            normalize_course_name("航空自动控制基础（一）"),
        )

    def test_idempotent(self):
        sample = " 航空-自动控制基础（一） "
        once = normalize_course_name(sample)
        self.assertEqual(normalize_course_name(once), once)

    def test_distinguishing_words_are_kept(self):
        """基础/原理/实验/设计 是区分课程的词，绝不能被归一掉。"""
        for suffix in ("基础", "原理", "实验", "设计"):
            self.assertIn(
                suffix, normalize_course_name(f"自动控制{suffix}"), suffix
            )
        self.assertNotEqual(
            normalize_course_name("自动控制原理"), normalize_course_name("自动控制实验")
        )

    def test_empty(self):
        self.assertEqual(normalize_course_name(""), "")
        self.assertEqual(normalize_course_name("   "), "")
        self.assertEqual(normalize_course_name(None), "")


class TestResolveExact(unittest.TestCase):
    def setUp(self):
        self.resolver = CourseResolver()

    def test_full_name_is_exact(self):
        result = self.resolver.resolve("航空自动控制基础", COURSES)
        self.assertEqual(result.kind, MatchKind.EXACT)
        self.assertEqual(result.selected, "航空自动控制基础")
        self.assertTrue(result.resolved)

    def test_exact_ignores_case_width_space_and_punctuation(self):
        """带空格/连接符/括号的同一门课，输入怎么写都该精确命中。"""
        for query in (
            " 航空自动控制基础（一） ",
            "航空自动控制基础（一）",
            " 航空-自动控制基础（一）",
        ):
            result = self.resolver.resolve(query, ["航空自动控制基础（一）", "高等数学"])
            self.assertEqual(result.kind, MatchKind.EXACT, query)
        # 不带班次后缀的输入是（一）课的子串：唯一包含，自动命中
        result = self.resolver.resolve("航空自动控制基础", ["航空自动控制基础（一）", "高等数学"])
        self.assertEqual(result.kind, MatchKind.CONTAINS)
        self.assertEqual(result.selected, "航空自动控制基础（一）")

    def test_duplicate_courses_are_collapsed(self):
        """同一门课出现两份（重复导入等）不该被当成歧义。"""
        result = self.resolver.resolve(
            "航空自动控制基础", ["航空自动控制基础", "航空自动控制基础"]
        )
        self.assertEqual(result.kind, MatchKind.EXACT)


class TestResolveAlias(unittest.TestCase):
    def setUp(self):
        self.resolver = CourseResolver()

    def test_alias_maps_to_standard_name(self):
        result = self.resolver.resolve(
            "航控",
            ["航空自动控制基础"],
            aliases={"航控": "航空自动控制基础"},
        )
        self.assertEqual(result.kind, MatchKind.ALIAS)
        self.assertEqual(result.selected, "航空自动控制基础")

    def test_alias_pointing_outside_courses_is_ignored(self):
        """别名指向的必须是标准课程列表里的名字，否则当没配过。"""
        result = self.resolver.resolve(
            "航控",
            ["高等数学"],
            aliases={"航控": "航空自动控制基础"},
        )
        self.assertNotEqual(result.kind, MatchKind.ALIAS)


class TestResolveContains(unittest.TestCase):
    def setUp(self):
        self.resolver = CourseResolver()

    def test_unique_contains_auto_resolves(self):
        """评估的核心场景：/归到 自动控制 → 航空自动控制基础。"""
        result = self.resolver.resolve("自动控制", COURSES)
        self.assertEqual(result.kind, MatchKind.CONTAINS)
        self.assertEqual(result.selected, "航空自动控制基础")

    def test_multiple_contains_is_ambiguous_with_all_candidates(self):
        result = self.resolver.resolve("自动控制", AMBIGUOUS_COURSES)
        self.assertEqual(result.kind, MatchKind.AMBIGUOUS)
        self.assertIsNone(result.selected)
        # 候选按分数降序只影响展示顺序；关键是三门一个不少、且绝不自动选
        self.assertEqual(
            sorted(item.name for item in result.candidates),
            sorted(AMBIGUOUS_COURSES),
        )
        self.assertTrue(
            all(item.method is MatchKind.CONTAINS for item in result.candidates)
        )

    def test_short_query_never_auto_resolves(self):
        """评估钉死的一条：`控制` 两个字不许自动命中任何课程。

        未来可能同时存在 自动控制原理/飞行控制系统/现代控制理论——
        短输入含糊不得；三门课都含"控制"时更只能歧义。
        """
        result = self.resolver.resolve("控制", COURSES)
        self.assertFalse(result.resolved)

        result = self.resolver.resolve("控制", ["航空自动控制基础", "自动控制原理"])
        self.assertEqual(result.kind, MatchKind.AMBIGUOUS)

    def test_min_length_is_enforced_after_normalization(self):
        """长度按归一化后的字符数算：全角/空格不占便宜。"""
        short = "ＡＢ"  # NFKC 后是 "ab"，2 字符
        self.assertLess(len(normalize_course_name(short)), CONTAINS_MIN_QUERY_LENGTH)
        result = self.resolver.resolve(short, ["高等数学AB课程"])
        self.assertNotEqual(result.kind, MatchKind.CONTAINS)


class TestResolveFuzzy(unittest.TestCase):
    def setUp(self):
        self.resolver = CourseResolver()

    def test_clear_typo_auto_resolves(self):
        """错字（空→口）不是子串，走 fuzzy；唯一接近且大幅领先 → 自动识别。"""
        result = self.resolver.resolve("航口自动控制基础", COURSES)
        self.assertEqual(result.kind, MatchKind.FUZZY)
        self.assertEqual(result.selected, "航空自动控制基础")

    def test_dropped_char_also_resolves(self):
        """漏字（航空自动控基础，0.93）同样自动识别。"""
        result = self.resolver.resolve("航空自动控基础", COURSES)
        self.assertEqual(result.kind, MatchKind.FUZZY)
        self.assertEqual(result.selected, "航空自动控制基础")

    def test_close_scores_never_auto_resolve(self):
        """评估钉死的一条：第一名 0.9x、第二名咬得很近 → 歧义，绝不自动挑。"""
        courses = ["航空自动控制基础", "航空自动控制基础（下）", "高等数学"]
        result = self.resolver.resolve("航空自动控制基础理论", courses)
        self.assertEqual(result.kind, MatchKind.AMBIGUOUS)
        self.assertIsNone(result.selected)
        scores = [item.score for item in result.candidates]
        self.assertGreaterEqual(scores[0], 0.86)
        self.assertLess(scores[0] - scores[1], 0.12)

    def test_unrelated_is_not_found(self):
        result = self.resolver.resolve("西方哲学史", COURSES)
        self.assertEqual(result.kind, MatchKind.NOT_FOUND)
        self.assertIsNone(result.selected)
        self.assertEqual(result.candidates, ())

    def test_low_score_single_hit_still_asks_user(self):
        """分数过不了自动线但过了候选线：给候选确认，不猜也不装没看见。"""
        result = self.resolver.resolve("航空自动控基础理论", COURSES)
        self.assertEqual(result.kind, MatchKind.AMBIGUOUS)
        self.assertEqual(len(result.candidates), 1)

    def test_thresholds_are_configurable(self):
        """阈值可调：把自动线降到 0.8，0.93 的漏字场景阈值应被尊重（行为不变）。"""
        resolver = CourseResolver(fuzzy_auto_threshold=0.8)
        result = resolver.resolve("航空自动控基础", COURSES)
        self.assertEqual(result.kind, MatchKind.FUZZY)


class TestSimilarity(unittest.TestCase):
    def test_identical_scores_one(self):
        self.assertEqual(similarity("abc", "abc"), 1.0)
        self.assertEqual(similarity("航空自动控制基础", "航空自动控制基础"), 1.0)

    def test_unrelated_names_score_low(self):
        """中文课程名之间多少会沾上几个字，重要的是"足够低"，不是 0。"""
        self.assertEqual(similarity("abc", "xyz"), 0.0)
        self.assertLess(similarity("高等数学", "大学英语"), 0.5)
        self.assertLess(similarity("高等数学", "大学英语"), FUZZY_SUGGEST_THRESHOLD)

    def test_order_independent(self):
        self.assertEqual(
            similarity("航空自动控制基", "航空自动控制基础"),
            similarity("航空自动控制基础", "航空自动控制基"),
        )


if __name__ == "__main__":
    unittest.main()
