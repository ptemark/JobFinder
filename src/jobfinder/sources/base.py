"""Source protocol, result type, and the source registry (LLD §3.1).

Every provider adapter (Greenhouse, Lever, Ashby, Adzuna) implements the
:class:`Source` protocol and registers a factory under its name via
:func:`register_source` at import time. The pipeline (LLD §8) then asks
:func:`build_sources` to construct the enabled adapters from :class:`Settings`
and iterates them, wrapping each ``fetch`` in a per-source bulkhead.

Two enablement rules live at this layer:

* **Selection** — the CLI ``poll --source`` flag (LLD §10) narrows the run to a
  subset of registered names; ``build_sources`` honors that via ``only`` and
  fails fast on an unknown name.
* **Secrets** — an optional keyed source (Adzuna) is still *constructed* even
  when its secret is absent; its ``fetch`` returns an empty :class:`SourceResult`
  carrying a note rather than raising, so a missing key degrades cleanly
  (HLD §5.1, spec §3).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from jobfinder.models import RawPosting
    from jobfinder.settings import Settings


@dataclass
class SourceResult:
    """The outcome of one source's ``fetch`` (LLD §3.1).

    ``raw`` is already recency-filtered where the provider exposes a date before
    normalization; ``fetched`` is the provider's total and ``kept_after_recency``
    the count surviving that pre-filter, so the pipeline can log the
    ``fetched → kept_after_recency`` funnel (LLD §12). ``errors`` collects
    non-fatal notes (e.g. a skipped malformed posting, or a missing-secret skip).
    """

    source: str
    raw: list[RawPosting] = field(default_factory=list)
    fetched: int = 0
    kept_after_recency: int = 0
    errors: list[str] = field(default_factory=list)


@runtime_checkable
class Source(Protocol):
    """A provider adapter: fetch raw postings for one ATS/aggregator (LLD §3.1)."""

    name: str

    def fetch(self, *, max_age_days: int, throttle_s: float) -> SourceResult:
        """Fetch postings, dropping those older than ``max_age_days`` where the
        provider exposes a date pre-normalization. Never raises for an absent
        optional secret — returns an empty result with a note instead."""
        ...


# A factory builds a configured adapter from operational settings (LLD §3.1).
SourceFactory = Callable[["Settings"], "Source"]

# Name → factory. Populated by each adapter module at import time via
# register_source; consumed by build_sources. Concrete adapters land in
# T11 (Greenhouse), T12 (Lever), T21 (Ashby), T22 (Adzuna).
SOURCES: dict[str, SourceFactory] = {}


def register_source(name: str, factory: SourceFactory) -> None:
    """Register ``factory`` under ``name`` in the global :data:`SOURCES` registry.

    Adapters call this at import time. Re-registering the same name overwrites,
    so importing an adapter twice is harmless.
    """
    SOURCES[name] = factory


def build_sources(
    settings: Settings,
    *,
    only: Iterable[str] | None = None,
    registry: dict[str, SourceFactory] | None = None,
) -> list[Source]:
    """Construct the enabled source adapters from ``settings`` (LLD §3.1).

    Args:
        settings: operational settings passed to each factory.
        only: if given, restrict to these source names (the CLI ``--source``
            selection, LLD §10); an unknown name raises ``ValueError`` so a typo
            fails fast rather than silently running nothing.
        registry: the registry to build from; defaults to the global
            :data:`SOURCES`. Injectable for isolated tests.

    Returns:
        One constructed :class:`Source` per selected, registered name. Optional
        sources lacking their secret are still returned (they self-skip in
        ``fetch``); selection here is independent of secret presence.
    """
    reg = SOURCES if registry is None else registry
    if only is None:
        names = list(reg)
    else:
        only_set = set(only)
        unknown = only_set - reg.keys()
        if unknown:
            raise ValueError(f"unknown source(s): {sorted(unknown)}")
        names = [name for name in reg if name in only_set]
    return [reg[name](settings) for name in names]


__all__ = [
    "SOURCES",
    "Source",
    "SourceFactory",
    "SourceResult",
    "build_sources",
    "register_source",
]
