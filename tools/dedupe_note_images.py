"""清理笔记目录里重复的图片（默认只预览，加 --apply 才动手）。

**为什么需要它**：同一张课件被反复发送时，每次都会落一份原图与一条笔记；
个人库里出现"17 个文件其实只有 2 张不同的图"很常见。这个工具按**图片内容 hash**
去重：每张不同的图保留一条笔记，其余移入回收目录（不直接删除）。

保留规则：优先保留**用户手动归过类**的那条（课程目录不是「未分类」），
同一优先级取最新的一条。

顺带处理索引未引用、但磁盘上存在的图片（识别管道会额外存一份首图），
但**只在该图片内容已有保留副本时**才移动——绝不会把某张图的唯一副本搬走。

用法::

    python tools/dedupe_note_images.py --root <插件数据目录>/notes            # 预览
    python tools/dedupe_note_images.py --root <插件数据目录>/notes --apply    # 执行

移走的文件连同 ``manifest.json``（记录了每个文件的来源与 hash）放在
``notes/_trash_<时间>/``，确认无误后整个目录删掉即可；恢复就是搬回来。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

#: 未归类目录名。它是"没人工干预过"的标志，用于判断保留优先级
UNCLASSIFIED = "未分类"


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def load_indexes(root: Path) -> dict[str, dict]:
    """读所有课程的 index.json；索引坏了就跳过那一课并提示。"""
    indexes: dict[str, dict] = {}
    for index_path in sorted(root.glob("*/index.json")):
        try:
            with open(index_path, encoding="utf-8") as handle:
                indexes[index_path.parent.name] = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"  ! 读不了 {index_path.parent.name}/index.json（{exc}），跳过")
    return indexes


def write_index(course_dir: Path, payload: dict) -> None:
    """原子写索引（临时文件 + os.replace），与插件自身的写法一致。"""
    target = course_dir / "index.json"
    temp = course_dir / "index.json.tmp"
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)


def main() -> int:
    parser = argparse.ArgumentParser(description="按图片内容去重笔记目录")
    parser.add_argument("--root", required=True, help="笔记根目录（含各课程子目录）")
    parser.add_argument("--apply", action="store_true", help="真的动手（默认只预览）")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    root = Path(args.root)
    if not root.is_dir():
        print(f"目录不存在: {root}")
        return 1
    indexes = load_indexes(root)

    # 1. 收集带图的笔记：hash → [(课程, 笔记)]
    loose: list[tuple[str, str, dict]] = []
    for course, payload in indexes.items():
        for note in payload.get("notes", []):
            rel = str(note.get("file") or "")
            if not rel.startswith("img/"):
                continue  # 纯文字笔记不是"图"，不参与
            path = root / course / rel
            if not path.is_file():
                print(f"  ! 索引指向的文件不存在：{course}/{rel}（先手动确认，跳过）")
                continue
            loose.append((digest_of(path), course, note))

    by_hash: dict[str, list[tuple[str, dict]]] = {}
    for digest, course, note in loose:
        by_hash.setdefault(digest, []).append((course, note))

    def keep_rank(item: tuple[str, dict]) -> tuple[int, str]:
        course, note = item
        return (1 if course != UNCLASSIFIED else 0, str(note.get("created_at") or ""))

    keep = {digest: max(items, key=keep_rank) for digest, items in by_hash.items()}
    keep_ids = {str(note.get("id")) for _course, note in keep.values()}
    print(f"笔记 {len(loose)} 条带图，对应 {len(keep)} 张不同的图。保留：")
    for digest, (course, note) in sorted(keep.items()):
        print(f"  {digest}  {course}/{note.get('id')}")

    # 2. 要移走的：重复笔记的图 + 索引未引用的重复副本
    referenced = {
        f"{course}/{note.get('file')}" for _d, course, note in loose
    }
    #: 保留笔记引用的文件**绝对不动**：两条笔记共享同一文件时（手工编辑过索引
    #: 才会出现），移走非保留那份会把保留笔记的唯一副本也带走
    kept_paths = {root / course / str(note["file"]) for course, note in keep.values()}
    plan: list[tuple[str, Path, str]] = []
    for digest, course, note in loose:
        if str(note.get("id")) in keep_ids:
            continue
        path = root / course / str(note["file"])
        if path in kept_paths:
            print(f"  ! {course}/{note['file']} 同时被保留笔记引用，跳过（共享文件）")
            continue
        plan.append(("重复笔记的图", path, digest))
    for path in sorted(root.glob("*/img/*")):
        rel = f"{path.parent.parent.name}/{path.parent.name}/{path.name}"
        if rel in referenced:
            continue
        digest = digest_of(path)
        if digest in by_hash:
            plan.append(("索引未引用的重复副本", path, digest))
        else:
            print(f"  ! 索引未引用的孤儿文件且没有保留副本，**不动**：{rel}（{digest}）")

    total_kb = sum(path.stat().st_size for _r, path, _d in plan) / 1024
    print(f"\n计划移走 {len(plan)} 个文件（{total_kb:.0f} KB），"
          f"清理 {len(loose) - len(keep_ids)} 条重复笔记")
    for reason, path, digest in plan:
        print(f"  {reason}：{path.relative_to(root)}  ({digest})")
    if not args.apply:
        print("\n预览结束（加 --apply 才动手）")
        return 0

    trash = root / f"_trash_{time.strftime('%Y%m%d_%H%M%S')}"
    moved = []

    for reason, path, digest in plan:
        target = trash / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        moved.append(
            {
                "reason": reason,
                "from": str(path.relative_to(root)),
                "hash": digest,
                "bytes": target.stat().st_size,
            }
        )
    for course, payload in indexes.items():
        notes = payload.get("notes", [])
        kept = [
            note
            for note in notes
            if not str(note.get("file") or "").startswith("img/")
            or str(note.get("id")) in keep_ids
        ]
        if len(kept) != len(notes):
            payload["notes"] = kept
            write_index(root / course, payload)
            print(f"  索引已更新：{course} {len(notes)} → {len(kept)} 条")
    (trash / "manifest.json").write_text(
        json.dumps(
            {
                "moved": moved,
                "kept": {h: [c, str(n.get("id"))] for h, (c, n) in keep.items()},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n完成。移走的文件在 {trash}（含 manifest.json），确认无误后删掉该目录即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
