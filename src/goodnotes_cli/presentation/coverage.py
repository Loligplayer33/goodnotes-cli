"""Format search coverage recommendations for callers."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..reader.coverage import CoverageResult


def format_search_coverage(result: CoverageResult) -> dict[str, Any]:
    recommendations = result["recommendations"]
    steps: list[str] = []
    for recommendation in recommendations:
        if recommendation.kind == "scan":
            assert (
                recommendation.scope_option is not None and recommendation.scope_value is not None
            )
            command = (
                f"goodnotes scan-pdfs 'QUERY' {recommendation.scope_option} "
                f"{shlex.quote(recommendation.scope_value)} --page-limit 200"
            )
            steps.append(
                f"To check copied PDF backgrounds in this scope, run `{command}`; "
                "replace `QUERY` with a phrase and continue with `--page-offset` if returned."
            )
        elif recommendation.kind == "refresh":
            steps.append("Refresh the read-only snapshot with `goodnotes sync`.")
        elif recommendation.kind == "inspect_pending":
            steps.append(
                "After Goodnotes finishes indexing, refresh the read-only snapshot with "
                "`goodnotes sync`."
            )
        elif recommendation.kind == "read":
            steps.append(
                "Use `goodnotes read` on selected pages to inspect stored text and its source."
            )
    formatted: dict[str, Any] = dict(result)
    del formatted["recommendations"]
    formatted["next_steps"] = list(dict.fromkeys(steps))[:5]
    return formatted
