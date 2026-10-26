"""成片提示词整理的纯文本契约；摄影规则始终来自可维护模板。"""
from __future__ import annotations

import hashlib
import re
from typing import Any

OPTIMIZER_KEY = "assembly.final_prompt_optimizer"
LOCKED_HEADINGS = ("参考图职责与优先级", "当前镜头的商品表达", "反伪影成像要求")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def locked_constraints(prompt: str) -> str:
    sections = list(re.finditer(r"^【([^\n】]+)】", prompt, re.MULTILINE))
    wanted = ("锁定约束",) if any(m.group(1) == "锁定约束" for m in sections) else LOCKED_HEADINGS
    if any(sum(match.group(1) == name for match in sections) != 1 for name in wanted):
        raise ValueError("成片模板的锁定约束标题缺失或重复")
    blocks = {
        match.group(1): prompt[match.start():sections[index + 1].start() if index + 1 < len(sections) else len(prompt)].strip()
        for index, match in enumerate(sections)
    }
    if any(not blocks[name].removeprefix(f"【{name}】").strip() for name in wanted):
        raise ValueError("成片模板的锁定约束不能为空")
    return "\n\n".join(blocks[name] for name in wanted)


def optimizer_input(packages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"shots": [
        {"shotId": key, "shotIndex": package["promptRuntime"]["shot_index"],
         "sourcePrompt": package["prompt"], "lockedConstraints": locked_constraints(package["prompt"]),
         "references": [{"index": index, "role": item["role"]} for index, item in enumerate(package["imageInputs"], 1)]}
        for key, package in packages.items()
    ]}


def validate_optimized_prompts(raw: dict[str, Any], packages: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    shots = raw.get("shots")
    if not isinstance(shots, list) or len(shots) != len(packages):
        raise ValueError("优化结果的镜头数量不匹配")
    result: dict[str, dict[str, str]] = {}
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError("优化结果的镜头格式不正确")
        key = str(shot.get("shotId", ""))
        if key not in packages or key in result:
            raise ValueError("优化结果包含未知或重复镜头")
        if not isinstance(shot.get("conflicts"), list) or shot["conflicts"]:
            raise ValueError(f"镜头 {key} 存在未解决冲突或缺少冲突检查：{str(shot.get('conflicts', ''))[:500]}")
        body = shot.get("optimizedText")
        summary = shot.get("changeSummary")
        if not isinstance(body, str) or not body.strip() or len(body) > 24000 or not isinstance(summary, str):
            raise ValueError("优化正文为空、过长或缺少改动摘要")
        if "{{" in body or "}}" in body:
            raise ValueError("优化结果残留未替换占位符")
        count = len(packages[key]["imageInputs"])
        refs = re.findall(r"(?:图|Image\s*)\s*(\d+)(?:\s*[-–至]\s*(\d+))?", body, re.IGNORECASE)
        if any(not 1 <= int(number) <= count for pair in refs for number in pair if number):
            raise ValueError("优化结果引用了不存在的图片编号")
        locked = locked_constraints(packages[key]["prompt"])
        # LLM 只提交可编辑正文；锁定块由代码直接从源文本取回。
        final = body.strip() + "\n\n" + locked
        result[key] = {"optimizedText": body.strip(), "lockedConstraints": locked,
                       "prompt": final, "changeSummary": summary[:2000]}
    return result
