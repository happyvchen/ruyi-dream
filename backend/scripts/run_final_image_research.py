from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.image_provider import get_image_provider
from app.main import build_new_final_task_package
from app.object_storage import get_bytes, put_bytes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one isolated final-image prompt research call.")
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--result-image-id", type=int, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--preview-file", type=Path)
    parser.add_argument("--environment-key")
    parser.add_argument("--name", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    package = build_new_final_task_package(args.task_id, args.result_image_id)
    package["prompt"] = args.prompt_file.read_text(encoding="utf-8").strip()

    if args.preview_file:
        preview_key = f"experiments/research/task-{args.task_id}/{args.name}-preview.png"
        put_bytes(preview_key, args.preview_file.read_bytes(), "image/png")
        for item in package["imageInputs"]:
            if item.get("role") == "preview_grid":
                item.update({"objectKey": preview_key, "filename": args.preview_file.name, "providerUrl": None})

    if args.environment_key:
        package["imageInputs"].append(
            {
                "role": "shoot_environment",
                "objectKey": args.environment_key,
                "filename": Path(args.environment_key).name,
            }
        )

    output_key = f"experiments/research/task-{args.task_id}/{args.name}.png"
    result = await get_image_provider().generate(package, output_key)

    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    (args.artifact_dir / f"{args.name}.png").write_bytes(get_bytes(output_key))
    (args.artifact_dir / f"{args.name}-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"name": args.name, "providerRequestId": result.get("providerRequestId"), "outputKey": output_key}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
