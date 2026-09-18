"""按当前归一化规则重算公式库的 ``latex_normalized`` 与 ``fingerprint``。

**为什么需要这个**：v1.6.0 的归一化有个会改错公式的缺陷——它给顶层斜杠两侧补
括号，于是把 ``[\\sigma]=\\frac{\\sigma_{\\lim}}{S_{\\sigma}}=\\frac{\\sigma_{S}}{S_{\\sigma}}``
这种等式按斜杠重新结合成 ``([\\sigma]=(\\sigma_{\\lim}))/((S_{\\sigma})=…)``，
存下来的规范化 LaTeX 意思都不对。规则修好之后，已有记录需要重算一遍。

边界：只改整理层（``latex_normalized`` / ``fingerprint``），``latex_raw`` 与
笔记原文一概不动——这正是"原始层只增不改、整理层可重新生成"的用法。

用法::

    python tools/renormalize_formulas.py --db <notes.db 路径>            # 只看差异
    python tools/renormalize_formulas.py --db <notes.db 路径> --apply    # 写回

退出码 0 表示成功（有无差异都是 0）；数据库打不开或参数不合法返回 1。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
if "class_schedule" not in sys.modules:
    _package = types.ModuleType("class_schedule")
    _package.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules["class_schedule"] = _package

from class_schedule.formula import fingerprint, normalize_latex  # noqa: E402


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="重算公式库的规范化 LaTeX 与指纹")
    parser.add_argument("--db", required=True, help="notes.db 路径")
    parser.add_argument("--apply", action="store_true", help="真的写回（默认只看差异）")
    args = parser.parse_args()

    path = Path(args.db)
    if not path.is_file():
        print(f"找不到数据库: {path}")
        return 1

    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    rows = list(connection.execute(
        "SELECT id, name, latex_raw, latex_normalized, fingerprint FROM formulas ORDER BY id"
    ))
    print(f"公式共 {len(rows)} 条；{'写入模式' if args.apply else '预览模式（加 --apply 才写）'}")

    taken = {str(row["fingerprint"]): int(row["id"]) for row in rows}
    changed = same = conflicted = 0
    for row in rows:
        formula_id = int(row["id"])
        raw = str(row["latex_raw"] or "")
        new_normalized = normalize_latex(raw)
        new_fingerprint = fingerprint(new_normalized)
        if not new_fingerprint:
            print(f"  #{formula_id} 跳过：latex_raw 归一化后为空（{raw[:40]!r}）")
            same += 1
            continue
        if new_fingerprint == str(row["fingerprint"]):
            same += 1
            continue
        owner = taken.get(new_fingerprint)
        if owner is not None and owner != formula_id:
            # 两行归一化后撞成同一个公式：只提示，不擅自合并（谁留谁删是人的决定）
            print(
                f"  #{formula_id} 与 #{owner} 归一化后指纹相同，跳过："
                f"建议人工确认后合并（{new_normalized[:60]}）"
            )
            conflicted += 1
            continue
        changed += 1
        print(
            f"  #{formula_id} {row['name']}\n"
            f"      旧 {row['latex_normalized']!r}\n"
            f"      新 {new_normalized!r}"
        )
        if args.apply:
            connection.execute(
                "UPDATE formulas SET latex_normalized = ?, fingerprint = ? WHERE id = ?",
                (new_normalized, new_fingerprint, formula_id),
            )
            taken.pop(str(row["fingerprint"]), None)
            taken[new_fingerprint] = formula_id
    if args.apply and changed:
        connection.commit()
    connection.close()
    print(
        f"差异 {changed} 条，未变 {same} 条，指纹撞车待人工处理 {conflicted} 条"
        + ("（已写回）" if args.apply else "（未写回）")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
