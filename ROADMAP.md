# Roadmap to v1.0

## Product direction

`searxng-deep-research` is becoming a private, evidence-first research
gateway for AI agents. It is not intended to be only a thin SearXNG wrapper.

The first production vertical is **LLM Release Radar**:

- find newly released or materially updated LLMs;
- prefer primary sources such as model authors, Hugging Face, GitHub and papers;
- distinguish releases from rumours, reposts and minor documentation changes;
- merge duplicate announcements without hiding independent confirmation;
- preserve search and source provenance;
- produce evidence-backed reports with observable coverage, failures and latency.

The vertical is also the proving ground for reusable retrieval, ranking,
verification and agent-integration contracts. New domains are added only after
the shared contracts are measured and stable.

## Non-goals before v1.0

- replacing a general-purpose browser or crawler;
- promising exhaustive web coverage without a measured recall target;
- assigning quality bonuses from copied route labels alone;
- depending on a paid search provider for core operation;
- publishing benchmark claims that cannot be reproduced.

## Milestone 0 — Runtime truth and recovery

Goal: one canonical, reproducible and observable runtime on VPS-NL-1.

Deliverables:

1. Back up the current live SearXNG settings and deployment metadata without
   committing secrets.
2. Reconcile the live `/opt/searxng` deployment with the clean repository
   checkout; keep local secrets and proxy settings outside Git.
3. Enable the JSON search format required by the research pipeline while
   preserving loopback-only exposure.
4. Deploy the four already-tested v0.9 provenance/ranking commits.
5. Add a smoke/health command that reports:
   - HTTP and JSON endpoint status;
   - configured and responding engines;
   - CAPTCHA, 403, 429, timeout and empty-result counts;
   - checked Git revision and configuration fingerprint.
6. Document rollback and verify it before continuing.

Exit criteria:

- HTML and JSON probes return expected responses on loopback;
- the research entry point completes a no-LLM live smoke;
- live revision equals the approved repository revision;
- a failed engine is visible and does not fail the whole search;
- secrets and proxy credentials do not appear in Git or logs.

## Milestone 1 — LLM Release Radar baseline

Goal: establish a reproducible baseline before changing ranking.

Dataset:

- a frozen set of known releases and non-releases over representative 48-hour
  windows;
- large hosted models, open-weight models and local-size models;
- English and Russian queries;
- difficult cases: aliases, version suffixes, preview releases, quantizations,
  reposts and retrospective articles.

Primary-source channels:

- official vendor or laboratory announcements;
- Hugging Face model cards and organization activity;
- GitHub releases and repositories;
- arXiv or other paper identifiers;
- official RSS/Atom feeds where available.

Baseline metrics:

- release recall at 24 and 48 hours;
- primary-source rate at top K;
- duplicate-cluster precision;
- false release rate;
- median discovery lag;
- independent publisher count;
- citation precision and coverage;
- search/fetch error rate, latency and request budget.

Exit criteria:

- the dataset and replay fixtures run offline in CI;
- a separate scheduled live benchmark is clearly labelled non-deterministic;
- current v0.8.4/v0.9-C1 results are saved as the comparison baseline;
- every reported metric has a documented denominator.

## Milestone 2 — Retrieval contract

Goal: preserve what actually happened during retrieval.

Deliverables:

- typed `SearchHit`, `SearchProvenance` and fetched-document metadata;
- actual provider, engine, result position and timestamp;
- task role: `main`, `alternative`, `route_variant`, `falsification` or
  `gap_fill`;
- expected source kind such as `official`, `model_card`, `repository`, `paper`,
  `news`, `forum` or `review`;
- canonical URL and registrable-domain identity;
- deterministic merge rules for results found by multiple queries or engines.

Exit criteria:

- no observed search metadata is replaced by requested metadata;
- merged results retain every distinct retrieval path;
- legacy documents without provenance remain supported;
- contracts are covered by focused and end-to-end tests.

## Milestone 3 — Health-aware routing

Goal: routes change real retrieval behaviour and degrade gracefully.

Deliverables:

- validated engine/category/time-range allow-lists;
- route-specific source plans for LLM releases;
- per-engine circuit breaking for CAPTCHA, 403, 429 and repeated timeouts;
- fallback order: primary-source connectors, healthy SearXNG engines, optional
  external providers;
- explicit request, latency and provider-budget accounting.

Optional providers such as Exa, Tavily or Brave must be adapters. Core local
operation remains possible without their credentials.

Exit criteria:

- route execution is visible in provenance;
- failures are classified rather than silently converted to empty results;
- provider exhaustion has a user-facing explanation;
- deterministic tests cover fallback and recovery.

## Milestone 4 — Ranking and source independence

Goal: rank evidence quality instead of metadata self-consistency.

Signals may include:

- real per-query/per-engine position and independent query votes;
- primary-source and source-kind fit;
- freshness relative to the claimed release;
- route-specific authority;
- content relevance and extraction quality;
- independent publisher families;
- penalties for reposts, mirrors, aggregators and low-value pages.

The old proposed strict `task_route == target_route` bonus is intentionally not
implemented: every task in a plan currently inherits the same route, so the
bonus would be nearly constant and would not measure relevance.

Exit criteria:

- every signal has an ablation test against the Radar baseline;
- no new signal ships without improving an agreed metric or safety property;
- source independence uses registrable domains and publisher-family rules;
- scores expose their components for debugging.

## Milestone 5 — Verification and report quality

Goal: turn retrieved evidence into an honest release report.

Deliverables:

- explicit states for confirmed release, probable release, update-only,
  duplicate/repost, contradiction and insufficient evidence;
- span citations for claims that can be located in source text;
- separate analysis for large hosted models and locally runnable models;
- hardware-fit notes for RTX 3090, 4090 and 5090 based on documented model
  size, quantization and context assumptions;
- no fabricated certainty when primary sources are unavailable.

Exit criteria:

- report claims trace back to stored source evidence;
- conflicting dates and model aliases are surfaced;
- hardware recommendations state assumptions and uncertainty;
- user-facing output passes a curated human-review rubric.

## Milestone 6 — Agent product

Goal: make the pipeline a normal tool rather than source code an agent imports.

Deliverables:

- installed CLI: `searxng-research`;
- versioned JSON input/output schema;
- stable Python API;
- thin HTTP or MCP adapter;
- Hermes and OpenClaw integrations that call the stable interface;
- timeouts, request budgets, structured errors and health reporting.

Exit criteria:

- an agent can invoke the tool without `PYTHONPATH` or repository knowledge;
- the same request produces equivalent structured output through CLI and agent
  integration;
- service restarts and host reboots recover automatically;
- telemetry contains no secrets or fetched private data.

## Milestone 7 — GitHub showcase and v1.0

Deliverables:

- answer-first README with a 30-second quick start;
- architecture diagram and example Radar report;
- reproducible benchmark table with methodology;
- comparison of local-only and optional-provider modes;
- security/privacy model, contribution guide and public roadmap;
- tagged release, release notes and container/package artifacts;
- GitHub topics, issue milestones and a small demonstration workflow.

v1.0 definition of done:

- the live VPS and release artifacts use the same tagged source;
- the LLM Release Radar meets its agreed quality thresholds;
- CI contains offline regression/eval fixtures;
- scheduled live evaluation reports provider degradation;
- Hermes and OpenClaw use the public stable interface;
- documentation contains no internal-only paths or stale version claims.

## Expansion after the Radar

New domains are introduced as vertical packs containing:

- source taxonomy and primary-source registry;
- query and routing templates;
- authority and publisher-family policy;
- frozen evaluation cases;
- report schema and human-review rubric.

Candidate next domains should be selected by measurable value and source
availability rather than by adding general-purpose heuristics to the core.
