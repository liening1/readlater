"""ReadLater —— 本地稍后读服务。跑在 Mac 上，全平台浏览器访问。

启动：见 run.sh / README.md
"""
import json
import os
import re
import sqlite3
import threading
import uuid
import zlib
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from parser import (detect_type, enrich_with_pdf, fetch, is_challenge, parse_generic,
                    parse_video, parse_wechat, render_html_async)

BASE = Path(__file__).parent
# 数据目录可用环境变量覆盖（Docker 下指向挂载卷 /data）
DATA = Path(os.environ.get("READLATER_DATA") or (BASE / "data"))
DATA.mkdir(parents=True, exist_ok=True)
IMG_DIR = DATA / "images"
DB_PATH = DATA / "readlater.db"
TZ = timezone(timedelta(hours=8))

# uvicorn 线程池并发执行同步端点，SQLite 写必须串行化，否则 "database is locked"
_DB_LOCK = threading.Lock()

app = FastAPI(title="ReadLater", docs_url=None, redoc_url=None)


# ---------- DB ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # 读写不互斥
    conn.execute("PRAGMA busy_timeout=30000")    # 写冲突排队而非报错
    conn.execute(
        """CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            url TEXT UNIQUE,
            title TEXT DEFAULT '',
            author TEXT DEFAULT '',
            cover TEXT DEFAULT '',
            content_html TEXT DEFAULT '',
            excerpt TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            note TEXT DEFAULT '',
            status TEXT DEFAULT 'unread',
            created_at TEXT NOT NULL,
            read_at TEXT,
            deleted_at TEXT,
            archived_at TEXT,
            published_at TEXT,
            quality TEXT DEFAULT 'full',
            pinned INTEGER DEFAULT 0)""")
    # 老库自动迁移：补齐后加的列
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    for name, ddl in (("deleted_at", "TEXT"), ("archived_at", "TEXT"),
                      ("published_at", "TEXT"), ("quality", "TEXT DEFAULT 'full'"),
                      ("pinned", "INTEGER DEFAULT 0")):
        if name not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS review_state(
            item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
            seen_count INTEGER DEFAULT 0,
            last_seen TEXT,
            next_due TEXT,
            bucket TEXT DEFAULT '',
            favorite INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0)""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tags(
            name TEXT PRIMARY KEY,
            color TEXT DEFAULT '',
            pinned INTEGER DEFAULT 0,
            created_at TEXT)""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS highlights(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL)""")
    conn.commit()
    return conn


# 标签色板（与前端 TAG_COLORS 保持一致；Apple 系统色）
TAG_COLORS = ["#0071e3", "#ff9500", "#34c759", "#af52de", "#ff375f", "#8e8e93"]


def default_tag_color(name: str) -> str:
    """同一标签名恒定取到同一颜色（哈希，无需存储）。"""
    return TAG_COLORS[zlib.crc32(name.encode("utf-8")) % len(TAG_COLORS)]


def touch_tags(conn: sqlite3.Connection, names: list[str]) -> None:
    """打标签时自动登记元数据，保证每个标签有颜色。"""
    now = datetime.now(TZ).isoformat(timespec="seconds")
    for n in names:
        if n:
            conn.execute("INSERT OR IGNORE INTO tags(name,color,pinned,created_at) VALUES(?,?,0,?)",
                         (n, default_tag_color(n), now))


def row_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
    return d


# ---------- collect ----------
async def collect_url(url: str, note: str = "") -> dict:
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "不是有效的 http(s) 链接")

    conn = db()
    try:
        exists = conn.execute("SELECT id, title FROM items WHERE url=?", (url,)).fetchone()
    finally:
        conn.close()
    if exists:
        return {"id": exists["id"], "duplicate": True, "title": exists["title"]}

    item_key = uuid.uuid4().hex[:12]
    url_type = detect_type(url)
    title, author, cover = url[:80], "", ""
    content_html, excerpt, published = "", "", ""
    quality = "link_only"
    try:
        if url_type == "image":
            local = await _save_remote_image(url, item_key)
            if local:
                title = Path(local).name
                content_html = f'<img src="{local}" style="max-width:100%">'
                cover = local
                quality = "full"
            else:
                excerpt = "图片下载失败，仅存链接"
        elif url_type == "video":
            meta = await parse_video(url)
            title, author, cover = meta["title"], meta["author"], meta["cover"]
            excerpt = meta["excerpt"] or "视频链接"
            quality = meta.get("quality", "link_only")
            content_html = (f'<p>视频链接收藏。暂不支持站内播放，'
                            f'<a href="{url}" target="_blank">打开原视频</a></p>')
        else:
            resp = await fetch(url)
            ct = resp.headers.get("content-type", "")
            if ct.startswith("image/"):
                url_type = "image"
                local = await _save_remote_image(url, item_key)
                if local:
                    title, cover = Path(local).name, local
                    content_html = f'<img src="{local}" style="max-width:100%">'
                    quality = "full"
            elif "mp.weixin.qq.com" in url:
                url_type = "article"
                meta = await parse_wechat(resp.text, url, item_key)
                title, author, cover = meta["title"], meta["author"], meta["cover"]
                content_html, excerpt, published = (meta["content_html"],
                                                    meta["excerpt"], meta["published"])
                quality = meta.get("quality", "full")
            else:
                meta = await parse_generic(resp.text, url)
                title, author, cover = meta["title"], meta["author"], meta["cover"]
                content_html, excerpt = meta["content_html"], meta["excerpt"]
                published = meta.get("published", "")
                quality = meta.get("quality", "link_only")
                # JS 挑战页 / SPA 空壳 → 无头浏览器执行 JS 后重解析
                if quality == "link_only" or is_challenge(resp.text):
                    rendered = await render_html_async(url)
                    if rendered:
                        meta2 = await parse_generic(rendered, url)
                        if len(meta2.get("content_html") or "") > len(content_html):
                            title = meta2["title"] or title
                            author = meta2["author"] or author
                            cover = meta2["cover"] or cover
                            content_html = meta2["content_html"]
                            excerpt = meta2["excerpt"] or excerpt
                            published = meta2.get("published") or published
                            quality = meta2.get("quality", quality)
                            html_for_pdf = rendered
                        else:
                            html_for_pdf = resp.text
                    else:
                        html_for_pdf = resp.text
                else:
                    html_for_pdf = resp.text
                content_html, quality = await enrich_with_pdf(
                    html_for_pdf, url, content_html, quality)
    except Exception as e:  # 网络失败也先入账，保住链接
        excerpt = f"抓取失败（{type(e).__name__}），仅存链接"

    now = datetime.now(TZ).isoformat(timespec="seconds")
    with _DB_LOCK:
        conn = db()
        try:
            try:
                cur = conn.execute(
                    "INSERT INTO items(type,url,title,author,cover,content_html,excerpt,tags,"
                    "note,status,created_at,published_at,quality) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'unread',?,?,?)",
                    (url_type, url, title, author, cover, content_html, excerpt, "", note, now,
                     published, quality))
                conn.commit()
                item_id = cur.lastrowid
            except sqlite3.IntegrityError:  # 并发收集同一链接
                row = conn.execute("SELECT id, title FROM items WHERE url=?", (url,)).fetchone()
                return {"id": row["id"], "duplicate": True, "title": row["title"]}
        finally:
            conn.close()
    return {"id": item_id, "duplicate": False, "title": title, "type": url_type,
            "quality": quality}


async def _save_remote_image(img_url: str, item_key: str) -> str | None:
    from parser import _save_image
    return await _save_image(img_url, item_key, 0)


@app.post("/api/items/{item_id}/refetch")
async def refetch_item(item_id: int):
    """重新抓取并解析：用于早先只抓到挑战壳 / 空壳的条目。"""
    conn = db()
    try:
        row = conn.execute("SELECT url, type FROM items WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "条目不存在")
    url = row["url"]
    if not re.match(r"^https?://", url or ""):
        raise HTTPException(400, "该条目没有可重新抓取的远程链接")

    item_key = uuid.uuid4().hex[:12]
    url_type = row["type"] or detect_type(url)
    title = author = cover = content_html = excerpt = published = ""
    quality = "link_only"
    try:
        resp = await fetch(url)
        if "mp.weixin.qq.com" in url:
            meta = await parse_wechat(resp.text, url, item_key)
        else:
            meta = await parse_generic(resp.text, url)
        title, author, cover = meta["title"], meta["author"], meta["cover"]
        content_html, excerpt, published = (meta["content_html"], meta["excerpt"],
                                            meta.get("published", ""))
        quality = meta.get("quality", "link_only")
        html_for_pdf = resp.text
        if quality == "link_only" or is_challenge(resp.text):
            rendered = await render_html_async(url)
            if rendered:
                meta2 = await parse_generic(rendered, url)
                if len(meta2.get("content_html") or "") > len(content_html):
                    title = meta2["title"] or title
                    author = meta2["author"] or author
                    cover = meta2["cover"] or cover
                    content_html = meta2["content_html"]
                    excerpt = meta2["excerpt"] or excerpt
                    published = meta2.get("published") or published
                    quality = meta2.get("quality", quality)
                    html_for_pdf = rendered
        content_html, quality = await enrich_with_pdf(
            html_for_pdf, url, content_html, quality)
    except Exception as e:
        raise HTTPException(502, f"重新抓取失败：{type(e).__name__}")

    with _DB_LOCK:
        conn = db()
        try:
            conn.execute(
                "UPDATE items SET title=?, author=?, cover=?, content_html=?, excerpt=?,"
                " published_at=?, quality=? WHERE id=?",
                (title, author, cover, content_html, excerpt, published, quality, item_id))
            conn.commit()
            updated = row_dict(conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())
        finally:
            conn.close()
    return {"item": updated, "quality": quality}


# ---------- API ----------
class CollectBody(BaseModel):
    url: str
    note: str = ""


class PatchBody(BaseModel):
    status: Optional[str] = None
    tags: Optional[list[str]] = None
    note: Optional[str] = None
    archived: Optional[int] = None   # 1 归档 / 0 取消归档
    pinned: Optional[int] = None     # 1 置顶 / 0 取消


class ReviewFeedback(BaseModel):
    action: str  # keep / later / someday / archive / restore / favorite / unfavorite


class TagBody(BaseModel):
    name: str
    color: str = ""


class TagPatch(BaseModel):
    new_name: Optional[str] = None
    color: Optional[str] = None
    pinned: Optional[int] = None


class HighlightBody(BaseModel):
    item_id: int
    text: str
    note: str = ""


@app.get("/collect")
async def collect_get(url: str, note: str = ""):
    """Bookmarklet / 快捷指令通道：GET 即收集，完成后回首页。"""
    try:
        await collect_url(url, note)
    except Exception:
        pass  # 收集失败也照常跳转，避免打断浏览器操作
    return RedirectResponse("/", status_code=303)


@app.post("/api/collect")
async def collect_post(body: CollectBody):
    return await collect_url(body.url, body.note)


@app.post("/api/upload")
async def upload_image(file: UploadFile = File(...), note: str = Form("")):
    content = await file.read()
    if not content:
        raise HTTPException(400, "空文件")
    ext = ".png" if file.content_type == "image/png" else ".jpg"
    fname = f"{uuid.uuid4().hex[:12]}{ext}"
    (IMG_DIR / fname).write_bytes(content)
    now = datetime.now(TZ).isoformat(timespec="seconds")
    with _DB_LOCK:
        conn = db()
        try:
            cur = conn.execute(
                "INSERT INTO items(type,url,title,cover,content_html,tags,note,status,created_at)"
                " VALUES('image',?,?,?,?,?,?,'unread',?)",
                (f"local:{fname}", fname, f"/data/images/{fname}",
                 f'<img src="/data/images/{fname}" style="max-width:100%">', "", note, now))
            conn.commit()
            item_id = cur.lastrowid
        finally:
            conn.close()
    return {"id": item_id, "title": fname, "type": "image"}


@app.get("/api/items")
def list_items(status: str = "", type: str = "", tag: str = "", q: str = "",
               favorite: int = 0, today: int = 0, notes: int = 0,
               archived: int = 0, deleted: int = 0,
               limit: int = 50, offset: int = 0):
    sql = "SELECT * FROM items WHERE 1=1"
    args: list = []
    if deleted:                       # 回收站
        sql += " AND deleted_at IS NOT NULL"
    else:
        sql += " AND deleted_at IS NULL"
        sql += " AND archived_at IS NOT NULL" if archived else " AND archived_at IS NULL"
    if today:
        sql += " AND substr(created_at,1,10)=?"
        args.append(datetime.now(TZ).strftime("%Y-%m-%d"))
    if notes:
        sql += " AND note != ''"
    if favorite:
        sql += " AND id IN (SELECT item_id FROM review_state WHERE favorite=1)"
    if status in ("unread", "read"):
        sql += " AND status=?"
        args.append(status)
    if type in ("article", "video", "image", "web"):
        sql += " AND type=?"
        args.append(type)
    if tag:
        sql += " AND (',' || tags || ',') LIKE ?"
        args.append(f"%,{tag},%")
    if q:
        sql += " AND (title LIKE ? OR excerpt LIKE ? OR note LIKE ? OR url LIKE ?)"
        args += [f"%{q}%"] * 4
    sql += " ORDER BY pinned DESC, id DESC LIMIT ? OFFSET ?"
    args += [min(limit, 200), offset]
    conn = db()
    try:
        rows = [row_dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    return {"items": rows}


@app.get("/api/tags")
def list_tags():
    """标签列表：计数 + 元数据（颜色/置顶），排序 置顶 → 计数。"""
    conn = db()
    try:
        rows = conn.execute(
            "SELECT tags FROM items WHERE tags != '' AND deleted_at IS NULL").fetchall()
        meta = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM tags").fetchall()}
    finally:
        conn.close()
    counter: dict = {}
    for r in rows:
        for t in r["tags"].split(","):
            if t:
                counter[t] = counter.get(t, 0) + 1
    for name in counter:  # 老数据：条目里有、元数据表里没有的标签，补登记（不写库，仅展示用默认色）
        meta.setdefault(name, {"name": name, "color": default_tag_color(name),
                               "pinned": 0, "created_at": ""})
    out = [{"name": n, "count": c,
            "color": (meta.get(n) or {}).get("color") or default_tag_color(n),
            "pinned": (meta.get(n) or {}).get("pinned", 0)}
           for n, c in counter.items()]
    out.sort(key=lambda x: (-x["pinned"], -x["count"], x["name"]))
    return {"tags": out}


@app.post("/api/tags")
def create_tag(body: TagBody):
    name = body.name.strip().lstrip("#")
    if not name:
        raise HTTPException(400, "标签名不能为空")
    with _DB_LOCK:
        conn = db()
        try:
            color = body.color or default_tag_color(name)
            conn.execute("INSERT OR IGNORE INTO tags(name,color,pinned,created_at) VALUES(?,?,0,?)",
                         (name, color, datetime.now(TZ).isoformat(timespec="seconds")))
            if body.color:
                conn.execute("UPDATE tags SET color=? WHERE name=?", (color, name))
            conn.commit()
        finally:
            conn.close()
    return {"name": name, "color": color, "pinned": 0}


@app.patch("/api/tags/{name}")
def patch_tag(name: str, body: TagPatch):
    """改名（同步改写所有条目）/ 改色 / 置顶。"""
    old = name.strip()
    with _DB_LOCK:
        conn = db()
        try:
            meta = conn.execute("SELECT * FROM tags WHERE name=?", (old,)).fetchone()
            color = body.color if body.color is not None else (
                (meta["color"] if meta else "") or default_tag_color(old))
            pinned = body.pinned if body.pinned is not None else (meta["pinned"] if meta else 0)
            affected = 0
            final = old
            new = (body.new_name or "").strip().lstrip("#")
            if new and new != old:
                rows = conn.execute("SELECT id, tags FROM items WHERE tags != ''").fetchall()
                for r in rows:
                    parts = [t for t in r["tags"].split(",") if t]
                    if old in parts:
                        merged: list = []
                        for t in parts:
                            t2 = new if t == old else t
                            if t2 not in merged:
                                merged.append(t2)
                        conn.execute("UPDATE items SET tags=? WHERE id=?", (",".join(merged), r["id"]))
                        affected += 1
                conn.execute("DELETE FROM tags WHERE name=?", (old,))
                final = new
            conn.execute(
                "INSERT INTO tags(name,color,pinned,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET color=excluded.color, pinned=excluded.pinned",
                (final, color, pinned, datetime.now(TZ).isoformat(timespec="seconds")))
            conn.commit()
        finally:
            conn.close()
    return {"name": final, "color": color, "pinned": pinned, "affected": affected}


@app.delete("/api/tags/{name}")
def delete_tag(name: str):
    """从所有条目剥离该标签（条目本身保留），并删除元数据。"""
    tag = name.strip()
    with _DB_LOCK:
        conn = db()
        try:
            affected = 0
            rows = conn.execute("SELECT id, tags FROM items WHERE tags != ''").fetchall()
            for r in rows:
                parts = [t for t in r["tags"].split(",") if t]
                if tag in parts:
                    conn.execute("UPDATE items SET tags=? WHERE id=?",
                                 (",".join(t for t in parts if t != tag), r["id"]))
                    affected += 1
            conn.execute("DELETE FROM tags WHERE name=?", (tag,))
            conn.commit()
        finally:
            conn.close()
    return {"deleted": tag, "affected": affected}


@app.get("/api/review")
def review(n: int = 6):
    conn = db()
    try:
        today = datetime.now(TZ)
        mmdd = today.strftime("%m-%d")
        years = ",".join(str(y) for y in range(today.year - 5, today.year))
        randoms = [row_dict(r) for r in conn.execute(
            "SELECT * FROM items ORDER BY RANDOM() LIMIT ?", (min(n, 20),)).fetchall()]
        onthisday = [row_dict(r) for r in conn.execute(
            f"SELECT * FROM items WHERE strftime('%Y',created_at) IN ({years}) "
            "AND strftime('%m-%d',created_at)=? ORDER BY created_at DESC LIMIT 10",
            (mmdd,)).fetchall()]
    finally:
        conn.close()
    return {"random": randoms, "onthisday": onthisday, "date": today.isoformat(timespec="seconds")}


@app.get("/api/items/{item_id}")
def get_item(item_id: int):
    conn = db()
    try:
        r = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        raise HTTPException(404, "条目不存在")
    return row_dict(r)


@app.patch("/api/items/{item_id}")
def patch_item(item_id: int, body: PatchBody):
    with _DB_LOCK:
        conn = db()
        try:
            if conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is None:
                raise HTTPException(404, "条目不存在")
            if body.status in ("unread", "read"):
                conn.execute("UPDATE items SET status=?, read_at=? WHERE id=?",
                             (body.status,
                              datetime.now(TZ).isoformat(timespec="seconds")
                              if body.status == "read" else None,
                              item_id))
            if body.tags is not None:
                tag_list = [t.strip().lstrip("#") for t in body.tags if t.strip()]
                conn.execute("UPDATE items SET tags=? WHERE id=?",
                             (",".join(tag_list), item_id))
                touch_tags(conn, tag_list)
            if body.note is not None:
                conn.execute("UPDATE items SET note=? WHERE id=?", (body.note, item_id))
            if body.archived is not None:
                conn.execute("UPDATE items SET archived_at=? WHERE id=?",
                             (datetime.now(TZ).isoformat(timespec="seconds")
                              if body.archived else None, item_id))
            if body.pinned is not None:
                conn.execute("UPDATE items SET pinned=? WHERE id=?",
                             (1 if body.pinned else 0, item_id))
            conn.commit()
            r = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        finally:
            conn.close()
    return row_dict(r)


@app.delete("/api/items/{item_id}")
def delete_item(item_id: int):
    """软删除：移入回收站，可恢复。"""
    with _DB_LOCK:
        conn = db()
        try:
            cur = conn.execute(
                "UPDATE items SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                (datetime.now(TZ).isoformat(timespec="seconds"), item_id))
            conn.commit()
        finally:
            conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "条目不存在或已在回收站")
    return {"deleted": item_id, "soft": True}


@app.post("/api/items/{item_id}/restore")
def restore_item(item_id: int):
    with _DB_LOCK:
        conn = db()
        try:
            cur = conn.execute("UPDATE items SET deleted_at=NULL WHERE id=?", (item_id,))
            conn.commit()
        finally:
            conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "条目不存在")
    return {"restored": item_id}


@app.delete("/api/items/{item_id}/purge")
def purge_item(item_id: int):
    """永久删除（仅回收站内条目）。"""
    with _DB_LOCK:
        conn = db()
        try:
            conn.execute("DELETE FROM review_state WHERE item_id=?", (item_id,))
            cur = conn.execute("DELETE FROM items WHERE id=? AND deleted_at IS NOT NULL", (item_id,))
            conn.commit()
        finally:
            conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "条目不存在或不在回收站")
    return {"purged": item_id}


@app.post("/api/trash/empty")
def empty_trash():
    with _DB_LOCK:
        conn = db()
        try:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM items WHERE deleted_at IS NOT NULL").fetchall()]
            for i in ids:
                conn.execute("DELETE FROM review_state WHERE item_id=?", (i,))
            conn.execute("DELETE FROM items WHERE deleted_at IS NOT NULL")
            conn.commit()
        finally:
            conn.close()
    return {"purged": len(ids)}


@app.get("/api/counts")
def counts():
    """侧栏计数：一次取全，避免多次请求。"""
    conn = db()
    try:
        def one(sql, *args):
            return conn.execute(sql, args).fetchone()["c"]
        alive = "FROM items WHERE deleted_at IS NULL AND archived_at IS NULL"
        return {
            "today": one("SELECT COUNT(*) c " + alive + " AND substr(created_at,1,10)=?",
                         datetime.now(TZ).strftime("%Y-%m-%d")),
            "unread": one("SELECT COUNT(*) c " + alive + " AND status='unread'"),
            "all": one("SELECT COUNT(*) c " + alive),
            "starred": one("""SELECT COUNT(*) c FROM items i
                              JOIN review_state rs ON rs.item_id=i.id
                              WHERE i.deleted_at IS NULL AND i.archived_at IS NULL
                                AND rs.favorite=1"""),
            "notes": one("SELECT COUNT(*) c " + alive + " AND note != ''"),
            "archived": one("""SELECT COUNT(*) c FROM items
                               WHERE deleted_at IS NULL AND archived_at IS NOT NULL"""),
            "trash": one("SELECT COUNT(*) c FROM items WHERE deleted_at IS NOT NULL"),
        }
    finally:
        conn.close()


# ---------- 每日回顾（Readwise 式间隔重现） ----------
REVIEW_INTERVALS = {"keep": 7, "later": 14, "someday": 28}


@app.get("/api/review/due")
def review_due(n: int = 5):
    """今天到期的卡片：收藏★优先 → 从未回顾优先 → 最久未见优先；按类型轮转混排。"""
    n = min(max(n, 1), 20)
    conn = db()
    try:
        pool = conn.execute(
            """SELECT i.*, IFNULL(rs.seen_count,0) AS seen_count,
                      IFNULL(rs.favorite,0) AS favorite, rs.last_seen,
                      (SELECT text FROM highlights h WHERE h.item_id=i.id
                       ORDER BY RANDOM() LIMIT 1) AS hl
               FROM items i LEFT JOIN review_state rs ON rs.item_id = i.id
               WHERE i.deleted_at IS NULL AND i.archived_at IS NULL
                 AND (rs.item_id IS NULL
                  OR (rs.archived = 0 AND (rs.next_due IS NULL
                      OR substr(rs.next_due,1,10) <= date('now','localtime'))))
               ORDER BY favorite DESC,
                        CASE WHEN rs.last_seen IS NULL THEN 0 ELSE 1 END,
                        rs.last_seen ASC
               LIMIT 40""").fetchall()
    finally:
        conn.close()
    groups: dict[str, deque] = defaultdict(deque)
    for r in pool:
        groups[row_dict(r)["type"]].append(row_dict(r))
    picked: list = []
    while len(picked) < n and any(groups.values()):
        for dq in groups.values():
            if dq and len(picked) < n:
                picked.append(dq.popleft())
    return {"cards": picked[:n], "due_total": len(pool)}


@app.post("/api/review/{item_id}/feedback")
def review_feedback(item_id: int, body: ReviewFeedback):
    """曝光后反馈：keep 7天 / later 14天 / someday 28天，间隔随曝光次数拉长，★缩短。"""
    with _DB_LOCK:
        conn = db()
        try:
            if conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is None:
                raise HTTPException(404, "条目不存在")
            now = datetime.now(TZ)
            conn.execute("INSERT OR IGNORE INTO review_state(item_id) VALUES(?)", (item_id,))
            if body.action == "favorite":
                conn.execute("UPDATE review_state SET favorite=1 WHERE item_id=?", (item_id,))
            elif body.action == "unfavorite":
                conn.execute("UPDATE review_state SET favorite=0 WHERE item_id=?", (item_id,))
            elif body.action == "archive":
                conn.execute(
                    "UPDATE review_state SET archived=1, next_due=NULL WHERE item_id=?", (item_id,))
            elif body.action == "restore":
                conn.execute(
                    "UPDATE review_state SET archived=0, next_due=NULL WHERE item_id=?", (item_id,))
            elif body.action in REVIEW_INTERVALS:
                row = conn.execute(
                    "SELECT seen_count, favorite FROM review_state WHERE item_id=?",
                    (item_id,)).fetchone()
                days = REVIEW_INTERVALS[body.action] * (1.3 ** min(row["seen_count"], 10))
                if row["favorite"]:
                    days = max(2, days * 0.6)
                days = min(days, 180)
                conn.execute(
                    "UPDATE review_state SET seen_count=seen_count+1, last_seen=?, "
                    "next_due=?, bucket=?, archived=0 WHERE item_id=?",
                    (now.isoformat(timespec="seconds"),
                     (now + timedelta(days=days)).isoformat(timespec="seconds"),
                     body.action, item_id))
            else:
                raise HTTPException(400, "未知操作")
            conn.commit()
            r = conn.execute("SELECT * FROM review_state WHERE item_id=?", (item_id,)).fetchone()
        finally:
            conn.close()
    return dict(r)


@app.get("/api/review/stats")
def review_stats():
    today = datetime.now(TZ).date()
    conn = db()
    try:
        today_done = conn.execute(
            "SELECT COUNT(*) c FROM review_state WHERE substr(last_seen,1,10)=?",
            (today.isoformat(),)).fetchone()["c"]
        due_left = conn.execute(
            """SELECT COUNT(*) c FROM items i LEFT JOIN review_state rs ON rs.item_id=i.id
               WHERE rs.item_id IS NULL OR (rs.archived=0 AND (rs.next_due IS NULL
                   OR substr(rs.next_due,1,10) <= date('now','localtime')))""").fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) c FROM items WHERE id NOT IN "
            "(SELECT item_id FROM review_state WHERE archived=1)").fetchone()["c"]
        seen_days = {r["d"] for r in conn.execute(
            "SELECT DISTINCT substr(last_seen,1,10) d FROM review_state "
            "WHERE last_seen IS NOT NULL").fetchall()}
    finally:
        conn.close()
    streak = 0
    d = today
    if d.isoformat() not in seen_days:
        d -= timedelta(days=1)  # 今天还没回顾不打断连击
    while d.isoformat() in seen_days:
        streak += 1
        d -= timedelta(days=1)
    return {"today_done": today_done, "due_left": due_left,
            "active": active, "streak": streak}


@app.get("/api/items/{item_id}/highlights")
def list_highlights(item_id: int):
    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM highlights WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
    finally:
        conn.close()
    return {"highlights": [dict(r) for r in rows]}


@app.post("/api/highlights")
def add_highlight(body: HighlightBody):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "高亮内容为空")
    with _DB_LOCK:
        conn = db()
        try:
            if conn.execute("SELECT 1 FROM items WHERE id=?", (body.item_id,)).fetchone() is None:
                raise HTTPException(404, "条目不存在")
            cur = conn.execute(
                "INSERT INTO highlights(item_id,text,note,created_at) VALUES(?,?,?,?)",
                (body.item_id, text[:2000], body.note.strip(),
                 datetime.now(TZ).isoformat(timespec="seconds")))
            conn.commit()
            row = conn.execute("SELECT * FROM highlights WHERE id=?", (cur.lastrowid,)).fetchone()
        finally:
            conn.close()
    return dict(row)


@app.delete("/api/highlights/{hid}")
def delete_highlight(hid: int):
    with _DB_LOCK:
        conn = db()
        try:
            cur = conn.execute("DELETE FROM highlights WHERE id=?", (hid,))
            conn.commit()
        finally:
            conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "高亮不存在")
    return {"deleted": hid}


# ---------- 导出 / 导入（数据主权） ----------
@app.get("/api/export")
def export_data():
    """导出全部数据为 JSON（含正文、标签、回顾状态、高亮）。"""
    conn = db()
    try:
        items = [dict(r) for r in conn.execute("SELECT * FROM items ORDER BY id").fetchall()]
        tags = [dict(r) for r in conn.execute("SELECT * FROM tags").fetchall()]
        review = [dict(r) for r in conn.execute("SELECT * FROM review_state").fetchall()]
        highlights = [dict(r) for r in conn.execute("SELECT * FROM highlights").fetchall()]
    finally:
        conn.close()
    payload = {
        "app": "readlater",
        "version": 1,
        "exported_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "items": items, "tags": tags, "review_state": review, "highlights": highlights,
    }
    fname = f"readlater-export-{datetime.now(TZ).strftime('%Y%m%d-%H%M')}.json"
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/import")
async def import_data(file: UploadFile = File(...), mode: str = Form("auto")):
    """导入：ReadLater JSON 备份 / 浏览器或 Pocket 的书签 HTML。"""
    raw = await file.read()
    text = raw.decode("utf-8", errors="ignore")
    imported, skipped = 0, 0
    is_json = text.lstrip().startswith("{")
    with _DB_LOCK:
        conn = db()
        try:
            if is_json:
                try:
                    data = json.loads(text)
                except Exception:
                    raise HTTPException(400, "JSON 解析失败")
                rows = data.get("items") or []
                url_map = {}
                for it in rows:
                    url = (it.get("url") or "").strip()
                    if not url:
                        skipped += 1
                        continue
                    exists = conn.execute(
                        "SELECT id FROM items WHERE url=?", (url,)).fetchone()
                    if exists:
                        url_map[url] = exists["id"]
                        skipped += 1
                        continue
                    cur = conn.execute(
                        "INSERT INTO items(type,url,title,author,cover,content_html,excerpt,"
                        "tags,note,status,created_at,read_at,archived_at,pinned) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (it.get("type") or "web", url, it.get("title") or url,
                         it.get("author") or "", it.get("cover") or "",
                         it.get("content_html") or "", it.get("excerpt") or "",
                         it.get("tags") or "", it.get("note") or "",
                         it.get("status") or "unread",
                         it.get("created_at") or datetime.now(TZ).isoformat(timespec="seconds"),
                         it.get("read_at"), it.get("archived_at"), it.get("pinned") or 0))
                    url_map[url] = cur.lastrowid
                    imported += 1
                # 标签元数据
                for t in data.get("tags") or []:
                    conn.execute(
                        "INSERT OR IGNORE INTO tags(name,color,pinned,created_at) VALUES(?,?,?,?)",
                        (t.get("name"), t.get("color") or "", t.get("pinned") or 0,
                         t.get("created_at") or datetime.now(TZ).isoformat(timespec="seconds")))
                # 回顾状态（按原 item_id → URL 映射）
                id_to_url = {(it.get("id")): (it.get("url") or "") for it in rows}
                for rs in data.get("review_state") or []:
                    url = id_to_url.get(rs.get("item_id"))
                    new_id = url_map.get(url) if url else None
                    if not new_id:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO review_state(item_id,seen_count,last_seen,"
                        "next_due,bucket,favorite,archived) VALUES(?,?,?,?,?,?,?)",
                        (new_id, rs.get("seen_count") or 0, rs.get("last_seen"),
                         rs.get("next_due"), rs.get("bucket") or "",
                         rs.get("favorite") or 0, rs.get("archived") or 0))
                # 高亮
                for hl in data.get("highlights") or []:
                    url = id_to_url.get(hl.get("item_id"))
                    new_id = url_map.get(url) if url else None
                    if not new_id:
                        continue
                    conn.execute(
                        "INSERT INTO highlights(item_id,text,note,created_at) VALUES(?,?,?,?)",
                        (new_id, hl.get("text") or "", hl.get("note") or "",
                         hl.get("created_at") or datetime.now(TZ).isoformat(timespec="seconds")))
            else:
                soup = BeautifulSoup(text, "html.parser")
                for a in soup.find_all("a", href=True):
                    url = a["href"].strip()
                    if not re.match(r"^https?://", url):
                        continue
                    if conn.execute("SELECT 1 FROM items WHERE url=?", (url,)).fetchone():
                        skipped += 1
                        continue
                    title = (a.get_text(strip=True) or url)[:300]
                    conn.execute(
                        "INSERT INTO items(type,url,title,excerpt,status,created_at) "
                        "VALUES(?,?,?,?,'unread',?)",
                        (detect_type(url), url, title, "从浏览器书签导入",
                         datetime.now(TZ).isoformat(timespec="seconds")))
                    imported += 1
            conn.commit()
        finally:
            conn.close()
    return {"imported": imported, "skipped": skipped,
            "kind": "json" if is_json else "bookmarks"}


@app.get("/api/stats/overview")
def stats_overview(weeks: int = 53):
    """统计页：近 N 周（默认 53）每日收藏/回顾热力图 + 总量数字。"""
    weeks = min(max(weeks, 4), 53)
    today = datetime.now(TZ).date()
    conn = db()
    try:
        collects = {r["d"]: r["c"] for r in conn.execute(
            "SELECT substr(created_at,1,10) d, COUNT(*) c FROM items "
            "WHERE deleted_at IS NULL GROUP BY d").fetchall()}
        reviews = {r["d"]: r["c"] for r in conn.execute(
            "SELECT substr(last_seen,1,10) d, COUNT(*) c FROM review_state "
            "WHERE last_seen IS NOT NULL GROUP BY d").fetchall()}
        total = conn.execute(
            "SELECT COUNT(*) c FROM items WHERE deleted_at IS NULL").fetchone()["c"]
        read = conn.execute(
            "SELECT COUNT(*) c FROM items WHERE status='read' AND deleted_at IS NULL").fetchone()["c"]
        by_type = {r["type"]: r["c"] for r in conn.execute(
            "SELECT type, COUNT(*) c FROM items WHERE deleted_at IS NULL GROUP BY type").fetchall()}
        tag_rows = conn.execute(
            "SELECT tags FROM items WHERE tags != '' AND deleted_at IS NULL").fetchall()
        streak_days = {r["d"] for r in conn.execute(
            "SELECT DISTINCT substr(last_seen,1,10) d FROM review_state "
            "WHERE last_seen IS NOT NULL").fetchall()}
    finally:
        conn.close()
    tags = set()
    for r in tag_rows:
        tags.update(t for t in r["tags"].split(",") if t)
    streak = 0
    d = today
    if d.isoformat() not in streak_days:
        d -= timedelta(days=1)
    while d.isoformat() in streak_days:
        streak += 1
        d -= timedelta(days=1)
    monday = today - timedelta(days=today.weekday())
    start = monday - timedelta(weeks=weeks - 1)  # 从周一对齐往前数 N 周
    days = []
    cur = start
    while cur <= today:
        ds = cur.isoformat()
        days.append({"date": ds, "collects": collects.get(ds, 0),
                     "reviews": reviews.get(ds, 0)})
        cur += timedelta(days=1)
    return {"days": days, "total": total, "read": read, "by_type": by_type,
            "tags": len(tags), "streak": streak}


@app.get("/api/timeline/calendar")
def timeline_calendar(month: str = ""):
    """某月每天的记录数（收藏 / 已读 / 回顾分项），供月历标注。"""
    if not re.match(r"^\d{4}-\d{2}$", month or ""):
        month = datetime.now(TZ).strftime("%Y-%m")
    conn = db()
    try:
        days: dict = {}

        def bump(d, k):
            if not d:
                return
            cell = days.setdefault(d, {"collects": 0, "reads": 0, "reviews": 0})
            cell[k] += 1

        for r in conn.execute(
                "SELECT substr(created_at,1,10) d FROM items "
                "WHERE deleted_at IS NULL AND substr(created_at,1,7)=?", (month,)):
            bump(r["d"], "collects")
        for r in conn.execute(
                "SELECT substr(read_at,1,10) d FROM items "
                "WHERE read_at IS NOT NULL AND deleted_at IS NULL "
                "AND substr(read_at,1,7)=?", (month,)):
            bump(r["d"], "reads")
        for r in conn.execute(
                "SELECT substr(last_seen,1,10) d FROM review_state "
                "WHERE last_seen IS NOT NULL AND substr(last_seen,1,7)=?", (month,)):
            bump(r["d"], "reviews")
    finally:
        conn.close()
    return {"month": month, "days": days}


@app.get("/api/timeline")
def timeline_day(date: str = ""):
    """某天的记录流：收藏 / 已读 / 回顾三类事件合并，按时间倒序。"""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date or ""):
        date = datetime.now(TZ).strftime("%Y-%m-%d")
    conn = db()
    try:
        events = []
        for r in conn.execute(
                "SELECT id,type,title,author,excerpt,tags,note,created_at ts FROM items "
                "WHERE deleted_at IS NULL AND substr(created_at,1,10)=?", (date,)):
            d = row_dict(r); d["kind"] = "collect"; d["time"] = r["ts"]; events.append(d)
        for r in conn.execute(
                "SELECT i.id,i.type,i.title,i.author,i.excerpt,i.tags,i.note,i.read_at ts "
                "FROM items i WHERE i.read_at IS NOT NULL AND i.deleted_at IS NULL "
                "AND substr(i.read_at,1,10)=?", (date,)):
            d = row_dict(r); d["kind"] = "read"; d["time"] = r["ts"]; events.append(d)
        for r in conn.execute(
                "SELECT i.id,i.type,i.title,i.author,i.excerpt,i.tags,i.note,rs.last_seen ts "
                "FROM review_state rs JOIN items i ON i.id=rs.item_id "
                "WHERE rs.last_seen IS NOT NULL AND i.deleted_at IS NULL "
                "AND substr(rs.last_seen,1,10)=?", (date,)):
            d = row_dict(r); d["kind"] = "review"; d["time"] = r["ts"]; events.append(d)
    finally:
        conn.close()
    events.sort(key=lambda e: e["time"], reverse=True)
    return {"date": date, "events": events}


app.mount("/data", StaticFiles(directory=DATA), name="data")  # 必须先于 "/" 挂载，否则图片路由被吞
app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
