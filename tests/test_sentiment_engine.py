"""测试 sentiment_engine — 情绪分析核心逻辑"""

import sys
sys.path.insert(0, '/Users/mac/workspace/SerenityMonitor')

from sentiment_engine import (
    _get_market_prefix, _build_news_url, _analyze_sentiment,
    _parse_news_items, compute_sentiment_score, _sentiment_label,
    BULLISH_KEYWORDS, BEARISH_KEYWORDS,
)


class TestMarketPrefix:
    def test_sh_stock_600(self):
        """600xxx → sh"""
        assert _get_market_prefix("600460") == "sh"

    def test_sz_stock_002(self):
        """002xxx → sz"""
        assert _get_market_prefix("002281") == "sz"

    def test_sz_stock_000(self):
        """000xxx → sz"""
        assert _get_market_prefix("000988") == "sz"

    def test_fallback_unknown(self):
        """未配置的 6 开头代码 fallback → sh"""
        assert _get_market_prefix("688999") == "sh"


class TestBuildNewsUrl:
    def test_build_sz_url(self):
        url = _build_news_url("002281")
        assert "sz002281" in url
        assert "sina.com.cn" in url

    def test_build_sh_url(self):
        url = _build_news_url("600460")
        assert "sh600460" in url


class TestAnalyzeSentiment:
    def test_bullish_keyword(self):
        for kw in BULLISH_KEYWORDS:
            title = f"公司发布{kw}公告"
            assert _analyze_sentiment(title) == 1, f"Expected bullish for '{title}'"

    def test_bearish_keyword(self):
        for kw in BEARISH_KEYWORDS:
            title = f"公司发布{kw}公告"
            assert _analyze_sentiment(title) == -1, f"Expected bearish for '{title}'"

    def test_neutral_title(self):
        assert _analyze_sentiment("今日正常交易") == 0
        assert _analyze_sentiment("") == 0
        assert _analyze_sentiment("公司召开股东大会") == 0

    def test_bullish_takes_precedence(self):
        """牛市关键词优先于熊市（先检查牛市列表）"""
        # "买入" 是 bullish，"减持" 是 bearish → 先匹配买入
        assert _analyze_sentiment("买入减持混合标题") == 1


class TestParseNewsItems:
    def test_empty_html(self):
        assert _parse_news_items("") == []
        assert _parse_news_items("<html></html>") == []

    def test_single_news_item(self):
        html = """
        <div class="datelist"><ul>
        &nbsp;&nbsp;&nbsp;2026-06-06 11:54&nbsp;&nbsp;
        <a target='_blank' href='http://example.com'>公司发布增持公告</a> <br>
        </ul></div>
        """
        items = _parse_news_items(html)
        assert len(items) == 1
        assert items[0]["title"] == "公司发布增持公告"
        assert "2026" in items[0]["date"]

    def test_multiple_news_items(self):
        html = """
        <div class="datelist"><ul>
        &nbsp;&nbsp;&nbsp;2026-06-06 11:54&nbsp;&nbsp;
        <a href='http://a.com'>第一条新闻标题</a><br>
        &nbsp;&nbsp;&nbsp;2026-06-05 09:30&nbsp;&nbsp;
        <a href='http://b.com'>第二条新闻标题</a><br>
        </ul></div>
        """
        items = _parse_news_items(html)
        assert len(items) == 2

    def test_short_title_filtered(self):
        """标题少于 4 个字被过滤"""
        html = """
        <div class="datelist"><ul>
        &nbsp;&nbsp;&nbsp;2026-06-06 11:54&nbsp;&nbsp;
        <a href='http://a.com'>AB</a><br>
        </ul></div>
        """
        items = _parse_news_items(html)
        assert len(items) == 0

    def test_no_datelist_div(self):
        """没有 datelist 区域时返回空"""
        html = "<html><body>没有新闻列表</body></html>"
        items = _parse_news_items(html)
        assert items == []

    def test_unicode_in_title(self):
        """中文和特殊字符正常解析"""
        html = """
        <div class="datelist"><ul>
        &nbsp;&nbsp;&nbsp;2026-06-06 14:22&nbsp;&nbsp;
        <a href='http://x.com'>公司中标🎯重大项目合同超预期</a><br>
        </ul></div>
        """
        items = _parse_news_items(html)
        assert len(items) == 1
        assert "中标" in items[0]["title"]


class TestSentimentLabel:
    def test_high_score(self):
        assert "积极" in _sentiment_label(85)
        assert "积极" in _sentiment_label(100)

    def test_mid_high_score(self):
        assert "偏多" in _sentiment_label(60)
        assert "偏多" in _sentiment_label(69)

    def test_neutral_score(self):
        assert "中性" in _sentiment_label(50)
        assert "中性" in _sentiment_label(45)

    def test_mid_low_score(self):
        assert "偏空" in _sentiment_label(35)
        assert "偏空" in _sentiment_label(30)

    def test_low_score(self):
        assert "消极" in _sentiment_label(10)
        assert "消极" in _sentiment_label(0)


class TestComputeSentimentKeywordPath:
    """关键词路径 — compute_sentiment_score 的回退逻辑"""

    def test_bullish_news_raises_score(self, monkeypatch):
        """正面新闻提升情绪分"""
        def mock_fetch(code):
            return [{"title": "公司发布增持买入公告", "date": "2026-06-08", "source": "新浪"}]
        monkeypatch.setattr('sentiment_engine.fetch_sentiment_data', mock_fetch)
        monkeypatch.setattr('sentiment_engine.LLM_AVAILABLE', False)

        score = compute_sentiment_score("002281")
        assert score > 50

    def test_bearish_news_lowers_score(self, monkeypatch):
        """负面新闻降低情绪分"""
        def mock_fetch(code):
            return [{"title": "公司发布减持利空公告", "date": "2026-06-08", "source": "新浪"}]
        monkeypatch.setattr('sentiment_engine.fetch_sentiment_data', mock_fetch)
        monkeypatch.setattr('sentiment_engine.LLM_AVAILABLE', False)

        score = compute_sentiment_score("002281")
        assert score < 50

    def test_mixed_news(self, monkeypatch):
        """混合新闻互相抵消"""
        def mock_fetch(code):
            return [
                {"title": "公司发布增持公告", "date": "2026-06-08", "source": "新浪"},
                {"title": "公司发布减持公告", "date": "2026-06-08", "source": "新浪"},
            ]
        monkeypatch.setattr('sentiment_engine.fetch_sentiment_data', mock_fetch)
        monkeypatch.setattr('sentiment_engine.LLM_AVAILABLE', False)

        score = compute_sentiment_score("002281")
        assert score == 50.0  # +5 -5 = 0

    def test_no_news_returns_default(self, monkeypatch):
        """无新闻返回默认 50"""
        def mock_fetch(code):
            return []
        monkeypatch.setattr('sentiment_engine.fetch_sentiment_data', mock_fetch)
        monkeypatch.setattr('sentiment_engine.LLM_AVAILABLE', False)

        score = compute_sentiment_score("002281")
        assert score == 50.0

    def test_score_capped_at_100(self, monkeypatch):
        """分数不会超过 100"""
        def mock_fetch(code):
            news = [{"title": f"公司发布{k}公告", "date": "2026-06-08", "source": "新浪"}
                    for k in BULLISH_KEYWORDS * 3]
            return news
        monkeypatch.setattr('sentiment_engine.fetch_sentiment_data', mock_fetch)
        monkeypatch.setattr('sentiment_engine.LLM_AVAILABLE', False)

        score = compute_sentiment_score("002281")
        assert score == 100.0

    def test_score_floor_at_0(self, monkeypatch):
        """分数不会低于 0"""
        def mock_fetch(code):
            news = [{"title": f"公司发布{k}公告", "date": "2026-06-08", "source": "新浪"}
                    for k in BEARISH_KEYWORDS * 3]
            return news
        monkeypatch.setattr('sentiment_engine.fetch_sentiment_data', mock_fetch)
        monkeypatch.setattr('sentiment_engine.LLM_AVAILABLE', False)

        score = compute_sentiment_score("002281")
        assert score == 0.0

    def test_exception_in_fetch_returns_default(self, monkeypatch):
        """获取数据异常返回 50"""
        def mock_fetch(code):
            raise RuntimeError("network error")
        monkeypatch.setattr('sentiment_engine.fetch_sentiment_data', mock_fetch)
        monkeypatch.setattr('sentiment_engine.LLM_AVAILABLE', False)

        score = compute_sentiment_score("002281")
        assert score == 50.0

    def test_exception_in_llm_falls_back_to_keyword(self, monkeypatch):
        """LLM失败后回退到关键词模式"""
        def mock_fetch(code):
            return [{"title": "公司发布增持公告", "date": "2026-06-08", "source": "新浪"}]
        monkeypatch.setattr('sentiment_engine.fetch_sentiment_data', mock_fetch)
        monkeypatch.setattr('sentiment_engine.LLM_AVAILABLE', True)
        monkeypatch.setattr('sentiment_engine._analyze_sentiment_llm', lambda c, n, t: None)

        score = compute_sentiment_score("002281")
        assert score > 50  # 回退到关键词路径
