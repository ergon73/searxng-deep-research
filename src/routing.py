"""
Retrieval routing (skill 6.3: retrieval-routing).

Classifies user query intent and recommends SearXNG search parameters
(engines, categories, time_range) plus route-specific query variants.

This is an ADVISORY layer: it returns recommended parameters, but
the caller (Hermes / Ерёма / the chat) decides whether to apply them
to the actual web_search() call.

Spec: ~/.hermes/skills/research/retrieval-routing/SKILL.md
Source audit: /tmp/hermes-recomendation-07062026.txt, section 6.3 + 7 (Phase C)

Why advisory (not auto)?
- Routing a news query into categories=news is usually safe. But
  routing a medical query into engines=curewiki only is dangerous.
- Better to surface the recommendation in `build_search_plan_preview`
  and let a human confirm before applying.

Hard rules:
- Never route high-stakes queries to a narrower engine set without
  confirmation.
- For "reviews" route, prefer query variants (q + "reddit", q + "forum")
  over engine restriction — SearXNG engines vary by deployment.
- For "academic" route, only return engines if the user query is clearly
  research-oriented (arxiv, paper, etc.), not just any "study" word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ====================================================================
# Route definitions
# ====================================================================

# Each route maps to (suggested_engines, suggested_categories, default_time_range).
# Engines are hints; if a particular engine is not enabled in the SearXNG
# instance, SearXNG will silently skip it. We don't fail on missing engines.
ROUTE_PARAMS: dict[str, dict] = {
    "general": {
        "engines": None,
        "categories": None,
        "default_time_range": None,
    },
    "news": {
        "engines": None,
        "categories": "news",
        # FIX 2026-06-07: month вместо day. day — слишком узко для запросов
        # типа "Apple CEO 2024" (нужны статьи за 2024, не за последние 24 часа).
        "default_time_range": "month",
    },
    "llm_release": {
        # Radar discovery needs general web indexes. Scientific/code engines
        # are queried through source-specific variants or connectors instead
        # of polluting every broad release query.
        "engines": "presearch,bing,mojeek",
        "categories": "general",
        # SearXNG has no 48-hour value. A week avoids losing releases that
        # search indexes expose a day or two late; classification applies the
        # exact 48-hour cutoff later from primary-source evidence.
        "default_time_range": "week",
    },
    "forums": {
        "engines": "reddit,stackoverflow,hackernews,github",
        "categories": None,
        "default_time_range": None,
    },
    "docs": {
        "engines": "github,mdn,stackoverflow,wikidata",
        "categories": None,
        "default_time_range": None,
    },
    "academic": {
        "engines": "arxiv,semanticscholar,pubmed,openalex",
        "categories": "science",
        "default_time_range": "year",
    },
    "github": {
        "engines": "github",
        "categories": None,
        "default_time_range": None,
    },
    "reviews": {
        # Reviews don't have a single engine; we use query variants.
        # See _review_query_variants().
        "engines": None,
        "categories": None,
        "default_time_range": None,
    },
    "security": {
        "engines": "github,nvd,cve,securitytracker",
        "categories": None,
        "default_time_range": "year",
    },
    "product": {
        # Product comparisons: use query variants and category.
        "engines": None,
        "categories": None,
        "default_time_range": None,
    },
    # FIX 2026-06-07: technical + wiki routes added для eval set coverage.
    # Без них все factual queries падали в 'general', eval routing_accuracy = 0%.
    "technical": {
        # FIX 2026-06-07 (online eval q1_falcon9): factual technical queries
        # (SpaceX, Apple, GPT, Falcon, ...) need Wikipedia + arxiv + semanticscholar
        # for encyclopedic / research content. github/stackoverflow/wikidata
        # return 0 results for "Falcon 9" because there's no code repo.
        "engines": "wikipedia,arxiv,semanticscholar",
        "categories": "general",
        "default_time_range": "year",
    },
    "wiki": {
        # FIX 2026-06-07: same reasoning — wikidata is structured triples,
        # not Wikipedia articles. Wikipedia engine is what we want.
        "engines": "wikipedia,wikidata",
        "categories": "general",
        "default_time_range": None,
    },
}


# ====================================================================
# Keyword classifiers (deterministic, multi-language)
# ====================================================================

# Each entry: (route_name, list_of_patterns)
# Patterns are case-insensitive regex. A match contributes +score to that
# route. We pick the route with the highest score; ties broken by order.

_NEWS_PATTERNS = [
    r"\bnews\b",
    r"\bновост[ьи]\b",
    r"сегодня",
    r"вчера",
    r"latest",
    r"breaking",
    r"событи[ея]",
    r"сообщил[аи]?",
    # FIX 2026-06-07: recency patterns для year-based queries.
    # "Apple CEO 2024" / "GPT-4 2023" — это news queries (люди/события, не железо).
    r"\bв\s+(20[2-9]\d)\s+год[ау]?\b",  # "в 2024 году"
    r"\b(20[2-9]\d)\s+год[ау]?\b",  # "2024 год" / "2024 года"
    r"\bin\s+(20[2-9]\d)\b",  # "in 2024"
    r"\b(20[2-9]\d)\s+launch",  # "2024 launch"
    r"\bвышел\b",
    r"\bвышла\b",
    r"\bвыпустил[аи]?\b",  # "вышел", "выпустил"
    r"\bзапущен[аы]?\b",
    r"\bзапустил[аи]?\b",
    r"\bрелиз[а-яё]*\b",
    r"\bанонсиров[а-яё]*\b",
    r"\bпредстав[а-яё]*\b",  # "запущен", "релиз", "анонсирован"
    r"\bcurrent\b",
    r"\bтекущ[аио]?\b",
]

_LLM_RELEASE_PATTERNS = [
    r"\b(?:new|latest|released?|launch(?:ed)?|upcoming)\b.{0,50}\b(?:llms?|language models?|foundation models?)\b",
    r"\b(?:llms?|language models?|foundation models?)\b.{0,50}\b(?:released?|releases?|launch(?:ed)?|announcement)\b",
    r"\b(?:нов(?:ая|ые|ый)|свеж(?:ая|ие|ий)|вышедш(?:ая|ие|ий))\b.{0,50}\b(?:llm|языков(?:ая|ые|ой) модел[ьи])\b",
    r"\b(?:llm|языков(?:ая|ые|ой) модел[ьи])\b.{0,50}\b(?:релиз|вышл[аи]?|выпущен[аы]?|анонс)\b",
]

_FORUMS_PATTERNS = [
    r"\breddit\b",
    r"\bфорум[ауом]?\b",
    r"\bforum[s]?\b",
    r"\bdiscussion\b",
    r"\bdiscourse\b",
    r"\bсообществ[ао]\b",
    r"hacker ?news",
    r"\bhn\b",
    r"\bобсуждени[ея]\b",
]

_REVIEWS_PATTERNS = [
    r"\bотзыв[аыу]?\b",
    r"\breview[s]?\b",
    r"\buser experience\b",
    r"\bопыт\b",
    r"мнени[ея] польз[ователей]*",
    r"\bopinions?\b",
    r"что дума[ею]т[ь]? о",
    r"стоит ли покупать",
]

_DOCS_PATTERNS = [
    r"\bдокументаци[яи]\b",
    r"\bdocs?\b",
    r"\bdocumentation\b",
    r"\bapi reference\b",
    r"\btutorial\b",
    r"\bгайд[ау]?\b",
    r"\bhow to\b",
    r"\bmanual\b",
    r"\bруководств[оа]\b",
    r"\bgetting started\b",
]

_ACADEMIC_PATTERNS = [
    r"\barxiv\b",
    r"\bresearch paper\b",
    r"\bисследовани[ея]\b",
    r"\bстать[яьи]\b",
    r"\bpaper[s]?\b",
    r"\bpubmed\b",
    r"\bsemantics? scholar\b",
    r"\bpeer[- ]reviewed\b",
    r"\bacademic\b",
    r"\bнаучн[аыо]\b",
    r"\bдиссертаци[яи]\b",
    r"\bstudy\b",  # weak signal: requires context
]

_GITHUB_PATTERNS = [
    r"\bgithub\b",
    r"\brepo(sitory)?\b",
    r"\bрепозитори[йя]\b",
    r"\bsource code\b",
    r"\bисходник[иа]?\b",
    r"\bgit clone\b",
]

# FIX 2026-06-07: technical route для техники/устройств/продуктов/AI.
# Срабатывает на: SpaceX, Falcon, ракет*, Apple, iPhone, GPT, NVIDIA, Tesla,
# MacBook, Windows, Linux, AMD, Intel, CPU, GPU, RAM, плавления, температура.
_TECHNICAL_PATTERNS = [
    # Brand names (technical products / companies)
    r"\bspacex\b",
    r"\bfalcon\s*9\b",
    r"\bfalcon\s*heavy\b",
    r"\bnasa\b",
    r"\broscosmos\b",
    r"\bESA\b",
    r"\bapple\b",
    r"\biphone\b",
    r"\bmacbook\b",
    r"\bipad\b",
    r"\bnvidia\b",
    r"\btesla\b",
    r"\bamd\b",
    r"\bintel\b",
    r"\bsamsung\b",
    r"\bgoogle\b",
    r"\bmicrosoft\b",
    # AI models
    r"\bgpt[- ]?\d?\b",
    r"\bchatgpt\b",
    r"\bclaude\b",
    r"\bgemini\b",
    r"\bllama\b",
    r"\bdeepseek\b",
    r"\bmistral\b",
    # Technical nouns (RU + EN)
    r"\bракет[аыу]?\b",
    r"\bспутник[иа]?\b",
    r"\bстарт[аы]?\b",
    r"\bзапус[кт][а-яё]*\b",
    r"\bступен[ьи]\w*\b",
    r"\brocket[s]?\b",
    r"\blaunch(es)?\b",
    r"\bsatellite[s]?\b",
    # Technical metrics
    r"\btda?\b",
    r"\bgpu\b",
    r"\bcpu\b",
    r"\bram\b",
    r"\bssd\b",
    r"\bprocessor\b",
    r"\bчип[ау]?\b",
    r"\bвидеокарт[аыу]?\b",
    r"\bпроцессор[ау]?\b",
    r"\bоперативн[аяо][\s-]?памят[ьи]\b",
]

# FIX 2026-06-07: wiki route для factual / encyclopedic queries.
# Срабатывает на: что такое, кто основал, когда основан, история, страны,
# города, физические константы, биология, космос.
_WIKI_PATTERNS = [
    # Definition / explanation
    r"\bчто такое\b",
    r"\bчто\s+значит\b",
    r"\bопределени[ея]\b",
    r"\bwhat is\b",
    r"\bwhat are\b",
    r"\bdefine\b",
    r"\bdefinition\b",
    # Founding / history (RU)
    r"\bоснован[аы]?\b",
    r"\bосновани[ея]\b",
    r"\bобразован[аы]?\b",
    r"\bсоздан[аы]?\b",
    r"\bучрежд[ёе]н[аы]?\b",
    r"\bfounded\b",
    r"\bestablished\b",
    r"\bformation\b",
    # Countries / geography / people
    r"\bстран[аыуо]?\s*[-\s]?член[аов]?\b",
    r"\bстран[аыуо]?\b",
    r"\bгород[ау]?\b",
    r"\bнаселённ[аыо]?\w*\b",
    r"\bстолиц[аыу]?\b",
    r"\bнаселение\b",
    r"\bтерритори[яи]\b",
    r"\bcountry\b",
    r"\bcity\b",
    r"\bpopulation\b",
    r"\bcapital\b",
    # Scientific facts
    r"\bтемператур[аыу]?\s+плавлени[яе]\b",
    r"\bмасса\b",
    r"\bатомн[аыо]\s+масс[аыу]?\b",
    r"\bмолекул[аыу]?\b",
    r"\bэлемент[аы]?\b",
    r"\bвселенн[аыу]?\b",
    r"\bпланет[аыу]?\b",
    r"\bзвезд[аыу]?\b",
    r"\bматери[яи]\b",
    r"\bэнерги[яи]\b",
    r"\bквантов[аыо]?\b",
    r"\bgalaxy\b",
    r"\buniverse\b",
    r"\bplanet[s]?\b",
    r"\bstar[s]?\b",
    r"\bmatter\b",
    r"\benergy\b",
    r"\bquantum\b",
]

_SECURITY_PATTERNS = [
    r"\bcve\b",
    r"\bvulnerab(ilit)?y\b",
    r"\bexploit\b",
    r"\b0[- ]?day\b",
    r"\bsecurity advisory\b",
    r"\bpatch(ed)?\b",
    r"\bуязвимост[ьи]\b",
    r"\bэксплойт[ау]?\b",
]

_PRODUCT_PATTERNS = [
    r"\bvs\.?\b",
    r"\bcompared?\b",
    r"\bсравн[еи]ть?\b",
    r"\balternative[s]?\b",
    r"\bаналог[иа]?\b",
    r"\bобзор[аы]?\b",
    r"\bpricing\b",
    r"\bchangelog\b",
    r"\bbenchmark[s]?\b",
]


_CLASSIFIERS: list[tuple[str, list[str]]] = [
    # Vertical-specific, high-signal route must win over generic news/technical.
    ("llm_release", _LLM_RELEASE_PATTERNS),
    ("security", _SECURITY_PATTERNS),
    ("academic", _ACADEMIC_PATTERNS),
    ("github", _GITHUB_PATTERNS),
    ("news", _NEWS_PATTERNS),
    ("docs", _DOCS_PATTERNS),
    ("forums", _FORUMS_PATTERNS),
    ("reviews", _REVIEWS_PATTERNS),
    ("product", _PRODUCT_PATTERNS),
    # FIX 2026-06-07: technical + wiki routes added. Порядок важен:
    # technical ПЕРЕД wiki, т.к. техника часто wiki-ифицируема (Apple wiki),
    # но если явный tech signal — technical выигрывает.
    ("technical", _TECHNICAL_PATTERNS),
    ("wiki", _WIKI_PATTERNS),
]


def _score_route(query: str) -> dict[str, float]:
    """Return {route_name: match_count} for the query.

    FIX 2026-06-07: добавлен recency boost — если query содержит year 2020+,
    news score получает +0.5, чтобы technical queries типа "Apple CEO 2024"
    классифицировались как news (а не technical).
    """
    q = query.lower()
    scores: dict[str, float] = {}
    for route, patterns in _CLASSIFIERS:
        hits = 0
        for pat in patterns:
            if re.search(pat, q, re.IGNORECASE):
                hits += 1
        if hits > 0:
            scores[route] = float(hits)
    # Recency boost: news предпочтительнее technical для текущих queries
    if re.search(r"\b(20[2-9]\d)\b", q):
        if "news" in scores:
            scores["news"] += 0.5
        # Если news не было — добавляем с весом 0.5 (ниже technical,
        # но достаточно чтобы news не проиграл technical с hits=1 vs hits=1+0.5=1.5)
        elif "technical" in scores:
            scores["news"] = scores.get("news", 0.0) + 0.5
    return scores


# ====================================================================
# Recency detection
# ====================================================================

_RECENCY_PATTERNS = [
    r"\blatest\b",
    r"\bновейш\b",
    r"\bпоследн[иея]?\b",
    r"\bнедавн[ио]?\b",
    r"\bcurrent\b",
    r"\bтекущ[аио]?\b",
    r"\b2026\b",
    r"\b2025\b",
    r"\bв этом году\b",
    r"\byesterday\b",
    r"\bвчера\b",
    r"\btoday\b",
    r"\bсегодня\b",
]

_RECENCY_TIME_RANGES = {
    # Words that suggest very recent (last day)
    "day": [r"\btoday\b", r"\bсегодня\b", r"\bвчера\b", r"\byesterday\b", r"\bbreaking\b", r"\bтолько что\b"],
    # Last week
    "week": [
        r"\bthis week\b",
        r"\bна этой неделе\b",
        r"\bнедавн[ио]\b",
        r"\blast\s+48\s+hours?\b",
        r"\bpast\s+48\s+hours?\b",
        r"\bпоследн(?:ие|их)\s+48\s+час",
        r"\bза\s+последн(?:ие|их)\s+48\s+час",
    ],
    # Last month
    "month": [r"\bthis month\b", r"\bв этом месяце\b"],
    # Last year
    "year": [r"\b2026\b", r"\b2025\b", r"\bthis year\b", r"\bв этом году\b"],
}


def _detect_recency(query: str) -> str | None:
    """Return inferred time_range (day/week/month/year) or None."""
    q = query.lower()
    for time_range, patterns in _RECENCY_TIME_RANGES.items():
        for pat in patterns:
            if re.search(pat, q, re.IGNORECASE):
                return time_range
    return None


# ====================================================================
# Query variants for routes that need them (reviews, product, forums)
# ====================================================================


def _review_query_variants(query: str) -> list[str]:
    """Generate query variants for 'reviews' route.

    SearXNG doesn't have a universal 'reviews' engine. To surface
    user opinions / forum discussions, we add qualifiers.
    """
    return [
        f"{query} reddit",
        f"{query} forum",
        f"{query} user experience",
        f"{query} отзывы",
    ]


def _product_query_variants(query: str) -> list[str]:
    """Generate query variants for 'product' route."""
    return [
        f"{query} review",
        f"{query} pricing",
        f"{query} changelog",
    ]


def _forum_query_variants(query: str) -> list[str]:
    """Generate query variants for 'forums' route."""
    return [
        f"{query} reddit",
        f"{query} site:reddit.com",
        f"{query} discussion",
    ]


def _docs_query_variants(query: str) -> list[str]:
    """Generate query variants for 'docs' route."""
    return [
        f"{query} site:github.com",
        f"{query} documentation",
        f"{query} tutorial",
    ]


def _llm_release_query_variants(query: str) -> list[str]:
    """Split release discovery into announcement and open-weight channels."""
    return [
        f"{query} official announcement",
        f"{query} open weights Hugging Face GitHub",
    ]


# ====================================================================
# Public API
# ====================================================================


@dataclass
class Intent:
    """Classified user intent with routing recommendations.

    Fields:
        route: primary route name (general, news, llm_release, forums, docs,
               academic, github, reviews, security, product)
        confidence: 0.0-1.0; how confident the classifier is
        engines: suggested SearXNG engines (comma-separated) or None
        categories: suggested SearXNG categories or None
        time_range: inferred recency (day/week/month/year) or None
        query_variants: route-specific extra queries to broaden coverage
        all_routes: list of (route, score) pairs sorted by score desc
                     for transparency in the preview
    """

    route: str
    confidence: float
    engines: str | None = None
    categories: str | None = None
    time_range: str | None = None
    query_variants: list[str] = field(default_factory=list)
    all_routes: list[tuple[str, float]] = field(default_factory=list)
    routing_warning: bool = False


def classify_intent(query: str) -> Intent:
    """Classify a user query and return routing recommendations.

    Pure function, no network. Safe to call in dry-run / preview mode.

    Args:
        query: The raw user query (any length, any language).

    Returns:
        Intent with primary route + suggested SearXNG params +
        route-specific query variants + confidence.
    """
    if not query or not query.strip():
        return Intent(
            route="general",
            confidence=0.0,
            engines=None,
            categories=None,
            time_range=None,
            query_variants=[],
            all_routes=[],
        )

    scores = _score_route(query)
    if not scores:
        route = "general"
        confidence = 0.5  # no signal, default to general with low confidence
    else:
        # Sort by score desc; ties broken by classifier order (security first)
        sorted_routes = sorted(
            scores.items(),
            key=lambda x: (-x[1], [r for r, _ in _CLASSIFIERS].index(x[0])),
        )
        route, top_score = sorted_routes[0]
        # Confidence scales with top score (1 hit -> 0.6, 2 -> 0.75, 3+ -> 0.9)
        if top_score >= 3:
            confidence = 0.9
        elif top_score >= 2:
            confidence = 0.75
        else:
            confidence = 0.6

    params = ROUTE_PARAMS.get(route, ROUTE_PARAMS["general"])
    engines = params["engines"]
    categories = params["categories"]
    default_tr = params["default_time_range"]

    # Recency override: if query has explicit recency words, use those
    # over the route's default.
    inferred_tr = _detect_recency(query)
    if route == "llm_release" and inferred_tr == "year":
        # A calendar year in a Radar query describes the current window; it
        # must not expand a last-48-hour search to a whole year.
        inferred_tr = None
    time_range = inferred_tr or default_tr

    # Route-specific query variants
    variants: list[str] = []
    if route == "reviews":
        variants = _review_query_variants(query)
    elif route == "product":
        variants = _product_query_variants(query)
    elif route == "forums":
        variants = _forum_query_variants(query)
    elif route == "docs":
        variants = _docs_query_variants(query)
    elif route == "llm_release":
        variants = _llm_release_query_variants(query)

    # Build all_routes for transparency
    all_routes = sorted(
        scores.items(),
        key=lambda x: (-x[1], [r for r, _ in _CLASSIFIERS].index(x[0])),
    )

    # Compute warning flag here so callers don't need to call
    # should_warn_about_routing() separately.
    warning = False
    if confidence < 0.75:
        warning = True
    elif route == "security" and engines:
        warning = True
    elif len(all_routes) >= 2 and all_routes[0][1] == all_routes[1][1]:
        warning = True

    return Intent(
        route=route,
        confidence=confidence,
        engines=engines,
        categories=categories,
        time_range=time_range,
        query_variants=variants,
        all_routes=all_routes,
        routing_warning=warning,
    )


def should_warn_about_routing(intent: Intent) -> bool:
    """True if the routing decision should be surfaced to the user.

    Reasons to warn:
    - Confidence < 0.75 (ambiguous classification)
    - High-stakes route (security) with narrow engines
    - Multiple routes tied or close in score
    """
    if intent.confidence < 0.75:
        return True
    if intent.route == "security" and intent.engines:
        # Narrowing security search could miss context
        return True
    if len(intent.all_routes) >= 2:
        top, second = intent.all_routes[0], intent.all_routes[1]
        if top[1] == second[1]:
            # Tie — let the user disambiguate
            return True
    return False
