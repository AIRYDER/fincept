"""Sentiment engine modules. Importing this package registers all sentiment modules."""

from __future__ import annotations

from quant_foundry.modules.sentiment.finbert import FinBERTSentiment
from quant_foundry.modules.sentiment.llm_anthropic import AnthropicSentiment
from quant_foundry.modules.sentiment.llm_ensemble import LLMEnsemble4Sentiment
from quant_foundry.modules.sentiment.llm_minimax import MiniMaxSentiment
from quant_foundry.modules.sentiment.llm_openai import OpenAISentiment
from quant_foundry.modules.sentiment.llm_xai import XAISentiment
from quant_foundry.modules.sentiment.naive_wordlist import (
    NaiveWordlistMultilingualSentiment,
    NaiveWordlistSentiment,
)

__all__ = [
    "AnthropicSentiment",
    "FinBERTSentiment",
    "LLMEnsemble4Sentiment",
    "MiniMaxSentiment",
    "NaiveWordlistMultilingualSentiment",
    "NaiveWordlistSentiment",
    "OpenAISentiment",
    "XAISentiment",
]
