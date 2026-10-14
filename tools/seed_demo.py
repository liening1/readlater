#!/usr/bin/env python3
"""生成演示数据（离线直接写库，供截图/试用）。

用法：
    ./.venv/bin/python tools/seed_demo.py           # 默认 12 条
    ./.venv/bin/python tools/seed_demo.py --count 20

清理：./.venv/bin/python tools/clean_demo.py
说明：会先把当前最大条目 id 记入 .demo_state.json，clean_demo.py 依此精确删除。
"""
import argparse
import json
import sqlite3
import sys
import zlib
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "readlater.db"
IMG = BASE / "data" / "images"
STATE = BASE / ".demo_state.json"
TZ = timezone(timedelta(hours=8))

TITLES = [
    ("卡片盒笔记法：为什么「连接」比「分类」重要", "article", "阅读,方法论"),
    ("间隔重复的认知科学基础：遗忘曲线与最优复习点", "article", "阅读,方法论"),
    ("渐进式总结：把收藏变成能用的笔记", "article", "阅读,知识管理"),
    ("费曼学习法的四个步骤与实践误区", "article", "阅读,方法论"),
    ("深度工作：如何在注意力稀缺时代保持专注", "article", "阅读,工具"),
    ("信息过载时代的阅读策略：少看、精读、重读", "article", "阅读"),
    ("为什么我们收藏了却从不回看", "article", "阅读,知识管理"),
    ("一个本地优先的稍后读工具设计笔记", "web", "工具,稍后读"),
    ("视频：如何用卡片笔记构建个人知识体系", "video", "阅读,方法论"),
    ("播客：注意力经济与内容的复利", "video", "阅读,工具"),
    ("元认知：思考自己如何思考", "article", "阅读,方法论"),
    ("时间管理的本质是注意力管理", "article", "工具"),
    ("从信息焦虑到信息节制：我的减法实践", "article", "阅读"),
    ("知识管理的三种失败模式", "article", "知识管理"),
    ("阅读笔记的复利：为什么每本书都值得留一句话", "article", "阅读,方法论"),
    ("本地优先软件：数据主权与个人计算的回归", "web", "工具"),
]


def make_png(path: Path, w: int, h: int, rgb: tuple):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=12)
    args = ap.parse_args()

    IMG.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")

    maxid = conn.execute("SELECT IFNULL(MAX(id), 0) FROM items").fetchone()[0]
    STATE.write_text(json.dumps({"max_item_id": maxid}, indent=2))
    print(f"记录基线 max_item_id={maxid} → {STATE.name}")

    now = datetime.now(TZ)
    colors = ["#0071e3", "#ff9500", "#34c759", "#af52de", "#ff375f", "#8e8e93"]
    created_spread = [0, 1, 2, 3, 4, 6, 8, 10, 13, 17, 21, 26]
    review_spread = [0, 1, 2, 3, 4, 5, 6, 8, 10, 13, 17, 21]
    new_ids = []

    for i in range(args.count):
        title, typ, tags = TITLES[i % len(TITLES)]
        created = now - timedelta(days=created_spread[i % len(created_spread)], hours=2 + i % 9)
        read = i % 3 != 0
        cover = ""
        content = f"<p>{title}——演示数据，用于截图预览。</p>"
        if typ == "image":
            fname = f"demo_{i}.png"
            make_png(IMG / fname, 800, 500, (0, 113, 227) if i % 2 else (255, 149, 0))
            cover = f"/data/images/{fname}"
            content = f'<img src="{cover}" style="max-width:100%">'
        elif typ == "video":
            content = (f'<p>视频链接收藏。暂不支持站内播放，'
                       f'<a href="https://example.com/demo-video">打开原视频</a></p>')
        note = ("值得回看的观点：先建立连接，再谈分类。" if i == 1 else "")
        cur = conn.execute(
            "INSERT INTO items(type,url,title,author,cover,content_html,excerpt,tags,note,"
            "status,created_at,read_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (typ, f"demo://{maxid + i + 1}", title, "演示来源", cover, content,
             f"{title}。摘要演示数据，用于展示列表与回顾卡片的排版效果。",
             tags, note, "read" if read else "unread",
             created.isoformat(timespec="seconds"),
             (created + timedelta(hours=4)).isoformat(timespec="seconds") if read else None))
        item_id = cur.lastrowid
        new_ids.append(item_id)
        for name in [x for x in tags.split(",") if x]:
            color = colors[sum(ord(c) for c in name) % len(colors)]
            conn.execute("INSERT OR IGNORE INTO tags(name,color,pinned,created_at) VALUES(?,?,0,?)",
                         (name, color, now.isoformat(timespec="seconds")))
        last_seen = now - timedelta(days=review_spread[i % len(review_spread)], hours=i % 5)
        due = (now - timedelta(hours=2)) if i < 6 else (last_seen + timedelta(days=7 + i))
        conn.execute(
            "INSERT INTO review_state(item_id,seen_count,last_seen,next_due,bucket,favorite,archived)"
            " VALUES(?,?,?,?,?,?,0)",
            (item_id, 1 + (i % 3), last_seen.isoformat(timespec="seconds"),
             due.isoformat(timespec="seconds"), "keep", 1 if i == 2 else 0))
        if i == 3:
            conn.execute("UPDATE tags SET pinned=1 WHERE name=?", (tags.split(",")[0],))

    conn.commit()
    print(f"已插入演示条目 {len(new_ids)} 条：{new_ids}")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
