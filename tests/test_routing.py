"""
Tests for skill 6.3: retrieval-routing.

Coverage:
1. classify_intent() for each route (news, forums, docs, academic,
   github, security, product, reviews, general)
2. should_warn_about_routing() conditions
3. adapt_query() integration: routing fields appear in result
4. build_search_plan_preview() includes routing section
5. Route-specific query variants
6. Recency detection
7. Adversarial: multi-route ambiguity, short queries, edge cases
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from query_adaptation import adapt_query, build_search_plan_preview
from routing import Intent, classify_intent, should_warn_about_routing

# ====================================================================
# 1. Per-route classification
# ====================================================================


class TestRouteClassification:
    """Each route's classifier should fire on canonical queries."""

    def test_route_news(self):
        i = classify_intent("latest news on BPLA attacks in Moscow")
        assert i.route == "news", f"expected news, got {i.route}"
        assert i.categories == "news"
        assert i.time_range in ("day", "week", "month", "year")
        assert i.confidence >= 0.6

    def test_route_llm_release_radar(self):
        i = classify_intent("new LLM model releases in the last 48 hours July 2026")
        assert i.route == "llm_release"
        assert i.time_range == "week"
        assert i.categories is None
        assert i.engines == "presearch,bing,mojeek"
        assert len(i.query_variants) == 2
        assert any("official announcement" in query for query in i.query_variants)
        assert any("Hugging Face" in query for query in i.query_variants)

    def test_route_llm_release_radar_ru(self):
        i = classify_intent("новые вышедшие LLM модели за последние 48 часов")
        assert i.route == "llm_release"
        assert i.time_range == "week"

    def test_route_news_ru(self):
        i = classify_intent("новости про БПЛА сегодня в Москве")
        assert i.route == "news"

    def test_route_forums(self):
        i = classify_intent("What do people think about Flutter on reddit")
        assert i.route == "forums"
        assert "reddit" in (i.engines or "").lower()

    def test_route_forums_ru(self):
        i = classify_intent("обсуждение на форуме про Python async")
        assert i.route == "forums"

    def test_route_docs(self):
        i = classify_intent("Python documentation for asyncio")
        assert i.route == "docs"

    def test_route_docs_ru(self):
        i = classify_intent("документация по FastAPI")
        assert i.route == "docs"

    def test_route_academic(self):
        i = classify_intent("arxiv paper on retrieval augmented generation")
        assert i.route == "academic"
        assert "arxiv" in (i.engines or "").lower()
        assert i.time_range == "year"

    def test_route_github(self):
        i = classify_intent("github repository for searxng source code")
        assert i.route == "github"
        assert "github" in (i.engines or "").lower()

    def test_route_security(self):
        i = classify_intent("CVE-2026-12345 vulnerability exploit details")
        assert i.route == "security"
        assert i.engines is not None
        # Security with narrow engines should trigger warning
        assert i.confidence >= 0.6

    def test_route_reviews(self):
        i = classify_intent("iPhone 17 Pro review отзывы")
        assert i.route == "reviews"
        # Should have query variants, not engine restriction
        assert len(i.query_variants) > 0
        assert any("reddit" in v for v in i.query_variants)

    def test_route_product(self):
        i = classify_intent("Flutter vs React Native comparison")
        assert i.route == "product"
        assert len(i.query_variants) > 0

    def test_route_general_fallback(self):
        """Query with no markers → general with low confidence."""
        i = classify_intent("обычный запрос без специальных маркеров")
        assert i.route == "general"
        assert i.confidence < 0.7
        assert i.routing_warning is True  # low conf = warn


# ====================================================================
# 2. should_warn_about_routing()
# ====================================================================


class TestRoutingWarnings:
    def test_low_confidence_warns(self):
        i = Intent(route="general", confidence=0.5)
        assert should_warn_about_routing(i) is True

    def test_high_confidence_no_warn(self):
        i = Intent(route="news", confidence=0.9, categories="news")
        assert should_warn_about_routing(i) is False

    def test_security_with_engines_warns(self):
        """Narrowing security search could miss context → warn."""
        i = Intent(
            route="security", confidence=0.85,
            engines="github,nvd,cve",
        )
        assert should_warn_about_routing(i) is True

    def test_tie_between_routes_warns(self):
        """Two routes with equal scores → user disambiguates."""
        i = Intent(
            route="forums", confidence=0.6,
            all_routes=[("forums", 1.0), ("reviews", 1.0)],
        )
        assert should_warn_about_routing(i) is True


# ====================================================================
# 3. adapt_query() integration
# ====================================================================


class TestAdaptQueryRouting:
    """The routing fields must appear in adapt_query() result."""

    def test_routing_fields_present(self):
        r = adapt_query("latest news on BPLA attacks")
        for field in (
            "inferred_route", "routing_confidence",
            "suggested_engines", "suggested_categories",
            "suggested_time_range", "query_variants",
            "all_routes", "routing_warning",
        ):
            assert field in r, f"missing field: {field}"

    def test_news_route_surfaced(self):
        r = adapt_query("breaking news today about inflation")
        assert r["inferred_route"] == "news"
        assert r["suggested_categories"] == "news"

    def test_general_route_low_confidence(self):
        r = adapt_query("обычный запрос")
        assert r["inferred_route"] == "general"
        # General with no signal → low confidence
        assert r["routing_confidence"] < 0.7
        assert r["routing_warning"] is True

    def test_short_passthrough_also_gets_routing(self):
        """Even short queries get routing recommendations."""
        r = adapt_query("arxiv RAG paper")
        assert r["adaptation_method"] == "passthrough"
        # Routing still runs
        assert r["inferred_route"] == "academic"


# ====================================================================
# 4. build_search_plan_preview() includes routing
# ====================================================================


class TestPreviewIncludesRouting:
    def test_preview_shows_route(self):
        r = adapt_query("latest news on BPLA attacks")
        r["raw_query"] = "latest news on BPLA attacks"
        preview = build_search_plan_preview(r)
        assert "inferred_route" in preview
        assert "news" in preview
        assert "advisory" in preview.lower()

    def test_preview_skips_routing_for_general(self):
        """When route is general, don't bloat the preview with routing noise."""
        r = adapt_query("обычный запрос")
        r["raw_query"] = "обычный запрос"
        preview = build_search_plan_preview(r)
        # General route is not surfaced (low signal)
        assert "inferred_route" not in preview

    def test_preview_shows_query_variants(self):
        r = adapt_query("iPhone 17 Pro review отзывы")
        r["raw_query"] = "iPhone 17 Pro review отзывы"
        preview = build_search_plan_preview(r)
        assert "query_variants" in preview
        # Should mention reddit
        assert "reddit" in preview


# ====================================================================
# 5. Recency detection
# ====================================================================


class TestRecencyDetection:
    def test_today_implies_day(self):
        i = classify_intent("what happened today in Moscow")
        assert i.time_range == "day"

    def test_yesterday_implies_day(self):
        i = classify_intent("новости вчера про Python")
        assert i.time_range == "day"

    def test_2026_implies_year(self):
        i = classify_intent("trends in AI 2026")
        assert i.time_range == "year"

    def test_no_recency_words(self):
        i = classify_intent("Python documentation")
        assert i.time_range is None

    def test_recency_overrides_route_default(self):
        """If explicit recency words exist, use them over route default."""
        i = classify_intent("arxiv paper 2026")
        # academic default is year, but 2026 → year, so consistent
        assert i.time_range == "year"


# ====================================================================
# 6. Adversarial
# ====================================================================


class TestAdversarialRouting:
    """Edge cases that might break the classifier."""

    def test_empty_query(self):
        i = classify_intent("")
        assert i.route == "general"
        assert i.confidence == 0.0

    def test_whitespace_only(self):
        i = classify_intent("   \n\t  ")
        assert i.route == "general"

    def test_multi_route_query(self):
        """A query that has markers for multiple routes.

        Result: one route wins, all_routes contains all candidates.
        """
        i = classify_intent("reddit discussion about Python documentation")
        # 'docs' or 'forums' could win; either is OK as long as all_routes is populated
        assert i.route in ("forums", "docs")
        assert len(i.all_routes) >= 1

    def test_security_with_news_words(self):
        """Security query with 'news' word shouldn't route to news."""
        i = classify_intent("CVE-2026-12345 vulnerability news")
        # 'security' has 3 hits (CVE, vulnerability, exploit), 'news' has 1
        # Classifier order: security first
        assert i.route == "security"

    def test_academic_with_study_word(self):
        """'study' is a weak academic signal. Should not over-route."""
        i = classify_intent("I want to study Spanish")
        # 'study' alone is not academic
        # Could be general or academic with low conf
        if i.route == "academic":
            assert i.routing_warning is True  # low conf

    def test_github_words_dont_override_forum_intent(self):
        """A query that's clearly about forums shouldn't route to github."""
        i = classify_intent("discussion on reddit about open source repos")
        # forums has 2 hits (discussion, reddit), github has 1 (repos)
        assert i.route in ("forums", "github")
        if i.route == "github":
            # If it does, it should warn
            assert i.routing_warning is True

    def test_reviews_route_skips_engine_restriction(self):
        """Reviews don't have a single engine — should use query variants."""
        i = classify_intent("отзывы о новом iPhone")
        assert i.route == "reviews"
        # No engine restriction
        assert i.engines is None
        # But has variants
        assert len(i.query_variants) > 0

    def test_huge_mixed_query(self):
        """Long query with many markers: highest count wins.

        This is an intentionally messy real-world query. We assert
        the system produces a valid route, with multiple candidates in
        all_routes (so the user can see the options).
        """
        q = (
            "github repository source code for arxiv paper on Python "
            "documentation with security vulnerability CVE-2026-12345"
        )
        i = classify_intent(q)
        # Multiple routes score → all_routes has >= 2 entries
        assert len(i.all_routes) >= 2, (
            f"Expected multi-route, got {i.all_routes}"
        )
        # Top route should be one of the high-scorers
        assert i.route in ("github", "security", "academic", "docs")
        # Confidence scales with top score
        assert i.confidence >= 0.75
