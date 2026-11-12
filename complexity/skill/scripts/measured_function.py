"""One function's complexity scores and the limits they are held to."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

DEFAULT_LIMITS = {"cognitive": 8, "cyclomatic": 16}
# NavHur-owned repositories get 6/18; every other owner (DCA work repos, PedroNavHur, the t3code fork) keeps 8/16.
# Cyclomatic 18 (6 x 3) keeps a long flat switch or match legal while cognitive 6 catches nesting.
OWNER_LIMITS = {"navhur": {"cognitive": 6, "cyclomatic": 18}}

# The coverage tag (and note) for a function the repository's lint could not be consulted for.
LINT_UNCHECKED = "[not lint-checked]"


def pedro_limits(owner: str | None) -> dict[str, int]:
    """Pedro's limits for a repository owned by `owner` (None: no GitHub origin)."""
    return dict(OWNER_LIMITS.get((owner or "").lower(), DEFAULT_LIMITS))


@dataclass
class Function:
    path: str
    name: str
    line: int
    end_line: int
    cognitive: int | None = None
    cyclomatic: int | None = None
    # metric -> (severity, repository max) for metrics the repository's own lint flags;
    # None when no repository lint was consulted.
    lint: dict[str, tuple[str, int | None]] | None = None
    limits: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LIMITS))

    def score(self, metric: str) -> int | None:
        return self.cognitive if metric == "cognitive" else self.cyclomatic

    def excess(self) -> dict[str, int]:
        """metric -> the limit it exceeds (the stricter of Pedro's and the repository's)."""
        over = {}
        for metric, limit in self.limits.items():
            score = self.score(metric)
            if score is None:
                continue
            threshold = self.exceeded_limit(metric, score, limit)
            if threshold is not None:
                over[metric] = threshold
        return over

    def exceeded_limit(self, metric: str, score: int, limit: int) -> int | None:
        flagged = (self.lint or {}).get(metric)
        if flagged and flagged[1] is not None:
            limit = min(limit, flagged[1])
        return limit if score > limit or flagged else None

    def over(self) -> list[str]:
        return [f"{metric} {self.score(metric)} > {limit}" for metric, limit in self.excess().items()]

    def lint_tag(self, metrics: Iterable[str] | None = None) -> str:
        """The lint tag of `metrics` (default: every metric over its limit)."""
        if self.lint is None:
            return f" {LINT_UNCHECKED}"
        severities = {self.lint[m][0] for m in (self.excess() if metrics is None else metrics) if m in self.lint}
        if "error" in severities:
            return " [lint: error]"
        return " [lint: warning]" if severities else " [lint passes]"


@dataclass
class Measurement:
    """The changed source files of a range and the functions measured in them."""
    by_language: dict[str, list[str]]
    functions: list[Function] = field(default_factory=list)
    changed: list[Function] = field(default_factory=list)
    unmeasured: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    analyzers: list[str] = field(default_factory=list)
