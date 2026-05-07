"""
一次性迁移脚本：将 chat_history 中的历史工具消息迁移至 tool_execution_log 表。

用法：
    python -m chatbot.scripts.migrate_tool_logs [--dry-run] [--db-path PATH]

    --dry-run   仅打印迁移计划，不写入
    --db-path   指定 SQLite 数据库路径（默认从 config 读取）
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate(db_path: str, dry_run: bool = False):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 1. 查找所有带 tool_calls 的 assistant 消息
    assistant_rows = conn.execute(
        "SELECT id, session_id, timestamp, tool_calls FROM chat_history "
        "WHERE role = 'assistant' AND tool_calls IS NOT NULL "
        "ORDER BY session_id, timestamp"
    ).fetchall()

    print(f"找到 {len(assistant_rows)} 条带 tool_calls 的 assistant 消息")

    migrated = 0
    skipped = 0

    for asst in assistant_rows:
        session_id = asst["session_id"]
        asst_ts = asst["timestamp"]
        asst_id = asst["id"]

        try:
            tool_calls = json.loads(asst["tool_calls"])
        except (json.JSONDecodeError, TypeError):
            print(f"  跳过 assistant id={asst_id}: tool_calls JSON 解析失败")
            skipped += 1
            continue

        if not isinstance(tool_calls, list):
            tool_calls = [tool_calls]

        for step_idx, tc in enumerate(tool_calls, 1):
            tc_id = tc.get("id", "")
            func = tc.get("function", {})
            tool_name = func.get("name", "unknown")
            arguments_str = func.get("arguments", "{}")

            try:
                arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
            except json.JSONDecodeError:
                arguments = {}

            # 2. 查找对应的 tool 结果消息
            tool_row = conn.execute(
                "SELECT id, content FROM chat_history "
                "WHERE role = 'tool' AND session_id = ? AND "
                "(tool_calls LIKE ? OR tool_calls LIKE ?) "
                "ORDER BY timestamp LIMIT 1",
                (session_id, f'%"{tc_id}"%', f'%{tc_id}%')
            ).fetchone()

            result_summary = ""
            error = None
            tool_msg_id = None

            if tool_row:
                result_summary = tool_row["content"][:2000]
                tool_msg_id = tool_row["id"]
                if any(k in result_summary for k in ("Error", "error", "Exception", "失败", "超时")):
                    error = result_summary[:2000]
            else:
                result_summary = "(结果消息未找到)"
                error = "对应的 tool 消息缺失"

            print(
                f"  session={session_id} step={step_idx} tool={tool_name} "
                f"assistant_id={asst_id} tool_id={tool_msg_id}"
            )

            if not dry_run:
                conn.execute(
                    "INSERT INTO tool_execution_log "
                    "(session_id, request_id, step, trigger, tool_name, arguments, "
                    "result_summary, error, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        f"migrated_{asst_id}",
                        step_idx,
                        "llm",
                        tool_name,
                        json.dumps(arguments, ensure_ascii=False),
                        result_summary,
                        error,
                        asst_ts,
                    ),
                )

                # 删除原始 tool 消息
                if tool_msg_id:
                    conn.execute("DELETE FROM chat_history WHERE id = ?", (tool_msg_id,))

            migrated += 1

        # 删除原始 assistant 消息（含 tool_calls）
        if not dry_run:
            conn.execute("DELETE FROM chat_history WHERE id = ?", (asst_id,))

    if not dry_run:
        conn.commit()
        print(f"\n迁移完成：已迁移 {migrated} 条工具记录，删除 {len(assistant_rows)} 条 assistant + {migrated} 条 tool 消息")
    else:
        print(f"\n[Dry Run] 将迁移 {migrated} 条工具记录，删除 {len(assistant_rows)} 条 assistant + {migrated} 条 tool 消息")

    # 验证
    remaining = conn.execute(
        "SELECT COUNT(*) FROM chat_history WHERE role = 'tool' OR (role = 'assistant' AND tool_calls IS NOT NULL)"
    ).fetchone()[0]
    log_count = conn.execute("SELECT COUNT(*) FROM tool_execution_log").fetchone()[0]
    print(f"迁移后残留工具消息: {remaining}，tool_execution_log 总行数: {log_count}")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="迁移 chat_history 工具消息至 tool_execution_log")
    parser.add_argument("--dry-run", action="store_true", help="仅打印迁移计划")
    parser.add_argument("--db-path", default=None, help="SQLite 数据库路径")
    args = parser.parse_args()

    db_path = args.db_path
    if not db_path:
        # 尝试从 config 读取默认路径
        try:
            from pathlib import Path
            import yaml
            config_file = Path(__file__).resolve().parent.parent.parent.parent.parent / "config" / "config_bot_base.yaml"
            if config_file.exists():
                with open(config_file, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                db_path = cfg.get("db_path", "")
        except Exception:
            pass

    if not db_path:
        print("错误：请通过 --db-path 指定数据库路径，或确保 config_bot_base.yaml 中配置了 db_path")
        sys.exit(1)

    print(f"数据库: {db_path}")
    print(f"模式: {'Dry Run' if args.dry_run else '正式迁移'}\n")

    migrate(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
