#!/usr/bin/env python3
"""清理 tools/seed_demo.py 生成的演示数据（依据 .demo_state.json 精确删除，不动真实数据）。

用法：./.venv/bin/python tools/clean_demo.py
"""
import json
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "readlater.db"
STATE = BASE / ".demo_state.json"


def main():
    if not STATE.exists():
        print("✗ 未找到 .demo_state.json，无法确定基线；请勿手动删除，改用 git 记录。")
        return 1
    maxid = json.loads(STATE.read_text())["max_item_id"]

    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    fake = [r[0] for r in conn.execute("SELECT id FROM items WHERE id > ?", (maxid,))]
    covers = [r[0] for r in conn.execute(
        "SELECT cover FROM items WHERE id > ? AND cover LIKE '/data/images/demo_%'", (maxid,))]

    for i in fake:
        conn.execute("DELETE FROM review_state WHERE item_id=?", (i,))
    conn.execute("DELETE FROM items WHERE id > ?", (maxid,))
    conn.execute("""DELETE FROM tags WHERE name NOT IN (
        SELECT DISTINCT trim(value) FROM items,
        json_each('["' || replace(tags, ',', '","') || '"]') WHERE tags != '')""")
    conn.commit()

    removed_files = 0
    for cover in covers:
        p = BASE / cover.lstrip("/")
        if p.exists():
            p.unlink()
            removed_files += 1

    left = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    print(f"已删除演示条目 {len(fake)} 条、演示图片 {removed_files} 个；剩余真实条目 {left} 条。")
    conn.close()
    STATE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
