"""文章解析器：微信公众号专项 + 通用网页 og 标签兜底。

网络分流策略（针对国内网络环境）：
- 微信域（mp.weixin.qq.com / mmbiz.qpic.cn 等）直连，国内可达且不应走代理
- 其他外网域先尝试直连，失败后自动走代理兜底
- 代理优先级：READLATER_PROXY 环境变量 > 系统代理环境变量 > http://127.0.0.1:7897（Clash 默认混合端口）
"""
import os
import re
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
            "published": published, "content_html": content_html, "excerpt": excerpt}


async def parse_generic(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = _clean_title(_meta(soup, "og:title") or (soup.title.get_text() if soup.title else ""))
    cover = _meta(soup, "og:image")
    desc = _clean_title(_meta(soup, "og:description") or _meta(soup, "description"))
    _sanitize(soup)
    main = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in main.find_all(["p", "li", "h2", "h3"])]
    paras = [p for p in paras if len(p) > 20][:120]
    text = "\n".join(paras)[:12000]
    content_html = "".join(f"<p>{p}</p>" for p in paras[:80])
    excerpt = desc or text[:160]
    return {"title": title or url[:80], "author": _clean_title(_meta(soup, "og:site_name")),
            "cover": cover, "published": "", "content_html": content_html, "excerpt": excerpt}


async def parse_video(url: str) -> dict:
    """视频链接只存元信息，不强求内容。"""
    try:
        resp = await fetch(url)
        return await parse_generic(resp.text, url)
    except Exception:
        return {"title": url[:80], "author": "", "cover": "",
                "published": "", "content_html": "", "excerpt": "视频链接（未抓取到元信息）"}
