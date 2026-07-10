"""
quant_foundry.modules.sentiment.language — language detection + translation utilities.

Provides lightweight, dependency-free language detection and multilingual
sentiment resources so the sentiment engines can handle non-English media
(European and Asian markets).  All sentiment engines currently assume
English; this module adds the glue needed to support Spanish, French,
German, Chinese, and Japanese alongside English.

Public surface:
    - :func:`detect_language` — heuristic ISO 639-1 language detection.
    - :func:`is_english` — quick boolean check.
    - :func:`translate_prompt` — language-appropriate LLM prompt fragment.
    - :data:`MULTILINGUAL_WORDLISTS` — per-language positive/negative word
      lists for the naive wordlist engine.

The detection is intentionally heuristic (no heavy deps).  For production
use, swap :func:`detect_language` for a proper library (``langdetect``,
``fasttext``) — the rest of the system is agnostic to the implementation.
"""

from __future__ import annotations

import re
import unicodedata

#: Supported language codes.
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "es", "fr", "de", "zh", "ja")

#: Default language when detection fails.
DEFAULT_LANGUAGE = "en"


# --------------------------------------------------------------------------- #
# Character-class helpers                                                      #
# --------------------------------------------------------------------------- #

# CJK Unified Ideographs (common Chinese + shared with Japanese Kanji).
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# Hiragana + Katakana (Japanese-only syllabaries).
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
# Cyrillic (Russian, Ukrainian, etc.).
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")


def _letters_only(text: str) -> str:
    """Return only the letter characters of ``text`` (strip digits/punct)."""
    return "".join(
        ch
        for ch in unicodedata.normalize("NFKD", text)
        if unicodedata.category(ch).startswith(("L", "M"))
    )


# --------------------------------------------------------------------------- #
# Language detection                                                           #
# --------------------------------------------------------------------------- #


def detect_language(text: str) -> str:
    """Detect the language of a text string.

    Returns an ISO 639-1 code (``"en"``, ``"fr"``, ``"de"``, ``"zh"``,
    ``"ja"``).  Uses a simple heuristic approach (no heavy deps):

    - Check for CJK characters → ``"zh"`` or ``"ja"`` (Kana → Japanese).
    - Check for Cyrillic → ``"ru"``.
    - Check for common European language markers (accented letters,
      frequent function words).
    - Default to ``"en"`` if no match.

    For production use, this should be replaced with a proper language
    detection library (``langdetect``, ``fasttext``), but this heuristic
    avoids adding a dependency.
    """
    if not text or not text.strip():
        return DEFAULT_LANGUAGE

    sample = text[:1000]  # only look at the first 1000 chars for speed

    # --- CJK / Japanese ---------------------------------------------------- #
    if _KANA_RE.search(sample):
        # Hiragana/Katakana are unique to Japanese.
        return "ja"
    if _CJK_RE.search(sample):
        # CJK ideographs without Kana → treat as Chinese.
        return "zh"

    # --- Cyrillic ---------------------------------------------------------- #
    if _CYRILLIC_RE.search(sample):
        return "ru"

    # --- European languages (heuristic by function words + accents) ------- #
    lowered = sample.lower()
    words = set(re.findall(r"[a-zà-ÿ]+", lowered))

    # French markers.
    french_markers = {
        "le",
        "la",
        "les",
        "un",
        "une",
        "des",
        "et",
        "de",
        "du",
        "que",
        "qui",
        "dans",
        "pour",
        "avec",
        "sur",
        "pas",
        "ne",
        "ce",
        "cette",
        "est",
        "sont",
        "a",
        "au",
        "aux",
        "by",  # "by" appears in fr headlines
    }
    # German markers.
    german_markers = {
        "der",
        "die",
        "das",
        "und",
        "den",
        "dem",
        "des",
        "ein",
        "eine",
        "einer",
        "eines",
        "mit",
        "von",
        "zu",
        "ist",
        "sind",
        "auf",
        "für",
        "nicht",
        "auch",
        "noch",
        "aber",
        "als",
        "bei",
        "durch",
    }
    # Spanish markers.
    spanish_markers = {
        "el",
        "la",
        "los",
        "las",
        "un",
        "una",
        "unos",
        "unas",
        "y",
        "de",
        "del",
        "que",
        "en",
        "es",
        "son",
        "por",
        "con",
        "para",
        "se",
        "su",
        "al",
        "lo",
        "más",
        "pero",
        "como",
        "sin",
        "sobre",
        "entre",
    }

    fr_hits = len(words & french_markers)
    de_hits = len(words & german_markers)
    es_hits = len(words & spanish_markers)

    # Accented-character hints (only count if no function-word signal).
    has_german_umlauts = bool(re.search(r"[äöüß]", lowered))
    has_french_accents = bool(re.search(r"[àâçéèêëîïôûùüÿœ]", lowered))
    has_spanish_tilde = bool(re.search(r"[ñ¿¡]", lowered))

    # Score each candidate language.
    scores = {
        "fr": fr_hits + (1 if has_french_accents and fr_hits == 0 else 0),
        "de": de_hits + (1 if has_german_umlauts and de_hits == 0 else 0),
        "es": es_hits + (1 if has_spanish_tilde and es_hits == 0 else 0),
    }

    best_lang, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score >= 2:
        return best_lang

    # Fallback: if we have accented chars but no function words, guess by
    # the accent type.
    if has_german_umlauts:
        return "de"
    if has_spanish_tilde:
        return "es"
    if has_french_accents:
        return "fr"

    # Default to English.
    return DEFAULT_LANGUAGE


def is_english(text: str) -> bool:
    """Quick check if text is likely English."""
    return detect_language(text) == "en"


# --------------------------------------------------------------------------- #
# LLM prompt fragments                                                         #
# --------------------------------------------------------------------------- #

#: Base English system prompt (shared with the LLM engines).
_BASE_EN_PROMPT = (
    "You are a financial sentiment analyzer. Given a social media post "
    "or news headline about a stock, return a JSON object with two fields: "
    '"score" (a float in [-1, 1] where -1 is very bearish, 0 is neutral, '
    '1 is very bullish) and "confidence" (a float in [0, 1] indicating '
    "how confident you are in the assessment). "
    "Return ONLY the JSON object, no other text."
)

#: Per-language prompt fragments.  LLMs are multilingual, so we ask the
#: model to analyze sentiment in the text's native language directly —
#: no translation needed.  Each fragment instructs the model to reason
#: in the target language but still return the same JSON schema.
_LANGUAGE_PROMPTS: dict[str, str] = {
    "en": _BASE_EN_PROMPT,
    "es": (
        "Eres un analizador de sentimiento financiero. Dada una publicación "
        "de redes sociales o un titular de noticias sobre una acción, devuelve "
        'un objeto JSON con dos campos: "score" (un float en [-1, 1] donde '
        '-1 es muy bajista, 0 es neutral, 1 es muy alcista) y "confidence" '
        "(un float en [0, 1] que indica tu confianza en la valoración). "
        "Analiza el texto en español. Devuelve SOLO el objeto JSON, sin otro "
        "texto."
    ),
    "fr": (
        "Vous êtes un analyseur de sentiment financier. Étant donné une "
        "publication sur les réseaux sociaux ou un titre d'actualité sur une "
        'action, renvoyez un objet JSON avec deux champs : "score" (un '
        "float dans [-1, 1] où -1 est très baissier, 0 est neutre, 1 est "
        'très haussier) et "confidence" (un float dans [0, 1] indiquant '
        "votre confiance dans l'évaluation). Analysez le texte en français. "
        "Renvoyez UNIQUEMENT l'objet JSON, sans autre texte."
    ),
    "de": (
        "Du bist ein Finanz-Sentiment-Analysator. Analysiere einen "
        "Social-Media-Beitrag oder eine Schlagzeile über eine Aktie und gib "
        'ein JSON-Objekt mit zwei Feldern zurück: "score" (ein Float in '
        "[-1, 1], wobei -1 sehr bärisch, 0 neutral und 1 sehr bullisch ist) "
        'und "confidence" (ein Float in [0, 1], der deine Sicherheit '
        "angibt). Analysiere den Text auf Deutsch. Gib NUR das JSON-Objekt "
        "zurück, kein anderer Text."
    ),
    "zh": (
        "你是一名金融情感分析器。给定一条关于股票的社交媒体帖子或新闻标题，"
        '返回一个包含两个字段的 JSON 对象："score"（[-1, 1] 范围内的浮点数，'
        '-1 表示非常看跌，0 表示中性，1 表示非常看涨）和 "confidence"'
        "（[0, 1] 范围内的浮点数，表示你对评估的置信度）。"
        "请用中文分析文本。只返回 JSON 对象，不要其他文字。"
    ),
    "ja": (
        "あなたは金融センチメント分析器です。株式に関するソーシャルメディアの"
        "投稿またはニュース見出しを分析し、2 つのフィールドを持つ JSON オブジェクト"
        'を返してください："score"（[-1, 1] の浮動小数点数。-1 は非常に弱気、'
        '0 は中立、1 は非常に強気）と "confidence"（[0, 1] の浮動小数点数で、'
        "評価に対する確信度を示す）。テキストを日本語で分析してください。"
        "JSON オブジェクトのみを返し、他のテキストは含めないでください。"
    ),
}


def translate_prompt(target_language: str) -> str:
    """Return a sentiment analysis prompt in the target language.

    For LLM-based sentiment engines, we ask the LLM to analyze sentiment
    in the native language directly (no translation needed).  Returns the
    appropriate prompt fragment.  Falls back to English if the language
    is not supported.
    """
    return _LANGUAGE_PROMPTS.get(target_language, _BASE_EN_PROMPT)


# --------------------------------------------------------------------------- #
# Multilingual sentiment word lists                                            #
# --------------------------------------------------------------------------- #

#: English word lists (reused from naive_wordlist.py for consistency).
_EN_POSITIVE = [
    "beat",
    "beats",
    "surge",
    "surges",
    "jump",
    "jumps",
    "rise",
    "rises",
    "gain",
    "gains",
    "profit",
    "profits",
    "raise",
    "raises",
    "upgrade",
    "outperform",
    "strong",
    "growth",
    "grow",
    "win",
    "wins",
    "approve",
    "approved",
    "launch",
    "unveil",
    "partner",
    "partnership",
    "record",
    "high",
    "boost",
    "boosts",
    "rally",
    "soar",
    "soars",
    "breakthrough",
]
_EN_NEGATIVE = [
    "miss",
    "misses",
    "fall",
    "falls",
    "drop",
    "drops",
    "cut",
    "cuts",
    "lower",
    "lowers",
    "loss",
    "losses",
    "downgrade",
    "weak",
    "decline",
    "declines",
    "sue",
    "sued",
    "sues",
    "lawsuit",
    "settlement",
    "probe",
    "investigation",
    "hack",
    "breach",
    "ban",
    "sanction",
    "recall",
    "halt",
    "delay",
    "fire",
    "fraud",
    "default",
    "bankrupt",
    "warning",
]

#: Spanish positive/negative financial sentiment words.
_ES_POSITIVE = [
    "ganancia",
    "ganancias",
    "sube",
    "suben",
    "crece",
    "crecen",
    "aumenta",
    "aumentan",
    "beneficio",
    "beneficios",
    "récord",
    "alza",
    "repunte",
    "fuerte",
    "sólido",
    "crecimiento",
    "victoria",
    "gana",
    "ganan",
    "aprobado",
    "lanza",
    "alianza",
    "sociedad",
    "impulso",
    "impulsan",
    "positivo",
    "optimista",
    "éxito",
    "superar",
    "supera",
    "recuperación",
]
_ES_NEGATIVE = [
    "pérdida",
    "pérdidas",
    "cae",
    "caen",
    "baja",
    "bajan",
    "recorta",
    "recortan",
    "débil",
    "declive",
    "decliva",
    "demanda",
    "querella",
    "acuerdo",
    "investigación",
    "fraude",
    "quiebra",
    "suspenso",
    "sanción",
    "sancionado",
    "prohibición",
    "retraso",
    "alerta",
    "riesgo",
    "negativo",
    "pesimista",
    "fracaso",
    "incumple",
    "incumplimiento",
    "despido",
    "despidos",
    "crisis",
    "retiro",
]

#: French positive/negative financial sentiment words.
_FR_POSITIVE = [
    "bénéfice",
    "bénéfices",
    "hausse",
    "monte",
    "montent",
    "grimpe",
    "grimpe",
    "croissance",
    "croît",
    "progression",
    "record",
    "rebond",
    "fort",
    "solide",
    "gagne",
    "victoire",
    "approuvé",
    "lance",
    "partenariat",
    "impulsion",
    "positif",
    "optimiste",
    "succès",
    "dépasse",
    "dépassement",
    "ralliement",
    "envol",
    "perce",
    "prometteur",
    "favorable",
    "excellent",
]
_FR_NEGATIVE = [
    "perte",
    "pertes",
    "baisse",
    "tombe",
    "tombent",
    "chute",
    "chutes",
    "faible",
    "déclin",
    "décline",
    "procès",
    "poursuite",
    "poursuites",
    "enquête",
    "fraude",
    "faillite",
    "défaut",
    "sanction",
    "sanctionné",
    "interdiction",
    "retard",
    "alerte",
    "risque",
    "négatif",
    "pessimiste",
    "échec",
    "crise",
    "licenciement",
    "licenciements",
    "récession",
    "dégradation",
    "dégrade",
    "recule",
    "reculent",
]

#: German positive/negative financial sentiment words.
_DE_POSITIVE = [
    "gewinn",
    "gewinne",
    "steigt",
    "steigen",
    "wächst",
    "wachsen",
    "anstieg",
    "anstiege",
    "rekord",
    "erholung",
    "rallye",
    "stark",
    "solide",
    "wachstum",
    "sieg",
    "gewinnt",
    "genehmigt",
    "startet",
    "partnerschaft",
    "schub",
    "positiv",
    "optimistisch",
    "erfolg",
    "übertrifft",
    "durchbruch",
    "aufschwung",
    "hoch",
    "hochpunkt",
    "verbesserung",
    "gut",
    "aussichtsreich",
]
_DE_NEGATIVE = [
    "verlust",
    "verluste",
    "fällt",
    "fallen",
    "rückgang",
    "rückgänge",
    "schwach",
    "rückläufig",
    "klage",
    "klagen",
    "untersuchung",
    "betrug",
    "pleite",
    "insolvenz",
    "ausfall",
    "sanktion",
    "sanktioniert",
    "verbot",
    "verzögerung",
    "warnung",
    "risiko",
    "negativ",
    "pessimistisch",
    "misserfolg",
    "krise",
    "entlassung",
    "entlassungen",
    "rezession",
    "verschlechterung",
    "einbruch",
    "bricht",
    "abbau",
]

#: Chinese positive/negative financial sentiment words.
_ZH_POSITIVE = [
    "上涨",
    "上涨",
    "大涨",
    "飙升",
    "猛涨",
    "创新高",
    "高企",
    "利好",
    "盈利",
    "增长",
    "增长",
    "强劲",
    "突破",
    "反弹",
    "回升",
    "上涨",
    "利好",
    "大涨",
    "走强",
    "走高",
    "攀升",
    "上扬",
    "丰收",
    "成功",
    "批准",
    "合作",
    "伙伴",
    "推出",
    "发布",
    "超越",
    "优异",
    "表现强劲",
]
_ZH_NEGATIVE = [
    "下跌",
    "大跌",
    "暴跌",
    "重挫",
    "下挫",
    "下滑",
    "走低",
    "利空",
    "亏损",
    "损失",
    "疲软",
    "下滑",
    "调查",
    "诉讼",
    "欺诈",
    "破产",
    "违约",
    "制裁",
    "禁止",
    "召回",
    "停牌",
    "延迟",
    "警告",
    "风险",
    "负面",
    "悲观",
    "失败",
    "危机",
    "裁员",
    "衰退",
    "恶化",
    "暴跌",
]

#: Japanese positive/negative financial sentiment words.
_JA_POSITIVE = [
    "上昇",
    "急騰",
    "高騰",
    "最高値",
    "好調",
    "強気",
    "利益",
    "増益",
    "成長",
    "伸び",
    "上振れ",
    "好況",
    "反発",
    "回復",
    "急伸",
    "高値",
    "ブレイク",
    "成功",
    "承認",
    "提携",
    "パートナーシップ",
    "発表",
    "強い",
    "堅調",
    "買い",
    "プラス",
    "期待",
    "躍進",
    "躍進",
    "好転",
    "改善",
    "上方修正",
]
_JA_NEGATIVE = [
    "下落",
    "急落",
    "暴落",
    "安値",
    "不調",
    "弱気",
    "損失",
    "減益",
    "減少",
    "下振れ",
    "不況",
    "下落",
    "急落",
    "安値",
    "調査",
    "訴訟",
    "不正",
    "破綻",
    "違約",
    "制裁",
    "禁止",
    "リコール",
    "停止",
    "遅延",
    "警告",
    "リスク",
    "マイナス",
    "悲観",
    "失敗",
    "危機",
    "リストラ",
    "悪化",
    "下方修正",
]

#: Multilingual word lists keyed by ISO 639-1 code.  Each language has
#: ``positive`` and ``negative`` lists with at least 20 words each.
MULTILINGUAL_WORDLISTS: dict[str, dict[str, list[str]]] = {
    "en": {"positive": _EN_POSITIVE, "negative": _EN_NEGATIVE},
    "es": {"positive": _ES_POSITIVE, "negative": _ES_NEGATIVE},
    "fr": {"positive": _FR_POSITIVE, "negative": _FR_NEGATIVE},
    "de": {"positive": _DE_POSITIVE, "negative": _DE_NEGATIVE},
    "zh": {"positive": _ZH_POSITIVE, "negative": _ZH_NEGATIVE},
    "ja": {"positive": _JA_POSITIVE, "negative": _JA_NEGATIVE},
}


__all__ = [
    "DEFAULT_LANGUAGE",
    "MULTILINGUAL_WORDLISTS",
    "SUPPORTED_LANGUAGES",
    "detect_language",
    "is_english",
    "translate_prompt",
]
