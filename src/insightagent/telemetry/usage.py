"""Usage and cost estimation for InsightAgent V5.0.

When a provider reports real token usage we use it; otherwise we fall back to a
character-based estimate. The ``*_est`` field/property names are kept for
backward compatibility but now hold the best-known value (real when available).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..api.messages import Message, ModelResponse


# Approximate USD price per 1M tokens (input, output), matched by lowercased
# substring of the model name. These are illustrative defaults; override via
# UsageTracker(prices=...) for accurate billing.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-sonnet": (3.00, 15.00),
    "qwen2.5-72b": (0.40, 0.40),
    "qwen": (0.30, 0.30),
}


@dataclass
class UsageSample:
    input_chars: int
    output_chars: int
    input_tokens_est: int
    output_tokens_est: int
    is_estimated: bool = True
    cost_usd: float = 0.0
    model: str | None = None

    @property
    def total_tokens_est(self) -> int:
        return self.input_tokens_est + self.output_tokens_est


@dataclass
class UsageTracker:
    samples: list[UsageSample] = field(default_factory=list)
    prices: dict[str, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_PRICES))

    def record_model_call(
        self, messages: list[Message], response: ModelResponse, model: str | None = None
    ) -> UsageSample:
        input_chars = sum(len(message.content) for message in messages)
        output_chars = len(response.content) + sum(len(str(call.arguments)) for call in response.tool_calls)
        if response.usage is not None:
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            is_estimated = False
        else:
            input_tokens = estimate_tokens(input_chars)
            output_tokens = estimate_tokens(output_chars)
            is_estimated = True
        sample = UsageSample(
            input_chars=input_chars,
            output_chars=output_chars,
            input_tokens_est=input_tokens,
            output_tokens_est=output_tokens,
            is_estimated=is_estimated,
            cost_usd=self._estimate_cost(model, input_tokens, output_tokens),
            model=model,
        )
        self.samples.append(sample)
        return sample

    @property
    def total_input_tokens_est(self) -> int:
        return sum(sample.input_tokens_est for sample in self.samples)

    @property
    def total_output_tokens_est(self) -> int:
        return sum(sample.output_tokens_est for sample in self.samples)

    @property
    def total_tokens_est(self) -> int:
        return self.total_input_tokens_est + self.total_output_tokens_est

    @property
    def total_cost_usd(self) -> float:
        return round(sum(sample.cost_usd for sample in self.samples), 6)

    @property
    def turns(self) -> int:
        return len(self.samples)

    @property
    def source(self) -> str:
        if not self.samples:
            return "none"
        estimated = sum(1 for sample in self.samples if sample.is_estimated)
        if estimated == 0:
            return "actual"
        if estimated == len(self.samples):
            return "estimated"
        return "mixed"

    def _estimate_cost(self, model: str | None, input_tokens: int, output_tokens: int) -> float:
        rates = self._rate_for_model(model)
        if rates is None:
            return 0.0
        input_rate, output_rate = rates
        return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000

    def _rate_for_model(self, model: str | None) -> tuple[float, float] | None:
        if not model:
            return None
        lowered = model.lower()
        for key, rates in self.prices.items():
            if key in lowered:
                return rates
        return None

    def summary(self) -> str:
        return (
            f"turns={self.turns} "
            f"input_tokens_est={self.total_input_tokens_est} "
            f"output_tokens_est={self.total_output_tokens_est} "
            f"total_tokens_est={self.total_tokens_est} "
            f"source={self.source} "
            f"cost_usd={self.total_cost_usd:.6f}"
        )


def estimate_tokens(chars: int) -> int:
    return max(1, (chars + 3) // 4) if chars else 0
