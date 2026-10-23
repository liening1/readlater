"""文章解析器：微信公众号专项 + 通用网页 og 标签兜底。

网络分流策略（针对国内网络环境）：
- 微信域（mp.weixin.qq.com / mmbiz.qpic.cn 等）直连，国内可达且不应走代理
- 其他外网域先尝试直连，失败后自动走代理兜底
- 代理优先级：READLATER_PROXY 环境变量 > 系统代理环境变量 > http://127.0.0.1:7897（Clash 默认混合端口）
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag

IMG_DIR = Path(__file__).parent / "data" / "images"
IMG_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

WEIXIN_HOSTS = ("mp.weixin.qq.com", "mmbiz.qpic.cn", "res.wx.qq.com", "mmbiz.qlogo.cn")
VIDEO_PATTERNS = (
    "bilibili.com", "b23.tv", "youtube.com", "youtu.be", "v.qq.com",
    "ixigua.com", "douyin.com", "kuaishou.com", "weishi.qq.com",
)


def proxy_url() -> str:
    return (os.environ.get("READLATER_PROXY")
            or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
            or "http://127.0.0.1:7897")


def _is_weixin(url: str) -> bool:
    return any(h in url for h in WEIXIN_HOSTS)


def _clients(headers: dict):
    """生成 (直连客户端, 代理兜底客户端)。"""
    direct = httpx.AsyncClient(timeout=25, follow_redirects=True,
                               headers=headers, trust_env=False)
    via_proxy = httpx.AsyncClient(timeout=25, follow_redirects=True,
                                  headers=headers, trust_env=False, proxy=proxy_url())
    return direct, via_proxy


async def fetch(url: str) -> httpx.Response:
    headers = dict(HEADERS)
    if "mp.weixin.qq.com" in url:
        headers["Referer"] = "https://mp.weixin.qq.com/"
    direct, via_proxy = _clients(headers)
    if _is_weixin(url):
        async with direct:
            return await direct.get(url)
    try:
        async with direct:
            return await direct.get(url)
    except Exception:
        async with via_proxy:
            return await via_proxy.get(url)


def detect_type(url: str, content_type: str = "") -> str:
    if "mp.weixin.qq.com" in url:
        return "article"
    if content_type.startswith("image/") or re.search(
            r"\.(jpe?g|png|gif|webp|avif)(\?|$)", url, re.I):
        return "image"
    if any(p in url for p in VIDEO_PATTERNS):
        return "video"
    return "web"


def _meta(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find(
        "meta", attrs={"name": prop})
    return (tag.get("content") or "").strip() if tag else ""


def _clean_title(raw: str) -> str:
    return re.sub(r"\s+", " ", raw or "").strip()[:300]


# ---------- 无头浏览器兜底（JS 挑战页 / SPA） ----------
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)
CHALLENGE_MARKERS = ("Client Challenge", "_fs-ch", "cf-challenge", "Just a moment...",
                     "Attention Required", "Enable JavaScript and cookies to continue")


def chrome_path() -> str | None:
    p = os.environ.get("READLATER_CHROME")
    if p and Path(p).exists():
        return p
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    return shutil.which("google-chrome") or shutil.which("chromium")


def is_challenge(html: str) -> bool:
    """识别 JS 机器人挑战/空壳页：内容极少且出现挑战标记。"""
    if not html:
        return True
    return len(html) < 40000 and any(m in html for m in CHALLENGE_MARKERS)


def render_html(url: str, budget_ms: int = 15000, timeout: int = 40) -> str | None:
    """用无头 Chrome 执行 JS 后取 DOM。

    关键点：必须伪装成真实浏览器 UA——headless 默认 UA 含 "HeadlessChrome"，
    Fastly/Cloudflare 一类挑战会直接拒绝，页面永远停在挑战壳上。
    """
    exe = chrome_path()
    if not exe:
        return None
    profile = tempfile.mkdtemp(prefix="rl-chrome-")
    try:
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-sandbox",
               f"--user-data-dir={profile}", f"--user-agent={HEADERS['User-Agent']}",
               f"--virtual-time-budget={budget_ms}", f"--timeout={budget_ms + 5000}",
               "--dump-dom", url]
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
        html = out.stdout.decode("utf-8", "ignore")
        return html if len(html) > 2000 else None
    except Exception:
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)


async def render_html_async(url: str) -> str | None:
    return await asyncio.to_thread(render_html, url)


# ---------- PDF 正文抽取（工作论文 / 报告类页面） ----------
def find_pdf_link(html: str, base: str) -> str:
    m = re.search(r'href="([^"]+\.pdf[^"]*)"', html or "", re.I)
    if not m:
        return ""
    href = m.group(1)
    if href.startswith("//"):
        return "https:" + href
    if not href.startswith("http"):
        from urllib.parse import urljoin
        return urljoin(base, href)
    return href


async def pdf_text(url: str, max_pages: int = 12, max_bytes: int = 25_000_000) -> str:
    try:
        from io import BytesIO

        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        resp = await fetch(url)
        if len(resp.content) > max_bytes:
            return ""
        reader = PdfReader(BytesIO(resp.content))
        chunks: list[str] = []
        for page in reader.pages[:max_pages]:
            chunks.append(page.extract_text() or "")
        text = re.sub(r"[ \t]+", " ", "\n".join(chunks))
        return re.sub(r"\n{2,}", "\n", text).strip()
    except Exception:
        return ""


async def enrich_with_pdf(html: str, url: str, content_html: str, quality: str) -> tuple[str, str]:
    """落地页正文太短时，找页面上的 PDF 并抽取正文追加到内容里。"""
    if len(content_html) >= 4000:
        return content_html, quality
    pdf_url = find_pdf_link(html, url)
    if not pdf_url:
        return content_html, quality
    text = await pdf_text(pdf_url)
    if not text or len(text) < 1200:
        return content_html, quality
    paras = [p.strip() for p in re.split(r"\n+", text) if len(p.strip()) > 40][:160]
    body = "".join(f"<p>{p}</p>" for p in paras)
    out = (content_html or "") + \
        '<h2>PDF 正文（自动抽取）</h2>' + body + \
        f'<p><a href="{pdf_url}" target="_blank" rel="noopener">查看原始 PDF ↗</a></p>'
    return out, "full"


# ---------- 元数据抽取（JSON-LD / meta 多路兜底） ----------
def _jsonld_nodes(soup: BeautifulSoup) -> list:
    nodes: list = []
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                nodes.append(d)
                for k in ("@graph", "mainEntity", "itemListElement"):
                    if isinstance(d.get(k), list):
                        stack.extend(d[k])
                    elif isinstance(d.get(k), dict):
                        stack.append(d[k])
            elif isinstance(d, list):
                stack.extend(d)
    return nodes


def _ld_first(nodes: list, keys: tuple) -> str:
    for n in nodes:
        for k in keys:
            v = n.get(k) if isinstance(n, dict) else None
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict) and isinstance(v.get("name"), str):
                return v["name"].strip()
            if isinstance(v, list) and v:
                first = v[0]
                if isinstance(first, str):
                    return first.strip()
                if isinstance(first, dict) and isinstance(first.get("name"), str):
                    return first["name"].strip()
    return ""


def _ld_image(nodes: list) -> str:
    for n in nodes:
        img = n.get("image") if isinstance(n, dict) else None
        if isinstance(img, str):
            return img
        if isinstance(img, dict) and isinstance(img.get("url"), str):
            return img["url"]
        if isinstance(img, list) and img:
            first = img[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict) and isinstance(first.get("url"), str):
                return first["url"]
    return ""


def _tidy_title(raw: str) -> str:
    """去掉站点后缀：取最长片段（"标题 | 栏目 | 站点" → "标题"）。"""
    raw = _clean_title(raw)
    if not raw:
        return ""
    parts = [p.strip() for p in re.split(r"\s+[|｜·–—]\s+|\s+-\s+", raw) if p.strip()]
    if len(parts) <= 1:
        return raw
    longest = max(parts, key=len)
    return longest if len(longest) >= 12 else parts[0]


def _pick_main(soup: BeautifulSoup) -> Tag:
    for sel in ("article", "main", "[role=main]",
                ".article-body", ".article-content", ".post-content", ".entry-content",
                ".content-body", "#content", ".content"):
        node = soup.select_one(sel)
        if node is not None and len(node.get_text(" ", strip=True)) > 200:
            return node
    return soup.body or soup


BAD_IMG_HINTS = ("icon", "logo", "sprite", "arrow", "search", "ellipse", "favicon",
                 "placeholder", "spinner", "loading", "avatar", "flag", "social",
                 "divider", "bullet", "badge")
GOOD_IMG_HINTS = ("hero", "banner", "cover", "featured", "single-image", "lead",
                  "teaser", "thumbnail", "card-image")


def _abs_url(base: str, src: str) -> str:
    """把 //host/x、/path、相对路径统一成绝对 URL。"""
    src = (src or "").strip()
    if not src or src.startswith("data:") or src.startswith("javascript:"):
        return ""
    if src.startswith("//"):
        return "https:" + src
    if src.lower().startswith("http"):
        return src
    from urllib.parse import urljoin
    return urljoin(base, src)


def _img_src(img: Tag) -> str:
    for attr in ("src", "data-src", "data-original", "data-lazy-src",
                 "data-srcset", "srcset"):
        v = img.get(attr)
        if v:
            return v.split(",")[0].strip().split(" ")[0]
    parent = img.parent
    if parent is not None and parent.name == "picture":
        source = parent.find("source")
        if source is not None:
            v = source.get("srcset") or source.get("data-srcset") or ""
            if v:
                return v.split(",")[0].strip().split(" ")[0]
    return ""


def _is_bad_image(url: str) -> bool:
    low = url.lower()
    if low.endswith(".svg") or low.endswith(".gif"):
        return True
    return any(h in low for h in BAD_IMG_HINTS)


def pick_cover(soup: BeautifulSoup, base: str, existing: str = "") -> str:
    """封面兜底：og/JSON-LD 没有时，从正文里挑最像"主图"的一张。

    关键修正：
    - 相对路径 / 协议相对路径（//cdn/x.jpg）要绝对化，否则前端加载不出来
    - 跳过图标类图片（第一张往往是 search.svg）
    - 优先 class/id 带 hero/banner/cover/featured 的图
    """
    if existing:
        fixed = _abs_url(base, existing)
        if fixed:
            return fixed
    best = ""
    for scope in (_pick_main(soup), soup):
        for img in scope.find_all("img"):
            src = _abs_url(base, _img_src(img))
            if not src or _is_bad_image(src):
                continue
            hint = (" ".join(img.get("class") or []) + " " + (img.get("id") or "")).lower()
            if any(k in hint for k in GOOD_IMG_HINTS):
                return src
            w = str(img.get("width") or "").strip()
            if w.isdigit() and int(w) >= 500:
                return src
            if not best:
                best = src
        if best:
            return best
    return best


def _blockify(main: Tag, soup: BeautifulSoup, base: str = "") -> tuple[str, str]:
    """把主体转成干净的段落 HTML（含正文图片），并返回纯文本。"""
    for t in main.find_all(["script", "style", "link", "iframe", "form", "button", "nav",
                            "aside", "footer", "noscript", "svg"]):
        t.decompose()
    blocks: list[str] = []
    for el in main.find_all(["h1", "h2", "h3", "p", "li", "blockquote", "figcaption", "img"]):
        if el.name == "img":
            src = _abs_url(base, _img_src(el))
            if not src or _is_bad_image(src):
                continue
            if len(blocks) >= 120:
                continue
            blocks.append(f'<img src="{src}" loading="lazy" alt="">')
            continue
        txt = el.get_text(" ", strip=True)
        if len(txt) < 20:
            continue
        tag = el.name if el.name in ("h2", "h3", "blockquote") else "p"
        blocks.append(f"<{tag}>{txt}</{tag}>")
        if len(blocks) >= 160:
            break
    html = "".join(blocks)
    text = "\n".join(BeautifulSoup(b, "html.parser").get_text(" ", strip=True)
                     for b in blocks if not b.startswith("<img"))
    return html, text


def _quality(text: str) -> str:
    n = len(text.strip())
    return "full" if n >= 800 else ("partial" if n >= 200 else "link_only")


async def _save_image(img_url: str, item_key: str, idx: int) -> str | None:
    """下载图片到本地（微信域直连，其余先直连后代理兜底），失败返回 None。"""
    headers = {"User-Agent": HEADERS["User-Agent"]}
    if "mmbiz.qpic.cn" in img_url:
        headers["Referer"] = "https://mp.weixin.qq.com/"
    direct, via_proxy = _clients(headers)
    try:
        try:
            async with direct:
                resp = await direct.get(img_url)
        except Exception:
            async with via_proxy:
                resp = await via_proxy.get(img_url)
        resp.raise_for_status()
        if not resp.content:
            return None
        ext = ".jpg"
        m = re.search(r"image/(\w+)", resp.headers.get("content-type", ""))
        if m:
            ext = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}.get(
                m.group(1).lower(), ".jpg")
        fname = f"{item_key}_{idx}{ext}"
        (IMG_DIR / fname).write_bytes(resp.content)
        return f"/data/images/{fname}"
    except Exception:
        return None


def _sanitize(node: Tag) -> Tag:
    for t in node.find_all(["script", "style", "link", "iframe", "form", "button"]):
        t.decompose()
    return node


async def parse_wechat(html: str, url: str, item_key: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = _clean_title(_meta(soup, "og:title")
                         or (soup.find(id="activity-name").get_text() if soup.find(id="activity-name") else ""))
    author = _clean_title(_meta(soup, "og:article:author")
                          or (soup.find(id="js_name").get_text() if soup.find(id="js_name") else "")
                          or "微信公众号")
    cover = _meta(soup, "og:image") or _meta(soup, "msg_cdn_url")
    published = ""
    em = soup.find(id="publish_time")
    if em:
        published = em.get_text(strip=True)
    else:
        m = re.search(r'var\s+ct\s*=\s*"(\d{10})"', html)
        if m:
            published = m.group(1)

    body = soup.find(id="js_content")
    excerpt, content_html = "", ""
    if body:
        _sanitize(body)
        for i, img in enumerate(body.find_all("img")):
            src = img.get("data-src") or img.get("src") or ""
            if not src.startswith("http"):
                img.decompose()
                continue
            local = await _save_image(src, item_key, i)
            new = soup.new_tag("img")
            new["src"] = local if local else src
            new["loading"] = "lazy"
            img.replace_with(new)
        content_html = str(body)
        text = BeautifulSoup(content_html, "html.parser").get_text(" ", strip=True)
        excerpt = text[:160]
    else:
        text = soup.get_text(" ", strip=True)
        excerpt = text[:160]
    return {"title": title or url[:80], "author": author, "cover": cover,
            "published": published, "content_html": content_html, "excerpt": excerpt,
            "quality": _quality(text)}


async def parse_generic(html: str, url: str) -> dict:
    """通用网页解析：JSON-LD / og / twitter / 正文启发式多路兜底，并给出质量等级。"""
    soup = BeautifulSoup(html, "html.parser")
    ld = _jsonld_nodes(soup)

    title_raw = (_meta(soup, "og:title") or _meta(soup, "twitter:title")
                 or _ld_first(ld, ("headline", "name"))
                 or (soup.title.get_text() if soup.title else ""))
    title = _tidy_title(title_raw) or url[:80]
    if title.strip().lower() in ("client challenge", "just a moment...", ""):
        title = url[:80]

    cover = (_meta(soup, "og:image") or _meta(soup, "og:image:secure_url")
             or _meta(soup, "twitter:image") or _ld_image(ld))
    cover = pick_cover(soup, url, cover)
    desc = _clean_title(_meta(soup, "og:description") or _meta(soup, "description")
                        or _meta(soup, "twitter:description")
                        or _ld_first(ld, ("description", "abstract")))
    author = (_clean_title(_meta(soup, "author") or _meta(soup, "article:author"))
              or _ld_first(ld, ("author", "creator"))
              or _clean_title(_meta(soup, "og:site_name")))
    published = (_meta(soup, "article:published_time") or _meta(soup, "datePublished")
                 or _ld_first(ld, ("datePublished", "dateCreated")))
    if not published:
        t = soup.find("time")
        if t is not None and t.get("datetime"):
            published = t["datetime"]
    published = (published or "")[:19].replace("T", " ")

    if not cover:  # 最后兜底：正文里第一张够大的图
        for img in soup.find_all("img"):
            src = img.get("src") or ""
            w = str(img.get("width") or "")
            if src.startswith("http") and ((w.isdigit() and int(w) >= 500) or "large" in src):
                cover = src
                break

    main = _pick_main(soup)
    content_html, text = _blockify(main, soup, url)
    quality = _quality(text)
    if not desc and text:
        desc = text[:160].replace("\n", " ")
    return {"title": title, "author": author, "cover": cover,
            "published": published, "content_html": content_html,
            "excerpt": desc[:300], "quality": quality}


async def parse_video(url: str) -> dict:
    """视频链接只存元信息，不强求内容。"""
    try:
        resp = await fetch(url)
        return await parse_generic(resp.text, url)
    except Exception:
        return {"title": url[:80], "author": "", "cover": "",
                "published": "", "content_html": "",
                "excerpt": "视频链接（未抓取到元信息）", "quality": "link_only"}
