from __future__ import annotations

from langchain_core.messages import AIMessage

from insightagent.graph.usage import UsageAccumulator


def test_usage_accumulator_uses_langchain_usage_metadata() -> None:
    accumulator = UsageAccumulator({"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})

    accumulator.record(
        AIMessage(
            content="完成",
            usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        )
    )

    assert accumulator.snapshot() == {
        "input_tokens": 13,
        "output_tokens": 8,
        "total_tokens": 21,
    }


def test_usage_accumulator_uses_provider_response_metadata_when_needed() -> None:
    accumulator = UsageAccumulator()

    accumulator.record(
        AIMessage(
            content="完成",
            response_metadata={
                "token_usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
            },
        )
    )

    assert accumulator.snapshot() == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
    }


def test_usage_accumulator_records_zero_without_token_metadata() -> None:
    accumulator = UsageAccumulator()

    accumulator.record(AIMessage(content="不按字符数估算"))

    assert accumulator.snapshot() == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def test_graph_model_node_uses_the_shared_usage_accumulator() -> None:
    from insightagent.graph.nodes import _merge_usage

    usage = _merge_usage(
        {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        AIMessage(
            content="完成",
            response_metadata={
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
            },
        ),
    )

    assert usage == {"input_tokens": 12, "output_tokens": 9, "total_tokens": 21}
