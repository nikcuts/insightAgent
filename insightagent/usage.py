"""Usage and cost estimation for InsightAgent V5.0."""

from __future__ import annotations

from dataclasses import dataclass

from .messages import Message, ModelResponse


@dataclass
class UsageSample:
    input_chars: int
    output_chars: int
    input_tokens_est: int
    output_tokens_est: int
    model: str | None = None


@dataclass
class UsageTracker:
    samples: list[UsageSample]

    def __init__(self) -> None:
        self.samples = []

    def record_model_call(self, messages: list[Message], response: ModelResponse, model: str | None = None) -> UsageSample:
        input_chars = sum(len(message.content) for message in messages)
        output_chars = len(response.content) + sum(len(str(call.arguments)) for call in response.tool_calls)
        sample = UsageSample(
            input_chars=input_chars,
            output_chars=output_chars,
            input_tokens_est=estimate_tokens(input_chars),
            output_tokens_est=estimate_tokens(output_chars),
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
    def turns(self) -> int:
        return len(self.samples)

    def summary(self) -> str:
        return (
            f"turns={self.turns} "
            f"input_tokens_est={self.total_input_tokens_est} "
            f"output_tokens_est={self.total_output_tokens_est} "
            f"total_tokens_est={self.total_tokens_est}"
        )


def estimate_tokens(chars: int) -> int:
    return max(1, (chars + 3) // 4) if chars else 0
