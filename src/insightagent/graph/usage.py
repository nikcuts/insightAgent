"""Provider-reported token usage aggregation for graph state."""

from __future__ import annotations

from collections.abc import Mapping

from langchain_core.messages import AIMessage


_INPUT_KEYS = ("input_tokens", "prompt_tokens", "prompt_token_count", "input_token_count")
_OUTPUT_KEYS = (
    "output_tokens",
    "completion_tokens",
    "completion_token_count",
    "output_token_count",
)
_TOTAL_KEYS = ("total_tokens", "total_token_count")


class UsageAccumulator:
    """Accumulate only token counts reported by LangChain or the provider."""

    def __init__(self, current: Mapping[str, object] | None = None) -> None:
        source = current or {}
        self._totals = {
            key: _token_count(source.get(key))
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }

    def record(self, message: AIMessage) -> None:
        """Add one response without falling back to character-based estimates."""
        reported = _reported_usage(message)
        for key, value in reported.items():
            self._totals[key] += value

    def snapshot(self) -> dict[str, int]:
        return dict(self._totals)


def _reported_usage(message: AIMessage) -> dict[str, int]:
    metadata = _mapping(message.usage_metadata)
    response_metadata = _mapping(message.response_metadata)
    sources = (
        metadata,
        _mapping(response_metadata.get("token_usage")),
        _mapping(response_metadata.get("usage")),
        response_metadata,
    )
    input_tokens = _first_token_count(sources, _INPUT_KEYS)
    output_tokens = _first_token_count(sources, _OUTPUT_KEYS)
    total_tokens = _first_token_count(sources, _TOTAL_KEYS)
    if total_tokens == 0 and (input_tokens or output_tokens):
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _first_token_count(sources: tuple[Mapping[str, object], ...], keys: tuple[str, ...]) -> int:
    for source in sources:
        for key in keys:
            if key in source:
                return _token_count(source[key])
    return 0


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _token_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


__all__ = ["UsageAccumulator"]
