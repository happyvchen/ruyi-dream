"""一次性清理脚本：删除对象存储里遗留的分镜预览派生图。

背景：分镜预览生成步骤已从后端彻底移除（见迁移 038）。历史上分镜预览成图曾
写入 ``generation-tasks/<task_id>/preview/preview-<id>.png`` 这类 key，迁移只删除了
数据库里的 ``generation_previews`` 记录，对象存储里的派生图仍会残留。本脚本负责
把这些 ``generation-tasks/*/preview/`` 前缀下的对象删除。

安全边界：
- 只匹配 ``generation-tasks/<segment>/preview/`` 前缀下的对象，绝不触碰成片、主推款
  上传、缩略图或其他前缀。
- 幂等：重复运行只会删除仍存在的 preview 对象。
- 默认 dry-run，只打印将删除的 key；必须显式传 ``--apply`` 才真正删除。

用法：
    python scripts/cleanup_storyboard_preview_objects.py            # 预演，只打印
    python scripts/cleanup_storyboard_preview_objects.py --apply    # 真正删除
    python scripts/cleanup_storyboard_preview_objects.py --limit 50 # 限制处理数量
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.object_storage import clean_key, s3_client


GENERATION_TASKS_PREFIX = "generation-tasks/"


def is_storyboard_preview_key(object_key: str) -> bool:
    """仅当 key 形如 ``generation-tasks/<segment>/preview/...`` 时返回 True。"""
    key = clean_key(object_key)
    parts = key.split("/")
    return (
        len(parts) >= 4
        and parts[0] == "generation-tasks"
        and parts[2] == "preview"
        and bool(parts[3])
    )


def iter_storyboard_preview_keys():
    """列出所有分镜预览前缀下的对象 key。"""
    settings = get_settings()
    client = s3_client()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=GENERATION_TASKS_PREFIX):
        for item in page.get("Contents", []) or []:
            key = item.get("Key")
            if key and is_storyboard_preview_key(key):
                yield key


def cleanup(apply: bool, limit: int | None) -> tuple[int, int]:
    """返回 (matched, deleted)。dry-run 时 deleted 恒为 0。"""
    settings = get_settings()
    client = s3_client()
    matched = 0
    deleted = 0
    batch: list[dict[str, str]] = []

    def flush_batch() -> None:
        nonlocal deleted, batch
        if not batch:
            return
        client.delete_objects(Bucket=settings.s3_bucket, Delete={"Objects": batch})
        deleted += len(batch)
        batch = []

    for key in iter_storyboard_preview_keys():
        matched += 1
        print(f"{'DELETE' if apply else 'DRY-RUN'} {key}")
        if apply:
            batch.append({"Key": key})
            if len(batch) >= 1000:
                flush_batch()
        if limit is not None and matched >= limit:
            break
    if apply:
        flush_batch()
    return matched, deleted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="删除对象存储里遗留的分镜预览派生图（generation-tasks/*/preview/）。"
    )
    parser.add_argument("--apply", action="store_true", help="真正删除；不传则只做 dry-run。")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少个对象。")
    args = parser.parse_args()

    matched, deleted = cleanup(apply=args.apply, limit=args.limit)
    if args.apply:
        print(f"done: matched={matched} deleted={deleted}")
    else:
        print(f"dry-run: matched={matched} (加 --apply 才真正删除)")


if __name__ == "__main__":
    main()
