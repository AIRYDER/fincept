from fincept_bus.streams import RETENTION, STREAM_SIG_NEWS_IMPACT


def test_news_impact_stream_has_explicit_retention() -> None:
    assert STREAM_SIG_NEWS_IMPACT == "sig.news_impact"
    assert RETENTION[STREAM_SIG_NEWS_IMPACT] == 50_000
