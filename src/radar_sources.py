"""Source-native discovery signals for the LLM Release Radar.

Search results are useful for announcements, but model hubs expose stronger
machine-readable signals. This module converts Hugging Face model metadata
into *candidates*. It deliberately does not call a fresh repository a
confirmed release: release dates still require primary-source verification.
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

_BASE_RELATIONS = {
    "adapter",
    "finetune",
    "merge",
    "quantized",
}
HF_MODELS_ENDPOINT = "https://huggingface.co/api/models"
HF_USER_AGENT = "searxng-deep-research-radar/0.1"
DEFAULT_PIPELINE_TAGS = (
    "text-generation",
    "text-to-text-generation",
    "image-text-to-text",
)
DEFAULT_SORTS = ("createdAt", "lastModified")


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _card_data(payload: dict[str, Any]) -> dict[str, Any]:
    card = payload.get("cardData")
    return card if isinstance(card, dict) else {}


def _first_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _base_model_from_payload(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    card = _card_data(payload)
    base_model = _first_string(card.get("base_model"))
    relation = _first_string(card.get("base_model_relation"))

    raw_tags = payload.get("tags")
    tags = raw_tags if isinstance(raw_tags, list) else []
    for raw_tag in tags:
        if not isinstance(raw_tag, str) or not raw_tag.startswith("base_model:"):
            continue
        suffix = raw_tag.removeprefix("base_model:")
        prefix, separator, remainder = suffix.partition(":")
        if separator and prefix in _BASE_RELATIONS and remainder:
            relation = relation or prefix
            base_model = base_model or remainder
        elif suffix:
            base_model = base_model or suffix

    return base_model, relation


@dataclass(frozen=True)
class HuggingFaceModelSignal:
    """One model-repository event observed through the Hugging Face API."""

    repo_id: str
    created_at: datetime
    last_modified: datetime
    pipeline_tag: str | None
    library_name: str | None
    base_model: str | None
    relation: str | None
    downloads: int
    likes: int
    gated: bool | str

    @property
    def is_derivative(self) -> bool:
        return bool(self.base_model and self.base_model.casefold() != self.repo_id.casefold())

    @property
    def family_id(self) -> str:
        return self.base_model or self.repo_id

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{quote(self.repo_id, safe='/')}"

    @property
    def api_url(self) -> str:
        return f"https://huggingface.co/api/models/{quote(self.repo_id, safe='/')}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at.isoformat().replace("+00:00", "Z")
        payload["last_modified"] = self.last_modified.isoformat().replace("+00:00", "Z")
        payload["is_derivative"] = self.is_derivative
        payload["family_id"] = self.family_id
        payload["url"] = self.url
        payload["api_url"] = self.api_url
        return payload


def parse_huggingface_model(payload: dict[str, Any]) -> HuggingFaceModelSignal | None:
    """Parse one Hub API object, rejecting incomplete or malformed records."""
    repo_id = payload.get("id")
    created_at = _parse_datetime(payload.get("createdAt"))
    last_modified = _parse_datetime(payload.get("lastModified"))
    if not isinstance(repo_id, str) or "/" not in repo_id or not created_at or not last_modified:
        return None

    base_model, relation = _base_model_from_payload(payload)
    pipeline_tag = payload.get("pipeline_tag")
    library_name = payload.get("library_name")
    downloads = payload.get("downloads")
    likes = payload.get("likes")
    gated = payload.get("gated", False)
    return HuggingFaceModelSignal(
        repo_id=repo_id,
        created_at=created_at,
        last_modified=last_modified,
        pipeline_tag=pipeline_tag if isinstance(pipeline_tag, str) else None,
        library_name=library_name if isinstance(library_name, str) else None,
        base_model=base_model,
        relation=relation,
        downloads=downloads if type(downloads) is int and downloads >= 0 else 0,
        likes=likes if type(likes) is int and likes >= 0 else 0,
        gated=gated if isinstance(gated, (bool, str)) else False,
    )


@dataclass(frozen=True)
class HuggingFaceCandidate:
    """A family-level lead that still requires primary-source verification."""

    family_id: str
    state_hint: str
    reasons: tuple[str, ...]
    score: float
    new_repositories: int
    updated_repositories: int
    derivative_repositories: int
    first_signal_at: datetime
    last_signal_at: datetime
    model_url: str
    model_api_url: str
    signals: tuple[HuggingFaceModelSignal, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "family_id": self.family_id,
            "state_hint": self.state_hint,
            "reasons": list(self.reasons),
            "score": self.score,
            "new_repositories": self.new_repositories,
            "updated_repositories": self.updated_repositories,
            "derivative_repositories": self.derivative_repositories,
            "first_signal_at": self.first_signal_at.isoformat().replace("+00:00", "Z"),
            "last_signal_at": self.last_signal_at.isoformat().replace("+00:00", "Z"),
            "model_url": self.model_url,
            "model_api_url": self.model_api_url,
            "requires_primary_verification": True,
            "signals": [signal.to_dict() for signal in self.signals],
        }


def _candidate_state(
    *,
    new_root: int,
    new_derivatives: int,
    updated: int,
) -> tuple[str, tuple[str, ...], float]:
    if new_derivatives >= 2:
        return (
            "derivative_burst",
            ("multiple_derivatives_created_in_window",),
            0.8,
        )
    if new_root:
        return (
            "new_root_repository",
            ("root_repo_created_in_window",),
            0.7,
        )
    if new_derivatives:
        return (
            "derivative_signal",
            ("derivative_created_in_window",),
            0.5,
        )
    if updated:
        return (
            "update_only",
            ("modified_without_new_root",),
            0.2,
        )
    return ("outside_window", (), 0.0)


def build_huggingface_candidates(
    signals: list[HuggingFaceModelSignal],
    *,
    since: datetime,
    until: datetime | None = None,
) -> list[HuggingFaceCandidate]:
    """Cluster Hub signals by base-model family and label, never confirm."""
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    since = since.astimezone(UTC)
    if until is None:
        until = datetime.max.replace(tzinfo=UTC)
    elif until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    until = until.astimezone(UTC)
    if until < since:
        raise ValueError("until must not be earlier than since")

    grouped: dict[str, list[HuggingFaceModelSignal]] = {}
    for signal in signals:
        created_in_window = since <= signal.created_at <= until
        modified_in_window = since <= signal.last_modified <= until
        if not created_in_window and not modified_in_window:
            continue
        grouped.setdefault(signal.family_id, []).append(signal)

    candidates: list[HuggingFaceCandidate] = []
    for family_id, family_signals in grouped.items():
        ordered = sorted(family_signals, key=lambda signal: (signal.created_at, signal.repo_id))
        new_signals = [signal for signal in ordered if since <= signal.created_at <= until]
        new_root = sum(not signal.is_derivative for signal in new_signals)
        new_derivatives = sum(signal.is_derivative for signal in new_signals)
        updated = sum(signal.created_at < since <= signal.last_modified <= until for signal in ordered)
        state_hint, reasons, base_score = _candidate_state(
            new_root=new_root,
            new_derivatives=new_derivatives,
            updated=updated,
        )
        if state_hint == "outside_window":
            continue

        engagement = (
            sum(signal.likes for signal in ordered)
            + min(
                1000,
                sum(signal.downloads for signal in ordered),
            )
            / 100
        )
        score = round(min(1.0, base_score + min(0.15, engagement / 1000)), 4)
        encoded_family = quote(family_id, safe="/")
        event_times = [
            event_time
            for signal in ordered
            for event_time in (signal.created_at, signal.last_modified)
            if since <= event_time <= until
        ]
        candidates.append(
            HuggingFaceCandidate(
                family_id=family_id,
                state_hint=state_hint,
                reasons=reasons,
                score=score,
                new_repositories=len(new_signals),
                updated_repositories=updated,
                derivative_repositories=new_derivatives,
                first_signal_at=min(event_times),
                last_signal_at=max(event_times),
                model_url=f"https://huggingface.co/{encoded_family}",
                model_api_url=f"https://huggingface.co/api/models/{encoded_family}",
                signals=tuple(ordered),
            )
        )

    return sorted(candidates, key=lambda candidate: (-candidate.score, candidate.family_id.casefold()))


@dataclass(frozen=True)
class HuggingFaceDiscoveryReport:
    """Bounded Hub discovery run with explicit channel failures and truncation."""

    since: datetime
    checked_at: datetime
    fetched_records: int
    signals: tuple[HuggingFaceModelSignal, ...]
    candidates: tuple[HuggingFaceCandidate, ...]
    channels: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, str], ...]

    @property
    def unique_signals(self) -> int:
        return len(self.signals)

    def to_dict(self) -> dict[str, Any]:
        failed_channels = {error["channel"] for error in self.errors}

        def window_complete(sort: str) -> bool:
            relevant = [channel for channel in self.channels if channel["sort"] == sort]
            return bool(relevant) and all(
                not channel["truncated"] and channel["channel"] not in failed_channels for channel in relevant
            )

        return {
            "source": "huggingface",
            "since": self.since.isoformat().replace("+00:00", "Z"),
            "checked_at": self.checked_at.isoformat().replace("+00:00", "Z"),
            "fetched_records": self.fetched_records,
            "unique_signals": self.unique_signals,
            "channels": list(self.channels),
            "errors": list(self.errors),
            "truncated": any(bool(channel["truncated"]) for channel in self.channels),
            "creation_window_complete": window_complete("createdAt"),
            "modification_window_complete": window_complete("lastModified"),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def format_discovery_report(
    report: HuggingFaceDiscoveryReport,
    *,
    top: int,
) -> dict[str, Any]:
    """Bound user-facing output without hiding source-channel truncation."""
    if not 1 <= top <= 500:
        raise ValueError("top must be between 1 and 500")
    payload = report.to_dict()
    candidates = payload["candidates"]
    payload["candidate_count"] = len(candidates)
    payload["candidates"] = candidates[:top]
    payload["output_truncated"] = len(candidates) > top
    return payload


_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _next_page_url(headers: Any) -> str | None:
    raw_link = headers.get("Link") if hasattr(headers, "get") else None
    if not isinstance(raw_link, str):
        return None
    match = _NEXT_LINK_RE.search(raw_link)
    return match.group(1) if match else None


def _is_safe_huggingface_page(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "huggingface.co"
        and parsed.port is None
        and parsed.path == "/api/models"
    )


def _reached_window_boundary(
    payloads: list[dict[str, Any]],
    *,
    sort: str,
    since: datetime,
) -> bool:
    field = "createdAt" if sort == "createdAt" else "lastModified"
    timestamps = [
        parsed for payload in payloads if (parsed := _parse_datetime(payload.get(field))) is not None
    ]
    return bool(timestamps and min(timestamps) < since)


def discover_huggingface(
    *,
    since: datetime,
    checked_at: datetime | None = None,
    pipeline_tags: tuple[str, ...] = DEFAULT_PIPELINE_TAGS,
    sorts: tuple[str, ...] = DEFAULT_SORTS,
    limit_per_channel: int = 100,
    max_pages_per_channel: int = 5,
    timeout: float = 15.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> HuggingFaceDiscoveryReport:
    """Collect bounded, unauthenticated Hub signals from fixed API channels."""
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    since = since.astimezone(UTC)
    checked_at = (checked_at or datetime.now(UTC)).astimezone(UTC)
    if not 1 <= limit_per_channel <= 500:
        raise ValueError("limit_per_channel must be between 1 and 500")
    if not 1 <= max_pages_per_channel <= 50:
        raise ValueError("max_pages_per_channel must be between 1 and 50")
    if not 0 < timeout <= 60:
        raise ValueError("timeout must be greater than 0 and at most 60")
    if not pipeline_tags or any(not tag or len(tag) > 80 for tag in pipeline_tags):
        raise ValueError("pipeline_tags must contain non-empty bounded strings")
    if any(sort not in DEFAULT_SORTS for sort in sorts):
        raise ValueError("unsupported Hugging Face sort")

    fetched_records = 0
    by_repo: dict[str, HuggingFaceModelSignal] = {}
    channels: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    context = ssl.create_default_context()

    for pipeline_tag in pipeline_tags:
        for sort in sorts:
            channel = f"{pipeline_tag}:{sort}"
            params = urllib.parse.urlencode(
                {
                    "pipeline_tag": pipeline_tag,
                    "sort": sort,
                    "direction": -1,
                    "limit": limit_per_channel,
                    "full": "true",
                }
            )
            request = urllib.request.Request(  # noqa: S310 - fixed HTTPS host
                f"{HF_MODELS_ENDPOINT}?{params}",
                headers={
                    "Accept": "application/json",
                    "User-Agent": HF_USER_AGENT,
                },
            )
            channel_records = 0
            pages = 0
            truncated = False
            next_url: str | None = request.full_url
            while next_url and pages < max_pages_per_channel:
                page_request = urllib.request.Request(  # noqa: S310 - validated fixed host
                    next_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": HF_USER_AGENT,
                    },
                )
                try:
                    with opener(
                        page_request,
                        timeout=timeout,
                        context=context,
                    ) as response:
                        decoded = json.loads(response.read())
                        candidate_next_url = _next_page_url(response.headers)
                except Exception as exc:
                    errors.append(
                        {
                            "channel": channel,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    break

                pages += 1
                payloads = (
                    [payload for payload in decoded if isinstance(payload, dict)]
                    if isinstance(decoded, list)
                    else []
                )
                fetched_records += len(payloads)
                channel_records += len(payloads)
                for payload in payloads:
                    signal = parse_huggingface_model(payload)
                    if signal is None:
                        continue
                    if not (
                        since <= signal.created_at <= checked_at
                        or since <= signal.last_modified <= checked_at
                    ):
                        continue
                    existing = by_repo.get(signal.repo_id)
                    if existing is None or signal.last_modified >= existing.last_modified:
                        by_repo[signal.repo_id] = signal

                if _reached_window_boundary(payloads, sort=sort, since=since):
                    next_url = None
                    break
                if not candidate_next_url:
                    next_url = None
                    break
                if not _is_safe_huggingface_page(candidate_next_url):
                    errors.append(
                        {
                            "channel": channel,
                            "error": "unsafe pagination URL",
                        }
                    )
                    truncated = True
                    next_url = None
                    break
                next_url = candidate_next_url

            if next_url and pages >= max_pages_per_channel:
                truncated = True
            channels.append(
                {
                    "channel": channel,
                    "pipeline_tag": pipeline_tag,
                    "sort": sort,
                    "records": channel_records,
                    "pages": pages,
                    "truncated": truncated,
                }
            )

    signals = tuple(sorted(by_repo.values(), key=lambda signal: signal.repo_id.casefold()))
    candidates = tuple(
        build_huggingface_candidates(
            list(signals),
            since=since,
            until=checked_at,
        )
    )
    return HuggingFaceDiscoveryReport(
        since=since,
        checked_at=checked_at,
        fetched_records=fetched_records,
        signals=signals,
        candidates=candidates,
        channels=tuple(channels),
        errors=tuple(errors),
    )
