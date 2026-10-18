# -*- coding: utf-8 -*-
"""
每日科技新闻自动收集与邮件推送
=====================================
- 纯 Python 标准库实现，无需安装任何第三方依赖
- 从多个中英文 RSS 源抓取 AI / 科学 / 医疗 / 科技行业新闻
- 按时间窗口筛选最新条目，生成 HTML 邮件并发送
- 适合配合 Windows 任务计划程序实现每天 08:00 自动运行

用法:
    python news_collector.py              # 正常抓取并发送邮件
    python news_collector.py --dry-run    # 只抓取并打印，不发送邮件
    python news_collector.py --config my.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import threading
import html
import json
import hashlib
import hmac
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import smtplib
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parsedate_to_datetime

# 项目根目录（脚本所在目录）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")
LOG_FILE = os.path.join(BASE_DIR, "news_collector.log")

# 让 Windows 控制台以 UTF-8 输出，避免中文乱码（失败时静默忽略）
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 NewsCollector/1.0"
)

# --------------------------------------------------------------------------- #
# 强制优先 IPv4
# --------------------------------------------------------------------------- #
# 本机到部分站点（如 Cloudflare 托管的 zeli.app）的 IPv6 握手会长时间挂起，
# 而 IPv4 正常。Python 在 TLS 阶段不会自动回退到 IPv4，这里直接过滤掉 IPv6。
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_v4(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
    infos = _orig_getaddrinfo(host, port, family, type, proto, flags)
    v4 = [item for item in infos if item[0] == socket.AF_INET]
    return v4 or infos


socket.getaddrinfo = _getaddrinfo_v4

# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # 轮转：单文件上限 2MB，保留 3 份，避免长期运行把日志撑到几百 MB
        RotatingFileHandler(
            LOG_FILE,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("news")

# 提供一个兜底配置，即便没有 config.json 也能跑起来（但发不了邮件）
FALLBACK_CONFIG = {
    "email": {
        "smtp_host": "smtp-mail.outlook.com",
        "smtp_port": 587,
        "use_tls": True,
        "use_ssl": False,
        "sender": "",
        "password": "",
        "sender_name": "每日科技新闻",
        "recipients": [],
    },
    "settings": {
        "hours_lookback": 24,
        "max_per_category": 8,
        "max_per_category_by": {},
        "subject_prefix": "【每日科技新闻】",
        "request_timeout": 20,
        "translate": True,
        "translate_target": "zh",
        "translate_delay": 0.25,
        "translate_workers": 4,
        "translate_summary": True,
        "translate_max_requests": 80,
        "fetch_og_image": True,
        "og_image_delay": 0.2,
        "og_image_limit": 40,
        "og_image_workers": 6,
        "fetch_workers": 6,
        "send_after_hour": 8,
        "send_retries": 3,
        "send_retry_delay": 15,
        "github_trending": True,
        "github_count": 20,
        "github_since_days": 7,
        "github_min_stars": 100,
        "github_lookback_days": 60,
        "github_search_pages": 3,
        "github_pool_max": 600,
    },
    "zeli": {
        "enabled": True,
        "rss": "https://zeli.app/zh/rss.xml",
        "limit": 15,
        "timeout": 60,
        "retries": 3,
    },
    "wuzao": {
        "enabled": False,
        "period": "daily",
        "limit": 30,
        "timeout": 30,
        "retries": 2,
    },
    "archive": {
        "enabled": True,
        "dir": os.path.join(BASE_DIR, "history"),
    },
    "alert": {
        "enabled": True,
        "desktop_copy": True,
    },
    "sources": [],
}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
# ${NAME} 或 ${NAME:-默认值}，用于把密钥放在环境变量 / CI secrets 而不是配置文件里
_ENV_PLACEHOLDER_RE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}"
)


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个字典，override 优先。"""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_dotenv(path: str) -> None:
    """读取同目录 .env，注入 os.environ。

    只填空位（不覆盖已有环境变量），所以 GitHub Actions 注入的 secrets 优先。
    文件不存在或格式异常都不影响运行——CI 环境本来就没有 .env。
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                name = name.strip()
                if name and not os.environ.get(name):
                    os.environ[name] = value.strip().strip('"').strip("'")
    except OSError as exc:
        log.debug("读取 .env 失败：%s", exc)


def _expand_env(value):
    """把字符串里的 ${VAR} / ${VAR:-default} 替换为环境变量值。

    递归处理 dict / list，让整个配置文件都可以写占位符，密钥不再落盘：
        "password": "${SMTP_PASSWORD}"
        "sender":   "${SMTP_SENDER:-nobody@example.com}"
    未定义且没有默认值时保留原样（便于启动时给出"配置不完整"的明确报错）。
    """
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if not isinstance(value, str):
        return value

    def _sub(m):
        name, default = m.group(1), m.group(2)
        got = os.environ.get(name, "").strip()
        if got:
            return got
        if default is not None:
            return default
        log.debug("环境变量 %s 未设置，保留占位符原文。", name)
        return m.group(0)

    return _ENV_PLACEHOLDER_RE.sub(_sub, value)


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        log.warning("未找到配置文件 %s，使用内置默认配置。", path)
        return dict(FALLBACK_CONFIG)
    _load_dotenv(os.path.join(os.path.dirname(os.path.abspath(path)), ".env"))
    with open(path, "r", encoding="utf-8") as fh:
        user_cfg = json.load(fh)
    merged = deep_merge(FALLBACK_CONFIG, user_cfg)
    return _expand_env(merged)


def resolve_config_path(cli_path: str | None) -> str:
    if cli_path:
        return cli_path if os.path.isabs(cli_path) else os.path.join(BASE_DIR, cli_path)
    return DEFAULT_CONFIG


# --------------------------------------------------------------------------- #
# RSS / Atom 解析
# --------------------------------------------------------------------------- #
def _strip_ns(tag: str) -> str:
    """去掉 XML 命名空间前缀，例如 {http://...}entry -> entry。"""
    return tag.rsplit("}", 1)[-1].lower()


def _first_text(node: ET.Element, names: tuple[str, ...]) -> str:
    """在直接子节点中查找第一个匹配标签的文本（支持嵌套子元素/CDATA）。"""
    for child in node:
        if _strip_ns(child.tag) in names:
            # 有些源的 title 里包着 <a> 等子节点，用 itertext 汇总文本
            text = "".join(child.itertext()).strip()
            if text:
                return text
            return (child.text or "").strip()
    return ""


def _extract_link(node: ET.Element) -> str:
    """取条目正文链接。

    RSS 2.0 直接给 <link>文本</link>；Atom 则用 <link rel="alternate" href="...">。
    关键点：Atom 的 <link rel="self"> 指向 feed 自身，不是文章页，早期版本按
    「第一个 link」取会把文章链接变成 feed 地址（The Verge / TechCrunch 等
    Atom 源都会中招），所以这里按 rel 优先级筛选。
    """
    # 1) RSS 风格：<link>文本</link>
    link = _first_text(node, ("link",))
    if link:
        return link

    # 2) Atom 风格：按 rel 优先级挑选 href
    best: str = ""
    fallback: str = ""
    for child in node:
        if _strip_ns(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if not href:
            continue
        rel = (child.get("rel") or "alternate").strip().lower()
        if rel == "alternate":
            # alternate 是正文链接，直接采用；若出现多个则取第一个
            if not best:
                best = href
        elif rel not in ("self", "enclosure", "hub", "replies", "edit"):
            # 其它 rel 作为兜底（例如某些源用 rel="related"）
            if not fallback:
                fallback = href
    return best or fallback


def _parse_date(raw: str) -> datetime | None:
    """尽力解析 RFC822 或 ISO8601 时间，统一返回带时区的 datetime。"""
    if not raw:
        return None
    text = raw.strip()

    # 优先尝试 RFC822（RSS 常见）
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except (TypeError, ValueError, IndexError):
        pass

    # 再尝试 ISO8601（Atom 常见）
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass

    # 最后尝试一些常见的自定义格式（如 "Oct 16, 2026 2:25pm"）
    for fmt in ("%b %d, %Y %I:%M%p", "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M%p",
                "%b %d, %Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _sanitize_xml(raw_bytes: bytes) -> bytes:
    """修复常见的不规范 XML：非法实体、未转义的 & 和控制字符。"""
    # 尽量按 UTF-8 解码，失败则忽略非法字节
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = raw_bytes.decode("utf-8", errors="ignore")

    # 去掉 BOM 和 XML 不支持的非法控制字符
    text = text.lstrip("\ufeff")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    # 保护 CDATA 区间，只替换其外部的裸 &
    # （CDATA 内的 & 是字面量，再转义一次会让摘要出现字面的 "&amp;"）
    cdata_re = re.compile(r"<!\[CDATA\[.*?\]\]>", re.S)
    bare_amp_re = re.compile(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);)")
    parts: list[str] = []
    last = 0
    for match in cdata_re.finditer(text):
        parts.append(bare_amp_re.sub("&amp;", text[last:match.start()]))
        parts.append(match.group(0))  # CDATA 原样保留
        last = match.end()
    parts.append(bare_amp_re.sub("&amp;", text[last:]))
    return "".join(parts).encode("utf-8")


def _looks_image(url: str) -> bool:
    return bool(re.search(r"\.(jpe?g|png|webp|gif|avif)(\?|$)", url, re.I))


def _normalize_url(url: str, base: str = "") -> str:
    """处理协议相对(//)、相对路径、HTML 实体。"""
    url = html.unescape(url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.lower().startswith(("http://", "https://")):
        return url
    if url.startswith("data:"):
        return ""
    if base:
        return urllib.parse.urljoin(base, url)
    return url


def _extract_image(node: ET.Element) -> str:
    """从条目中尽力提取一张配图 URL。"""
    # 1) enclosure / media:content / media:thumbnail / itunes:image
    for child in node:
        tag = _strip_ns(child.tag)
        if tag in ("enclosure", "content", "thumbnail"):
            url = child.get("url") or child.get("href") or ""
            typ = (child.get("type") or "").lower()
            medium = (child.get("medium") or "").lower()
            if url and (typ.startswith("image") or medium == "image" or _looks_image(url)):
                return _normalize_url(url)
        elif tag == "image":
            url = child.get("href") or child.get("url") or ""
            if url:
                return _normalize_url(url)
    # 2) 从正文 HTML 里找第一张 <img>
    for child in node:
        if _strip_ns(child.tag) in ("description", "summary", "content", "encoded"):
            text = "".join(child.itertext())
            match = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", text, re.I)
            if match:
                return _normalize_url(match.group(1))
    return ""


def fetch_og_image(url: str, timeout: int) -> str:
    """抓取文章页面的 og:image / twitter:image，失败返回空串。"""
    if not url:
        return ""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read(300000)  # og 标签通常在 <head>，读前 300KB 足够
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return ""
    text = raw.decode("utf-8", "ignore")
    patterns = (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image(?::src)?["\']',
        r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)["\']',
    )
    for pat in patterns:
        match = re.search(pat, text, re.I)
        if match:
            return _normalize_url(match.group(1), base=url)
    return ""


def enrich_images(grouped: dict[str, list[dict]], config: dict) -> None:
    """为没有配图的条目抓取 og:image 兜底（原地修改）。

    串行版本实测要 28 秒（占整轮耗时近六成），这里改为线程池并发：
    fetch_og_image 是纯网络 IO、各条目互不干扰，并发安全。
    """
    settings = config["settings"]
    if not settings.get("fetch_og_image", True):
        return
    timeout = int(settings.get("request_timeout", 20))
    delay = float(settings.get("og_image_delay", 0.2))
    limit = int(settings.get("og_image_limit", 40))
    workers = max(1, int(settings.get("og_image_workers", 6)))
    import time as _time

    missing = [it for items in grouped.values() for it in items if not it.get("image")]

    # limit 计的是「发起的请求数」。每个待抓条目恰好对应一次请求，
    # 所以在提交前截断即可精确控制请求总数，不会像早期版本那样超发。
    truncated = 0
    if len(missing) > limit:
        truncated = len(missing) - limit
        missing = missing[:limit]
        log.info("og:image 待抓 %d 条，超过上限 %d，已截断 %d 条。",
                 len(missing) + truncated, limit, truncated)

    def fetch_one(item: dict) -> str:
        try:
            img = fetch_og_image(item.get("link", ""), timeout)
        except Exception as exc:  # noqa: BLE001
            # 单个条目失败不该拖垮整批；fetch_og_image 本身已处理常见网络异常，
            # 这里兜住意外类型（如正则超时、编码错误等）
            log.warning("og:image 抓取异常 (%s): %s", item.get("link", "")[:80], exc)
            img = ""
        # 间隔改到每个 worker 内部：既保持对同一站点的礼貌，
        # 又不会像串行那样让整个流程等待 N 倍的 delay。
        if delay > 0:
            _time.sleep(delay)
        return img

    results: list[str] = []
    if workers > 1 and len(missing) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            # map 保持输入顺序，返回逐条对应的结果
            results = list(pool.map(fetch_one, missing))
    else:
        results = [fetch_one(it) for it in missing]

    done = 0
    for item, img in zip(missing, results):
        if img:
            item["image"] = img
            done += 1

    total = sum(len(v) for v in grouped.values())
    have = sum(1 for items in grouped.values() for it in items if it.get("image"))
    log.info("配图补充完成：%d/%d 条有配图（发起 %d 次请求，成功 %d 条，并发 %d）。",
             have, total, len(missing), done, workers)


def parse_feed(raw_bytes: bytes) -> list[dict]:
    """解析 RSS 2.0 / Atom feed，返回条目列表。"""
    items: list[dict] = []
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError:
        # 尝试修复后再解析
        try:
            root = ET.fromstring(_sanitize_xml(raw_bytes))
        except ET.ParseError as exc:
            log.warning("XML 解析失败: %s", exc)
            return items

    root_tag = _strip_ns(root.tag)
    # 找到所有 item（RSS）或 entry（Atom）
    entries = [n for n in root.iter() if _strip_ns(n.tag) in ("item", "entry")]
    if not entries and root_tag in ("item", "entry"):
        entries = [root]

    for node in entries:
        title = _first_text(node, ("title",))
        link = _extract_link(node)
        date_raw = _first_text(node, ("pubdate", "published", "updated", "date"))
        summary = _first_text(node, ("description", "summary", "content"))
        summary = _clean_summary(summary)

        if not title:
            continue
        # 标题里的 hashtag（#piday #raspberrypi）是发布标签，不是标题内容
        title = _strip_hashtags(title) or title
        items.append(
            {
                "title": title,
                "link": link,
                "published": _parse_date(date_raw),
                "summary": summary,
                "image": _extract_image(node),
            }
        )
    return items


# 部分中文源（如 InfoQ）的 description 只有一句「点击查看原文」之类的链接文案，
# 当成摘要显示毫无意义，直接丢弃。
_JUNK_SUMMARY_RE = re.compile(
    r"^(?:点击(?:查看|阅读)(?:原文|全文|详情)|阅读原文|阅读全文|查看原文|查看全文|"
    r"查看更多|更多内容|详见原文|原文链接|read\s+more|continue\s+reading|"
    r"view\s+(?:original|full)\s*(?:article|post)?)[\s。.!！…]*$",
    re.I,
)

# 很多源（Hackaday、TechCrunch…）的 description 以 <a>Read more</a> 收尾，
# 去标签后会留下一个孤零零的「…阅读更多」。截断后它就卡在末尾，很扎眼。
_TRAILING_MORE_RE = re.compile(
    r"(?:[…\.。\s]*)(?:read\s+more|continue\s+reading|阅读更多|阅读全文|"
    r"查看全文|查看更多|继续阅读)\s*[»›>…\.。]*\s*$",
    re.I,
)

# 标题/摘要里的 hashtag（#piday #raspberrypi）属于发布标签，不是内容。
_HASHTAG_RE = re.compile(r"(?:^|\s)#[A-Za-z0-9_]{2,}\b")


def _strip_trailing_more(text: str) -> str:
    """去掉摘要末尾的「阅读更多 / Read more」残留。"""
    if not text:
        return ""
    cleaned = _TRAILING_MORE_RE.sub("", text).strip()
    return cleaned


def _strip_hashtags(text: str) -> str:
    """去掉 hashtag 标签，并清理因此产生的多余空格。"""
    if not text or "#" not in text:
        return text
    cleaned = _HASHTAG_RE.sub("", text)
    return " ".join(cleaned.split()).strip()


def _clean_summary(text: str, limit: int = 220) -> str:
    """去 HTML 标签、压缩空白、去掉尾部噪音、截断；纯链接文案返回空串。"""
    if not text:
        return ""
    # 简易去标签
    result = []
    inside = False
    for ch in text:
        if ch == "<":
            inside = True
        elif ch == ">":
            inside = False
        elif not inside:
            result.append(ch)
    clean = html.unescape("".join(result))
    clean = " ".join(clean.split())
    # 只有「点击查看原文」这类占位文案时不当作摘要
    if _JUNK_SUMMARY_RE.match(clean):
        return ""
    clean = _strip_trailing_more(clean)
    clean = _strip_hashtags(clean)
    if not clean:
        return ""
    if len(clean) > limit:
        clean = clean[:limit].rstrip() + "…"
    return clean


def fetch_source(source: dict, timeout: int) -> list[dict]:
    """抓取单个 RSS 源，失败时返回空列表。"""
    name = source.get("name", "?")
    url = source.get("url", "")
    if not url:
        return []
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        log.warning("抓取失败 [%s] %s -> %s", name, url, exc)
        return []

    items = parse_feed(raw)
    for it in items:
        it["source"] = name
        it["category"] = source.get("category", "其他")
    log.info("抓取成功 [%s] %d 条", name, len(items))
    return items


# --------------------------------------------------------------------------- #
# GitHub 开源项目：真实 star 增速榜
# --------------------------------------------------------------------------- #
GH_STARS_FILE = os.path.join(BASE_DIR, "github_stars.json")


def _load_gh_stars() -> dict:
    """读取历史 star 快照：{ "owner/repo": {"YYYY-MM-DD": stars} }"""
    if os.path.exists(GH_STARS_FILE):
        try:
            with open(GH_STARS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (ValueError, OSError):
            pass
    return {}


def _save_gh_stars(data: dict) -> None:
    try:
        with open(GH_STARS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True)
    except OSError as exc:
        log.warning("写入 star 快照失败: %s", exc)


def _github_search(
    query: str,
    token: str,
    timeout: int,
    per_page: int = 100,
    max_pages: int = 1,
    sort: str = "stars",
) -> list[dict]:
    """调用 GitHub Search API，支持翻页。

    GitHub 搜索接口单次最多返回 100 条、总结果上限 1000 条，所以翻页超过
    10 页没有意义。max_pages=1 即为旧行为（只看第一页）。

    sort 可选 "stars"（偏向大项目）或 "updated"（偏向近期活跃、包括低星
    新项目）。增速榜想抓的是"正在起飞"的项目，只按 stars 排序会让低星新项目
    永远进不了候选池，因此两种排序都要跑。
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    per_page = max(1, min(int(per_page), 100))
    max_pages = max(1, int(max_pages))
    collected: list[dict] = []
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        params = urllib.parse.urlencode(
            {
                "q": query,
                "sort": sort,
                "order": "desc",
                "per_page": per_page,
                "page": page,
            }
        )
        url = "https://api.github.com/search/repositories?" + params
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
            # 翻页途中失败不影响已取到的结果
            if not collected:
                log.warning("GitHub 搜索失败 (%s, sort=%s): %s", query, sort, exc)
            else:
                log.warning("GitHub 搜索第 %d 页失败（已取得 %d 条）: %s", page, len(collected), exc)
            break

        items = data.get("items") or []
        if not items:
            break

        for repo in items:
            full = repo.get("full_name", "")
            if full and full not in seen:
                seen.add(full)
                collected.append(repo)

        # 终止条件按"已翻过的位置"判断，而不是已去重的条数：
        # GitHub Search 的 total_count 上限是 1000，且相邻页可能有重复项，
        # 用去重后的 len(collected) 比较会永远达不到上限而白翻到 max_pages。
        if len(items) < per_page:
            break                      # 本页不满，没有下一页
        fetched = page * per_page
        total_count = data.get("total_count")
        if isinstance(total_count, int) and fetched >= min(total_count, 1000):
            break                      # 已翻完 API 允许的全部结果

    return collected


def fetch_github_trending(config: dict) -> list[dict]:
    """「7 日 star 增速榜」：每天记录 star 数，用真实增量（而非总 star）排名。

    第 1 天没有历史数据时，用「近 7 天新建项目的累计 star」兜底；
    随着快照积累，会自动过渡为真实的跨天增量榜。
    """
    settings = config["settings"]
    if not settings.get("github_trending", True):
        return []
    count = int(settings.get("github_count", 20))
    window = int(settings.get("github_since_days", 7))        # 统计窗口（天）
    min_stars = int(settings.get("github_min_stars", 100))
    lookback = int(settings.get("github_lookback_days", 60))  # 候选池：近 N 天新建
    timeout = int(settings.get("request_timeout", 20))
    # 翻页数与池子上限：只取第一页时，低星新项目永远进不了候选池
    pages = max(1, int(settings.get("github_search_pages", 3)))
    pool_cap = max(count * 10, int(settings.get("github_pool_max", 600)))

    token = ""
    gh_cfg = config.get("github") or {}
    if isinstance(gh_cfg, dict):
        token = (gh_cfg.get("token") or "").strip()
    token = token or os.environ.get("GITHUB_TOKEN", "").strip()

    today = datetime.now(timezone.utc).date()
    since_long = (today - timedelta(days=lookback)).isoformat()
    since_short = (today - timedelta(days=window)).isoformat()

    # 候选池 = 近 lookback 天新建的高星项目 ∪ 近 window 天新建的项目
    # （后者在第 1 天就能提供"真实增量"：它们的全部 star 都是窗口内新增的）
    #
    # 每条查询跑两种排序：
    #   sort=stars   —— 偏向大体量项目，保证榜单质量下限
    #   sort=updated —— 偏向近期活跃，覆盖 stars 排序漏掉的低星新项目
    # 只用 stars 排序时，低星但增速猛的项目会被前 100 条大项目完全挡住。
    queries = [
        (f"created:>{since_long} stars:>{min_stars}", "stars"),
        (f"created:>{since_long} stars:>{min_stars}", "updated"),
        (f"created:>{since_short} stars:>{min_stars}", "stars"),
        (f"created:>{since_short} stars:>{min_stars}", "updated"),
    ]
    merged: dict[str, dict] = {}
    for query, sort in queries:
        for repo in _github_search(query, token, timeout, max_pages=pages, sort=sort):
            full = repo.get("full_name", "")
            if full and full not in merged:
                merged[full] = repo
    repos = list(merged.values())
    # 池子排序：新建越近越靠前，保证同分时新项目优先；再截到上限
    repos.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    repos = repos[:pool_cap]
    if not repos:
        return []

    today_s = today.isoformat()
    store = _load_gh_stars()

    # 1) 记录今天的 star 快照
    for repo in repos:
        full = repo.get("full_name", "")
        if full:
            store.setdefault(full, {})[today_s] = int(repo.get("stargazers_count", 0))

    # 2) 清理过期快照，避免文件无限增长
    keep_since = (today - timedelta(days=window * 3)).isoformat()
    for full in list(store.keys()):
        hist = store[full]
        for day in list(hist.keys()):
            if day < keep_since:
                del hist[day]
        if not hist:
            del store[full]
    _save_gh_stars(store)

    # 3) 计算真实增量
    window_start = (today - timedelta(days=window)).isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()
    scored: list[tuple[int, str, dict]] = []
    for repo in repos:
        full = repo.get("full_name", "")
        if not full:
            continue
        stars = int(repo.get("stargazers_count", 0))
        hist = store.get(full, {})
        past = [(d, s) for d, s in hist.items() if window_start <= d <= yesterday]
        if past:
            d0, s0 = min(past)
            growth = stars - s0
            period = (today - date.fromisoformat(d0)).days
            tag = f"⭐ {stars:,} · 近 {period} 日 +{growth:,}"
        else:
            created = (repo.get("created_at") or "")[:10]
            if created and created >= window_start:
                growth = stars
                age = (today - date.fromisoformat(created)).days
                tag = f"⭐ {stars:,} · 新建 {age} 天"
            else:
                continue
        if growth <= 0:
            continue
        scored.append((growth, tag, repo))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        chosen = scored[:count]
    else:
        # 数据不足时兜底：按累计 star 排序
        chosen = [
            (0, f"⭐ {int(r.get('stargazers_count', 0)):,} · 累计", r)
            for r in repos[:count]
        ]

    items: list[dict] = []
    for _growth, tag, repo in chosen:
        full = repo.get("full_name", "")
        language = repo.get("language") or "N/A"
        items.append(
            {
                "title": full,
                "link": repo.get("html_url", ""),
                "summary": repo.get("description") or "",
                "published": _parse_date(repo.get("pushed_at") or repo.get("created_at") or ""),
                # GitHub 自动生成的项目预览图
                "image": f"https://opengraph.githubassets.com/1/{full}",
                "source": "GitHub",
                "category": "GitHub 开源项目 7 日增速榜",
                "translate_field": "summary",
                "meta_override": f"GitHub · {tag} · {language}",
            }
        )
    log.info("GitHub 增速榜：候选 %d 个，有真实增量 %d 个，输出 %d 个。",
             len(repos), len(scored), len(items))
    return items


# --------------------------------------------------------------------------- #
# GitHub 趋势榜（来自 wuzao.com「无噪」）
# --------------------------------------------------------------------------- #
# 该站基于自己的星标快照提供「1 日 / 7 日 / 30 日增长榜」，每榜 30 条，
# 增量、star 总数、主语言、描述都已算好，无需我们自己积累快照。
# 这里解析其服务端渲染的 HTML（页面不依赖 JS 渲染，直接抓即可）。
WUZAO_BASE = "https://www.wuzao.com/projects/trends"
WUZAO_PERIODS = {"daily": "1 日", "weekly": "7 日", "monthly": "30 日"}

# 以「仓库链接 + 紧跟其后的卡片内容」为锚点切块；负向前瞻到下一个仓库链接或文末
WUZAO_CARD_RE = re.compile(
    r'<a href="https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"[^>]*>\s*\1/\2\s*</a>'
    r"(.*?)(?=<a href=\"https://github\.com/|$)",
    re.S,
)
WUZAO_GAIN_RE = re.compile(r">\s*\+([\d,]+)\s*<")
# star 总数紧跟在一个 </svg> 之后（星形图标），例如：</svg> 293.0k ⭐ </span>
# 因此不能要求数字紧跟在 '>' 后面，否则匹配不到。
WUZAO_STARS_RE = re.compile(r"([\d.]+\s*[kKmM]?)\s*⭐")
WUZAO_LANG_RE = re.compile(
    r'rounded text-xs font-medium[^"]*"[^>]*>\s*([^<]+?)\s*</span>'
)
WUZAO_DESC_RE = re.compile(
    r'<p class="text-sm text-slate-600[^"]*"[^>]*>(.*?)</p>', re.S
)


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _parse_star_display(raw: str) -> str:
    """把 '293.0k' 这类缩写还原成整数，失败则原样返回。

    星数用 k/M 缩写展示，直接拿来排序会出错，这里转回真实数值。
    """
    text = raw.replace(",", "").strip().lower()
    m = re.match(r"^([\d.]+)\s*([km])?$", text)
    if not m:
        return raw
    try:
        value = float(m.group(1))
    except ValueError:
        return raw
    suffix = m.group(2)
    if suffix == "k":
        value *= 1000
    elif suffix == "m":
        value *= 1000000
    return str(int(value))


def parse_wuzao_trends(raw_bytes: bytes, limit: int = 30) -> list[dict]:
    """解析无噪 GitHub 趋势榜页面，返回按增量降序的条目列表。

    只依赖服务端渲染的 HTML：每张卡片含仓库名、主语言标签、描述、
    star 总数（缩写形态）与「N 日增长」增量。
    """
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = raw_bytes.decode("utf-8", errors="ignore")

    rows: list[dict] = []
    seen: set[str] = set()
    for m in WUZAO_CARD_RE.finditer(text):
        full = f"{m.group(1)}/{m.group(2)}"
        if full in seen:
            continue
        block = m.group(3)

        gain_m = WUZAO_GAIN_RE.search(block)
        stars_m = WUZAO_STARS_RE.search(block)
        lang_m = WUZAO_LANG_RE.search(block)
        desc_m = WUZAO_DESC_RE.search(block)

        gain = int(gain_m.group(1).replace(",", "")) if gain_m else 0
        stars_raw = stars_m.group(1).strip() if stars_m else ""
        language = _strip_tags(lang_m.group(1)) if lang_m else ""
        desc = _strip_tags(desc_m.group(1)) if desc_m else ""

        seen.add(full)
        rows.append({
            "repo": full,
            "link": f"https://github.com/{full}",
            "gain": gain,
            "stars_raw": stars_raw,
            "stars": _parse_star_display(stars_raw),
            "language": language or "N/A",
            "desc": desc,
        })

    # 站点本身按增量降序排列，这里再排一次做防御，
    # 同时保证增量缺失的条目不会被排到最前面。
    rows.sort(key=lambda r: r["gain"], reverse=True)
    if limit and limit > 0:
        rows = rows[:limit]
    return rows


def fetch_wuzao_trends(config: dict) -> list[dict]:
    """抓取无噪趋势榜，失败返回空列表（调用方负责兜底）。"""
    wz = config.get("wuzao") or {}
    if not wz.get("enabled", False):
        return []

    period = str(wz.get("period", "daily")).strip().lower()
    if period not in WUZAO_PERIODS:
        log.warning("wuzao.period 只支持 %s，收到 %r，回退为 daily。",
                    "/".join(WUZAO_PERIODS), period)
        period = "daily"

    limit = int(wz.get("limit", 30))
    if limit <= 0:
        limit = 30
    timeout = max(int(config["settings"].get("request_timeout", 20)),
                  int(wz.get("timeout", 30)))
    retries = max(1, int(wz.get("retries", 2)))
    url = wz.get("url") or f"{WUZAO_BASE}/{period}/"

    import time as _time

    raw = b""
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                raw = resp.read()
            break
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError) as exc:
            log.warning("无噪趋势榜抓取失败（第 %d/%d 次）: %s", attempt, retries, exc)
            if attempt < retries:
                _time.sleep(2 * attempt)

    if not raw:
        log.warning("无噪趋势榜抓取最终失败。")
        return []

    rows = parse_wuzao_trends(raw, limit=limit)
    period_label = WUZAO_PERIODS[period]
    if not rows:
        log.warning("无噪趋势榜解析结果为空（页面结构可能已变化）。")
        return []

    items: list[dict] = []
    for rank, row in enumerate(rows, start=1):
        repo = row["repo"]
        language = row["language"]
        stars_display = row["stars_raw"] or row["stars"]
        items.append({
            "title": repo,
            "link": row["link"],
            "summary": row["desc"],          # 先放无噪的描述，稍后由 GitHub 原简介覆盖
            "published": datetime.now(timezone.utc),
            # GitHub 自动生成的项目预览图
            "image": f"https://opengraph.githubassets.com/1/{repo}",
            "source": "GitHub 趋势榜",
            "category": f"GitHub {period_label}增长榜 · 无噪",
            "translate_field": "",           # 项目名（owner/repo）不翻译
            "meta_override": f"{period_label} +{row['gain']:,} · ⭐ {stars_display} · {language}",
            "rank": rank,
            "_wuzao_desc": row["desc"],      # 无噪自带的中文描述，用作译文来源
        })

    log.info("无噪 %s趋势榜：解析 %d 条，最高 +%s。",
             period_label, len(items), f"{rows[0]['gain']:,}")

    # 每天榜单里的项目都在变，所以对每条都去 GitHub 取原简介，
    # 而不是只在"无噪没给描述"时才补——否则新项目永远拿不到原文。
    if int(wz.get("fetch_descriptions", True)):
        _fill_github_descriptions(items, config)

    return items


# --------------------------------------------------------------------------- #
# GitHub 原简介补全（无噪榜单只给中文，这里取英文原文用于"原文 + 译文"展示）
# --------------------------------------------------------------------------- #
GH_DESC_FILE = os.path.join(BASE_DIR, "github_descriptions.json")

# 空简介的重试间隔：仓库可能后续才补上 description，不能永久记为"没有"
GH_DESC_RETRY_DAYS = 7


def _load_gh_descriptions() -> dict:
    """读取简介缓存。兼容旧格式（纯字符串）与新格式（{d, t}）。"""
    try:
        with open(GH_DESC_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_gh_descriptions(data: dict) -> None:
    try:
        with open(GH_DESC_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=0)
    except OSError as exc:
        log.warning("写入 GitHub 简介缓存失败: %s", exc)


def _cached_description(store: dict, repo: str) -> tuple[bool, str]:
    """查缓存，返回 (是否可复用, 简介)。

    空简介超过 GH_DESC_RETRY_DAYS 天会重新请求，避免"当时没有、后来补上"
    的仓库被永久当成没有简介。
    """
    entry = store.get(repo)
    if entry is None:
        return False, ""
    if isinstance(entry, str):          # 旧格式
        return True, entry
    if not isinstance(entry, dict):
        return False, ""
    desc = entry.get("d") or ""
    if desc:
        return True, desc
    # 空简介：看是否到了重试时间
    stamp = entry.get("t") or ""
    try:
        tried = date.fromisoformat(stamp)
    except ValueError:
        return False, ""
    if (date.today() - tried).days >= GH_DESC_RETRY_DAYS:
        return False, ""
    return True, ""


def _fetch_github_description(repo: str, token: str, timeout: int) -> str:
    """从 GitHub API 取单个仓库的英文原简介。失败返回空串。"""
    url = f"https://api.github.com/repos/{repo}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError, ValueError) as exc:
        log.warning("GitHub 简介抓取失败 (%s): %s", repo, exc)
        return ""
    desc = (data.get("description") or "").strip()
    return _clean_summary(desc) if desc else ""


def _fill_github_descriptions(items: list[dict], config: dict) -> None:
    """为榜单里每个项目取 GitHub 英文原简介，写入 summary。

    结果布局：summary 是英文原文，summary_translation 是中文译文
    （由 translate_all 填充，无噪自带的中文描述会被优先复用）。
    若 GitHub 上没有简介，则退回无噪给出的描述。
    """
    if not items:
        return

    settings = config["settings"]
    timeout = max(5, int(settings.get("request_timeout", 20)))
    workers = max(1, int(settings.get("github_desc_workers", 6)))
    token = ""
    gh_cfg = config.get("github") or {}
    if isinstance(gh_cfg, dict):
        token = (gh_cfg.get("token") or "").strip()
    token = token or os.environ.get("GITHUB_TOKEN", "").strip()

    store = _load_gh_descriptions()
    today = date.today().isoformat()
    fetched = 0
    todo: list[dict] = []

    for it in items:
        repo = it.get("title", "")
        if not repo:
            continue
        usable, desc = _cached_description(store, repo)
        if usable:
            if desc:
                it["summary"] = desc
            continue
        todo.append(it)

    if todo:
        log.info("从 GitHub 拉取 %d 个项目简介（并发 %d）…", len(todo), workers)
        results: dict[str, str] = {}
        if workers > 1 and len(todo) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_fetch_github_description, it["title"], token, timeout): it
                    for it in todo
                }
                for fut in concurrent.futures.as_completed(futures):
                    it = futures[fut]
                    results[it["title"]] = fut.result()
        else:
            for it in todo:
                results[it["title"]] = _fetch_github_description(
                    it["title"], token, timeout)

        for repo, desc in results.items():
            store[repo] = {"d": desc, "t": today}
            if desc:
                fetched += 1
        _save_gh_descriptions(store)

        for it in todo:
            desc = results.get(it["title"], "")
            if desc:
                it["summary"] = desc

    have = sum(1 for it in items if it.get("summary", "").strip())
    log.info("GitHub 简介：%d/%d 条有原文（新增请求 %d 次，缓存命中 %d 次）。",
             have, len(items), len(todo), len(items) - len(todo))


# --------------------------------------------------------------------------- #
# Hacker News 中文摘要（来自 zeli.app）
# --------------------------------------------------------------------------- #
ZELI_ITEM_RE = re.compile(r"<li>(.*?)</li>", re.S)


def _zeli_plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def parse_zeli_digest(raw_bytes: bytes) -> list[dict]:
    """解析 zeli.app 的 RSS，取出最新一天 HN 中文摘要条目。"""
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError as exc:
        log.warning("zeli RSS 解析失败: %s", exc)
        return []

    entries = [n for n in root.iter() if _strip_ns(n.tag) == "item"]
    if not entries:
        return []

    def _pub(node: ET.Element) -> datetime:
        raw = _first_text(node, ("pubdate", "published", "updated", "date"))
        return _parse_date(raw) or datetime.min.replace(tzinfo=timezone.utc)

    latest = max(entries, key=_pub)
    desc = _first_text(latest, ("description", "summary", "content", "encoded"))
    digest_date = _pub(latest).astimezone().strftime("%Y-%m-%d")

    items: list[dict] = []
    for li in ZELI_ITEM_RE.findall(desc):
        head = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', li, re.S)
        if not head:
            continue
        title = _zeli_plain(head.group(2))
        if not title:
            continue
        link = head.group(1)
        src_link = re.search(r'<a href="([^"]+)"[^>]*>🔗', li)
        if src_link:
            link = src_link.group(1)
        source_name = ""
        sn = re.search(r"🔗\s*([^<]+)</a>", li)
        if sn:
            source_name = sn.group(1).strip()
        points = re.search(r"▲\s*([\d,]+)", li)
        comments = re.search(r"💬\s*([\d,]+)", li)
        body = li.split("<br/>", 1)[1] if "<br/>" in li else ""
        summary = _zeli_plain(body)
        pts = int(points.group(1).replace(",", "")) if points else 0
        cmt = int(comments.group(1).replace(",", "")) if comments else 0

        meta = f"HN · ▲ {pts:,}"
        if cmt:
            meta += f" · 💬 {cmt:,}"
        if source_name:
            meta += f" · {source_name}"
        items.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "published": _pub(latest),
                "source": "Hacker News",
                "category": "Hacker News 中文摘要（Zeli）",
                "translate_field": "",  # 内容已是中文，无需翻译
                "meta_override": meta,
                "_points": pts,
            }
        )

    items.sort(key=lambda x: x["_points"], reverse=True)
    for it in items:
        it.pop("_points", None)
    log.info("zeli HN 摘要：%s 共 %d 条。", digest_date, len(items))
    return items


def fetch_zeli(config: dict) -> list[dict]:
    zc = config.get("zeli") or {}
    if not zc.get("enabled", False):
        return []
    rss = zc.get("rss") or "https://zeli.app/zh/rss.xml"
    limit = int(zc.get("limit", 15))
    retries = max(1, int(zc.get("retries", 3)))
    timeout = max(int(config["settings"].get("request_timeout", 20)),
                  int(zc.get("timeout", 60)))
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, text/xml, */*"}

    import time as _time

    raw = b""
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(rss, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                raw = resp.read()
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            log.warning("zeli 抓取失败（第 %d/%d 次，超时 %ds）: %s",
                        attempt, retries, timeout, exc)
            if attempt < retries:
                _time.sleep(2 * attempt)

    if not raw:
        log.warning("zeli 抓取最终失败，跳过该板块。")
        return []

    items = parse_zeli_digest(raw)
    return items[:limit] if limit > 0 else items


# --------------------------------------------------------------------------- #
# 翻译
# --------------------------------------------------------------------------- #
CACHE_FILE = os.path.join(BASE_DIR, "translation_cache.json")
STATE_FILE = os.path.join(BASE_DIR, "send_state.json")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            return {}
    return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.warning("写入状态文件失败: %s", exc)


def _detect_lang(text: str) -> str:
    """粗略判断语种：CJK 字符多于拉丁字母 -> zh，否则 en。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return "zh" if cjk > latin else "en"


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            return {}
    return {}


# 翻译缓存上限：超过后按插入顺序淘汰最旧的条目。
# 每天新增约 100 条，不设上限的话一年会涨到几万条、文件几百 KB。
CACHE_MAX_ENTRIES = 5000


def save_cache(cache: dict) -> None:
    """写入翻译缓存；超过上限时保留最近的部分。"""
    if len(cache) > CACHE_MAX_ENTRIES:
        # dict 保持插入顺序，砍掉最早写入的条目
        drop = len(cache) - CACHE_MAX_ENTRIES
        keys = list(cache.keys())[:drop]
        for k in keys:
            cache.pop(k, None)
        log.info("翻译缓存超过上限 %d，淘汰最旧 %d 条。",
                 CACHE_MAX_ENTRIES, drop)
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=0)
    except OSError as exc:
        log.warning("写入翻译缓存失败: %s", exc)


# 翻译接口熔断状态：一旦被限流（429）或额度耗尽，本次运行剩余任务直接放弃，
# 避免几十条请求全部撞在同一个限流上、既拖慢流程又加深封禁。
_TRANSLATE_BLOCKED = {"reason": ""}
_TRANSLATE_LOCK = threading.Lock()


def _translation_blocked() -> bool:
    return bool(_TRANSLATE_BLOCKED["reason"])


def _trip_breaker(reason: str) -> None:
    """线程安全地设置熔断标记，只记录一次日志。"""
    with _TRANSLATE_LOCK:
        first = not _TRANSLATE_BLOCKED["reason"]
        _TRANSLATE_BLOCKED["reason"] = reason
    if first:
        log.warning("翻译接口%s，本次运行停止翻译。", reason)


# --------------------------------------------------------------------------- #
# 火山引擎机器翻译（AK/SK 签名，支持批量）
# --------------------------------------------------------------------------- #
VOLC_HOST = "open.volcengineapi.com"
VOLC_SERVICE = "translate"
VOLC_ACTION = "TranslateText"
VOLC_VERSION = "2020-06-01"
VOLC_CONTENT_TYPE = "application/json"

# 火山引擎按字符计费，单次请求的 TextList 不宜过大；
# 分批还能让部分失败不至于丢掉整批结果。
VOLC_BATCH_SIZE = 20

# 语言代码映射：项目内部用 en / zh，火山用 en / zh
_VOLC_LANG = {"zh": "zh", "zh-CN": "zh", "en": "en"}


def _volc_sign(ak: str, sk: str, method: str, host: str, path: str,
               query: dict, body: bytes, region: str) -> dict:
    """按火山引擎 HMAC-SHA256 规范生成请求头。

    规范见 https://docs.volcengine.com/docs/6369/67269
    纯标准库实现，不引入额外依赖。
    """
    now = datetime.now(timezone.utc)
    xdate = now.strftime("%Y%m%dT%H%M%SZ")
    date = xdate[:8]

    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(query.items())
    )
    body_hash = hashlib.sha256(body).hexdigest()

    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = (
        f"content-type:{VOLC_CONTENT_TYPE}\n"
        f"host:{host}\n"
        f"x-content-sha256:{body_hash}\n"
        f"x-date:{xdate}\n"
    )
    canonical_request = "\n".join(
        [method, path, canonical_query, canonical_headers, signed_headers, body_hash]
    )

    credential_scope = f"{date}/{region}/{VOLC_SERVICE}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256",
        xdate,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    k_date = hmac.new(sk.encode(), date.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, VOLC_SERVICE.encode(), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"request", hashlib.sha256).digest()
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return {
        "Content-Type": VOLC_CONTENT_TYPE,
        "Host": host,
        "X-Date": xdate,
        "X-Content-Sha256": body_hash,
        "Authorization": (
            f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


def _volc_translate_batch(texts: list[str], target: str, ak: str, sk: str,
                          region: str, timeout: int) -> list[str] | None:
    """调用火山引擎批量翻译。返回与输入等长的译文列表，失败返回 None。

    发送前会把已知专有名词（Anthropic / OpenAI / Ember ...）替换成占位符，
    翻译后再还原，避免被当成普通单词硬翻（"Anthropic" → "人类奥林匹克队"）。
    """
    if _translation_blocked() or not texts:
        return []

    protected: list[str] = []
    mappings: list[dict[str, str]] = []
    for t in texts:
        pt, mp = _protect_terms(t)
        protected.append(pt)
        mappings.append(mp)

    tgt = _VOLC_LANG.get(target, "zh")
    payload = {"TargetLanguage": tgt, "TextList": protected}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    query = {"Action": VOLC_ACTION, "Version": VOLC_VERSION}
    headers = _volc_sign(ak, sk, "POST", VOLC_HOST, "/", query, body, region)

    url = f"https://{VOLC_HOST}/?Action={VOLC_ACTION}&Version={VOLC_VERSION}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except OSError:
            pass
        if exc.code in (401, 403):
            _trip_breaker(f"鉴权失败（HTTP {exc.code}），请检查 AK/SK")
        elif exc.code == 429:
            _trip_breaker("限流（HTTP 429）")
        else:
            log.warning("火山翻译请求失败 HTTP %d: %s", exc.code, detail)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("火山翻译请求异常: %s", exc)
        return None

    items = data.get("TranslationList")
    if not isinstance(items, list):
        # 配额或权限问题会在这里返回错误信息
        err = (data.get("ResponseMetadata") or {}).get("Error") or {}
        msg = err.get("Message") or str(data)[:200]
        log.warning("火山翻译返回异常: %s", msg)
        return None

    out: list[str] = []
    for it in items:
        out.append((it.get("Translation") or "").strip())
    # 长度对不上时按失败处理，避免译文与原文错配
    if len(out) != len(texts):
        log.warning("火山翻译返回条数不符（%d != %d），本次结果丢弃。",
                    len(out), len(texts))
        return None
    # 还原被保护的专有名词
    return [_restore_terms(t, m) for t, m in zip(out, mappings)]


def _mymemory(text: str, src: str, tgt: str, timeout: int) -> str | None:
    """调用 MyMemory 免费翻译接口，失败返回 None。

    遇到 429 / 额度耗尽会设置熔断标记，后续调用立即返回 None。
    """
    if _translation_blocked():
        return None

    params = urllib.parse.urlencode({"q": text, "langpair": f"{src}|{tgt}"})
    url = "https://api.mymemory.translated.net/get?" + params
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        if exc.code in (429, 403):
            _trip_breaker(f"限流（HTTP {exc.code}）")
        return None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None

    if data.get("quotaFinished"):
        _trip_breaker("额度已用完（MyMemory）")
        return None
    result = (data.get("responseData") or {}).get("translatedText") or ""
    status = (data.get("responseStatus") or 200)
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 200
    if status != 200 or not result:
        return None
    # 接口偶尔会回传错误提示文本
    if "MYMEMORY WARNING" in result.upper():
        return None
    return result.strip()


def _resolve_langpair(text: str, target: str) -> tuple[str, str, str] | None:
    """确定翻译方向，返回 (源语种, 目标 MyMemory 代码, 缓存键语种对)。

    缓存键必须在所有调用点用同一套规则生成：早期 translate_all 预检查缓存时
    直接用 target（'zh'），而 translate_title 写入时用 'zh-CN'，导致预检查
    永远命中不了缓存。这里把规则收拢到一处。

    返回 None 表示「无需翻译」（空文本或已是目标语言）。
    """
    if not text:
        return None
    src = _detect_lang(text)
    if target == "auto":
        tgt = "en" if src == "zh" else "zh-CN"
    else:
        if (src == "zh" and target == "zh") or (src == "en" and target == "en"):
            return None  # 与目标语言一致
        tgt = "zh-CN" if target == "zh" else "en"
    return src, tgt, f"{src}|{tgt}"


def translate_title(text: str, target: str, timeout: int, cache: dict) -> str | None:
    """翻译单条标题，带缓存。target: auto / zh / en。返回译文或 None。"""
    resolved = _resolve_langpair(text, target)
    if resolved is None:
        return None
    src, tgt, pair = resolved

    key = f"{pair}|{text}"
    if key in cache:
        return cache[key]

    # MyMemory 的 langpair 代码
    pair_src = "zh-CN" if src == "zh" else "en"
    result = _mymemory(text, pair_src, tgt, timeout)
    if result:
        cache[key] = result
    return result


def _is_noop_translation(src_text: str, translated: str) -> bool:
    """判断译文是否等于「没翻」。

    机器翻译遇到专有名词、版本号、缩写时经常原样返回，例如：
        "frida v17.24.0"  →  "frida v17.24.0"
    这类译文渲染出来只是重复一遍原文，属于噪音，应当隐藏。

    判定不能只比全等：机器翻译有时会加标点或改空格，
    因此对"去掉空白与常见标点后完全一致"的情况也视为无效。
    """
    if not translated:
        return True
    if src_text.strip() == translated.strip():
        return True

    def norm(s: str) -> str:
        return re.sub(r"[\s·・.,;:!?、，。；：！？\"'“”‘’()（）\-—_]+", "", s).lower()

    return norm(src_text) == norm(translated)


def _looks_like_identifier(text: str) -> bool:
    """判断文本是否像一个「不该翻译的标识符」。

    工具名、项目名、库名常是单个词或驼峰串，机器翻译会把它当普通单词硬翻，
    产生 "IMSIsting → 即兴表演"、"TornadoC2 → 龙卷风C2" 这类误译。
    这类标题保持原样更有意义。
    """
    t = text.strip()
    if not t or len(t) > 40:
        return False
    # 含空格 / 明显句子结构 → 不是标识符
    if " " in t or "\n" in t:
        return False
    # 纯 CJK 不需要判断
    if _detect_lang(t) == "zh":
        return False
    # 全是 ASCII 字母数字与 . _ - / 且无空格 → 视为标识符
    return bool(re.fullmatch(r"[A-Za-z0-9._\-/+]+", t))


# --------------------------------------------------------------------------- #
# 术语保护：机器翻译会把公司名/产品名当普通词硬翻
#   Anthropic → 人类奥林匹克队 / 人类学的
#   Ember     → 余烬
# 这里用占位符把已知专有名词替换出来，翻完再还原，保证不被误译。
# --------------------------------------------------------------------------- #
_PROTECTED_TERMS = [
    # 公司 / 组织
    "Anthropic", "OpenAI", "DeepMind", "Microsoft", "Google", "Apple", "Meta",
    "Amazon", "Nvidia", "Tesla", "SpaceX", "ByteDance", "Alibaba", "Tencent",
    "Baidu", "Huawei", "Xiaomi", "Samsung", "Intel", "AMD", "Qualcomm", "ARM",
    "Cloudflare", "Cisco", "Oracle", "Salesforce", "Adobe", "Netflix", "Spotify",
    "Mozilla", "GitHub", "GitLab", "Docker", "Kubernetes", "Linux", "Ubuntu",
    # AI 模型 / 产品
    "ChatGPT", "GPT-4", "GPT-5", "Claude", "Gemini", "Llama", "Mistral",
    "Copilot", "Midjourney", "Stable Diffusion", "DeepSeek", "Qwen", "Kimi",
    "Grok", "Perplexity", "Sora", "Whisper", "Codex", "Cursor", "Windsurf",
    # 技术名词（容易被硬翻）
    "Rust", "Python", "JavaScript", "TypeScript", "Golang", "Kotlin", "Swift",
    "React", "Vue", "Angular", "Node.js", "Deno", "Bun", "WebAssembly", "WASM",
    "API", "SDK", "CLI", "GUI", "CPU", "GPU", "NPU", "TPU", "RAM", "ROM", "SSD",
    "USB", "HDMI", "GPIO", "I2C", "SPI", "UART", "JTAG", "SDR", "RFID", "NFC",
    "Bluetooth", "WiFi", "LTE", "GSM", "UMTS", "NR", "DNS", "HTTP", "HTTPS",
    "TLS", "SSL", "SSH", "SMTP", "JSON", "XML", "YAML", "HTML", "CSS", "SQL",
    "LLM", "RAG", "MCP", "MVC", "ORM", "CRUD", "JWT", "OAuth", "SAML",
    "Docker", "Podman", "Redis", "Kafka", "Nginx", "Apache", "PostgreSQL",
    "MySQL", "SQLite", "MongoDB", "Elasticsearch", "Grafana", "Prometheus",
    # 硬件 / 开发板
    "Raspberry Pi", "Arduino", "ESP32", "ESP8266", "STM32", "FreeRTOS",
    "CircuitPython", "MicroPython", "Adafruit", "Hackaday", "Hackster",
    "STEMMA", "Qwiic", "Grove",
    # 常见易误译词
    "Ember", "Kindle", "AirPods", "iPhone", "iPad", "MacBook", "macOS", "iOS",
    "Android", "Windows", "Chrome", "Chromium", "Firefox", "Safari", "Edge",
]

# 按长度降序，保证 "Raspberry Pi" 先于 "Pi" 匹配
_PROTECTED_TERMS.sort(key=len, reverse=True)
# 占位符必须足够"不可翻译"：早期用 ⟦0⟧ 会被接口吞掉或改写，
# 改成 ZQX 前缀的伪词，机器翻译通常原样保留。
_TERM_PLACEHOLDER = "ZQXTERM{}QXZ"


def _protect_terms(text: str) -> tuple[str, dict[str, str]]:
    """把已知专有名词替换成占位符，返回 (替换后文本, 占位符→原词映射)。"""
    mapping: dict[str, str] = {}
    out = text
    for idx, term in enumerate(_PROTECTED_TERMS):
        # 允许词后紧跟所有格 's / ’s / s（如 Anthropics' 是 Anthropic 的笔误变体），
        # 否则 "Anthropics'" 匹配不到 "Anthropic"。
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(term)}"
            rf"(?:[’']?s(?![A-Za-z0-9])|[’'](?![A-Za-z0-9])|(?![A-Za-z0-9]))"
        )
        if not pattern.search(out):
            continue
        token = _TERM_PLACEHOLDER.format(idx)
        # 同一术语可能多次出现，统一用同一个占位符
        out = pattern.sub(token, out)
        mapping[token] = term
    return out, mapping


def _restore_terms(text: str, mapping: dict[str, str]) -> str:
    """还原占位符；接口可能改动大小写或加空格，用宽松匹配兜底。"""
    if not mapping:
        return text
    out = text
    for token, term in mapping.items():
        out = out.replace(token, term)
        # 兜底 1：大小写被改（zqxterm0qxz）
        out = re.sub(re.escape(token), term, out, flags=re.I)
        # 兜底 2：占位符内部被插入空格（ZQX TERM 0 QXZ）
        inner = token[len("ZQX"):-len("QXZ")]
        loose = re.compile(r"ZQX\s*" + re.escape(inner) + r"\s*QXZ", re.I)
        out = loose.sub(term, out)
    return out


def _resolve_translate_provider(config: dict) -> tuple[str, str, str, str]:
    """决定用哪个翻译后端，返回 (provider, ak, sk, region)。

    优先级：
      1. translate.provider 显式指定 volcengine，且 AK/SK 齐全 → 火山引擎
      2. 未指定但环境变量/配置里有火山 AK+SK → 火山引擎
      3. 否则 → MyMemory

    AK/SK 优先从环境变量读（不落盘），其次读配置。
    """
    tr = config.get("translate") or {}
    if not isinstance(tr, dict):
        tr = {}

    ak = (os.environ.get("VOLC_ACCESS_KEY")
          or tr.get("volc_access_key") or "").strip()
    sk = (os.environ.get("VOLC_SECRET_KEY")
          or tr.get("volc_secret_key") or "").strip()
    region = (os.environ.get("VOLC_REGION")
              or tr.get("volc_region") or "cn-north-1").strip()

    want = str(tr.get("provider", "auto")).strip().lower()
    if want == "mymemory":
        return "mymemory", "", "", ""
    if want == "volcengine":
        if ak and sk:
            return "volcengine", ak, sk, region
        log.warning("translate.provider=volcengine 但缺少 AK/SK，回退 MyMemory。")
        return "mymemory", "", "", ""
    # auto
    if ak and sk:
        return "volcengine", ak, sk, region
    return "mymemory", "", "", ""


def translate_all(grouped: dict[str, list[dict]], config: dict) -> None:
    """为条目补充译文：标题写 title_translation，简介写 summary_translation。

    - 只译与目标语言不同的字段（zh 模式下中文标题跳过）
    - 译文等于原文时视为无效，不写入字段，渲染层因此不会显示无意义的译文行
    - 标题与简介并发翻译；命中缓存、重复文本都不发请求
    """
    settings = config["settings"]
    if not settings.get("translate", True):
        return
    target = settings.get("translate_target", "auto")
    timeout = int(settings.get("request_timeout", 20))
    delay = float(settings.get("translate_delay", 0.25))
    workers = max(1, int(settings.get("translate_workers", 4)))
    want_summary = bool(settings.get("translate_summary", True))
    max_requests = max(0, int(settings.get("translate_max_requests", 80)))
    cache = load_cache()
    _TRANSLATE_BLOCKED["reason"] = ""     # 每次运行重置熔断

    # 选择翻译后端：配了火山引擎 AK/SK 就用火山（质量好、可批量），
    # 否则退回 MyMemory（免费但按 IP 限流）。
    provider, ak, sk, region = _resolve_translate_provider(config)
    if provider == "volcengine":
        log.info("翻译后端：火山引擎机器翻译（批量）。")
    else:
        log.info("翻译后端：MyMemory（免费，按 IP 限流）。")

    all_items = [it for items in grouped.values() for it in items]

    # 1) 收集待请求任务；先查缓存，命中的直接落盘
    tasks: list[tuple[dict, str, str, str, str, str]] = []
    # (item, out_key, cache_key, raw, pair_src, tgt)
    hits = 0
    reused = 0
    skipped_id = 0
    for it in all_items:
        # 无噪榜单自带中文描述时直接复用，比机器翻译质量高、还省一次请求。
        # 仅在简介需要译文（即原文不是中文）时才复用。
        pre = it.pop("_wuzao_desc", "") or ""
        pairs: list[tuple[str, str]] = []
        field = it.get("translate_field", "title")
        if field:
            pairs.append(("title_translation", field))
        if want_summary:
            pairs.append(("summary_translation", "summary"))
        for out_key, src_key in pairs:
            raw = (it.get(src_key) or "").strip()
            if not raw:
                continue
            # 标题若是单个标识符（工具名/库名），翻译只会产生误译，直接跳过
            if out_key == "title_translation" and _looks_like_identifier(raw):
                skipped_id += 1
                continue
            resolved = _resolve_langpair(raw, target)
            if resolved is None:
                continue          # 已是目标语言，无需翻译
            # 无噪的中文描述可直接充当简介译文
            if out_key == "summary_translation" and pre:
                if _detect_lang(pre) == "zh" and not _is_noop_translation(raw, pre):
                    it[out_key] = pre
                    reused += 1
                    continue
            src, tgt, pair = resolved
            key = f"{pair}|{raw}"
            cached = cache.get(key)
            if cached is not None:
                if not _is_noop_translation(raw, cached):
                    it[out_key] = cached
                    hits += 1
                continue
            pair_src = "zh-CN" if src == "zh" else "en"
            tasks.append((it, out_key, key, raw, pair_src, tgt))

    # 2) 同一段文本只请求一次
    unique: dict[str, tuple[str, str]] = {}   # cache_key -> (text, tgt)
    for _it, _out, key, raw, _ps, tgt in tasks:
        unique.setdefault(key, (raw, tgt))

    pending = list(unique.items())
    # 上限只在 MyMemory（按 IP 限流、额度紧张）时才有意义；
    # 火山引擎按字符计费且可批量，不需要限制条数，否则会白白漏翻。
    if provider != "volcengine" and max_requests and len(pending) > max_requests:
        log.info("翻译请求 %d 段超过单次上限 %d，本次只翻前 %d 段，其余下次再补。",
                 len(pending), max_requests, max_requests)
        pending = pending[:max_requests]

    results: dict[str, str] = {}
    import time as _time

    if provider == "volcengine":
        # 火山引擎支持批量：把多段文本打包进一次请求，
        # 请求数从「每段一次」降到「每批一次」，也就不需要并发。
        batches = [pending[i:i + VOLC_BATCH_SIZE]
                   for i in range(0, len(pending), VOLC_BATCH_SIZE)]
        log.info("翻译：缓存命中 %d 条，复用无噪译文 %d 条，"
                 "需请求 %d 段 → 打包成 %d 批（火山引擎，每批≤%d 条）。",
                 hits, reused, len(pending), len(batches), VOLC_BATCH_SIZE)

        for batch in batches:
            if _translation_blocked():
                break
            texts = [text for _key, (text, _tgt) in batch]
            # 同一批里目标语言相同（本项目只译成中文），取第一个即可
            tgt = batch[0][1][1]
            out = _volc_translate_batch(
                texts, tgt, ak, sk, region, timeout)
            if not out:
                continue
            for (key, _payload), tr in zip(batch, out):
                if tr:
                    results[key] = tr
                    cache[key] = tr
            if delay > 0:
                _time.sleep(delay)
    else:
        log.info("翻译：缓存命中 %d 条，复用无噪译文 %d 条，"
                 "需请求 %d 段（去重后），并发 %d。",
                 hits, reused, len(pending), workers)

        def translate_one(job: tuple[str, tuple[str, str]]) -> tuple[str, str]:
            key, (text, tgt) = job
            pair_src = "zh-CN" if _detect_lang(text) == "zh" else "en"
            result = _mymemory(text, pair_src, tgt, timeout)
            if delay > 0:
                _time.sleep(delay)      # 对目标站点的礼貌间隔
            return key, result or ""

        if pending:
            if workers > 1 and len(pending) > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    for key, tr in pool.map(translate_one, pending):
                        if tr:
                            results[key] = tr
                            cache[key] = tr
            else:
                for job in pending:
                    key, tr = translate_one(job)
                    if tr:
                        results[key] = tr
                        cache[key] = tr

    # 3) 回填到条目
    for it, out_key, key, raw, _ps, _tgt in tasks:
        tr = results.get(key)
        if tr and not _is_noop_translation(raw, tr):
            it[out_key] = tr

    save_cache(cache)
    n_title = sum(1 for it in all_items if it.get("title_translation"))
    n_summary = sum(1 for it in all_items if it.get("summary_translation"))
    log.info("翻译完成：标题 %d/%d 条，简介 %d/%d 条（新增 %d 段译文，跳过标识符 %d 条）。",
             n_title, len(all_items), n_summary, len(all_items),
             len(results), skipped_id)
    if _translation_blocked():
        log.warning("本次翻译被中断：%s。译文已缓存，下次运行会自动续上。",
                    _TRANSLATE_BLOCKED["reason"])


# --------------------------------------------------------------------------- #
# 整理新闻
# --------------------------------------------------------------------------- #
def _normalize_link_key(link: str) -> str:
    """把链接归一化成判重键：去掉协议、www、追踪参数与末尾斜杠。

    同一篇文章常带不同的 utm_* / fbclid 参数，或被 http/https 混用，
    直接比字符串会把同一篇当成两篇。
    """
    if not link:
        return ""
    try:
        parsed = urllib.parse.urlparse(link.strip())
    except ValueError:
        return link.strip().lower()
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").rstrip("/")
    # 只保留有意义的查询参数，丢掉追踪类
    keep = []
    for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=False):
        if k.lower().startswith(("utm_", "fbclid", "gclid", "ref", "spm", "from")):
            continue
        keep.append((k, v))
    query = urllib.parse.urlencode(sorted(keep))
    return f"{host}{path}" + (f"?{query}" if query else "")


def collect_news(config: dict) -> dict[str, list[dict]]:
    settings = config["settings"]
    timeout = int(settings.get("request_timeout", 20))
    lookback = int(settings.get("hours_lookback", 24))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback)

    sources = list(config.get("sources", []))

    # 并发抓取：单源超时不再串行累加（最坏 15×20s），正常情况下耗时接近最慢的单个源
    all_items: list[dict] = []
    if sources:
        workers = max(1, min(int(settings.get("fetch_workers", 6)), len(sources)))
        if workers > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                # submit 的顺序 = 配置顺序，日志与结果顺序都保持稳定
                futures = [pool.submit(fetch_source, s, timeout) for s in sources]
                for fut in futures:
                    try:
                        all_items.extend(fut.result())
                    except Exception as exc:  # noqa: BLE001
                        log.warning("抓取任务异常（已跳过该源）: %s", exc)
        else:
            for source in sources:
                all_items.extend(fetch_source(source, timeout))

    # 按发布时间过滤（没有时间的保留，视为可能的最新）
    fresh = []
    for it in all_items:
        published = it.get("published")
        if published is None or published >= cutoff:
            fresh.append(it)

    # 去重：同一篇内容可能被多个源收录（标题相同、链接不同），
    # 也可能标题略有差异但链接相同。两者都视为重复，保留时间较新的一条。
    seen_titles: set[str] = set()
    seen_links: set[str] = set()
    deduped: list[dict] = []
    for it in sorted(
        fresh,
        key=lambda x: x.get("published") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    ):
        title_key = (it.get("title") or "").strip().lower()
        link_key = _normalize_link_key(it.get("link") or "")
        # 标题和链接都缺失的条目无法判重，直接保留
        if not title_key and not link_key:
            deduped.append(it)
            continue
        if (title_key and title_key in seen_titles) or (
                link_key and link_key in seen_links):
            continue
        if title_key:
            seen_titles.add(title_key)
        if link_key:
            seen_links.add(link_key)
        deduped.append(it)

    # 按分类分组，每类限制条数
    #
    # max_per_category 是全局默认值；max_per_category_by 支持按分类覆盖。
    # 源多的分类（如 7 个源的网络安全）用默认值会丢掉大部分源的内容，
    # 因此允许单独给这类分类配更大的上限。
    grouped: dict[str, list[dict]] = {}
    max_per_cat = int(settings.get("max_per_category", 8))
    overrides = settings.get("max_per_category_by") or {}
    if not isinstance(overrides, dict):
        log.warning("settings.max_per_category_by 不是字典，忽略该项。")
        overrides = {}
    limits = {str(k): int(v) for k, v in overrides.items() if str(v).lstrip("-").isdigit()}

    for it in deduped:
        cat = it.get("category", "其他")
        cap = limits.get(cat, max_per_cat)
        bucket = grouped.setdefault(cat, [])
        if len(bucket) < cap:
            bucket.append(it)

    # 提示被截断的分类，避免"源很多却每类都只有 8 条"时不知情
    for cat, cap in limits.items():
        if cap > max_per_cat:
            got = len(grouped.get(cat, ()))
            if got >= cap:
                log.info("分类「%s」达到自定义上限 %d 条（全局默认 %d）。",
                         cat, cap, max_per_cat)

    return grouped


# --------------------------------------------------------------------------- #
# 生成邮件
# --------------------------------------------------------------------------- #
def _fmt_time(dt: datetime | None, cfg: dict | None = None) -> str:
    """按 settings.timezone 显示发布时间；无时区或无法解析时退回系统本地。"""
    if dt is None:
        return "时间未知"
    tz = _resolve_tz(cfg or {})
    try:
        return (dt.astimezone(tz) if tz is not None else dt.astimezone()).strftime("%m-%d %H:%M")
    except (ValueError, OSError):
        return dt.strftime("%m-%d %H:%M")


def build_html(grouped: dict[str, list[dict]], config: dict) -> str:
    settings = config["settings"]
    today = current_now(config).strftime("%Y年%m月%d日")
    total = sum(len(v) for v in grouped.values())

    parts: list[str] = []
    parts.append(
        """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;color:#1f2328;">
<div style="max-width:720px;margin:0 auto;padding:28px 18px;">
<div style="background:linear-gradient(135deg,#2563eb,#7c3aed);border-radius:16px 16px 0 0;padding:32px 34px;color:#fff;">
<div style="font-size:27px;font-weight:700;letter-spacing:.5px;">📡 每日科技新闻速览</div>
"""
    )
    parts.append(
        f'<div style="margin-top:10px;font-size:15px;opacity:.92;">{today} · 共 {total} 条 · '
        f"AI / 科学 / 医疗 / 科技行业</div>"
    )
    parts.append(
        "</div>"
        '<div style="background:#fff;border-radius:0 0 16px 16px;padding:12px 34px 34px;'
        'box-shadow:0 1px 3px rgba(0,0,0,.08);">'
    )

    if total == 0:
        parts.append(
            '<p style="color:#666;padding:24px 0;font-size:16px;">本次没有抓取到符合条件的新闻，'
            "可能是网络问题或时间窗口内暂无更新。</p>"
        )

    for category, items in grouped.items():
        parts.append(
            f'<h2 style="font-size:20px;margin:34px 0 14px;padding-left:12px;'
            f'border-left:5px solid #2563eb;color:#111;">{html.escape(category)}'
            f'<span style="color:#888;font-size:14px;font-weight:400;"> · {len(items)} 条</span></h2>'
        )
        for it in items:
            title = html.escape(it.get("title", ""))
            link = html.escape(it.get("link", "#"), quote=True)
            source = html.escape(it.get("source", ""))
            time_str = _fmt_time(it.get("published"), config)
            summary = html.escape(it.get("summary", ""))
            translation = html.escape(it.get("title_translation", ""))
            summary_tr = html.escape(it.get("summary_translation", ""))
            image = html.escape(it.get("image", ""), quote=True)
            link_attr = f'href="{link}" target="_blank" rel="noopener"'

            # 排行榜条目带名次徽章（前三名用高亮色，其余用灰色）
            # 注意：Python 里 isinstance(True, int) 为 True，必须显式排除 bool，
            # 否则 rank=True 会渲染出写着 "True" 的徽章。
            rank = it.get("rank")
            if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
                rank = None
            rank_badge = ""
            if rank is not None:
                if rank <= 3:
                    bg, fg = ("#fde68a", "#92400e")   # 金
                elif rank <= 10:
                    bg, fg = ("#e0e7ff", "#3730a3")   # 蓝
                else:
                    bg, fg = ("#f1f5f9", "#64748b")   # 灰
                rank_badge = (
                    f'<span style="display:inline-block;min-width:26px;height:26px;'
                    f'line-height:26px;text-align:center;border-radius:8px;'
                    f'background:{bg};color:{fg};font-size:13px;font-weight:700;'
                    f'margin-right:8px;vertical-align:2px;">{rank}</span>'
                )

            # 右侧文字块
            text_block = (
                f'<a {link_attr} style="color:#1a4fd6;text-decoration:none;font-size:18px;'
                f'font-weight:600;line-height:1.5;">{rank_badge}{title}</a>'
            )
            if translation:
                text_block += (
                    f'<div style="margin-top:8px;font-size:16px;color:#6b7280;'
                    f'line-height:1.65;">→ {translation}</div>'
                )
            if summary:
                text_block += (
                    f'<div style="margin-top:9px;font-size:15px;color:#5a6472;line-height:1.8;">{summary}</div>'
                )
            # 简介译文紧跟原文，读者可对照理解（原文在上、译文在下）
            if summary_tr:
                text_block += (
                    f'<div style="margin-top:6px;font-size:14px;color:#6b7280;'
                    f'line-height:1.75;">{summary_tr}</div>'
                )
            # meta_override 的内容来自外部页面（如 zeli 的原站域名），必须转义后再拼接，
            # 否则外部可控的尖括号内容会破坏邮件 HTML 结构。
            meta = it.get("meta_override") or (source + " · " + time_str)
            text_block += (
                f'<div style="margin-top:10px;font-size:13px;color:#9aa2ad;line-height:1.5;">'
                f"{html.escape(meta)}</div>"
            )
            text_block += (
                f'<div style="margin-top:12px;"><a {link_attr} style="color:#1a4fd6;'
                f'font-size:14px;font-weight:600;text-decoration:underline;">🔗 查看原文</a></div>'
            )

            if image:
                parts.append(
                    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                    'style="border-bottom:1px solid #f0f1f3;"><tr>'
                    f'<td width="124" valign="top" style="padding:18px 14px 18px 0;">'
                    f'<a {link_attr}><img src="{image}" width="124" alt="" border="0" '
                    f'style="width:124px;height:93px;object-fit:cover;border-radius:10px;display:block;"></a>'
                    f"</td>"
                    f'<td valign="top" style="padding:18px 0;">{text_block}</td>'
                    "</tr></table>"
                )
            else:
                parts.append(
                    f'<div style="padding:18px 0;border-bottom:1px solid #f0f1f3;">{text_block}</div>'
                )


    parts.append(
        '<div style="margin-top:30px;padding-top:16px;border-top:1px solid #eee;'
        'font-size:12.5px;color:#9aa2ad;text-align:center;">'
        "由 news_collector.py 自动生成 · 数据来自公开 RSS 源</div>"
    )
    parts.append("</div></div></body></html>")
    return "".join(parts)


def build_text(grouped: dict[str, list[dict]], config: dict | None = None) -> str:
    lines = ["每日科技新闻速览", "=" * 30, ""]
    total = sum(len(v) for v in grouped.values())
    if total == 0:
        lines.append("本次没有抓取到符合条件的新闻。")
    for category, items in grouped.items():
        lines.append(f"# {category} ({len(items)})")
        for it in items:
            lines.append(f"- {it.get('title','')}")
            if it.get("title_translation"):
                lines.append(f"  → {it['title_translation']}")
            if it.get("summary"):
                lines.append(f"  {it['summary']}")
            if it.get("summary_translation"):
                lines.append(f"  → {it['summary_translation']}")
            if it.get("image"):
                lines.append(f"  [图] {it['image']}")
            if it.get("link"):
                lines.append(f"  {it['link']}")
            meta = it.get("meta_override") or " · ".join(
                x for x in [it.get("source", ""), _fmt_time(it.get("published"), config)] if x
            )
            if meta:
                lines.append(f"  [{meta}]")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 本地存档
# --------------------------------------------------------------------------- #
def resolve_archive_dir(config: dict) -> str:
    """解析存档目录：支持相对路径（相对项目目录）与绝对路径。"""
    arch = config.get("archive") or {}
    directory = arch.get("dir") or "history"
    if not os.path.isabs(directory):
        directory = os.path.join(BASE_DIR, directory)
    return directory


def save_archive(config: dict, today: str, html_body: str, text_body: str) -> None:
    """把当天的日报存成 HTML + 纯文本，并刷新索引页。"""
    arch = config.get("archive") or {}
    if not arch.get("enabled", True):
        return
    directory = resolve_archive_dir(config)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        log.warning("创建存档目录失败: %s", exc)
        return

    html_path = os.path.join(directory, f"{today}.html")
    text_path = os.path.join(directory, f"{today}.txt")
    try:
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(html_body)
        with open(text_path, "w", encoding="utf-8") as fh:
            fh.write(text_body)
    except OSError as exc:
        log.warning("写入存档失败: %s", exc)
        return

    _write_archive_index(directory)
    _prune_archive(directory, int(arch.get("keep_days", 0)))
    log.info("已本地存档: %s", html_path)


def _prune_archive(directory: str, keep_days: int) -> None:
    """删除超过 keep_days 天的存档（keep_days<=0 表示永久保留）。"""
    if keep_days <= 0:
        return
    cutoff = date.today() - timedelta(days=keep_days)
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext not in (".html", ".txt"):
            continue
        try:
            day = date.fromisoformat(stem)
        except ValueError:
            continue          # index.html 等非日期文件跳过
        if day >= cutoff:
            continue
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError as exc:
            log.warning("删除过期存档失败 %s: %s", name, exc)
    if removed:
        log.info("清理过期存档 %d 个（保留最近 %d 天）。", removed, keep_days)


def _count_items(text_path: str) -> int:
    """统计一份日报纯文本里的条目数（每条以 "- " 开头）。"""
    try:
        with open(text_path, "r", encoding="utf-8", errors="ignore") as fh:
            return sum(1 for line in fh if line.startswith("- "))
    except OSError:
        return 0


def _write_archive_index(directory: str) -> None:
    """生成/刷新存档目录的索引页 index.html（含每日条数与搜索框）。"""
    try:
        files = sorted(
            (f for f in os.listdir(directory) if f.endswith(".html") and f != "index.html"),
            reverse=True,
        )
    except OSError:
        return

    entries: list[tuple[str, str, int]] = []
    total_items = 0
    for f in files:
        day = f[:-5]
        count = _count_items(os.path.join(directory, day + ".txt"))
        total_items += count
        entries.append((day, f, count))

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>科技日报存档</title>",
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',"
        "sans-serif;max-width:720px;margin:36px auto;padding:0 18px;color:#1f2328;background:#f4f5f7;font-size:16px;}"
        "h1{font-size:26px;margin:0 0 6px;}"
        ".stat{color:#7a8290;font-size:14px;margin-bottom:18px;}"
        "#q{width:100%;box-sizing:border-box;padding:12px 14px;font-size:16px;border:1px solid #dfe3e8;"
        "border-radius:10px;margin-bottom:16px;outline:none;background:#fff;}"
        "#q:focus{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.12);}"
        "ul{list-style:none;padding:0;margin:0;}"
        "li{margin:12px 0;background:#fff;border-radius:12px;padding:18px 22px;display:flex;"
        "align-items:center;justify-content:space-between;gap:12px;"
        "box-shadow:0 1px 3px rgba(0,0,0,.06);}"
        "li.hide{display:none;}"
        "a{color:#1a4fd6;text-decoration:none;font-size:18px;font-weight:600;}"
        "a:hover{text-decoration:underline;}"
        ".n{color:#7a8290;font-size:14px;white-space:nowrap;background:#f4f5f7;"
        "border-radius:999px;padding:3px 12px;}"
        ".empty{color:#9aa2ad;padding:20px 0;}"
        "</style></head><body>",
        "<h1>📚 科技日报存档</h1>",
        f'<div class="stat">共 {len(entries)} 天 · 累计 {total_items} 条</div>',
        '<input id="q" type="search" placeholder="搜索日期，例如 2026-10 ..." autocomplete="off">',
        '<ul id="list">',
    ]
    for day, f, count in entries:
        parts.append(
            f'<li data-text="{html.escape(day, quote=True)}">'
            f'<a href="{html.escape(f, quote=True)}">{html.escape(day)}</a>'
            f'<span class="n">{count} 条</span></li>'
        )
    parts.append("</ul>")
    parts.append('<div class="empty" id="empty" style="display:none">没有匹配的日期</div>')
    parts.append(
        "<script>"
        "var q=document.getElementById('q'),list=document.getElementById('list'),"
        "empty=document.getElementById('empty');"
        "function apply(){var v=q.value.trim().toLowerCase(),shown=0;"
        "Array.prototype.forEach.call(list.children,function(li){"
        "var hit=!v||(li.getAttribute('data-text')||'').toLowerCase().indexOf(v)>=0;"
        "li.classList.toggle('hide',!hit);if(hit)shown++;});"
        "empty.style.display=shown?'none':'block';}"
        "q.addEventListener('input',apply);apply();"
        "</script></body></html>"
    )
    try:
        with open(os.path.join(directory, "index.html"), "w", encoding="utf-8") as fh:
            fh.write("".join(parts))
    except OSError as exc:
        log.warning("写入索引页失败: %s", exc)


# --------------------------------------------------------------------------- #
# 发送邮件
# --------------------------------------------------------------------------- #
def _smtp_send(ec: dict, sender: str, password: str, recipients: list[str], message) -> None:
    """单次投递（不含重试逻辑）。"""
    host = ec.get("smtp_host", "smtp-mail.outlook.com")
    port = int(ec.get("smtp_port", 587))
    use_ssl = bool(ec.get("use_ssl", False))
    use_tls = bool(ec.get("use_tls", True))
    timeout = int(ec.get("smtp_timeout", 30))

    if use_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=timeout) as server:
            server.login(sender, password)
            server.sendmail(sender, recipients, message.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.ehlo()
            if use_tls:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            server.login(sender, password)
            server.sendmail(sender, recipients, message.as_string())


def _clean_addr(value) -> str:
    """取单个纯邮箱地址：容忍 "名字 <a@b.com>" 形式，只留 a@b.com。"""
    text = str(value or "").strip()
    if "<" in text and ">" in text:
        text = text[text.rfind("<") + 1: text.rfind(">")]
    return text.strip().strip(",;").strip()


def _split_recipients(value) -> list[str]:
    """解析收件人列表。

    支持三种写法，便于用环境变量传多个地址：
        ["a@x.com", "b@y.com"]      配置里直接写数组
        "a@x.com,b@y.com"          环境变量里逗号分隔
        "a@x.com; b@y.com"         分号也算分隔
    """
    if isinstance(value, str):
        parts: list[str] = []
        for chunk in value.replace(";", ",").split(","):
            addr = _clean_addr(chunk)
            if addr:
                parts.append(addr)
        return parts
    if isinstance(value, (list, tuple)):
        return [a for a in (_clean_addr(v) for v in value) if a]
    return []


def send_email(config: dict, subject: str, html_body: str, text_body: str) -> None:
    """发送邮件；失败会按 send_retries 自动重试，全部失败后抛出异常。"""
    ec = config["email"]
    settings = config.get("settings", {})
    sender = _clean_addr(ec.get("sender", ""))
    password = ec.get("password", "")
    recipients = _split_recipients(ec.get("recipients", []))

    if not sender or not password or not recipients:
        raise RuntimeError(
            "邮件配置不完整：请在 config.json 中填写 email.sender / email.password / email.recipients"
        )

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = formataddr((str(Header(ec.get("sender_name", "每日科技新闻"), "utf-8")), sender))
    message["To"] = ", ".join(recipients)
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    retries = max(1, int(settings.get("send_retries", 3)))
    delay = float(settings.get("send_retry_delay", 15))
    import time as _time

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            _smtp_send(ec, sender, password, recipients, message)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("发送失败（第 %d/%d 次尝试）: %s", attempt, retries, exc)
            if attempt < retries and delay > 0:
                _time.sleep(delay)
            continue
        log.info("邮件已发送至: %s（第 %d 次尝试成功）", ", ".join(recipients), attempt)
        return

    if last_exc is not None:
        raise last_exc


# --------------------------------------------------------------------------- #
# 失败告警
# --------------------------------------------------------------------------- #
ALERT_FILENAME = "!发送失败提醒.txt"


def _desktop_dirs() -> list[str]:
    """可能的桌面目录（含 OneDrive 重定向的情况）。"""
    home = os.path.expanduser("~")
    cands = [os.path.join(home, "Desktop"), os.path.join(home, "OneDrive", "Desktop")]
    return [d for d in cands if os.path.isdir(d)]


def alert_paths() -> list[str]:
    ac_dir = [os.path.join(BASE_DIR, ALERT_FILENAME)]
    return ac_dir + [os.path.join(d, ALERT_FILENAME) for d in _desktop_dirs()]


def write_failure_alert(config: dict, today: str, exc: Exception, archive_dir: str) -> None:
    """发送失败时留下显眼的提醒（项目目录 + 桌面），下次成功后自动清除。"""
    ac = config.get("alert") or {}
    if not ac.get("enabled", True):
        return
    text = (
        "科技日报发送失败\n"
        "========================================\n"
        f"日期: {today}\n"
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"原因: {exc}\n\n"
        "今天的日报已经生成并保存在:\n"
        f"  {os.path.join(archive_dir, today + '.html')}\n"
        "（可直接用浏览器打开查看，内容完整）\n\n"
        "排查建议:\n"
        "  1) 查看 news_collector.log 末尾的 ERROR\n"
        "  2) 检查 config.json 里的邮箱 / 授权码是否正确\n"
        "  3) 确认网络可正常访问外网\n"
        "  4) 手动补发:  python news_collector.py --force\n\n"
        "（下次成功发送后，本文件会自动删除）\n"
    )
    targets = [os.path.join(BASE_DIR, ALERT_FILENAME)]
    if ac.get("desktop_copy", True):
        targets += [os.path.join(d, ALERT_FILENAME) for d in _desktop_dirs()]
    for path in targets:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            log.warning("已写入失败提醒: %s", path)
        except OSError as err:
            log.warning("写入失败提醒出错 (%s): %s", path, err)


def clear_failure_alert() -> None:
    """发送成功后清掉提醒文件。"""
    for path in alert_paths():
        if os.path.exists(path):
            try:
                os.remove(path)
                log.info("已清除失败提醒: %s", path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# 时区
# --------------------------------------------------------------------------- #
def _resolve_tz(config: dict):
    """按 settings.timezone 解析时区，失败则退回系统本地时区。

    为什么需要：GitHub Actions 容器 TZ=UTC，北京时间 08:00 触发时 UTC 才 00:00。
    若用 datetime.now()，"今天是哪天"和"过了发送时间没"都会错 8 小时，
    导致任务每天静默跳过（退出码 0，workflow 显示成功但邮件没发）。
    """
    name = str((config.get("settings") or {}).get("timezone", "") or "").strip()
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except ImportError:
        log.info("当前 Python 没有 zoneinfo 支持，timezone=%r 未生效（用系统本地时区）。", name)
        return None
    except Exception:
        # Windows 默认没装 tzdata，会走到这里；本地时区通常已经对了。
        log.info("时区库里找不到 %r（Windows 需 pip install tzdata），改用系统本地时区。", name)
        return None


def current_now(config: dict | None = None) -> datetime:
    """当前时间。配置了 settings.timezone 就按该时区，否则用系统本地时间。"""
    tz = _resolve_tz(config or {})
    if tz is not None:
        return datetime.now(tz)
    return datetime.now()


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
ZELI_CATEGORY = "Hacker News 中文摘要（Zeli）"
GITHUB_FALLBACK_CATEGORY = "GitHub 开源项目增速榜"


def main() -> int:
    parser = argparse.ArgumentParser(description="每日科技新闻收集与邮件推送")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只抓取不发送邮件")
    parser.add_argument("--force", action="store_true", help="忽略“今天已发送/未到时间”限制，强制执行")
    args = parser.parse_args()

    config_path = resolve_config_path(args.config)
    log.info("加载配置: %s", config_path)
    config = load_config(config_path)

    now = current_now(config)
    tz_label = (now.tzname()
                or now.astimezone().tzname
                or "系统本地时间")
    today = now.strftime("%Y-%m-%d")
    send_after_hour = int(config["settings"].get("send_after_hour", 8))

    # 去重与时间窗判断：避免定时任务与登录补跑重复发送。
    # CI（GitHub Actions）容器时区是 UTC，若按 UTC 判断，"今天"和"发送时间"
    # 都会错 8 小时——所以用配置的显式时区来算，而不是容器本地时间。
    if not args.dry_run and not args.force:
        state = load_state()
        if state.get("last_sent_date") == today:
            log.info("今天(%s %s)已经成功发送过邮件，本次跳过。", today, tz_label)
            return 0
        if now.hour < send_after_hour:
            log.info("当前 %s(%s) 未到发送时间(%02d:00)，跳过，等待定时触发。",
                     now.strftime("%H:%M"), tz_label, send_after_hour)
            return 0

    log.info("开始抓取新闻源…")
    grouped = collect_news(config)

    log.info("抓取 Hacker News 中文摘要（Zeli）…")
    zeli_items = fetch_zeli(config)
    if zeli_items:
        # 分类名取常量而非 items[0]，避免单条缺字段时整组挂错键
        grouped[ZELI_CATEGORY] = zeli_items

    # GitHub 趋势榜：优先用无噪的榜单（数据现成、含 1 日增量与中文描述），
    # 抓取或解析失败时回退到自建的 star 快照增量算法。
    gh_items: list[dict] = []
    wz_enabled = bool((config.get("wuzao") or {}).get("enabled", False))
    if wz_enabled:
        log.info("抓取 GitHub 趋势榜（无噪）…")
        gh_items = fetch_wuzao_trends(config)

    if not gh_items:
        if wz_enabled:
            log.info("无噪榜单不可用，回退到自建 GitHub 增速榜。")
        log.info("抓取 GitHub 7 日 star 增速榜…")
        gh_items = fetch_github_trending(config)
    if gh_items:
        # 分类名以实际数据为准（无噪榜与自建榜的标题不同），
        # 但缺字段时退回常量，不依赖 items[0]
        gh_cat = next(
            (it.get("category") for it in gh_items if it.get("category")),
            GITHUB_FALLBACK_CATEGORY,
        )
        grouped[gh_cat] = gh_items

    total = sum(len(v) for v in grouped.values())
    log.info("抓取完成，共整理 %d 条内容。", total)

    log.info("补充新闻配图…")
    enrich_images(grouped, config)

    log.info("开始翻译标题…")
    translate_all(grouped, config)

    subject = f"{config['settings'].get('subject_prefix','【每日科技新闻】')}{today}"

    html_body = build_html(grouped, config)
    text_body = build_text(grouped, config)

    save_archive(config, today, html_body, text_body)

    if args.dry_run:
        log.info("[dry-run] 不发送邮件。邮件主题: %s", subject)
        print(text_body)
        return 0

    try:
        send_email(config, subject, html_body, text_body)
    except Exception as exc:  # noqa: BLE001
        log.error("发送失败: %s", exc)
        write_failure_alert(config, today, exc, resolve_archive_dir(config))
        return 1

    clear_failure_alert()

    # 记录今天已成功发送，供登录补跑去重
    state = load_state()
    state["last_sent_date"] = today
    state["last_sent_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
