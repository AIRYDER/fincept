"""Source adapter modules. Importing this package registers all source modules."""

from __future__ import annotations

from quant_foundry.modules.sources.newsapi import NewsAPISource
from quant_foundry.modules.sources.reddit import RedditSource
from quant_foundry.modules.sources.stocktwits import StockTwitsSource
from quant_foundry.modules.sources.x_twitter import XTwitterSource

__all__ = [
    "NewsAPISource",
    "RedditSource",
    "StockTwitsSource",
    "XTwitterSource",
]
