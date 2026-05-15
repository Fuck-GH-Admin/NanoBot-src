"""
世界书自动去重融合脚本。

用法：
    python -m chatbot.scripts.dedup_worldbook [--dry-run] [--config PATH]

流程：
    1. 加载 config/worldbook.json，创建时间戳备份
    2. 第一阶段：严格物理去重（content 完全一致 → 保留最新）
    3. 第二阶段：保守聚类（Jaccard ≥ 0.5 / 完全一致 / 子集关系）
    4. 对 2+ 条目的簇调用 LLM 智能融合
    5. 输出 config/worldbook_clean.json + 执行报告
"""

import argparse
import asyncio
import json
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import httpx

# ── 路径解析 ──
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# ── NoneBot 最小初始化（独立运行时 __init__.py 需要）──
import nonebot
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from chatbot.utils.path_utils import CONFIG_DIR as _CONFIG_DIR, WORLDBOOK_PATH as _WORLDBOOK_PATH


# ════════════════════════════════════════════════════════════════
# 第一阶段：严格物理去重
# ════════════════════════════════════════════════════════════════

def _exact_dedup(entries: list[dict]) -> list[dict]:
    """
    严格物理去重：content 完全一致（strip 后）的条目只保留最后出现的一条。
    返回去重后的列表，保持原有顺序。
    """
    seen: dict[str, int] = {}  # content → 在 result 中的索引
    result: list[dict] = []

    for e in entries:
        content = e.get("content", "").strip()
        if not content:
            result.append(e)
            continue
        if content in seen:
            # 用当前条目替换之前那条（保留最新的）
            old_idx = seen[content]
            result[old_idx] = e
        else:
            seen[content] = len(result)
            result.append(e)

    return result


# ════════════════════════════════════════════════════════════════
# 第二阶段：保守聚类
# ════════════════════════════════════════════════════════════════

def _get_key_set(entry: dict) -> set[str]:
    """提取条目的 key 集合（去空、去重）。"""
    keys = entry.get("key") or entry.get("keys") or []
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    return {k for k in keys if isinstance(k, str) and k}


def _should_merge(set_a: set[str], set_b: set[str]) -> bool:
    """
    判断两个 key 集合是否应合并。满足任一条件即合并：
    1. 完全一致
    2. Jaccard 相似度 ≥ 0.5
    3. 其中一个是另一个的子集（且两者都非空）
    """
    if not set_a or not set_b:
        return False

    # 条件 1：完全一致
    if set_a == set_b:
        return True

    # 条件 3：子集关系（必须在 Jaccard 之前检查，因为子集可能 Jaccard < 0.5）
    if set_a <= set_b or set_b <= set_a:
        return True

    # 条件 2：Jaccard ≥ 0.5
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    if union > 0 and intersection / union >= 0.5:
        return True

    return False


def _cluster_entries(entries: list[dict]) -> list[list[int]]:
    """
    保守聚类：仅当两个条目的 key 满足严格相似条件时才连通。
    使用并查集，但边的判定条件远比"只要有交集"严格。
    """
    n = len(entries)
    parent = list(range(n))
    rank = [0] * n

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    # 预计算所有 key 集合
    key_sets = [_get_key_set(entries[i]) for i in range(n)]

    # O(N²) 两两比较（N 通常 < 500，完全可接受）
    for i in range(n):
        if not key_sets[i]:
            continue
        for j in range(i + 1, n):
            if not key_sets[j]:
                continue
            if _should_merge(key_sets[i], key_sets[j]):
                union(i, j)

    # 收集簇（仅返回 2+ 条目的簇）
    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)

    return [indices for indices in clusters.values() if len(indices) >= 2]


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

def _load_api_config() -> tuple[str, str, str]:
    """从 YAML 配置中读取 API 凭据（轻量级，不依赖 nonebot）。"""
    import yaml
    config_path = _CONFIG_DIR / "config_bot_base.yaml"
    if not config_path.exists():
        print(f"[ERROR] 配置文件不存在: {config_path}")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return (
        data.get("deepseek_api_key", ""),
        data.get("deepseek_api_url", "https://api.deepseek.com/chat/completions"),
        data.get("deepseek_memory_model_name", "deepseek-v4-flash"),
    )


def _backup_worldbook(path: Path) -> Path:
    """创建带时间戳的备份文件。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"worldbook_{ts}.json.bak")
    shutil.copy2(path, backup)
    print(f"[BACKUP] {backup.name}")
    return backup


async def _llm_merge(
    cluster_entries: list[dict],
    scope: str,
    api_key: str,
    api_url: str,
    model: str,
) -> dict | None:
    """调用 LLM 将一个簇内的冗余词条融合为单条，返回完整的 SillyTavern 兼容条目。"""
    entries_text = json.dumps(
        [{"key": e.get("key", []), "content": e.get("content", "")} for e in cluster_entries],
        ensure_ascii=False,
        indent=2,
    )

    system_prompt = (
        "你是一个设定集架构师。以下是关于同一个实体/主题的多个冗余设定。\n"
        "请将它们的所有关键细节（不遗漏任何有价值的信息）完美融合成一段连贯、清晰的设定文本，\n"
        "并提取一个最全面、最精简的关键词数组。\n\n"
        "【输出格式】\n"
        '返回一个 JSON 对象：{"key": ["关键词1", "关键词2"], "content": "融合后的设定文本"}\n'
        "规则：\n"
        "- key 数组去重，保留最核心、最有辨识度的关键词\n"
        "- content 必须覆盖原始条目中的所有有价值信息，不得遗漏\n"
        "- 输出纯 JSON，不要有其他文字"
    )

    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"请融合以下冗余设定：\n\n{entries_text}"},
        ],
        "response_format": {"type": "json_object"},
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 1500,
        "thinking": {"type": "disabled"},
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                api_url,
                json=request_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            if resp.status_code != 200:
                print(f"  [WARN] LLM API 错误: {resp.status_code}")
                return None

            data = resp.json()
            raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = json.loads(raw.strip())

            keys = parsed.get("key") or parsed.get("keys") or []
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.split(",") if k.strip()]
            content = str(parsed.get("content", "")).strip()

            if not content:
                return None

            # 以第一个条目为模板，保留其全部原始字段
            merged = dict(cluster_entries[0])
            merged["key"] = keys if isinstance(keys, list) else [str(keys)]
            merged["content"] = content
            merged["comment"] = "Auto-Merged"

            # SillyTavern 扩展字段兜底（防止模板本身缺失）
            merged.setdefault("keysecondary", [])
            merged.setdefault("selectiveLogic", 0)
            merged.setdefault("position", 0)
            merged.setdefault("depth", 4)
            merged.setdefault("order", 100)
            merged.setdefault("disable", False)
            merged.setdefault("selective", False)
            merged.setdefault("excludeRecursion", False)
            merged.setdefault("preventRecursion", False)
            merged.setdefault("group", "")
            merged.setdefault("groupOverride", False)
            merged.setdefault("groupWeight", 100)
            merged.setdefault("probability", 100)
            merged.setdefault("useProbability", True)
            merged.setdefault("outletName", "")
            merged.setdefault("role", 0)

            # 强制写入作用域
            if scope != "global":
                merged["custom_scope"] = scope
            else:
                merged.pop("custom_scope", None)

            return merged
    except Exception as e:
        print(f"  [WARN] LLM 融合失败: {e}")
        return None


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════

async def dedup(worldbook_path: Path, dry_run: bool = False) -> None:
    """主流程：加载 → 物理去重 → 保守聚类 → 融合 → 输出。"""
    if not worldbook_path.exists():
        print(f"[ERROR] 世界书文件不存在: {worldbook_path}")
        return

    api_key, api_url, model = _load_api_config()
    if not api_key:
        print("[ERROR] 未配置 deepseek_api_key，无法调用 LLM 融合")
        return

    # ── 1. 加载 ──
    raw_data = json.loads(worldbook_path.read_text(encoding="utf-8"))
    is_dict_format = isinstance(raw_data, dict)
    entries = raw_data.get("entries", []) if is_dict_format else raw_data
    original_count = len(entries)
    print(f"[LOAD] {original_count} 条词条")

    # ── 2. 备份 ──
    if not dry_run:
        _backup_worldbook(worldbook_path)

    # ── 3. 第一阶段：严格物理去重 ──
    entries = _exact_dedup(entries)
    exact_removed = original_count - len(entries)
    if exact_removed > 0:
        print(f"[PHASE 1] 物理去重: 移除 {exact_removed} 条 content 完全一致的冗余条目")
    else:
        print(f"[PHASE 1] 物理去重: 无重复 content")

    # ── 4. 第二阶段：按 scope 分组 + 保守聚类 ──
    scope_groups: dict[str, list[int]] = defaultdict(list)
    for i, e in enumerate(entries):
        scope = e.get("custom_scope", "global")
        scope_groups[scope].append(i)

    print(f"[PHASE 2] {len(scope_groups)} 个作用域，开始保守聚类...")

    merged_indices: set[int] = set()
    new_entries: list[dict] = []
    merge_count = 0

    for scope, indices in scope_groups.items():
        scope_entries = [entries[i] for i in indices]
        clusters = _cluster_entries(scope_entries)

        if not clusters:
            continue

        print(f"\n  [SCOPE] {scope}: {len(scope_entries)} 条 → {len(clusters)} 个冲突簇")

        for cluster_local_indices in clusters:
            cluster_global_indices = [indices[li] for li in cluster_local_indices]
            cluster_entries_list = [entries[i] for i in cluster_global_indices]
            cluster_keys = [_get_key_set(e) for e in cluster_entries_list]

            # dry-run 输出：展示簇详情
            print(f"    [CLUSTER] {len(cluster_entries_list)} 条:")
            for ki, ks in enumerate(cluster_keys):
                content_preview = cluster_entries_list[ki].get("content", "")[:60]
                print(f"      - keys={sorted(ks)}  content={content_preview!r}...")

            if dry_run:
                merge_count += len(cluster_entries_list) - 1
                continue

            merged = await _llm_merge(cluster_entries_list, scope, api_key, api_url, model)
            if merged:
                merged["uid"] = cluster_entries_list[0].get("uid", 0)
                new_entries.append(merged)
                for gi in cluster_global_indices:
                    merged_indices.add(gi)
                merge_count += len(cluster_entries_list) - 1
                print(f"    [MERGED] → keys={merged['key']}")
            else:
                print(f"    [SKIP] 融合失败，保留原样")

            await asyncio.sleep(1.0)

    # ── 5. 组装最终列表 ──
    if dry_run:
        print(f"\n{'='*50}")
        print(f"[DRY-RUN REPORT]")
        print(f"  原始词条数:      {original_count}")
        print(f"  物理去重移除:    {exact_removed}")
        print(f"  保守聚类合并:    {merge_count}")
        print(f"  预计最终词条数:  {original_count - exact_removed - merge_count}")
        return

    final_entries = []
    for i, e in enumerate(entries):
        if i not in merged_indices:
            final_entries.append(e)
    final_entries.extend(new_entries)

    for idx, e in enumerate(final_entries, start=1):
        e["uid"] = idx

    # ── 6. 落盘 ──
    clean_path = worldbook_path.with_name("worldbook_clean.json")
    output = {"entries": final_entries} if is_dict_format else final_entries
    clean_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── 7. 报告 ──
    print(f"\n{'='*50}")
    print(f"[REPORT]")
    print(f"  原始词条数:      {original_count}")
    print(f"  物理去重移除:    {exact_removed}")
    print(f"  保守聚类合并:    {merge_count}")
    print(f"  最终词条数:      {len(final_entries)}")
    print(f"  输出文件:        {clean_path}")
    print(f"  备份文件:        {worldbook_path.with_name('worldbook_*.json.bak')}")
    print(f"\n  请检查 {clean_path.name} 无误后，替换 worldbook.json。")


def main():
    parser = argparse.ArgumentParser(description="世界书自动去重融合")
    parser.add_argument("--dry-run", action="store_true", help="仅分析，不写入")
    parser.add_argument("--config", type=str, default=str(_WORLDBOOK_PATH), help="worldbook.json 路径")
    args = parser.parse_args()

    asyncio.run(dedup(Path(args.config), dry_run=args.dry_run))


if __name__ == "__main__":
    main()
