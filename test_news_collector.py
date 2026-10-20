# -*- coding: utf-8 -*-
"""news_collector 的单元测试（纯标准库 unittest）。

运行：
    python -m unittest test_news_collector -v
    python test_news_collector.py

覆盖的都是解析 / 清洗 / 渲染这类纯函数，不发网络请求。
"""

import importlib.util
import json
import re
import os
import sys
import unittest
import unittest.mock
import urllib.error
import contextlib
from datetime import datetime as _dt, timezone as _tz, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "news_collector", os.path.join(BASE_DIR, "news_collector.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["news_collector"] = module
    spec.loader.exec_module(module)
    return module


nc = _load_module()


class _OfflineGuard(unittest.TestCase):
    """基类：保证任何测试都不会发出真实网络请求。

    起因：单测只mock 了 _mymemory，没 mock 火山后端。一旦环境里
    存在 VOLC_ACCESS_KEY/VOLC_SECRET_KEY（本地 .env、CI secrets
    都会注入），_resolve_translate_provider 就选了火山，测试直接
    打真接口 —— 表现为 403 / 429，并触发熔断把后续断言全带崩。

    这里在 setUp 里把两个后端和网络层一起替换掉：既杜绝外发请求，
    也让"选了哪个后端"变成可断言的东西。
    """

    def setUp(self):
        self._saved_env = {}
        for key in ("VOLC_ACCESS_KEY", "VOLC_SECRET_KEY", "VOLC_REGION"):
            self._saved_env[key] = os.environ.pop(key, None)
        self._saved = {
            name: getattr(nc, name)
            for name in ("_volc_translate_batch", "_mymemory")
        }
        self.volc_calls = []
        self.mymemory_calls = []

        def fake_volc(texts, src, tgt, ak, sk, region, timeout):
            self.volc_calls.append(list(texts))
            return [f"[VT]{t}" for t in texts]

        def fake_mymemory(text, src, tgt, timeout):
            self.mymemory_calls.append(text)
            return f"[MT]{text}"

        nc._volc_translate_batch = fake_volc
        nc._mymemory = fake_mymemory

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(nc, name, fn)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _counter():
    i = 0
    while True:
        yield i
        i += 1


_COUNTER = _counter()


@contextlib.contextmanager
def _tmpdir():
    """项目内临时目录（沙箱里系统临时目录清理会被拒绝）。"""
    import shutil
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_tmp")
    os.makedirs(base, exist_ok=True)
    d = os.path.join(base, f"t{os.getpid()}_{next(_COUNTER)}")
    os.makedirs(d, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 缓存文件重定向
#
# 起因：TestFetchWuzaoTrends 把 urlopen 打成返回示例 HTML，而
# fetch_wuzao_trends 里的 _fill_github_descriptions 会把这些示例 HTML
# 当成"GitHub 原简介"存进 cache/github_descriptions.json —— 单测污染
# 了真实缓存，线上会出现 aaa/one、bbb/two 这种根本不存在于 GitHub 的
# 项目。
#
# 修法：整个测试会话期间把四个缓存文件的路径指到临时目录。
# --------------------------------------------------------------------------- #
_STATE_FILE_NAMES = (
    "GH_STARS_FILE", "GH_DESC_FILE", "CACHE_FILE", "STATE_FILE",
)

_ORIG_STATE_PATHS = {name: getattr(nc, name) for name in _STATE_FILE_NAMES}
_ORIG_CACHE_DIR = nc.CACHE_DIR
_REDIRECTED = None


def setUpModule():
    """把缓存写入重定向到项目内临时目录，别碰真实 cache/。"""
    global _REDIRECTED
    import shutil
    base = os.path.join(BASE_DIR, "_test_tmp")
    os.makedirs(base, exist_ok=True)
    d = os.path.join(base, f"cache_{os.getpid()}")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    _REDIRECTED = d
    nc.CACHE_DIR = d
    for name in _STATE_FILE_NAMES:
        setattr(nc, name, os.path.join(d, os.path.basename(_ORIG_STATE_PATHS[name])))


def tearDownModule():
    """还原真实路径，并把测试期间写出的缓存清掉。"""
    global _REDIRECTED
    import shutil
    nc.CACHE_DIR = _ORIG_CACHE_DIR
    for name in _STATE_FILE_NAMES:
        setattr(nc, name, _ORIG_STATE_PATHS[name])
    if _REDIRECTED:
        shutil.rmtree(_REDIRECTED, ignore_errors=True)
    _REDIRECTED = None



class TestStripNs(unittest.TestCase):
    def test_strips_namespace_and_lowercases(self):
        self.assertEqual(nc._strip_ns("{http://www.w3.org/2005/Atom}entry"), "entry")
        self.assertEqual(nc._strip_ns("ITEM"), "item")
        self.assertEqual(nc._strip_ns("title"), "title")


class TestParseDate(unittest.TestCase):
    def test_rfc822(self):
        dt = nc._parse_date("Tue, 17 Oct 2026 08:30:00 +0000")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 10)
        self.assertEqual(dt.day, 17)
        self.assertIsNotNone(dt.tzinfo)

    def test_iso8601_z_suffix(self):
        dt = nc._parse_date("2026-10-17T08:30:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.utcoffset().total_seconds(), 0)

    def test_custom_format(self):
        self.assertIsNotNone(nc._parse_date("Oct 16, 2026 2:25pm"))

    def test_empty_and_garbage_return_none(self):
        self.assertIsNone(nc._parse_date(""))
        self.assertIsNone(nc._parse_date("not a date at all"))


class TestSanitizeXml(unittest.TestCase):
    def test_escapes_bare_ampersand(self):
        out = nc._sanitize_xml(b"<t>a & b</t>").decode("utf-8")
        self.assertIn("a &amp; b", out)

    def test_keeps_valid_entities(self):
        out = nc._sanitize_xml(b"<t>&amp; &lt; &#39;</t>").decode("utf-8")
        self.assertIn("&amp;", out)
        self.assertIn("&lt;", out)
        self.assertIn("&#39;", out)

    def test_preserves_cdata_content_verbatim(self):
        """回归：早期版本会把 CDATA 内的 & 再转义一次，产生字面的 &amp;。"""
        raw = b'<d><![CDATA[AT&T &amp; co]]></d>'
        out = nc._sanitize_xml(raw).decode("utf-8")
        self.assertIn("CDATA[AT&T &amp; co]", out)
        self.assertNotIn("&amp;amp;", out)

    def test_strips_bom_and_control_chars(self):
        out = nc._sanitize_xml(b"\xef\xbb\xbf<t>a\x08b</t>").decode("utf-8")
        self.assertTrue(out.startswith("<t>"))
        self.assertIn("ab", out)


class TestExtractLink(unittest.TestCase):
    """回归：Atom 的 rel="self" 曾被当成文章链接。"""

    def test_rss_text_link(self):
        import xml.etree.ElementTree as ET

        node = ET.fromstring("<item><link>http://rss/</link></item>")
        self.assertEqual(nc._extract_link(node), "http://rss/")

    def test_atom_prefers_alternate_over_self(self):
        import xml.etree.ElementTree as ET

        xml = (
            '<entry xmlns="http://www.w3.org/2005/Atom">'
            '<link rel="self" href="http://feed/"/>'
            '<link rel="alternate" href="http://article/"/>'
            "</entry>"
        )
        node = ET.fromstring(xml)
        self.assertEqual(nc._extract_link(node), "http://article/")

    def test_atom_without_rel_defaults_to_alternate(self):
        import xml.etree.ElementTree as ET

        xml = (
            '<entry xmlns="http://www.w3.org/2005/Atom">'
            '<link href="http://article/"/>'
            "</entry>"
        )
        node = ET.fromstring(xml)
        self.assertEqual(nc._extract_link(node), "http://article/")

    def test_atom_only_self_falls_back_empty(self):
        import xml.etree.ElementTree as ET

        xml = (
            '<entry xmlns="http://www.w3.org/2005/Atom">'
            '<link rel="self" href="http://feed/"/>'
            "</entry>"
        )
        node = ET.fromstring(xml)
        self.assertEqual(nc._extract_link(node), "")


class TestParseFeed(unittest.TestCase):
    def test_parses_rss_item(self):
        raw = (
            b'<?xml version="1.0"?><rss><channel><item>'
            b"<title>Hello</title><link>http://x/</link>"
            b"<pubDate>Tue, 17 Oct 2026 08:30:00 +0000</pubDate>"
            b"<description>Body</description>"
            b"</item></channel></rss>"
        )
        items = nc.parse_feed(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Hello")
        self.assertEqual(items[0]["link"], "http://x/")

    def test_skips_item_without_title(self):
        raw = (
            b'<?xml version="1.0"?><rss><channel>'
            b"<item><link>http://x/</link></item>"
            b"<item><title>Keep</title><link>http://y/</link></item>"
            b"</channel></rss>"
        )
        items = nc.parse_feed(raw)
        self.assertEqual([i["title"] for i in items], ["Keep"])

    def test_recovers_from_bare_ampersand(self):
        raw = b'<?xml version="1.0"?><rss><channel><item><title>A & B</title></item></channel></rss>'
        items = nc.parse_feed(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "A & B")

    def test_broken_xml_returns_empty_list(self):
        self.assertEqual(nc.parse_feed(b"<rss><channel><item>"), [])


class TestCleanSummary(unittest.TestCase):
    def test_strips_tags_and_collapses_whitespace(self):
        self.assertEqual(nc._clean_summary("<p>  a   b </p>"), "a b")

    def test_unescapes_entities(self):
        self.assertEqual(nc._clean_summary("AT&amp;T &lt;b&gt;"), "AT&T <b>")

    def test_truncates_with_ellipsis(self):
        out = nc._clean_summary("x" * 300, limit=50)
        self.assertEqual(len(out), 51)
        self.assertTrue(out.endswith("…"))

    def test_empty_input(self):
        self.assertEqual(nc._clean_summary(""), "")


class TestNormalizeUrl(unittest.TestCase):
    def test_protocol_relative(self):
        self.assertEqual(nc._normalize_url("//cdn/img.png"), "https://cdn/img.png")

    def test_relative_against_base(self):
        self.assertEqual(nc._normalize_url("/a.png", base="http://s/x"), "http://s/a.png")

    def test_data_uri_rejected(self):
        self.assertEqual(nc._normalize_url("data:image/png;base64,AAA"), "")

    def test_empty(self):
        self.assertEqual(nc._normalize_url(""), "")


class TestDeepMerge(unittest.TestCase):
    def test_override_wins(self):
        self.assertEqual(nc.deep_merge({"a": 1, "b": 2}, {"b": 3}), {"a": 1, "b": 3})

    def test_recursive_merge(self):
        base = {"s": {"x": 1, "y": 2}}
        self.assertEqual(nc.deep_merge(base, {"s": {"y": 9}}), {"s": {"x": 1, "y": 9}})

    def test_base_not_mutated(self):
        base = {"s": {"x": 1}}
        nc.deep_merge(base, {"s": {"x": 2}})
        self.assertEqual(base, {"s": {"x": 1}})


class TestDetectLang(unittest.TestCase):
    def test_chinese(self):
        self.assertEqual(nc._detect_lang("今天的科技新闻"), "zh")

    def test_english(self):
        self.assertEqual(nc._detect_lang("Today's tech news"), "en")


class TestLooksImage(unittest.TestCase):
    def test_true_for_extensions(self):
        for u in ("http://a/b.jpg", "http://a/b.PNG?x=1", "http://a/b.webp"):
            self.assertTrue(nc._looks_image(u), u)

    def test_false_for_html(self):
        self.assertFalse(nc._looks_image("http://a/article.html"))


class TestBuildHtmlEscaping(unittest.TestCase):
    """P0 回归：meta_override 曾未转义，外部可控内容可注入 HTML。"""

    def _render(self, item):
        grouped = {"分类": [item]}
        return nc.build_html(grouped, {"settings": {"subject_prefix": ""}})

    def test_meta_override_is_escaped(self):
        out = self._render(
            {
                "title": "T",
                "link": "http://x/",
                "summary": "",
                "image": "",
                "source": "S",
                "published": None,
                "meta_override": '<b>x</b><img src=y onerror=alert(1)>',
            }
        )
        # 尖括号必须被中和成实体，标签不得成为可执行 HTML
        self.assertNotIn("<b>", out)
        self.assertNotIn("<img", out)
        self.assertIn("&lt;b&gt;", out)
        self.assertIn("&lt;img", out)

    def test_title_and_summary_are_escaped(self):
        out = self._render(
            {
                "title": "<script>bad()</script>",
                "link": "http://x/",
                "summary": "<b>s</b>",
                "image": "",
                "source": "S",
                "published": None,
            }
        )
        self.assertNotIn("<script>", out)
        self.assertNotIn("<b>s</b>", out)

    def test_image_url_quote_escaped(self):
        out = self._render(
            {
                "title": "T",
                "link": "http://x/",
                "summary": "",
                "image": 'http://i/a.png" onerror="alert(1)',
                "source": "S",
                "published": None,
            }
        )
        self.assertNotIn('onerror="alert(1)"', out)

    def test_default_meta_built_from_source_and_time(self):
        out = self._render(
            {
                "title": "T",
                "link": "http://x/",
                "summary": "",
                "image": "",
                "source": "MIT",
                "published": None,
            }
        )
        self.assertIn("MIT · 时间未知", out)


class TestEnrichImagesLimit(unittest.TestCase):
    """P1 回归：og_image_limit 曾按成功数计数，失败时请求数无上限。"""

    def _run(self, items, **settings):
        cfg = {
            "fetch_og_image": True,
            "request_timeout": 1,
            "og_image_delay": 0,
            "og_image_limit": 60,
            "og_image_workers": 6,
        }
        cfg.update(settings)
        return cfg

    def test_limit_counts_attempts_not_successes(self):
        calls = []
        lock = __import__("threading").Lock()

        def fake_fetch(url, timeout):
            with lock:
                calls.append(url)
            return ""  # 全部失败

        original = nc.fetch_og_image
        nc.fetch_og_image = fake_fetch
        try:
            items = [{"link": f"http://x/{i}", "image": ""} for i in range(20)]
            nc.enrich_images({"c": items}, {"settings": self._run(items, og_image_limit=5)})
        finally:
            nc.fetch_og_image = original
        self.assertEqual(len(calls), 5)

    def test_disabled_does_nothing(self):
        calls = []
        original = nc.fetch_og_image
        nc.fetch_og_image = lambda u, t: calls.append(u) or ""
        try:
            nc.enrich_images(
                {"c": [{"link": "http://x/", "image": ""}]},
                {"settings": self._run([], fetch_og_image=False, og_image_limit=5)},
            )
        finally:
            nc.fetch_og_image = original
        self.assertEqual(calls, [])


class TestEnrichImagesConcurrency(unittest.TestCase):
    """补图并发化：请求数仍精确受 limit 控制，且结果不错配。"""

    def _cfg(self, **over):
        s = {"fetch_og_image": True, "request_timeout": 1,
             "og_image_delay": 0, "og_image_limit": 60, "og_image_workers": 6}
        s.update(over)
        return {"settings": s}

    def _items(self, n):
        return [{"link": f"http://x/{i}", "image": ""} for i in range(n)]

    def test_parallel_faster_than_serial(self):
        """并发应显著快于串行（每个请求模拟 0.2s 真实网络）。"""
        import time as _time
        lock = _time_lock = __import__("threading").Lock()
        done_count = []

        def slow(url, timeout):
            _time.sleep(0.20)
            with _time_lock:
                done_count.append(url)
            return "http://img/x.png"

        original = nc.fetch_og_image
        nc.fetch_og_image = slow
        try:
            t0 = _time.perf_counter()
            items = self._items(12)
            nc.enrich_images({"c": items}, self._cfg(og_image_workers=6))
            parallel = _time.perf_counter() - t0

            t0 = _time.perf_counter()
            items2 = self._items(12)
            nc.enrich_images({"c": items2}, self._cfg(og_image_workers=1))
            serial = _time.perf_counter() - t0
        finally:
            nc.fetch_og_image = original

        self.assertLess(parallel, serial * 0.75)

    def test_results_not_mismatched_to_items(self):
        """并发下每条 URL 必须拿到属于自己的图，不能张冠李戴。"""
        def fetch(url, timeout):
            # 返回带 URL 标识的图片，便于校验配对关系
            return f"http://img/{url.rsplit('/', 1)[-1]}.png"

        original = nc.fetch_og_image
        nc.fetch_og_image = fetch
        try:
            items = self._items(20)
            nc.enrich_images({"c": items}, self._cfg(og_image_workers=8))
        finally:
            nc.fetch_og_image = original
        for i, it in enumerate(items):
            self.assertEqual(it["image"], f"http://img/{i}.png")

    def test_limit_truncates_before_dispatch(self):
        """limit 应在提交前截断，请求数精确等于 limit。"""
        calls = []
        lock = __import__("threading").Lock()

        def fake(url, timeout):
            with lock:
                calls.append(url)
            return "http://img/a.png"

        original = nc.fetch_og_image
        nc.fetch_og_image = fake
        try:
            items = self._items(50)
            nc.enrich_images({"c": items}, self._cfg(og_image_limit=8, og_image_workers=6))
        finally:
            nc.fetch_og_image = original
        self.assertEqual(len(calls), 8)
        # 被截断的条目保持空图，不报错
        self.assertEqual(sum(1 for it in items if it["image"]), 8)

    def test_items_already_have_image_are_skipped(self):
        calls = []
        original = nc.fetch_og_image
        nc.fetch_og_image = lambda u, t: calls.append(u) or "x"
        try:
            items = [{"link": "http://a", "image": "http://has"},
                     {"link": "http://b", "image": ""}]
            nc.enrich_images({"c": items}, self._cfg())
        finally:
            nc.fetch_og_image = original
        self.assertEqual(calls, ["http://b"])
        self.assertEqual(items[0]["image"], "http://has")

    def test_failures_leave_image_empty(self):
        original = nc.fetch_og_image
        nc.fetch_og_image = lambda u, t: ""
        try:
            items = self._items(6)
            nc.enrich_images({"c": items}, self._cfg())
        finally:
            nc.fetch_og_image = original
        self.assertTrue(all(it["image"] == "" for it in items))

    def test_exception_in_one_worker_does_not_abort_others(self):
        """某个请求抛异常时，其余条目仍应完成补图（不整批失败）。"""
        def flaky(url, timeout):
            if url.endswith("/3"):
                raise OSError("boom")
            return f"http://img/{url.rsplit('/', 1)[-1]}.png"

        original = nc.fetch_og_image
        nc.fetch_og_image = flaky
        try:
            items = self._items(8)
            # 不应抛出异常，且失败之外的条目都要拿到图
            nc.enrich_images({"c": items}, self._cfg(og_image_workers=4))
        finally:
            nc.fetch_og_image = original
        got = [it["image"] for it in items if it["image"]]
        self.assertEqual(len(got), 7)
        self.assertEqual(items[3]["image"], "")   # 抛异常的那条留空

    def test_single_worker_equivalent_to_serial(self):
        def fetch(url, timeout):
            return f"http://img/{url.rsplit('/', 1)[-1]}.png"

        original = nc.fetch_og_image
        nc.fetch_og_image = fetch
        try:
            items = self._items(10)
            nc.enrich_images({"c": items}, self._cfg(og_image_workers=1))
        finally:
            nc.fetch_og_image = original
        self.assertEqual(len([it for it in items if it["image"]]), 10)

    def test_empty_missing_list_is_noop(self):
        calls = []
        original = nc.fetch_og_image
        nc.fetch_og_image = lambda u, t: calls.append(u) or "x"
        try:
            nc.enrich_images({"c": []}, self._cfg())
        finally:
            nc.fetch_og_image = original
        self.assertEqual(calls, [])


class TestCollectNewsDedup(unittest.TestCase):
    def _config(self, **over):
        settings = {"request_timeout": 1, "hours_lookback": 24, "max_per_category": 10}
        settings.update(over)
        # collect_news 遍历 sources 并调用 fetch_source；这里给一个占位源，
        # fetch_source 已被打桩，返回什么与源本身无关
        return {"settings": settings, "sources": [{"name": "stub", "category": "AI"}]}

    def _patch(self, items):
        """打桩 fetch_source，返回 items。"""
        original = nc.fetch_source
        nc.fetch_source = lambda source, timeout: list(items)
        return original

    def test_dedup_by_title_and_cap_per_category(self):
        now = nc.datetime.now(nc.timezone.utc)

        def mk(t, c="AI"):
            return {
                "title": t,
                "link": f"http://{t}",
                "category": c,
                "source": "s",
                "published": now,
                "summary": "",
                "image": "",
            }

        items = [
            mk("dup"),
            mk("dup"),        # 同标题，第二个应被丢弃
            mk("other"),
            mk("third"),
            mk("third"),      # 同标题，第二个应被丢弃
        ]
        original = self._patch(items)
        try:
            grouped = nc.collect_news(self._config(max_per_category=2))
        finally:
            nc.fetch_source = original

        # 去重后剩 dup / other / third 共 3 条，再按上限截断为 2 条
        titles = [i["title"] for i in grouped["AI"]]
        self.assertEqual(len(titles), 2)
        self.assertEqual(titles.count("dup"), 1)
        self.assertEqual(titles.count("other"), 1)
        self.assertNotIn("third", titles)

    def test_dedup_keeps_all_when_cap_not_reached(self):
        now = nc.datetime.now(nc.timezone.utc)
        items = [
            {"title": t, "link": f"http://{t}", "category": "AI", "source": "s",
             "published": now, "summary": "", "image": ""}
            for t in ("dup", "dup", "third", "third")
        ]
        original = self._patch(items)
        try:
            grouped = nc.collect_news(self._config(max_per_category=10))
        finally:
            nc.fetch_source = original
        titles = sorted(i["title"] for i in grouped["AI"])
        self.assertEqual(titles, ["dup", "third"])

    def test_old_items_filtered_out(self):
        old = nc.datetime.now(nc.timezone.utc) - nc.timedelta(hours=48)
        items = [
            {"title": "old", "link": "http://o", "category": "AI",
             "source": "s", "published": old, "summary": "", "image": ""},
            {"title": "undated", "link": "http://u", "category": "AI",
             "source": "s", "published": None, "summary": "", "image": ""},
        ]
        original = self._patch(items)
        try:
            grouped = nc.collect_news(self._config())
        finally:
            nc.fetch_source = original
        titles = [i["title"] for i in grouped["AI"]]
        self.assertIn("undated", titles)
        self.assertNotIn("old", titles)

    def test_case_insensitive_title_dedup(self):
        now = nc.datetime.now(nc.timezone.utc)
        items = [
            {"title": "Apple News", "link": "http://a", "category": "AI",
             "source": "s", "published": now, "summary": "", "image": ""},
            {"title": "apple news", "link": "http://b", "category": "AI",
             "source": "s", "published": now, "summary": "", "image": ""},
        ]
        original = self._patch(items)
        try:
            grouped = nc.collect_news(self._config())
        finally:
            nc.fetch_source = original
        self.assertEqual(len(grouped["AI"]), 1)


class TestCollectNewsConcurrency(unittest.TestCase):
    def test_parallel_fetch_gathers_every_source(self):
        """并发抓取不能丢源：即使某个源很慢，其条目也必须出现。"""
        import time as _dt

        def slow_fetch(source, timeout):
            if source["name"] == "slow":
                _dt.sleep(0.20)
            else:
                _dt.sleep(0.01)
            return [{
                "title": source["name"],
                "link": "http://" + source["name"],
                "category": "AI",
                "source": source["name"],
                # 固定同一时刻，dedup 的排序不会打乱顺序
                "published": nc.datetime.now(nc.timezone.utc),
                "summary": "",
                "image": "",
            }]

        sources = [{"name": n, "category": "AI"} for n in ("slow", "b", "c", "d")]
        original = nc.fetch_source
        nc.fetch_source = slow_fetch
        try:
            grouped = nc.collect_news(
                {"settings": {"request_timeout": 1, "hours_lookback": 24,
                              "max_per_category": 10, "fetch_workers": 4},
                 "sources": sources}
            )
        finally:
            nc.fetch_source = original

        names = sorted(i["source"] for i in grouped["AI"])
        self.assertEqual(names, ["b", "c", "d", "slow"])

    def test_parallel_slower_than_serial_would_be(self):
        """并发总耗时应接近最慢单源，而非各源之和。"""
        import time as _dt

        def slow_fetch(source, timeout):
            _dt.sleep(0.30)
            return []

        sources = [{"name": f"s{i}", "category": "AI"} for i in range(4)]
        original = nc.fetch_source
        nc.fetch_source = slow_fetch
        try:
            cfg = {"settings": {"request_timeout": 1, "hours_lookback": 24,
                                "max_per_category": 1, "fetch_workers": 4},
                   "sources": sources}
            t0 = _dt.perf_counter()
            nc.collect_news(cfg)
            parallel = _dt.perf_counter() - t0

            cfg2 = dict(cfg)
            cfg2["settings"] = dict(cfg["settings"], fetch_workers=1)
            t0 = _dt.perf_counter()
            nc.collect_news(cfg2)
            serial = _dt.perf_counter() - t0
        finally:
            nc.fetch_source = original

        # 并发应显著快于串行（这里留足余量，避免 CI 抖动导致偶发失败）
        self.assertLess(parallel, serial * 0.75)

    def test_failing_source_does_not_abort_others(self):
        def flaky(source, timeout):
            if source["name"] == "boom":
                raise RuntimeError("simulated crash")
            return [{
                "title": source["name"],
                "link": "http://" + source["name"],
                "category": "AI",
                "source": source["name"],
                "published": nc.datetime.now(nc.timezone.utc),
                "summary": "",
                "image": "",
            }]

        sources = [{"name": "boom", "category": "AI"}, {"name": "ok", "category": "AI"}]
        original = nc.fetch_source
        nc.fetch_source = flaky
        try:
            grouped = nc.collect_news(
                {"settings": {"request_timeout": 1, "hours_lookback": 24,
                              "max_per_category": 10, "fetch_workers": 2},
                 "sources": sources}
            )
        finally:
            nc.fetch_source = original
        self.assertEqual([i["source"] for i in grouped["AI"]], ["ok"])


class TestMaxPerCategoryBy(unittest.TestCase):
    """分类级条数覆盖：源多的分类需要更大的上限。"""

    def _collect(self, caps, count=20):
        now = nc.datetime.now(nc.timezone.utc)
        items = [
            {"title": f"{cat}-{i}", "link": f"http://{cat}{i}", "category": cat,
             "source": "s", "published": now, "summary": "", "image": ""}
            for cat in ("网络安全", "医疗健康")
            for i in range(count)
        ]
        original = nc.fetch_source
        nc.fetch_source = lambda source, timeout: list(items)
        try:
            return nc.collect_news({
                "settings": {"request_timeout": 1, "hours_lookback": 24,
                             "fetch_workers": 1,
                             "sources": [{"name": "stub", "category": "AI"}], **caps},
                "sources": [{"name": "stub", "category": "AI"}],
            })
        finally:
            nc.fetch_source = original

    def test_override_applies_to_named_category_only(self):
        grouped = self._collect(
            {"max_per_category": 8, "max_per_category_by": {"网络安全": 12}}
        )
        self.assertEqual(len(grouped["网络安全"]), 12)
        self.assertEqual(len(grouped["医疗健康"]), 8)   # 未覆盖的仍用全局值

    def test_no_override_uses_global(self):
        grouped = self._collect({"max_per_category": 6})
        self.assertEqual(len(grouped["网络安全"]), 6)
        self.assertEqual(len(grouped["医疗健康"]), 6)

    def test_invalid_override_type_is_ignored(self):
        # 不是字典时应回退到全局值，而不是崩溃
        grouped = self._collect(
            {"max_per_category": 5, "max_per_category_by": ["bad", "shape"]}
        )
        self.assertEqual(len(grouped["网络安全"]), 5)

    def test_non_numeric_override_value_skipped(self):
        grouped = self._collect(
            {"max_per_category": 5, "max_per_category_by": {"网络安全": "many"}}
        )
        self.assertEqual(len(grouped["网络安全"]), 5)


class TestGithubSearchPaging(unittest.TestCase):
    """GitHub 搜索翻页：候选池天花板问题。"""

    def _patch_urlopen(self, pages):
        """pages: dict page_number -> items 列表"""
        import json as _json
        from urllib.parse import parse_qs, urlparse

        calls = []

        class FakeResp:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return _json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=None):
            url = request.full_url
            calls.append(url)
            # 注意：URL 里同时有 per_page 和 page，必须用 query 解析，
            # 直接 split("page=") 会误匹配到 "per_page="。
            q = parse_qs(urlparse(url).query)
            page = int(q.get("page", ["1"])[0])
            items = pages.get(page, [])
            return FakeResp({"items": items, "total_count": 300})

        return fake_urlopen, calls

    def test_stops_on_short_page(self):
        fake, calls = self._patch_urlopen({
            1: [{"full_name": f"r{i}"} for i in range(3)],
        })
        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = fake
        try:
            got = nc._github_search("q", "", 5, per_page=100, max_pages=3)
        finally:
            nc.urllib.request.urlopen = original
        # 第一页不满 100 → 立即停止，不该继续翻
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(got), 3)

    def test_continues_until_pages_exhausted(self):
        p1 = [{"full_name": f"a{i}"} for i in range(100)]
        p2 = [{"full_name": f"b{i}"} for i in range(100)]
        fake, calls = self._patch_urlopen({1: p1, 2: p2, 3: []})
        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = fake
        try:
            got = nc._github_search("q", "", 5, per_page=100, max_pages=3)
        finally:
            nc.urllib.request.urlopen = original
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(got), 200)   # 第3页空，第1、2页各100

    def test_dedups_across_pages(self):
        dup = [{"full_name": "same"} for _ in range(100)]
        fake, calls = self._patch_urlopen({1: dup, 2: dup})
        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = fake
        try:
            got = nc._github_search("q", "", 5, per_page=100, max_pages=2)
        finally:
            nc.urllib.request.urlopen = original
        self.assertEqual(len(got), 1)

    def test_stops_at_api_hard_limit(self):
        """total_count=1000 是 API 硬上限：翻到第 10 页就停，不该翻满 max_pages。"""
        import json as _json
        from urllib.parse import parse_qs, urlparse

        calls = []

        class _FakeResp:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return _json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_with_limit(request, timeout=None):
            url = request.full_url
            calls.append(url)
            q = parse_qs(urlparse(url).query)
            page = int(q.get("page", ["1"])[0])
            # 每页给不同的 full_name，模拟真实分页
            items = [{"full_name": f"p{page}-{i}"} for i in range(100)]
            return _FakeResp({"items": items, "total_count": 1000})

        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = fake_with_limit
        try:
            got = nc._github_search("q", "", 5, per_page=100, max_pages=50)
        finally:
            nc.urllib.request.urlopen = original
        self.assertEqual(len(calls), 10)   # 1000/100，硬上限
        self.assertEqual(len(got), 1000)

    def test_dup_pages_do_not_prevent_hard_limit_stop(self):
        """回归：早期用去重后的 len(collected) 比较 total_count，
        页面有重复项时永远达不到上限，会白翻到 max_pages。"""
        import json as _json
        from urllib.parse import parse_qs, urlparse

        calls = []

        class _FakeResp:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return _json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def dup_pages(request, timeout=None):
            url = request.full_url
            calls.append(url)
            q = parse_qs(urlparse(url).query)
            page = int(q.get("page", ["1"])[0])
            # 每页都返回同样的 100 条 → 去重后只剩 100
            items = [{"full_name": f"same{i}"} for i in range(100)]
            return _FakeResp({"items": items, "total_count": 1000})

        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = dup_pages
        try:
            got = nc._github_search("q", "", 5, per_page=100, max_pages=50)
        finally:
            nc.urllib.request.urlopen = original
        self.assertEqual(len(calls), 10)
        self.assertEqual(len(got), 100)

    def test_sort_param_is_passed_through(self):
        fake, calls = self._patch_urlopen({1: []})
        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = fake
        try:
            nc._github_search("q", "", 5, per_page=100, max_pages=1, sort="updated")
        finally:
            nc.urllib.request.urlopen = original
        self.assertIn("sort=updated", calls[0])

    def test_page_failure_keeps_partial_results(self):
        """翻页途中失败不应丢掉已取到的数据。"""
        import json as _json
        from urllib.parse import parse_qs, urlparse

        full = [{"full_name": f"ok{i}"} for i in range(100)]

        class _FakeResp:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return _json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def flaky(request, timeout=None):
            url = request.full_url
            q = parse_qs(urlparse(url).query)
            if int(q.get("page", ["1"])[0]) >= 2:
                raise OSError("network died")
            return _FakeResp({"items": full, "total_count": 300})

        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = flaky
        try:
            got = nc._github_search("q", "", 5, per_page=100, max_pages=3)
        finally:
            nc.urllib.request.urlopen = original
        self.assertEqual(len(got), 100)

    def test_first_page_failure_returns_empty(self):
        def boom(request, timeout=None):
            raise OSError("down")

        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = boom
        try:
            got = nc._github_search("q", "", 5)
        finally:
            nc.urllib.request.urlopen = original
        self.assertEqual(got, [])


class TestGithubTrendingPool(unittest.TestCase):
    """增速榜候选池：多种查询合并 + 池子上限。"""

    def _run(self, settings_extra=None, pages=None):
        queries_seen = []

        class _FakeResp:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return __import__("json").dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_search(query, token, timeout, per_page=100, max_pages=1, sort="stars"):
            queries_seen.append((query, sort))
            items = pages.get((query, sort), [])
            return items

        repo = lambda full, created: {
            "full_name": full, "html_url": "http://" + full,
            "description": "d", "language": "Python",
            "created_at": created, "pushed_at": created,
            "stargazers_count": 500,
        }

        settings = {"github_trending": True, "github_count": 20,
                    "github_since_days": 7, "github_min_stars": 100,
                    "github_lookback_days": 60, "request_timeout": 5}
        settings.update(settings_extra or {})

        today = nc.datetime.now(nc.timezone.utc).date()
        short = (today - nc.timedelta(days=7)).isoformat()
        long_ = (today - nc.timedelta(days=60)).isoformat()

        default_pages = {
            (f"created:>{long_} stars:>{settings['github_min_stars']}", "stars"): [],
            (f"created:>{long_} stars:>{settings['github_min_stars']}", "updated"): [],
            (f"created:>{short} stars:>{settings['github_min_stars']}", "stars"): [],
            (f"created:>{short} stars:>{settings['github_min_stars']}", "updated"):
                [repo("new/proj", f"{short}T00:00:00Z")],
        }
        pages = pages or default_pages

        orig_search = nc._github_search
        orig_load = nc._load_gh_stars
        orig_save = nc._save_gh_stars
        nc._github_search = fake_search
        nc._load_gh_stars = lambda: {}
        nc._save_gh_stars = lambda d: None
        try:
            items = nc.fetch_github_trending(
                {"settings": settings, "github": {"token": ""}}
            )
        finally:
            nc._github_search = orig_search
            nc._load_gh_stars = orig_load
            nc._save_gh_stars = orig_save
        return items, queries_seen

    def test_runs_both_sort_orders(self):
        """只用 stars 排序会漏掉低星新项目，两种排序都要跑。"""
        _items, queries = self._run()
        sorts = {s for _q, s in queries}
        self.assertEqual(sorts, {"stars", "updated"})
        self.assertEqual(len(queries), 4)   # 2 个时间窗 × 2 种排序

    def test_new_project_appears_in_output(self):
        items, _q = self._run()
        self.assertTrue(any(i["title"] == "new/proj" for i in items))

    def test_pool_cap_limits_candidates(self):
        many = [{"full_name": f"o/p{i}", "html_url": f"http://o/p{i}",
                 "description": "", "language": None,
                 "created_at": "2026-10-01T00:00:00Z",
                 "pushed_at": "2026-10-01T00:00:00Z",
                 "stargazers_count": 300} for i in range(200)]
        items, _q = self._run(
            settings_extra={"github_pool_max": 30},
            pages={("x", "stars"): many, ("x", "updated"): []},
        )
        self.assertLessEqual(len(items), 20)   # 输出仍受 github_count 限制

    def test_disabled_returns_empty(self):
        items, queries = self._run(settings_extra={"github_trending": False})
        self.assertEqual(items, [])
        self.assertEqual(queries, [])


# 一段精简的无噪榜单 HTML，结构与服务端渲染结果一致：
# 仓库链接 → 语言标签 → 描述 → </svg> star ⭐ → 增量
WUZAO_SAMPLE_HTML = """<html><body>
<a href="https://github.com/aaa/one" target="_blank" rel="nofollow noopener noreferrer noindex"
   class="hover:text-primary-600"> aaa/one </a>
<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium
      bg-primary-100 text-primary-800"> TypeScript </span>
<p class="text-sm text-slate-600 dark:text-slate-400 mb-2 line-clamp-2">第一个项目的描述</p>
</svg> 293.0k ⭐ </span>
<div class="text-2xl font-bold text-green-600"> <span> +22,945 </span> </div>

<a href="https://github.com/bbb/two" target="_blank" rel="nofollow noopener noreferrer noindex"
   class="hover:text-primary-600"> bbb/two </a>
<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium
      bg-primary-100 text-primary-800"> Python </span>
<p class="text-sm text-slate-600 dark:text-slate-400 mb-2 line-clamp-2"></p>
</svg> 71.6k ⭐ </span>
<div class="text-2xl font-bold text-green-600"> <span> +2,084 </span> </div>

<a href="https://github.com/ccc/three" target="_blank" rel="nofollow noopener noreferrer noindex"
   class="hover:text-primary-600"> ccc/three </a>
<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium
      bg-primary-100 text-primary-800"> Rust </span>
<p class="text-sm text-slate-600 dark:text-slate-400 mb-2 line-clamp-2">第三个</p>
</svg> 1.2M ⭐ </span>
<div class="text-2xl font-bold text-green-600"> <span> +900 </span> </div>
</body></html>"""


class TestParseWuzaoTrends(unittest.TestCase):
    def test_parses_all_fields(self):
        rows = nc.parse_wuzao_trends(WUZAO_SAMPLE_HTML.encode("utf-8"))
        self.assertEqual(len(rows), 3)
        first = rows[0]
        self.assertEqual(first["repo"], "aaa/one")
        self.assertEqual(first["link"], "https://github.com/aaa/one")
        self.assertEqual(first["gain"], 22945)
        self.assertEqual(first["stars_raw"], "293.0k")
        self.assertEqual(first["language"], "TypeScript")
        self.assertEqual(first["desc"], "第一个项目的描述")

    def test_sorted_by_gain_descending(self):
        rows = nc.parse_wuzao_trends(WUZAO_SAMPLE_HTML.encode("utf-8"))
        gains = [r["gain"] for r in rows]
        self.assertEqual(gains, sorted(gains, reverse=True))
        self.assertEqual(rows[0]["repo"], "aaa/one")

    def test_empty_description_becomes_empty_string(self):
        rows = nc.parse_wuzao_trends(WUZAO_SAMPLE_HTML.encode("utf-8"))
        by_repo = {r["repo"]: r for r in rows}
        self.assertEqual(by_repo["bbb/two"]["desc"], "")

    def test_limit_truncates(self):
        rows = nc.parse_wuzao_trends(WUZAO_SAMPLE_HTML.encode("utf-8"), limit=2)
        self.assertEqual(len(rows), 2)

    def test_limit_zero_means_all(self):
        rows = nc.parse_wuzao_trends(WUZAO_SAMPLE_HTML.encode("utf-8"), limit=0)
        self.assertEqual(len(rows), 3)

    def test_duplicate_repo_deduped(self):
        html = WUZAO_SAMPLE_HTML + """
<a href="https://github.com/aaa/one" target="_blank" class="x"> aaa/one </a>
<span class="rounded text-xs font-medium bg-primary-100"> Go </span>
<p class="text-sm text-slate-600 dark:text-slate-400 mb-2 line-clamp-2">重复</p>
</svg> 5k ⭐ </span>
<div class="text-2xl font-bold text-green-600"> <span> +1 </span> </div>
"""
        rows = nc.parse_wuzao_trends(html.encode("utf-8"))
        self.assertEqual(len(rows), 3)

    def test_garbage_html_returns_empty(self):
        self.assertEqual(nc.parse_wuzao_trends(b"<html>nothing here</html>"), [])

    def test_invalid_utf8_does_not_crash(self):
        raw = WUZAO_SAMPLE_HTML.encode("utf-8") + b"\xff\xfe"
        rows = nc.parse_wuzao_trends(raw)
        self.assertEqual(len(rows), 3)


class TestParseStarDisplay(unittest.TestCase):
    def test_k_suffix(self):
        self.assertEqual(nc._parse_star_display("293.0k"), "293000")
        self.assertEqual(nc._parse_star_display("71.6k"), "71600")

    def test_m_suffix(self):
        self.assertEqual(nc._parse_star_display("1.2M"), "1200000")

    def test_plain_number(self):
        self.assertEqual(nc._parse_star_display("1234"), "1234")

    def test_with_commas(self):
        self.assertEqual(nc._parse_star_display("12,345"), "12345")

    def test_unparseable_returned_as_is(self):
        self.assertEqual(nc._parse_star_display("--"), "--")
        self.assertEqual(nc._parse_star_display(""), "")


class TestFetchWuzaoTrends(unittest.TestCase):
    def _config(self, wuzao_cfg):
        return {
            "settings": {"request_timeout": 20},
            "wuzao": wuzao_cfg,
        }

    def _patch(self, payload=None, exc=None):
        original = nc.urllib.request.urlopen

        class _Resp:
            def __init__(self, data):
                self.data = data

            def read(self):
                return self.data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        calls = []

        def fake(request, timeout=None):
            calls.append(request.full_url)
            if exc:
                raise exc
            return _Resp(payload)

        nc.urllib.request.urlopen = fake
        return original, calls

    def test_disabled_returns_empty(self):
        items = nc.fetch_wuzao_trends(self._config({"enabled": False}))
        self.assertEqual(items, [])

    def test_builds_items_with_rank_and_meta(self):
        orig, _calls = self._patch(payload=WUZAO_SAMPLE_HTML.encode("utf-8"))
        try:
            items = nc.fetch_wuzao_trends(
                self._config({"enabled": True, "period": "daily", "limit": 30})
            )
        finally:
            nc.urllib.request.urlopen = orig
        self.assertEqual(len(items), 3)
        self.assertEqual([i["rank"] for i in items], [1, 2, 3])
        self.assertEqual(items[0]["title"], "aaa/one")
        self.assertIn("1 日 +22,945", items[0]["meta_override"])
        self.assertIn("⭐ 293.0k", items[0]["meta_override"])
        self.assertIn("TypeScript", items[0]["meta_override"])
        # 描述已是中文，不应再送去翻译
        self.assertEqual(items[0]["translate_field"], "")
        self.assertIn("无噪", items[0]["category"])

    def test_period_selects_url(self):
        orig, calls = self._patch(payload=WUZAO_SAMPLE_HTML.encode("utf-8"))
        try:
            nc.fetch_wuzao_trends(
                self._config({"enabled": True, "period": "weekly", "limit": 30})
            )
        finally:
            nc.urllib.request.urlopen = orig
        self.assertIn("/weekly/", calls[0])
        self.assertTrue(calls[0].startswith("https://www.wuzao.com/"))

    def test_unknown_period_falls_back_to_daily(self):
        orig, calls = self._patch(payload=WUZAO_SAMPLE_HTML.encode("utf-8"))
        try:
            nc.fetch_wuzao_trends(
                self._config({"enabled": True, "period": "yearly", "limit": 30})
            )
        finally:
            nc.urllib.request.urlopen = orig
        self.assertIn("/daily/", calls[0])

    def test_network_failure_returns_empty(self):
        orig, _c = self._patch(exc=OSError("network down"))
        try:
            items = nc.fetch_wuzao_trends(
                self._config({"enabled": True, "retries": 1})
            )
        finally:
            nc.urllib.request.urlopen = orig
        self.assertEqual(items, [])

    def test_empty_parse_returns_empty(self):
        orig, _c = self._patch(payload=b"<html>no repos here</html>")
        try:
            items = nc.fetch_wuzao_trends(
                self._config({"enabled": True, "retries": 1})
            )
        finally:
            nc.urllib.request.urlopen = orig
        self.assertEqual(items, [])


class TestBuildHtmlRankBadge(unittest.TestCase):
    def _render(self, item):
        return nc.build_html({"榜": [item]}, {"settings": {"subject_prefix": ""}})

    def _item(self, **over):
        base = {
            "title": "owner/repo", "link": "https://github.com/owner/repo",
            "summary": "", "image": "", "source": "GitHub",
            "published": None, "meta_override": "1 日 +100 · ⭐ 1.0k",
        }
        base.update(over)
        return base

    def test_rank_badge_rendered(self):
        out = self._render(self._item(rank=1))
        self.assertIn("background:#fde68a", out)   # 金色
        self.assertIn(">1</span>", out)

    def test_top3_use_warm_color(self):
        out2 = self._render(self._item(rank=2))
        out3 = self._render(self._item(rank=3))
        self.assertIn("#fde68a", out2)
        self.assertIn("#fde68a", out3)

    def test_rank_4_to_10_use_indigo(self):
        out = self._render(self._item(rank=7))
        self.assertIn("#e0e7ff", out)

    def test_beyond_10_uses_grey(self):
        out = self._render(self._item(rank=25))
        self.assertIn("#f1f5f9", out)

    def test_no_rank_no_badge(self):
        out = self._render(self._item())
        self.assertNotIn("border-radius:8px", out)

    def test_invalid_rank_no_badge(self):
        for bad in (0, -1, "3", None, True):
            out = self._render(self._item(rank=bad))
            self.assertNotIn("border-radius:8px", out, f"rank={bad!r}")

    def test_rank_is_escaped_contextually(self):
        # rank 必须是 int 才能渲染，杜绝注入
        out = self._render(self._item(rank="<img src=x>"))
        self.assertNotIn("<img src=x>", out)


class TestResolveLangpair(unittest.TestCase):
    """翻译方向与缓存键规则。

    回归：早期 translate_all 预检查缓存时用 target（'zh'），
    translate_title 写入时用 'zh-CN'，导致预检查永远 miss、缓存形同虚设。
    现在两者共用 _resolve_langpair。
    """

    def test_english_to_chinese(self):
        r = nc._resolve_langpair("Some English Headline", "zh")
        self.assertEqual(r[0], "en")
        self.assertEqual(r[1], "zh-CN")
        self.assertEqual(r[2], "en|zh-CN")

    def test_chinese_to_english_when_target_en(self):
        r = nc._resolve_langpair("中文标题", "en")
        self.assertEqual(r[0], "zh")
        self.assertEqual(r[1], "en")

    def test_auto_flips_both_directions(self):
        self.assertEqual(nc._resolve_langpair("English title", "auto")[1], "zh-CN")
        self.assertEqual(nc._resolve_langpair("中文标题", "auto")[1], "en")

    def test_same_language_needs_no_translation(self):
        self.assertIsNone(nc._resolve_langpair("中文标题", "zh"))
        self.assertIsNone(nc._resolve_langpair("English title", "en"))

    def test_empty_text(self):
        self.assertIsNone(nc._resolve_langpair("", "zh"))

    def test_cache_key_matches_between_call_paths(self):
        """translate_title 写入的键，必须等于 translate_all 预检查用的键。"""
        text = "Some English Headline"
        calls = []
        original = nc._mymemory
        nc._mymemory = lambda t, s, tgt, to: (calls.append(t), "译文")[1]
        try:
            cache = {}
            nc.translate_title(text, "zh", 5, cache)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(cache), 1)

            stored_key = next(iter(cache))
            src, tgt, pair = nc._resolve_langpair(text, "zh")
            self.assertEqual(stored_key, f"{pair}|{text}")
        finally:
            nc._mymemory = original


class TestTranslateAllBehaviour(_OfflineGuard):
    """translate_all 的实际行为。"""

    def _patch(self):
        calls = []
        original = nc._mymemory
        nc._mymemory = lambda t, s, tgt, to: (calls.append(t), f"[{tgt}]{t}")[1]
        return original, calls

    def _run(self, items, target="zh", cache=None):
        original, calls = self._patch()
        old_cache = nc.load_cache
        nc.load_cache = lambda: dict(cache or {})
        try:
            grouped = {"c": items}
            nc.translate_all(grouped, {"settings": {
                "translate": True, "translate_target": target,
                "translate_delay": 0, "request_timeout": 5,
            }})
            return grouped["c"], calls
        finally:
            nc._mymemory = original
            nc.load_cache = old_cache

    def test_target_zh_skips_chinese_titles(self):
        """target=zh 时中文标题不该被翻成英文（对中文读者无价值）。"""
        items, calls = self._run([
            {"title": "中文标题不需要翻译", "translate_field": "title"},
        ])
        self.assertNotIn("title_translation", items[0])
        self.assertEqual(calls, [])

    def test_target_zh_translates_english_titles(self):
        items, calls = self._run([
            {"title": "Some English Headline", "translate_field": "title"},
        ])
        self.assertTrue(items[0]["title_translation"].startswith("[zh-CN]"))
        self.assertEqual(len(calls), 1)

    def test_target_auto_translates_both_directions(self):
        items, calls = self._run([
            {"title": "Some English Headline", "translate_field": "title"},
            {"title": "中文标题", "translate_field": "title"},
        ], target="auto")
        self.assertTrue(items[0]["title_translation"].startswith("[zh-CN]"))
        self.assertTrue(items[1]["title_translation"].startswith("[en]"))

    def test_cached_item_avoids_network(self):
        """缓存命中不应再发请求。"""
        cache = {"en|zh-CN|Some English Headline": "缓存译文"}
        items, calls = self._run(
            [{"title": "Some English Headline", "translate_field": "title"}],
            cache=cache,
        )
        self.assertEqual(calls, [])
        self.assertEqual(items[0]["title_translation"], "缓存译文")

    def test_translate_field_controls_which_field_is_used(self):
        """GitHub 条目用 summary 作为翻译源。"""
        items, calls = self._run([
            {"title": "owner/repo", "summary": "An English description",
             "translate_field": "summary"},
        ])
        self.assertEqual(calls, ["An English description"])
        self.assertIn("An English description", items[0]["title_translation"])

    def test_disabled_skips_everything(self):
        original, calls = nc._mymemory, []
        nc._mymemory = lambda *a: (calls.append(a), "x")[1]
        old_cache = nc.load_cache
        nc.load_cache = lambda: {}
        try:
            grouped = {"c": [{"title": "English", "translate_field": "title"}]}
            nc.translate_all(grouped, {"settings": {"translate": False}})
            self.assertEqual(calls, [])
            self.assertNotIn("title_translation", grouped["c"][0])
        finally:
            nc._mymemory = original
            nc.load_cache = old_cache

    def test_empty_translate_field_skips(self):
        """translate_field 为空表示无需翻译（无噪描述已是中文）。"""
        items, calls = self._run([
            {"title": "owner/repo", "summary": "已经是中文的描述",
         "translate_field": ""},
        ])
        self.assertEqual(calls, [])
        self.assertNotIn("title_translation", items[0])


class TestEnrichImagesConcurrencyTiming(unittest.TestCase):
    """并发补图的耗时收益（用假网络保证稳定）。"""

    def test_six_workers_cut_time_to_about_sixth(self):
        import time as _time

        def slow(url, timeout):
            _time.sleep(0.15)
            return "http://img/x.png"

        original = nc.fetch_og_image
        nc.fetch_og_image = slow
        try:
            n = 18
            cfg = {"settings": {
                "fetch_og_image": True, "request_timeout": 5,
                "og_image_delay": 0, "og_image_limit": 60,
                "og_image_workers": 6,
            }}
            t0 = _time.perf_counter()
            nc.enrich_images({"c": [{"link": f"http://x/{i}", "image": ""}
                                    for i in range(n)]}, cfg)
            par = _time.perf_counter() - t0

            cfg["settings"]["og_image_workers"] = 1
            t0 = _time.perf_counter()
            nc.enrich_images({"c": [{"link": f"http://x/{i}", "image": ""}
                                    for i in range(n)]}, cfg)
            ser = _time.perf_counter() - t0
        finally:
            nc.fetch_og_image = original

        self.assertLess(par, ser * 0.5)


class TestIsNoopTranslation(unittest.TestCase):
    """无效译文判定：机器翻译遇到专有名词/版本号常原样返回。"""

    def test_identical_is_noop(self):
        self.assertTrue(nc._is_noop_translation("frida v17.24.0", "frida v17.24.0"))
        self.assertTrue(nc._is_noop_translation("TornadoC2", "TornadoC2"))

    def test_different_is_valid(self):
        self.assertFalse(nc._is_noop_translation("Main repo for hosting binaries",
                                                 "用于托管发布二进制文件的主存储库"))

    def test_case_and_punctuation_insensitive(self):
        self.assertTrue(nc._is_noop_translation("Hello World", "hello  world"))
        self.assertTrue(nc._is_noop_translation("frida v17", "frida v17."))

    def test_partial_change_counts_as_valid(self):
        self.assertFalse(nc._is_noop_translation("OshoosiClaw", "OshoosiClaw 项目"))

    def test_empty_translation_is_noop(self):
        self.assertTrue(nc._is_noop_translation("abc", ""))
        self.assertTrue(nc._is_noop_translation("", ""))


class TestGithubDescriptionFetch(unittest.TestCase):
    """每天榜单项目都在变，所以每个项目都要去 GitHub 取原简介。"""

    def _items(self, n=3):
        return [
            {"title": f"o{i}/r{i}", "summary": "", "rank": i + 1,
             "_wuzao_desc": ""}
            for i in range(n)
        ]

    def _cfg(self, **over):
        s = {"request_timeout": 20, "github_desc_workers": 4}
        s.update(over)
        return {"settings": s, "github": {"token": "t"}}

    def _patch(self, table=None, cache=None):
        table = table or {}
        calls = []
        lock = __import__("threading").Lock()
        store = dict(cache or {})
        original = nc._fetch_github_description
        original_load = nc._load_gh_descriptions
        original_save = nc._save_gh_descriptions

        def fake(repo, token, timeout):
            with lock:
                calls.append(repo)
            return table.get(repo, "")

        nc._fetch_github_description = fake
        nc._load_gh_descriptions = lambda: store
        nc._save_gh_descriptions = lambda d: store.update(d)
        return original, original_load, original_save, calls, store

    def _restore(self, saved):
        (nc._fetch_github_description, nc._load_gh_descriptions,
         nc._save_gh_descriptions) = saved

    def test_fetches_for_every_item_even_if_it_has_a_description(self):
        """关键回归：不能只在缺简介时才补，否则每天的新项目永远拿不到原文。"""
        saved = self._patch({"o0/r0": "Zero", "o1/r1": "One", "o2/r2": "Two"})
        try:
            items = self._items(3)
            items[0]["summary"] = "已经有无噪给的中文描述了"
            nc._fill_github_descriptions(items, self._cfg())
        finally:
            self._restore(saved[:3])
        self.assertEqual(sorted(saved[3]), ["o0/r0", "o1/r1", "o2/r2"])
        self.assertEqual(items[0]["summary"], "Zero")   # 被英文原文覆盖
        self.assertEqual(items[1]["summary"], "One")

    def test_empty_github_description_keeps_existing(self):
        """GitHub 上没有简介时，保留无噪给的中文描述。"""
        saved = self._patch({})
        try:
            items = self._items(1)
            items[0]["summary"] = "无噪的中文描述"
            nc._fill_github_descriptions(items, self._cfg())
        finally:
            self._restore(saved[:3])
        self.assertEqual(items[0]["summary"], "无噪的中文描述")

    def test_cache_skips_refetch(self):
        today = nc.date.today().isoformat()
        saved = self._patch({}, cache={
            "o0/r0": {"d": "Cached", "t": today},
            "o1/r1": {"d": "Cached2", "t": today},
            "o2/r2": {"d": "Cached3", "t": today}})
        try:
            items = self._items(3)
            nc._fill_github_descriptions(items, self._cfg())
        finally:
            self._restore(saved[:3])
        self.assertEqual(saved[3], [])          # 全部命中缓存
        self.assertEqual(items[0]["summary"], "Cached")

    def test_empty_cached_description_retried_after_retry_days(self):
        """当时没简介、超过重试窗口后会重新请求。"""
        old = (nc.date.today()
               - nc.timedelta(days=nc.GH_DESC_RETRY_DAYS + 1)).isoformat()
        fresh = nc.date.today().isoformat()
        saved = self._patch({"o0/r0": "Now has one"}, cache={
            "o0/r0": {"d": "", "t": old},      # 过期 → 重试
            "o1/r1": {"d": "", "t": fresh},    # 未过期 → 跳过
        })
        try:
            items = self._items(2)
            nc._fill_github_descriptions(items, self._cfg())
        finally:
            self._restore(saved[:3])
        self.assertIn("o0/r0", saved[3])
        self.assertNotIn("o1/r1", saved[3])
        self.assertEqual(items[0]["summary"], "Now has one")

    def test_legacy_string_cache_supported(self):
        """旧缓存是纯字符串，仍要能读。"""
        saved = self._patch({}, cache={"o0/r0": "Legacy description"})
        try:
            items = self._items(1)
            nc._fill_github_descriptions(items, self._cfg())
        finally:
            self._restore(saved[:3])
        self.assertEqual(items[0]["summary"], "Legacy description")
        self.assertEqual(saved[3], [])

    def test_empty_items_is_noop(self):
        saved = self._patch({})
        try:
            nc._fill_github_descriptions([], self._cfg())
        finally:
            self._restore(saved[:3])
        self.assertEqual(saved[3], [])

    def test_token_from_config_is_used(self):
        seen = {}
        original = nc._fetch_github_description
        ol, os_ = nc._load_gh_descriptions, nc._save_gh_descriptions
        nc._load_gh_descriptions = lambda: {}
        nc._save_gh_descriptions = lambda d: None

        def spy(repo, token, timeout):
            seen["token"] = token
            return "desc"

        nc._fetch_github_description = spy
        try:
            nc._fill_github_descriptions(
                [{"title": "a/one", "summary": ""}],
                {"settings": {"request_timeout": 5}, "github": {"token": "ghp_test"}},
            )
        finally:
            nc._fetch_github_description = original
            nc._load_gh_descriptions = ol
            nc._save_gh_descriptions = os_
        self.assertEqual(seen["token"], "ghp_test")

    def test_parallel_and_serial_give_same_result(self):
        table = {"o0/r0": "A", "o1/r1": "B", "o2/r2": "C"}
        for workers in (1, 4):
            saved = self._patch(table)
            try:
                items = self._items(3)
                nc._fill_github_descriptions(
                    items, self._cfg(github_desc_workers=workers))
            finally:
                self._restore(saved[:3])
            self.assertEqual([i["summary"] for i in items], ["A", "B", "C"])


class TestWuzaoDescReuse(_OfflineGuard):
    """无噪自带的中文描述优先当作译文，省掉一次机器翻译。"""

    def _run(self, item, target="zh"):
        calls = []
        original = nc._mymemory
        old_cache = nc.load_cache
        nc._mymemory = lambda t, s, tgt, to: (calls.append(t), f"[MT]{t}")[1]
        nc.load_cache = lambda: {}
        try:
            grouped = {"c": [item]}
            nc.translate_all(grouped, {"settings": {
                "translate": True, "translate_target": target,
                "translate_delay": 0, "request_timeout": 5,
                "translate_workers": 1, "translate_summary": True,
            }})
            return grouped["c"][0], calls
        finally:
            nc._mymemory = original
            nc.load_cache = old_cache

    def test_wuzao_chinese_reused_as_translation(self):
        item = {"title": "o/r", "summary": "An English description",
                "translate_field": "", "_wuzao_desc": "无噪的中文描述"}
        out, calls = self._run(item)
        self.assertEqual(out["summary"], "An English description")
        self.assertEqual(out["summary_translation"], "无噪的中文描述")
        self.assertEqual(calls, [])      # 没有发机器翻译请求

    def test_falls_back_to_machine_translation_when_no_wuzao_desc(self):
        item = {"title": "o/r", "summary": "An English description",
                "translate_field": "", "_wuzao_desc": ""}
        out, calls = self._run(item)
        self.assertEqual(out["summary_translation"], "[MT]An English description")
        self.assertEqual(len(calls), 1)

    def test_chinese_summary_needs_no_translation(self):
        item = {"title": "o/r", "summary": "这是一个中文简介",
                "translate_field": "", "_wuzao_desc": ""}
        out, calls = self._run(item)
        self.assertNotIn("summary_translation", out)
        self.assertEqual(calls, [])

    def test_noop_machine_translation_not_stored(self):
        """机器翻译原样返回（专有名词/版本号）时，不写入译文。"""
        original = nc._mymemory
        old_cache = nc.load_cache
        nc._mymemory = lambda t, s, tgt, to: t      # 原样返回，模拟翻不动
        nc.load_cache = lambda: {}
        try:
            item = {"title": "o/r", "summary": "frida v17.24.0",
                    "translate_field": "", "_wuzao_desc": ""}
            grouped = {"c": [item]}
            nc.translate_all(grouped, {"settings": {
                "translate": True, "translate_target": "zh",
                "translate_delay": 0, "request_timeout": 5,
                "translate_workers": 1, "translate_summary": True,
            }})
        finally:
            nc._mymemory = original
            nc.load_cache = old_cache
        self.assertNotIn("summary_translation", grouped["c"][0])


class TestBuildHtmlSummaryTranslation(unittest.TestCase):
    """简介译文的渲染：显示在英文原文下方。"""

    def _render(self, item):
        return nc.build_html({"c": [item]}, {"settings": {"subject_prefix": ""}})

    def _item(self, **over):
        base = {"title": "frida v17.24.0", "link": "https://github.com/x/y",
                "summary": "Main repo for hosting release binaries",
                "summary_translation": "用于托管发布二进制文件的主存储库",
                "image": "", "source": "GitHub 趋势榜", "published": None}
        base.update(over)
        return base

    def test_summary_translation_rendered_below_original(self):
        out = self._render(self._item())
        self.assertIn("用于托管发布二进制文件的主存储库", out)
        # 原文在译文之前
        self.assertLess(out.index("Main repo for hosting"),
                        out.index("用于托管发布"))

    def test_no_translation_renders_original_only(self):
        out = self._render(self._item(summary_translation=""))
        self.assertIn("Main repo for hosting", out)
        self.assertNotIn("font-size:14px;color:#6b7280", out)

    def test_summary_translation_is_escaped(self):
        out = self._render(self._item(
            summary_translation='<img src=x onerror=alert(1)>'))
        self.assertNotIn("<img src=x", out)
        self.assertIn("&lt;img", out)

    def test_chinese_summary_needs_no_translation(self):
        """简介本身是中文时，只显示原文（不产生译文行）。"""
        item = self._item(summary="开源 ElevenLabs 替代品",
                          summary_translation="")
        out = self._render(item)
        self.assertIn("开源 ElevenLabs 替代品", out)
        self.assertNotIn("font-size:14px;color:#6b7280", out)


class TestBuildTextSummary(unittest.TestCase):
    """文本版也要包含简介与译文。"""

    def test_summary_and_translation_included(self):
        g = {"c": [{"title": "frida v17.24.0", "link": "http://x",
                    "summary": "Main repo for hosting release binaries",
                    "summary_translation": "用于托管发布二进制文件的主存储库",
                    "image": "", "source": "GitHub"}]}
        out = nc.build_text(g)
        self.assertIn("Main repo for hosting release binaries", out)
        self.assertIn("用于托管发布二进制文件的主存储库", out)


class TestTranslateBreaker(_OfflineGuard):
    """限流熔断：撞到 429/额度耗尽后停止后续请求，避免加深封禁。"""

    def setUp(self):
        nc._TRANSLATE_BLOCKED["reason"] = ""

    def tearDown(self):
        nc._TRANSLATE_BLOCKED["reason"] = ""

    def _fake_open(self, code):
        class _Boom(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://x", code, "boom", {}, None)

        def raiser(request, timeout=None):
            raise _Boom()
        return raiser

    def test_429_sets_breaker(self):
        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = self._fake_open(429)
        try:
            r = nc._mymemory("hello", "en", "zh-CN", 5)
        finally:
            nc.urllib.request.urlopen = original
        self.assertIsNone(r)
        self.assertTrue(nc._translation_blocked())

    def test_breaker_short_circuits_later_calls(self):
        nc._TRANSLATE_BLOCKED["reason"] = "HTTP 429 限流"
        called = []
        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = lambda *a, **k: called.append(1)
        try:
            r = nc._mymemory("hello", "en", "zh-CN", 5)
        finally:
            nc.urllib.request.urlopen = original
        self.assertIsNone(r)
        self.assertEqual(called, [])      # 没有真的发请求

    def test_quota_finished_sets_breaker(self):
        import json as _json

        class _Resp:
            def read(self):
                return _json.dumps({"quotaFinished": True,
                                    "responseStatus": 200,
                                    "responseData": {"translatedText": ""}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = lambda *a, **k: _Resp()
        try:
            r = nc._mymemory("hello", "en", "zh-CN", 5)
        finally:
            nc.urllib.request.urlopen = original
        self.assertIsNone(r)
        self.assertTrue(nc._translation_blocked())

    def test_translate_all_resets_breaker_each_run(self):
        nc._TRANSLATE_BLOCKED["reason"] = "上次的限流"
        old_cache = nc.load_cache
        nc.load_cache = lambda: {}
        try:
            nc.translate_all({"c": []}, {"settings": {"translate": True}})
        finally:
            nc.load_cache = old_cache
        self.assertFalse(nc._translation_blocked())

    def test_max_requests_caps_pending(self):
        """translate_max_requests 只约束 MyMemory。

        火山走批量接口、不计入这个上限，所以必须显式把后端钉死成
        mymemory——否则环境里有 AK/SK 时会选火山，断言必然不成立。
        """
        calls = []
        original = nc._mymemory
        old_cache = nc.load_cache
        nc._mymemory = lambda t, s, tgt, to: (calls.append(t), f"译{t}")[1]
        nc.load_cache = lambda: {}
        try:
            items = [{"title": f"English headline {i}", "summary": "",
                      "translate_field": "title"} for i in range(10)]
            nc.translate_all({"c": items}, {
                "settings": {
                    "translate": True, "translate_target": "zh",
                    "translate_delay": 0, "request_timeout": 5,
                    "translate_workers": 1, "translate_summary": True,
                    "translate_max_requests": 3,
                },
                "translate": {"provider": "mymemory"},
            })
        finally:
            nc._mymemory = original
            nc.load_cache = old_cache
        self.assertEqual(len(calls), 3)   # 只翻前 3 段


class TestVolcengineTranslate(unittest.TestCase):
    """火山引擎机器翻译：AK/SK 签名 + 批量。"""

    def test_sign_produces_required_headers(self):
        h = nc._volc_sign("AK", "SK", "POST", "host", "/",
                          {"Action": "TranslateText", "Version": "2020-06-01"},
                          b"{}", "cn-north-1")
        for key in ("Authorization", "X-Date", "X-Content-Sha256",
                    "Content-Type", "Host"):
            self.assertIn(key, h)
        self.assertTrue(h["Authorization"].startswith("HMAC-SHA256 Credential=AK/"))
        self.assertIn("SignedHeaders=content-type;host;x-content-sha256;x-date",
                      h["Authorization"])
        self.assertRegex(h["X-Date"], r"^\d{8}T\d{6}Z$")

    def test_sign_is_deterministic_for_same_input(self):
        """同一秒内同一输入应产生相同签名（签名算法无随机性）。"""
        args = ("AK", "SK", "POST", "host", "/", {"A": "1"}, b"{}", "cn-north-1")
        a = nc._volc_sign(*args)
        b = nc._volc_sign(*args)
        if a["X-Date"] == b["X-Date"]:
            self.assertEqual(a["Authorization"], b["Authorization"])

    def test_sign_changes_with_body(self):
        base = nc._volc_sign("AK", "SK", "POST", "host", "/", {}, b"{}", "r")
        other = nc._volc_sign("AK", "SK", "POST", "host", "/", {}, b'{"x":1}', "r")
        self.assertNotEqual(base["X-Content-Sha256"], other["X-Content-Sha256"])

    def _resp(self, payload):
        import json as _json

        class _R:
            def read(self):
                return _json.dumps(payload, ensure_ascii=False).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return _R()

    def test_batch_translate_maps_results(self):
        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = lambda *a, **k: self._resp({
            "TranslationList": [
                {"Translation": "译文一"}, {"Translation": "译文二"},
            ]})
        try:
            out = nc._volc_translate_batch(
                ["one", "two"], "zh", "ak", "sk", "cn-north-1", 5)
        finally:
            nc.urllib.request.urlopen = original
        self.assertEqual(out, ["译文一", "译文二"])

    def test_count_mismatch_discards_result(self):
        """返回条数不符时丢弃整批，避免译文错配到别的条目。"""
        original = nc.urllib.request.urlopen
        nc.urllib.request.urlopen = lambda *a, **k: self._resp({
            "TranslationList": [{"Translation": "只有一条"}]})
        try:
            out = nc._volc_translate_batch(
                ["one", "two", "three"], "zh", "ak", "sk", "r", 5)
        finally:
            nc.urllib.request.urlopen = original
        self.assertIsNone(out)

    def test_auth_error_trips_breaker(self):
        nc._TRANSLATE_BLOCKED["reason"] = ""
        original = nc.urllib.request.urlopen

        def boom(request, timeout=None):
            raise urllib.error.HTTPError("u", 403, "denied", {}, None)

        nc.urllib.request.urlopen = boom
        try:
            out = nc._volc_translate_batch(["x"], "zh", "ak", "sk", "r", 5)
        finally:
            nc.urllib.request.urlopen = original
        self.assertIsNone(out)
        self.assertTrue(nc._translation_blocked())
        nc._TRANSLATE_BLOCKED["reason"] = ""

    def test_empty_input_returns_empty(self):
        self.assertEqual(
            nc._volc_translate_batch([], "zh", "ak", "sk", "r", 5), [])


class TestTranslateProviderResolution(unittest.TestCase):
    """后端选择：有火山 AK/SK 用火山，否则 MyMemory。"""

    def _cfg(self, **over):
        tr = {"provider": "auto"}
        tr.update(over)
        return {"settings": {}, "translate": tr}

    def setUp(self):
        for k in ("VOLC_ACCESS_KEY", "VOLC_SECRET_KEY", "VOLC_REGION"):
            os.environ.pop(k, None)

    tearDown = setUp

    def test_explicit_volcengine_with_keys(self):
        p, ak, sk, rg = nc._resolve_translate_provider(
            self._cfg(provider="volcengine",
                      volc_access_key="AK", volc_secret_key="SK"))
        self.assertEqual(p, "volcengine")
        self.assertEqual(ak, "AK")
        self.assertEqual(sk, "SK")

    def test_explicit_volcengine_without_keys_falls_back(self):
        p, _ak, _sk, _rg = nc._resolve_translate_provider(
            self._cfg(provider="volcengine"))
        self.assertEqual(p, "mymemory")

    def test_auto_uses_volcengine_when_keys_present(self):
        p, _ak, _sk, _rg = nc._resolve_translate_provider(
            self._cfg(volc_access_key="AK", volc_secret_key="SK"))
        self.assertEqual(p, "volcengine")

    def test_auto_falls_back_without_keys(self):
        p, _ak, _sk, _rg = nc._resolve_translate_provider(self._cfg())
        self.assertEqual(p, "mymemory")

    def test_explicit_mymemory_wins_over_keys(self):
        p, _ak, _sk, _rg = nc._resolve_translate_provider(
            self._cfg(provider="mymemory",
                      volc_access_key="AK", volc_secret_key="SK"))
        self.assertEqual(p, "mymemory")

    def test_env_vars_take_precedence(self):
        os.environ["VOLC_ACCESS_KEY"] = "ENV_AK"
        os.environ["VOLC_SECRET_KEY"] = "ENV_SK"
        try:
            p, ak, sk, _rg = nc._resolve_translate_provider(
                self._cfg(volc_access_key="CFG_AK", volc_secret_key="CFG_SK"))
        finally:
            os.environ.pop("VOLC_ACCESS_KEY", None)
            os.environ.pop("VOLC_SECRET_KEY", None)
        self.assertEqual(p, "volcengine")
        self.assertEqual(ak, "ENV_AK")
        self.assertEqual(sk, "ENV_SK")


class TestLooksLikeIdentifier(unittest.TestCase):
    """工具名/库名不该送去翻译（会产出「即兴表演」这类误译）。"""

    def test_tool_names_are_identifiers(self):
        for t in ("IMSIsting", "TornadoC2", "OshoosiClaw", "DebuggerJ-",
                  "owner/repo", "libssl.so", "node-pty"):
            self.assertTrue(nc._looks_like_identifier(t), t)

    def test_sentences_are_not_identifiers(self):
        for t in ("Bose's new wired earbuds are simple",
                  "Why Apple is making smart home devices"):
            self.assertFalse(nc._looks_like_identifier(t), t)

    def test_version_strings_not_treated_as_identifier(self):
        """带版本号的项目名仍可翻译（其中可能含描述性词汇）。"""
        self.assertFalse(nc._looks_like_identifier("frida v17.24.0"))

    def test_chinese_never_identifier(self):
        self.assertFalse(nc._looks_like_identifier("中文标题"))

    def test_empty_and_long(self):
        self.assertFalse(nc._looks_like_identifier(""))
        self.assertFalse(nc._looks_like_identifier("a" * 41))


class TestTranslateAllSkipsIdentifiers(_OfflineGuard):
    def test_identifier_title_not_sent_to_translator(self):
        calls = []
        original = nc._mymemory
        old_cache = nc.load_cache
        nc._mymemory = lambda t, s, tgt, to: (calls.append(t), f"译{t}")[1]
        nc.load_cache = lambda: {}
        try:
            items = [
                {"title": "TornadoC2", "summary": "", "translate_field": "title"},
                {"title": "A real English headline", "summary": "",
                 "translate_field": "title"},
            ]
            nc.translate_all({"c": items}, {"settings": {
                "translate": True, "translate_target": "zh",
                "translate_delay": 0, "request_timeout": 5,
                "translate_workers": 1, "translate_summary": False,
            }})
        finally:
            nc._mymemory = original
            nc.load_cache = old_cache
        self.assertEqual(calls, ["A real English headline"])
        self.assertNotIn("title_translation", items[0])
        self.assertIn("title_translation", items[1])


class TestTermProtection(unittest.TestCase):
    """术语保护：公司名/产品名不被机器翻译硬翻。"""

    def test_protect_and_restore_roundtrip(self):
        text = "Anthropic's Claude and OpenAI are rivals"
        protected, mapping = nc._protect_terms(text)
        self.assertNotIn("Anthropic", protected)
        self.assertNotIn("Claude", protected)
        self.assertNotIn("OpenAI", protected)
        restored = nc._restore_terms(protected, mapping)
        self.assertIn("Anthropic", restored)
        self.assertIn("Claude", restored)
        self.assertIn("OpenAI", restored)

    def test_possessive_form_matched(self):
        """回归：Anthropics' 带所有格和撇号，早期匹配不到 Anthropic。"""
        protected, mapping = nc._protect_terms("Anthropics' Claude cracks it")
        self.assertNotIn("Anthropic", protected)
        self.assertEqual(len(mapping), 2)

    def test_curly_apostrophe_matched(self):
        protected, _m = nc._protect_terms("Anthropic’s model")
        self.assertNotIn("Anthropic", protected)

    def test_word_boundary_respected(self):
        """API 不该命中 RAPID（RAPID 里含 API 子串）。"""
        protected, mapping = nc._protect_terms("The API is RAPID")
        self.assertIn("RAPID", protected)                  # 未被保护
        self.assertEqual(list(mapping.values()), ["API"])  # 只有 API 被保护

    def test_longer_term_wins(self):
        """Raspberry Pi 应整体匹配，不被拆开。"""
        protected, mapping = nc._protect_terms("Raspberry Pi Project")
        self.assertIn("Raspberry Pi", mapping.values())
        self.assertNotIn("Raspberry", protected)

    def test_unknown_terms_untouched(self):
        text = "A completely ordinary sentence"
        protected, mapping = nc._protect_terms(text)
        self.assertEqual(protected, text)
        self.assertEqual(mapping, {})

    def test_restore_handles_case_change(self):
        """接口可能改大小写。"""
        _p, mapping = nc._protect_terms("Claude is here")
        token = next(iter(mapping))
        self.assertEqual(nc._restore_terms(token.lower(), mapping), "Claude")

    def test_restore_handles_inserted_spaces(self):
        _p, mapping = nc._protect_terms("Claude is here")
        token = next(iter(mapping))
        inner = token[len("ZQX"):-len("QXZ")]
        spaced = f"ZQX {inner} QXZ"
        self.assertEqual(nc._restore_terms(spaced, mapping), "Claude")

    def test_multiple_occurrences_same_token(self):
        _p, mapping = nc._protect_terms("Claude and Claude again")
        self.assertEqual(len(mapping), 1)

    def test_restore_empty_mapping_is_identity(self):
        self.assertEqual(nc._restore_terms("anything", {}), "anything")


class TestJunkSummaryFilter(unittest.TestCase):
    """部分中文源的 description 只有「点击查看原文」这类占位文案。"""

    def test_junk_variants_return_empty(self):
        for t in ("点击查看原文", "阅读全文", "查看更多。", "Read more",
                  "Continue reading", "查看原文", "详见原文"):
            self.assertEqual(nc._clean_summary(t), "", t)

    def test_real_summary_kept(self):
        self.assertEqual(nc._clean_summary("这是一篇正常的摘要"),
                         "这是一篇正常的摘要")

    def test_junk_prefix_with_content_kept(self):
        """含实际内容时不丢弃。"""
        t = "点击查看原文，了解更多关于该漏洞的细节"
        self.assertTrue(nc._clean_summary(t))

    def test_empty_input(self):
        self.assertEqual(nc._clean_summary(""), "")

    def test_strips_html_before_judging(self):
        self.assertEqual(nc._clean_summary("<p>点击查看原文</p>"), "")


class TestSummaryNoiseCleanup(unittest.TestCase):
    """摘要尾部噪音与 hashtag 清理。"""

    def test_trailing_read_more_removed(self):
        for t in ("支持…阅读更多", "support for …read more", "内容…Read more »"):
            out = nc._clean_summary(t)
            self.assertNotIn("阅读更多", out)
            self.assertNotIn("read more", out.lower())

    def test_normal_ending_preserved(self):
        self.assertEqual(nc._clean_summary("正常结尾的内容。"), "正常结尾的内容。")

    def test_hashtags_stripped(self):
        self.assertEqual(
            nc._strip_hashtags("Raspberry Pi Project #piday #raspberrypi"),
            "Raspberry Pi Project")

    def test_leading_hashtag_stripped(self):
        self.assertEqual(nc._strip_hashtags("#circuitpythonparsec Use STEMMA"),
                         "Use STEMMA")

    def test_csharp_not_treated_as_hashtag(self):
        """C# / F# 不是 hashtag，不能删。"""
        self.assertEqual(nc._strip_hashtags("C# 和 F# 的区别"), "C# 和 F# 的区别")

    def test_text_without_hash_untouched(self):
        self.assertEqual(nc._strip_hashtags("no tags here"), "no tags here")


class TestLinkNormalization(unittest.TestCase):
    """判重用的链接归一化。"""

    def test_scheme_and_www_ignored(self):
        a = nc._normalize_link_key("https://www.example.com/a/b/")
        b = nc._normalize_link_key("http://example.com/a/b")
        self.assertEqual(a, b)

    def test_tracking_params_ignored(self):
        a = nc._normalize_link_key("https://x.com/p?utm_source=rss&id=5")
        b = nc._normalize_link_key("https://x.com/p?id=5")
        self.assertEqual(a, b)

    def test_fbclid_ignored(self):
        a = nc._normalize_link_key("https://x.com/p?id=5&fbclid=abc")
        b = nc._normalize_link_key("https://x.com/p?id=5")
        self.assertEqual(a, b)

    def test_different_paths_differ(self):
        self.assertNotEqual(nc._normalize_link_key("https://a.com/1"),
                            nc._normalize_link_key("https://a.com/2"))

    def test_meaningful_query_kept(self):
        a = nc._normalize_link_key("https://a.com/p?id=1")
        b = nc._normalize_link_key("https://a.com/p?id=2")
        self.assertNotEqual(a, b)

    def test_empty(self):
        self.assertEqual(nc._normalize_link_key(""), "")


class TestDedupByTitleAndLink(unittest.TestCase):
    """去重同时看标题和链接：同标题不同链接、同链接不同标题都要合并。"""

    def _config(self, items):
        def fake_fetch(source, timeout):
            return [dict(i) for i in items]
        original = nc.fetch_source
        nc.fetch_source = fake_fetch
        try:
            cfg = {"settings": {"request_timeout": 1, "hours_lookback": 9999,
                                "max_per_category": 100, "fetch_workers": 1},
                   "sources": [{"name": "stub", "category": "AI"}]}
            return nc.collect_news(cfg)
        finally:
            nc.fetch_source = original

    def _item(self, title, link, ts=None):
        from datetime import datetime, timezone
        return {"title": title, "link": link, "summary": "",
                "published": ts or datetime(2026, 10, 17, 12, tzinfo=timezone.utc)}

    def test_same_link_different_title_deduped(self):
        """同一篇文章被不同源转载，标题略有差异但链接相同。"""
        g = self._config([
            self._item("OpenAI 发布新模型", "https://x.com/post?utm_source=a"),
            self._item("OpenAI 推出新模型", "https://x.com/post?utm_source=b"),
        ])
        self.assertEqual(sum(len(v) for v in g.values()), 1)

    def test_same_title_different_link_deduped(self):
        g = self._config([
            self._item("Same Headline", "https://a.com/1"),
            self._item("Same Headline", "https://b.com/2"),
        ])
        self.assertEqual(sum(len(v) for v in g.values()), 1)

    def test_genuinely_different_kept(self):
        g = self._config([
            self._item("Title A", "https://a.com/1"),
            self._item("Title B", "https://b.com/2"),
        ])
        self.assertEqual(sum(len(v) for v in g.values()), 2)


class TestArchivePrune(unittest.TestCase):
    """存档保留策略。"""

    def test_old_files_removed(self):
        from datetime import date, timedelta
        with _tmpdir() as d:
            old = (date.today() - timedelta(days=40)).isoformat()
            new = date.today().isoformat()
            for name in (f"{old}.html", f"{old}.txt", f"{new}.html", "index.html"):
                open(os.path.join(d, name), "w", encoding="utf-8").write("x")
            nc._prune_archive(d, keep_days=30)
            left = sorted(os.listdir(d))
            self.assertNotIn(f"{old}.html", left)
            self.assertNotIn(f"{old}.txt", left)
            self.assertIn(f"{new}.html", left)
            self.assertIn("index.html", left)   # 索引文件不动

    def test_zero_means_keep_forever(self):
        from datetime import date, timedelta
        with _tmpdir() as d:
            old = (date.today() - timedelta(days=400)).isoformat()
            open(os.path.join(d, f"{old}.html"), "w", encoding="utf-8").write("x")
            nc._prune_archive(d, keep_days=0)
            self.assertIn(f"{old}.html", os.listdir(d))

    def test_non_date_files_untouched(self):
        with _tmpdir() as d:
            open(os.path.join(d, "notes.md"), "w", encoding="utf-8").write("x")
            nc._prune_archive(d, keep_days=1)
            self.assertIn("notes.md", os.listdir(d))


class TestTranslationCacheCap(unittest.TestCase):
    """翻译缓存超过上限时淘汰最旧条目。"""

    def test_oldest_evicted(self):
        original_file = nc.CACHE_FILE
        with _tmpdir() as d:
            nc.CACHE_FILE = os.path.join(d, "cache.json")
            original_max = nc.CACHE_MAX_ENTRIES
            nc.CACHE_MAX_ENTRIES = 5
            try:
                cache = {f"k{i}": f"v{i}" for i in range(10)}
                nc.save_cache(cache)
                import json
                saved = json.load(open(nc.CACHE_FILE, encoding="utf-8"))
            finally:
                nc.CACHE_FILE = original_file
                nc.CACHE_MAX_ENTRIES = original_max
            self.assertEqual(len(saved), 5)
            self.assertIn("k9", saved)      # 最新的保留
            self.assertNotIn("k0", saved)   # 最旧的淘汰

    def test_under_limit_unchanged(self):
        original_file = nc.CACHE_FILE
        with _tmpdir() as d:
            nc.CACHE_FILE = os.path.join(d, "cache.json")
            try:
                cache = {"a": "1", "b": "2"}
                nc.save_cache(cache)
                import json
                saved = json.load(open(nc.CACHE_FILE, encoding="utf-8"))
            finally:
                nc.CACHE_FILE = original_file
            self.assertEqual(saved, {"a": "1", "b": "2"})


_ENV_SAMPLE = chr(10).join([
    "# comment",
    '_T_G="quoted"',
    "",
    "no-equals-sign",
    "_T_H=plain",
    "",
])


class TestEnvPlaceholder(unittest.TestCase):
    """config.json 支持 ${VAR} / ${VAR:-默认值}，密钥不再落盘。"""

    def test_expands_name(self):
        os.environ["_T_A"] = "v1"
        try:
            self.assertEqual(nc._expand_env("${_T_A}"), "v1")
        finally:
            os.environ.pop("_T_A", None)

    def test_default_used_when_unset(self):
        os.environ.pop("_T_B", None)
        self.assertEqual(nc._expand_env("${_T_B:-fallback}"), "fallback")

    def test_env_wins_over_default(self):
        os.environ["_T_C"] = "real"
        try:
            self.assertEqual(nc._expand_env("${_T_C:-fallback}"), "real")
        finally:
            os.environ.pop("_T_C", None)

    def test_unset_without_default_keeps_placeholder(self):
        os.environ.pop("_T_D", None)
        self.assertEqual(nc._expand_env("${_T_D}"), "${_T_D}")

    def test_embedded_in_string(self):
        os.environ["_T_E"] = "mid"
        try:
            self.assertEqual(nc._expand_env("a-${_T_E}-b"), "a-mid-b")
        finally:
            os.environ.pop("_T_E", None)

    def test_recurses_into_dict_and_list(self):
        os.environ["_T_F"] = "x"
        try:
            got = nc._expand_env({"a": {"b": ["${_T_F}", 1, None]}})
            self.assertEqual(got, {"a": {"b": ["x", 1, None]}})
        finally:
            os.environ.pop("_T_F", None)

    def test_non_string_passthrough(self):
        self.assertEqual(nc._expand_env(7), 7)
        self.assertIsNone(nc._expand_env(None))
        self.assertTrue(nc._expand_env(True))

    def test_dotenv_fills_only_missing(self):
        with _tmpdir() as d:
            env_path = os.path.join(d, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write(_ENV_SAMPLE)
            os.environ.pop("_T_G", None)
            os.environ.pop("_T_H", None)
            try:
                nc._load_dotenv(env_path)
                self.assertEqual(os.environ["_T_G"], "quoted")
                self.assertEqual(os.environ["_T_H"], "plain")
            finally:
                os.environ.pop("_T_G", None)
                os.environ.pop("_T_H", None)

    def test_dotenv_does_not_override_existing_env(self):
        with _tmpdir() as d:
            env_path = os.path.join(d, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write("_T_I=from-file" + chr(10))
            os.environ["_T_I"] = "from-ci"
            try:
                nc._load_dotenv(env_path)
                self.assertEqual(os.environ["_T_I"], "from-ci")
            finally:
                os.environ.pop("_T_I", None)

    def test_dotenv_missing_file_is_noop(self):
        nc._load_dotenv(os.path.join(BASE_DIR, "_definitely_absent_.env"))

    def test_load_config_resolves_through_dotenv(self):
        with _tmpdir() as d:
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"email": {"password": "${_T_J}"}}, fh)
            with open(os.path.join(d, ".env"), "w", encoding="utf-8") as fh:
                fh.write("_T_J=resolved" + chr(10))
            os.environ.pop("_T_J", None)
            try:
                got = nc.load_config(cfg_path)
                self.assertEqual(got["email"]["password"], "resolved")
            finally:
                os.environ.pop("_T_J", None)


class TestShippedConfigHasNoSecrets(unittest.TestCase):
    """入库的 config.json 不应含任何明文密钥。"""

    def test_config_json_only_uses_placeholders_for_secrets(self):
        path = os.path.join(BASE_DIR, "config.json")
        if not os.path.exists(path):
            self.skipTest("config.json 不存在")
        cfg = json.load(open(path, encoding="utf-8"))
        for dotted in ("email.password", "github.token"):
            node, keys = cfg, dotted.split(".")
            for k in keys:
                node = node.get(k, {})
            self.assertTrue(
                str(node).startswith("${"),
                f"{dotted} 应写成 ${{ENV}} 占位符，实际为 {node!r}",
            )

    def test_no_github_pat_pattern_in_tracked_sources(self):
        pat = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")
        for name in ("news_collector.py", "README.md", "config.json",
                     ".env.example", "test_news_collector.py"):
            path = os.path.join(BASE_DIR, name)
            if not os.path.exists(path):
                continue
            self.assertIsNone(
                pat.search(open(path, encoding="utf-8").read()),
                f"{name} 疑似含明文 GitHub PAT",
            )

    def test_dotenv_is_listed_in_gitignore(self):
        path = os.path.join(BASE_DIR, ".gitignore")
        if not os.path.exists(path):
            self.skipTest(".gitignore 不存在")
        lines = [ln.strip() for ln in open(path, encoding="utf-8").read().splitlines()]
        self.assertIn(".env", lines)


class TestTimezoneResolve(unittest.TestCase):
    """CI（GitHub Actions）容器是 TZ=UTC，北京时间 08:00 触发时 UTC 才 00:00。
    若用 datetime.now() 判断日期和发送时间，会每天静默跳过（退出码 0，看起来成功）。"""

    def test_blank_timezone_falls_back_to_naive(self):
        for cfg in ({}, {"settings": {}}, {"settings": {"timezone": ""}},
                    {"settings": {"timezone": None}}):
            self.assertIsNone(nc._resolve_tz(cfg), cfg)

    def test_current_now_naive_when_no_timezone(self):
        n = nc.current_now({})
        self.assertIsNone(n.tzinfo, "没配时区时应退回 naive 本地时间")

    def test_current_now_is_aware_when_zone_resolves(self):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("Asia/Shanghai")
        except Exception:
            self.skipTest("本机没有 tzdata，跳过（CI 的 Linux 容器自带）")
        n = nc.current_now({"settings": {"timezone": "Asia/Shanghai"}})
        self.assertIsNotNone(n.tzinfo)
        self.assertEqual(n.utcoffset().total_seconds(), 8 * 3600)

    def test_ci_utc_trigger_maps_to_beijing_morning(self):
        """修复前 UTC 00:30 → hour=0 < send_after_hour → 跳过。修复后应为北京时间 08:30。"""
        cst = _tz(timedelta(hours=8))
        bj = _dt(2026, 10, 18, 0, 30, tzinfo=_tz.utc).astimezone(cst)
        self.assertEqual((bj.year, bj.month, bj.day), (2026, 10, 18))
        self.assertEqual(bj.hour, 8)
        self.assertGreaterEqual(bj.hour, 8, "北京时间应达到发送时间")

    def test_ci_utc_evening_maps_to_next_day_beijing(self):
        """UTC 10-18 16:05 = 北京 10-19 00:05，日期要跟着跨天，且应被发送时间闸拦下。"""
        cst = _tz(timedelta(hours=8))
        bj = _dt(2026, 10, 18, 16, 5, tzinfo=_tz.utc).astimezone(cst)
        self.assertEqual(bj.strftime("%Y-%m-%d"), "2026-10-19")
        self.assertLess(bj.hour, 8, "北京时间凌晨，应被 send_after_hour 拦下")

    def test_bad_timezone_name_falls_back(self):
        self.assertIsNone(nc._resolve_tz({"settings": {"timezone": "不存在的时区/xx"}}))

    def test_fixed_offset_used_when_tzdata_missing(self):
        """Windows 没装 tzdata 时，用固定 UTC 偏移兜底，而不是系统本地时区。

        系统本地时区一旦设错，"今天是哪天"会整体偏移，直接影响发送窗口判断。
        """
        import zoneinfo

        real = zoneinfo.ZoneInfo

        def boom(key, *a, **k):
            raise zoneinfo.ZoneInfoNotFoundError("no tzdata")

        with unittest.mock.patch.object(zoneinfo, "ZoneInfo", boom):
            nc._TZ_WARNED.clear()
            tz = nc._resolve_tz({"settings": {"timezone": "Asia/Shanghai"}})
        self.assertIsNotNone(tz, "没有 tzdata 时应退回固定偏移，不是 None")
        self.assertEqual(tz.utcoffset(None).total_seconds(), 8 * 3600)

    def test_fixed_offset_unknown_zone_returns_none(self):
        """固定偏移表里没有的时区（且无 tzdata）才真的退回 None。"""
        import zoneinfo

        real = zoneinfo.ZoneInfo

        def boom(key, *a, **k):
            raise zoneinfo.ZoneInfoNotFoundError("no tzdata")

        with unittest.mock.patch.object(zoneinfo, "ZoneInfo", boom):
            nc._TZ_WARNED.clear()
            tz = nc._resolve_tz({"settings": {"timezone": "America/New_York"}})
        self.assertIsNone(tz)

    def test_tz_label_is_a_string_not_a_method(self):
        """回归：曾经写成 now.astimezone().tzname（少对括号），把方法对象
        本身塞进日志，打印成 <built-in method tzname of datetime...>。"""
        label = nc._tz_label(nc.current_now({"settings": {"timezone": "Asia/Shanghai"}}))
        self.assertIsInstance(label, str)
        self.assertNotIn("built-in method", label)
        self.assertNotIn("<", label)

    def test_tz_label_never_empty(self):
        for dt in (nc.current_now({}), nc.current_now({"settings": {"timezone": "Asia/Shanghai"}})):
            self.assertTrue(nc._tz_label(dt).strip())

    def test_tz_warning_logged_only_once(self):
        """同一时区的降级提醒只打一次，否则每个新闻源都刷一遍。"""
        import zoneinfo

        def boom(key, *a, **k):
            raise zoneinfo.ZoneInfoNotFoundError("no tzdata")

        nc._TZ_WARNED.clear()
        with unittest.mock.patch.object(zoneinfo, "ZoneInfo", boom):
            with self.assertLogs(nc.log, level="INFO") as cm:
                for _ in range(5):
                    nc._resolve_tz({"settings": {"timezone": "Asia/Shanghai"}})
        hits = [l for l in cm.output if "Asia/Shanghai" in l]
        self.assertEqual(len(hits), 1, "时区降级提醒应只出现一次，实际 %d 次" % len(hits))

    def test_fmt_time_none(self):
        self.assertEqual(nc._fmt_time(None), "时间未知")

    def test_fmt_time_without_config_still_formats(self):
        d = _dt(2026, 10, 18, 12, 0, tzinfo=_tz.utc)
        self.assertRegex(nc._fmt_time(d), r"^\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertRegex(nc._fmt_time(d, {}), r"^\d{2}-\d{2} \d{2}:\d{2}$")

    def test_fmt_time_uses_configured_timezone(self):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("Asia/Shanghai")
        except Exception:
            self.skipTest("本机没有 tzdata")
        d = _dt(2026, 10, 18, 0, 30, tzinfo=_tz.utc)   # 北京 08:30
        self.assertEqual(nc._fmt_time(d, {"settings": {"timezone": "Asia/Shanghai"}}),
                         "10-18 08:30")

    def test_build_text_accepts_config(self):
        """签名从 (grouped) 变成 (grouped, config=None)，旧调用方式不能炸。"""
        g = {"AI": [{"title": "T", "link": "https://x.cn/a",
                     "published": _dt(2026, 10, 18, 0, 30, tzinfo=_tz.utc)}]}
        self.assertIn("T", nc.build_text(g))
        self.assertIn("T", nc.build_text(g, {"settings": {"timezone": "Asia/Shanghai"}}))

    def test_html_header_uses_configured_day(self):
        """标题日期必须来自 current_now()，不能写死某一天。

        早期版本断言 "2026年10月18日"——build_html 渲染的是当天，
        过了那天这个断言必挂（2026-10-19 的 CI 就是这么红的）。
        改成跟 current_now() 对齐：断言的是"逻辑"，不是"日期"。
        """
        g = {"AI": [{"title": "T", "link": "https://x.cn/a",
                     "published": _dt(2026, 10, 18, 0, 30, tzinfo=_tz.utc)}]}
        cfg = {"settings": {"timezone": "Asia/Shanghai",
                            "max_per_category": 8}}
        html = nc.build_html(g, cfg)
        self.assertIn(nc.current_now(cfg).strftime("%Y年%m月%d日"), html)

    def test_html_header_day_follows_configured_timezone(self):
        """跨日边界上，时区不同渲染出的日期必须跟着变。

        Asia/Shanghai 与 UTC 差 8 小时：UTC 10-18 16:30 已是上海 10-19 00:30。
        没有 tzdata 时构造不出这个场景，直接跳过。
        """
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("Asia/Shanghai")
        except Exception:
            self.skipTest("本机没有 tzdata")

        real_now = nc.current_now
        cfg = {"settings": {"timezone": "Asia/Shanghai",
                            "max_per_category": 8}}
        try:
            nc.current_now = lambda config=None: _dt(
                2026, 10, 18, 16, 30, tzinfo=_tz.utc
            ).astimezone(ZoneInfo("Asia/Shanghai"))
            g = {"AI": [{"title": "T", "link": "https://x.cn/a",
                         "published": _dt(2026, 10, 18, 0, 30, tzinfo=_tz.utc)}]}
            self.assertIn("2026年10月19日", nc.build_html(g, cfg))
        finally:
            nc.current_now = real_now


class TestRecipientParsing(unittest.TestCase):
    """config.json 里收件人改成了 ${SMTP_RECIPIENTS:-默认}，值可能是逗号分隔的字符串。"""

    def test_list_form(self):
        self.assertEqual(nc._split_recipients(["a@x.com", "b@y.com"]),
                         ["a@x.com", "b@y.com"])

    def test_comma_separated_string(self):
        self.assertEqual(nc._split_recipients("a@x.com,b@y.com"),
                         ["a@x.com", "b@y.com"])

    def test_semicolon_and_space_separated(self):
        self.assertEqual(nc._split_recipients("a@x.com; b@y.com"),
                         ["a@x.com", "b@y.com"])

    def test_display_name_form_is_stripped(self):
        self.assertEqual(nc._split_recipients("每日新闻 <p4rfa1t@outlook.com>"),
                         ["p4rfa1t@outlook.com"])

    def test_empty_entries_dropped(self):
        self.assertEqual(nc._split_recipients(["", "  ", "c@z.cn"]), ["c@z.cn"])
        self.assertEqual(nc._split_recipients(""), [])

    def test_wrong_type_gives_empty_list(self):
        self.assertEqual(nc._split_recipients(None), [])
        self.assertEqual(nc._split_recipients(123), [])

    def test_clean_addr_picks_angle_address(self):
        self.assertEqual(nc._clean_addr("每日科技新闻 <a@b.com>"), "a@b.com")
        self.assertEqual(nc._clean_addr("  1312630400@qq.com  "), "1312630400@qq.com")


class TestSenderRecipientNotHardcoded(unittest.TestCase):
    def test_config_uses_env_placeholders_for_email(self):
        path = os.path.join(BASE_DIR, "config.json")
        if not os.path.exists(path):
            self.skipTest("config.json 不存在")
        # 用原始 JSON 而非 load_config：后者会把 ${VAR:-默认} 展开掉。
        with open(path, encoding="utf-8") as fh:
            ec = json.load(fh)["email"]
        for key in ("sender", "password"):
            self.assertTrue(str(ec[key]).startswith("${"), f"email.{key} 应走环境变量")
        self.assertEqual(len(ec["recipients"]), 1)
        self.assertTrue(str(ec["recipients"][0]).startswith("${SMTP_RECIPIENTS"))

    def test_timezone_is_configured(self):
        path = os.path.join(BASE_DIR, "config.json")
        if not os.path.exists(path):
            self.skipTest("config.json 不存在")
        with open(path, encoding="utf-8") as fh:
            settings = json.load(fh)["settings"]
        self.assertEqual(settings.get("timezone"), "Asia/Shanghai")


class TestUnresolvedPlaceholderGivesClearError(unittest.TestCase):
    """Secret 没配时占位符会原样保留，之前会拿 "${SMTP_SENDER}" 当邮箱去连 SMTP，
    只报 "Connection unexpectedly closed"，排查方向被带偏。"""

    def _cfg(self, field, placeholder):
        ec = {"smtp_host": "smtp.qq.com", "smtp_port": 465, "use_ssl": True,
              "sender": "a@x.com", "password": "pw", "recipients": ["b@y.com"]}
        if field == "recipients":
            ec["recipients"] = [placeholder]
        else:
            ec[field] = placeholder
        return {"email": ec, "settings": {}}

    def test_missing_sender_names_the_variable(self):
        with self.assertRaises(RuntimeError) as cm:
            nc.send_email(self._cfg("sender", "${SMTP_SENDER}"), "s", "<p>x</p>", "x")
        msg = str(cm.exception)
        self.assertIn("SMTP_SENDER", msg)
        self.assertIn("Secrets and variables", msg)

    def test_missing_password_names_the_variable(self):
        with self.assertRaises(RuntimeError) as cm:
            nc.send_email(self._cfg("password", "${SMTP_PASSWORD}"), "s", "<p>x</p>", "x")
        self.assertIn("SMTP_PASSWORD", str(cm.exception))

    def test_missing_recipients_names_the_variable(self):
        with self.assertRaises(RuntimeError) as cm:
            nc.send_email(self._cfg("recipients", "${SMTP_RECIPIENTS}"), "s", "<p>x</p>", "x")
        self.assertIn("SMTP_RECIPIENTS", str(cm.exception))

    def test_error_fires_before_any_network_call(self):
        """必须在连 SMTP 之前就报，否则会白等三个重试。"""
        called = []
        real = nc._smtp_send
        nc._smtp_send = lambda *a, **k: called.append(1)
        try:
            with self.assertRaises(RuntimeError):
                nc.send_email(self._cfg("sender", "${SMTP_SENDER}"), "s", "<p>x</p>", "x")
        finally:
            nc._smtp_send = real
        self.assertEqual(called, [], "占位符没配时不该发出任何 SMTP 连接")


class TestConfigFileContainsNoAddresses(unittest.TestCase):
    """用户要求 config.json 里连邮箱地址本身都看不到。"""

    def _raw_config(self):
        path = os.path.join(BASE_DIR, "config.json")
        if not os.path.exists(path):
            self.skipTest("config.json 不存在")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_email_block_has_no_literal_address_or_default(self):
        ec = self._raw_config()["email"]
        for key in ("sender", "password"):
            val = str(ec[key])
            self.assertTrue(val.startswith("${") and val.endswith("}"),
                            f"email.{key} 应是纯占位符，实际 {val!r}")
        for val in ec["recipients"]:
            self.assertTrue(str(val).startswith("${") and str(val).endswith("}"),
                            f"recipients 应是纯占位符，实际 {val!r}")

    def test_no_email_address_anywhere_in_config_file(self):
        path = os.path.join(BASE_DIR, "config.json")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotRegex(text, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                            "config.json 里不应出现任何邮箱地址")

    def test_email_placeholders_carry_no_default_value(self):
        """`${VAR:-addr}` 会把地址留在文件里，email 段必须用纯 ${VAR}。
        翻译段的 `${VOLC_XXX:-}` 是空默认值，不含任何信息，放行。"""
        with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as fh:
            email = json.load(fh)["email"]
        values = [str(email["sender"]), str(email["password"])]
        values += [str(v) for v in email["recipients"]]
        for val in values:
            self.assertNotIn(":-", val, f"email 段占位符不应带默认值：{val!r}")


class TestCacheDirLayout(unittest.TestCase):
    """缓存文件统一放在 cache/ 目录，由 workflow commit 回仓库。

    以前靠 GitHub Actions cache 存，cache 连续 7 天不命中就过期删除：
    翻译缓存一丢要重跑全部翻译，star 快照一丢增速榜退回"新建 N 天"，
    send_state 一丢会重复发信。
    """

    def test_cache_dir_is_named_cache(self):
        """生产路径必须是 <项目目录>/cache/（测试期间会被重定向到临时目录）。"""
        self.assertEqual(
            os.path.basename(_ORIG_CACHE_DIR), "cache",
            f"生产缓存目录名应为 cache，实际是 {_ORIG_CACHE_DIR}")

    def test_all_state_files_live_in_cache_dir(self):
        for name in _STATE_FILE_NAMES:
            path = getattr(nc, name)
            self.assertEqual(
                os.path.dirname(path), nc.CACHE_DIR,
                f"{name} 应该在 cache/ 目录里，实际是 {path}")

    def test_state_paths_are_rebuilt_from_cache_dir(self):
        """四个路径常量都由 CACHE_DIR 派生，改 CACHE_DIR 才算改到位。"""
        for name in _STATE_FILE_NAMES:
            path = getattr(nc, name)
            self.assertTrue(path.startswith(nc.CACHE_DIR),
                            f"{name} 应由 CACHE_DIR 派生，实际是 {path}")

    def test_tests_do_not_touch_real_cache_dir(self):
        """单测期间缓存必须写进临时目录，不能污染真实 cache/。"""
        self.assertNotEqual(nc.CACHE_DIR, _ORIG_CACHE_DIR)

    def test_legacy_names_cover_the_four_state_files(self):
        self.assertEqual(
            set(nc._LEGACY_CACHE_NAMES),
            {"translation_cache.json", "github_stars.json",
             "github_descriptions.json", "send_state.json"})

    def test_migration_moves_root_file_into_cache(self):
        with _tmpdir() as tmp:
            root = os.path.join(tmp, "root")
            cdir = os.path.join(tmp, "cache")
            os.makedirs(root)
            os.makedirs(cdir)
            legacy = os.path.join(root, "github_stars.json")
            with open(legacy, "w", encoding="utf-8") as fh:
                json.dump({"a/b": {"2026-10-20": 5}}, fh)
            old_root, old_cache = nc.BASE_DIR, nc.CACHE_DIR
            old_flag = nc._CACHE_MIGRATED
            try:
                nc.BASE_DIR = root
                nc.CACHE_DIR = cdir
                nc._CACHE_MIGRATED = False
                nc.migrate_legacy_cache()
                target = os.path.join(cdir, "github_stars.json")
                self.assertTrue(os.path.exists(target), "老文件应被搬进 cache/")
                with open(target, encoding="utf-8") as fh:
                    self.assertEqual(json.load(fh), {"a/b": {"2026-10-20": 5}})
            finally:
                nc.BASE_DIR, nc.CACHE_DIR = old_root, old_cache
                nc._CACHE_MIGRATED = old_flag

    def test_migration_does_not_overwrite_existing_cache_file(self):
        """cache/ 里已有新数据时，老文件不许覆盖它。"""
        with _tmpdir() as tmp:
            root = os.path.join(tmp, "root")
            cdir = os.path.join(tmp, "cache")
            os.makedirs(root)
            os.makedirs(cdir)
            with open(os.path.join(root, "github_stars.json"), "w", encoding="utf-8") as fh:
                json.dump({"old": {"2026-10-19": 1}}, fh)
            with open(os.path.join(cdir, "github_stars.json"), "w", encoding="utf-8") as fh:
                json.dump({"new": {"2026-10-20": 2}}, fh)
            old_root, old_cache = nc.BASE_DIR, nc.CACHE_DIR
            old_flag = nc._CACHE_MIGRATED
            try:
                nc.BASE_DIR = root
                nc.CACHE_DIR = cdir
                nc._CACHE_MIGRATED = False
                nc.migrate_legacy_cache()
                with open(os.path.join(cdir, "github_stars.json"), encoding="utf-8") as fh:
                    self.assertEqual(json.load(fh), {"new": {"2026-10-20": 2}})
            finally:
                nc.BASE_DIR, nc.CACHE_DIR = old_root, old_cache
                nc._CACHE_MIGRATED = old_flag

    def test_migration_is_idempotent(self):
        with _tmpdir() as tmp:
            root = os.path.join(tmp, "root")
            cdir = os.path.join(tmp, "cache")
            os.makedirs(root)
            os.makedirs(cdir)
            with open(os.path.join(root, "send_state.json"), "w", encoding="utf-8") as fh:
                json.dump({"last_sent": "2026-10-20"}, fh)
            old_root, old_cache = nc.BASE_DIR, nc.CACHE_DIR
            old_flag = nc._CACHE_MIGRATED
            try:
                nc.BASE_DIR = root
                nc.CACHE_DIR = cdir
                nc._CACHE_MIGRATED = False
                nc.migrate_legacy_cache()
                nc._CACHE_MIGRATED = False
                nc.migrate_legacy_cache()   # 再跑一次不应报错
                self.assertTrue(os.path.exists(os.path.join(cdir, "send_state.json")))
            finally:
                nc.BASE_DIR, nc.CACHE_DIR = old_root, old_cache
                nc._CACHE_MIGRATED = old_flag

    def test_gitignore_does_not_ignore_cache_dir(self):
        """cache/ 必须入库，否则 workflow 提交不上去。"""
        path = os.path.join(BASE_DIR, ".gitignore")
        with open(path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh]
        self.assertNotIn("cache/", lines, ".gitignore 不该整目录忽略 cache/")
        self.assertNotIn("/cache/", lines)

    def test_workflow_commits_cache_dir(self):
        """workflow 的提交步骤必须把 cache/ 一起提交回仓库。"""
        path = os.path.join(BASE_DIR, ".github", "workflows", "daily-news.yml")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("cache/*", text, "workflow 应提交 cache/ 目录")
        self.assertNotIn("actions/cache/save", text,
                         "不应再用 Actions cache 存翻译缓存（7 天过期）")

    def test_ensure_cache_dir_creates_directory(self):
        with _tmpdir() as tmp:
            cdir = os.path.join(tmp, "cache")
            old_cache = nc.CACHE_DIR
            try:
                nc.CACHE_DIR = cdir
                nc._ensure_cache_dir()
                self.assertTrue(os.path.isdir(cdir))
            finally:
                nc.CACHE_DIR = old_cache




class TestCiGuard(unittest.TestCase):
    """CI 守卫：本地计划任务与 GitHub Actions 不能重复发信。"""

    def _guard(self):
        import importlib.util

        path = os.path.join(BASE_DIR, "ci_guard.py")
        spec = importlib.util.spec_from_file_location("ci_guard_mod", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _judge(self, runs, today="2026-10-20"):
        return self._guard()._judge_runs(runs, today)

    def test_successful_run_today_means_skip(self):
        """今天已有成功的 Actions 运行 -> 本地跳过。"""
        skip, reason = self._judge([{
            "run_number": 10, "created_at": "2026-10-20T07:28:12Z",
            "status": "completed", "conclusion": "success", "event": "schedule",
        }])
        self.assertTrue(skip, "今天 CI 成功了，本地不该再发")
        self.assertIn("成功", reason)

    def test_failed_run_today_means_local_takes_over(self):
        """今天 CI 跑了但失败 -> 本地兜底发送。"""
        skip, reason = self._judge([{
            "run_number": 8, "created_at": "2026-10-20T03:03:09Z",
            "status": "completed", "conclusion": "failure", "event": "schedule",
        }])
        self.assertFalse(skip, "CI 失败时本地应接管")
        self.assertIn("没有成功", reason)

    def test_no_run_today_means_local_takes_over(self):
        """今天完全没跑 -> 本地兜底。"""
        skip, _ = self._judge([{
            "run_number": 7, "created_at": "2026-10-19T08:11:32Z",
            "status": "completed", "conclusion": "success", "event": "schedule",
        }])
        self.assertFalse(skip, "今天没跑，本地应接管")

    def test_in_progress_run_means_do_not_race(self):
        """CI 正在跑 -> 本地别抢跑，等它。"""
        skip, reason = self._judge([{
            "run_number": 9, "created_at": "2026-10-20T07:00:00Z",
            "status": "in_progress", "conclusion": "", "event": "schedule",
        }])
        self.assertTrue(skip, "CI 还在跑，本地不该抢跑")
        self.assertIn("进行中", reason)

    def test_queued_run_means_do_not_race(self):
        """CI 排队中 -> 本地别抢跑。"""
        skip, _ = self._judge([{
            "run_number": 11, "created_at": "2026-10-20T09:00:00Z",
            "status": "queued", "conclusion": "", "event": "schedule",
        }])
        self.assertTrue(skip, "CI 还在排队，本地不该抢跑")

    def test_utc8_day_boundary(self):
        """UTC 10-19 16:00 = 北京 10-20，属于今天。

        Actions 的 created_at 是 UTC，"今天"按北京时间算，这个边界最容易错。
        """
        skip, _ = self._judge([{
            "run_number": 12, "created_at": "2026-10-19T16:00:00Z",
            "status": "completed", "conclusion": "success", "event": "schedule",
        }])
        self.assertTrue(skip, "UTC 10-19 16:00 就是北京时间 10-20，算今天")

    def test_local_state_today_means_skip(self):
        """本地 send_state 是今天 -> 跳过。"""
        mod = self._guard()
        with _tmpdir() as tmp:
            cdir = os.path.join(tmp, "cache")
            os.makedirs(cdir)
            with open(os.path.join(cdir, "send_state.json"), "w", encoding="utf-8") as fh:
                json.dump({"last_sent_date": "2026-10-20"}, fh)
            old = mod.BASE_DIR
            try:
                mod.BASE_DIR = tmp
                self.assertEqual(mod._local_state_date(), "2026-10-20")
            finally:
                mod.BASE_DIR = old

    def test_local_state_missing_returns_empty(self):
        """本地没有状态文件时返回空串而不是抛异常。"""
        mod = self._guard()
        with _tmpdir() as tmp:
            old = mod.BASE_DIR
            try:
                mod.BASE_DIR = tmp
                self.assertEqual(mod._local_state_date(), "")
            finally:
                mod.BASE_DIR = old

    def test_run_news_bat_calls_guard_before_sending(self):
        """run_news.bat 必须先跑守卫，再决定要不要执行主程序。"""
        path = os.path.join(BASE_DIR, "run_news.bat")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("ci_guard.py", text, "run_news.bat 应先调用 CI 守卫")
        self.assertLess(
            text.index("ci_guard.py"),
            text.index("news_collector.py"),
            "守卫必须在主程序之前执行",
        )

    def test_run_news_bat_skips_on_guard_exit_zero_or_two(self):
        """守卫返回 0（已处理）或 2（判定不了）时都要跳过。"""
        path = os.path.join(BASE_DIR, "run_news.bat")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('if "%GUARD%"=="0"', text)
        self.assertIn('if "%GUARD%"=="2"', text,
                      "判定不了时也要保守跳过，避免重复发信")

    def test_env_example_lists_github_token(self):
        """.env.example 要有 GITHUB_TOKEN（守卫查 Actions 状态要用）。"""
        path = os.path.join(BASE_DIR, ".env.example")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("GITHUB_TOKEN", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
