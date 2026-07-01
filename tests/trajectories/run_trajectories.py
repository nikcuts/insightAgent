"""Deterministic 12-task trajectory harness.

Runs 12 representative coding-agent tasks through the *real* CodeAgent + real
tools using scripted fake models that imitate common (often weak-model)
behaviours. No network or real LLM is used, so the run is fully reproducible.

For each task it records the full event trajectory, classifies the outcome
(done / failed_clean / iter_exhausted / stuck_in_plan), and writes:

  out/trajectories.jsonl   one JSON object per task (events + classification)
  out/trajectory_report.md human-readable success/failure report
  out/outcome_distribution.svg   zero-dependency donut chart
  out/outcome_distribution.png   optional PNG (only if Pillow is installed)

Usage:
    python3 -m tests.trajectories.run_trajectories
"""

from __future__ import annotations

import json
import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from insightagent.agent.core import CodeAgent
from insightagent.agent.task_state import TaskState
from insightagent.api.messages import ModelResponse, ToolCall
from insightagent.api.resilience import RetryPolicy
from insightagent.runtime.tool_context import ToolContext
from insightagent.tools import ToolRegistry, default_tools

MAX_ITER_WARNING = "Stopped after reaching the V5.0 max tool-iteration limit."

OUTCOME_LABELS = {
    "done": "正常完成 (done)",
    "failed_clean": "合理失败并解释 (failed, explained)",
    "iter_exhausted": "迭代耗尽 (iteration exhausted)",
    "stuck_in_plan": "卡在 plan 阶段 (stuck in plan)",
}
OUTCOME_COLORS = {
    "done": "#54B89A",
    "failed_clean": "#C75D5D",
    "iter_exhausted": "#E0A33E",
    "stuck_in_plan": "#7E6BD6",
}

CALC = "def calculate(a, op, b):\n    return {'+': a + b, '-': a - b, '*': a * b, '/': a / b}[op]\nif __name__ == '__main__':\n    print(calculate(2, '+', 3))\n"
NET_CMD = "python3 -c \"import sys; sys.stderr.write('Could not resolve host: registry.test\\n'); sys.exit(6)\""


class ScriptedModel:
    model = "scripted"

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)

    def complete(self, messages, tools, tool_choice=None):  # noqa: ANN001
        if self._responses:
            return self._responses.pop(0)
        return ModelResponse(content="(scripted model exhausted; finishing)", tool_calls=[])


@dataclass
class Scenario:
    id: str
    title: str
    task: str
    responses: list[ModelResponse]
    weakness: str
    permission_mode: str = "workspace-write"
    setup: Callable[[Path], None] | None = None
    max_tool_iterations: int = 10


@dataclass
class TrajectoryResult:
    id: str
    title: str
    weakness: str
    outcome: str
    iterations: int
    final: str
    reason: str
    tools_used: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    notable: list[str] = field(default_factory=list)


def _tc(name: str, args: dict[str, Any], call_id: str = "c") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args)


def build_scenarios() -> list[Scenario]:
    def write_notes(ws: Path) -> None:
        (ws / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    return [
        Scenario(
            id="t01_native_calculator",
            title="计算器（原生 tool_calls，能力达标的模型）",
            task="创建 calculator.py 并运行验证。",
            weakness="无（基线：模型正常发出 tool_calls）",
            responses=[
                ModelResponse(content="Plan: write calculator.py then run it.", tool_calls=[_tc("write_file", {"path": "calculator.py", "content": CALC}, "w1")]),
                ModelResponse(content="Run it.", tool_calls=[_tc("execute_command", {"command": "python3 calculator.py"}, "r1")]),
                ModelResponse(content="Done: calculator.py created, prints 5."),
            ],
        ),
        Scenario(
            id="t02_codeblock_only",
            title="计算器（弱模型只会输出代码块，不发 tool_calls）",
            task="写一个计算器 calculator.py 并运行。",
            weakness="弱模型把代码塞进 markdown 代码块，不发原生 tool_calls",
            responses=[
                ModelResponse(content=f"Here is calculator.py:\n```python\n{CALC}```"),
                ModelResponse(content='<tool_call>{"name": "execute_command", "arguments": {"command": "python3 calculator.py"}}</tool_call>'),
                ModelResponse(content="Done: recovered from code block, verified output."),
            ],
        ),
        Scenario(
            id="t03_text_protocol",
            title="文件创建（弱模型用文本协议 <tool_call> 调用）",
            task="创建 greeting.py，打印 hello。",
            weakness="模型不支持原生 function-calling，只能走文本协议",
            responses=[
                ModelResponse(content='<tool_call>{"name": "write_file", "arguments": {"path": "greeting.py", "content": "print(\'hello\')\\n"}}</tool_call>'),
                ModelResponse(content='<tool_call>{"name": "execute_command", "arguments": {"command": "python3 greeting.py"}}</tool_call>'),
                ModelResponse(content="Done via text protocol."),
            ],
        ),
        Scenario(
            id="t04_malformed_json",
            title="参数 JSON 有尾随逗号（宽容解析修复）",
            task="创建 data.py。",
            weakness="弱模型产出的 JSON 参数有尾随逗号等小错误",
            responses=[
                ModelResponse(content='<tool_call>{"name": "write_file", "arguments": {"path": "data.py", "content": "print(42)\\n",}}</tool_call>'),
                ModelResponse(content='<tool_call>{"name": "execute_command", "arguments": {"command": "python3 data.py"}}</tool_call>'),
                ModelResponse(content="Done: lenient JSON repair handled the trailing comma."),
            ],
        ),
        Scenario(
            id="t05_self_heal",
            title="自修复：先写出 bug，验证失败后修好再验证",
            task="创建 divide.py 并验证可运行。",
            weakness="首版代码有 NameError，需要按诊断修复",
            responses=[
                ModelResponse(content="plan", tool_calls=[_tc("write_file", {"path": "divide.py", "content": "print(value)\n"}, "w1")]),
                ModelResponse(content="run", tool_calls=[_tc("execute_command", {"command": "python3 divide.py"}, "r1")]),
                ModelResponse(content="fix it", tool_calls=[_tc("edit_file", {"path": "divide.py", "old": "print(value)", "new": "value = 10\nprint(value)"}, "e1")]),
                ModelResponse(content="re-run", tool_calls=[_tc("execute_command", {"command": "python3 divide.py"}, "r2")]),
                ModelResponse(content="Done: fixed NameError and verified output 10."),
            ],
        ),
        Scenario(
            id="t06_forced_tool_choice",
            title="升级式修复：模型空谈两轮后被强制工具调用",
            task="写 calc.py。",
            weakness="模型反复用散文空谈，不肯行动",
            responses=[
                ModelResponse(content="I am thinking about the architecture of the calculator."),
                ModelResponse(content="Still considering the best approach, no code yet."),
                ModelResponse(content="ok", tool_calls=[_tc("write_file", {"path": "calc.py", "content": CALC}, "w1")]),
                ModelResponse(content="run", tool_calls=[_tc("execute_command", {"command": "python3 calc.py"}, "r1")]),
                ModelResponse(content="Done after escalation forced a tool call."),
            ],
        ),
        Scenario(
            id="t07_dedup_readonly",
            title="读取去重：重复 read_file 被合成消息拦截",
            task="阅读 notes.txt 并总结。",
            weakness="模型反复读取同一个文件，浪费迭代",
            setup=write_notes,
            responses=[
                ModelResponse(content="read", tool_calls=[_tc("read_file", {"path": "notes.txt"}, "r1")]),
                ModelResponse(content="read again", tool_calls=[_tc("read_file", {"path": "notes.txt"}, "r2")]),
                ModelResponse(content="read once more", tool_calls=[_tc("read_file", {"path": "notes.txt"}, "r3")]),
                ModelResponse(content="Done: notes.txt has alpha and beta."),
            ],
        ),
        Scenario(
            id="t08_multifile",
            title="多文件任务：写模块 + 测试并运行",
            task="创建 mathutil.py 和 use.py 并运行 use.py。",
            weakness="无（多步骤组合任务）",
            responses=[
                ModelResponse(content="plan", tool_calls=[_tc("write_file", {"path": "mathutil.py", "content": "def add(a, b):\n    return a + b\n"}, "w1")]),
                ModelResponse(content="second file", tool_calls=[_tc("write_file", {"path": "use.py", "content": "from mathutil import add\nprint(add(2, 3))\n"}, "w2")]),
                ModelResponse(content="run", tool_calls=[_tc("execute_command", {"command": "python3 use.py"}, "r1")]),
                ModelResponse(content="Done: prints 5 from two modules."),
            ],
        ),
        Scenario(
            id="t09_network_down",
            title="网络不可用：退避后给出明确结论（合理失败）",
            task="从远程 registry 拉取并安装一个包。",
            weakness="环境网络不通；旧版会盲目重试到迭代耗尽",
            responses=[
                ModelResponse(content="fetch", tool_calls=[_tc("execute_command", {"command": NET_CMD}, "n1")]),
                ModelResponse(content="retry", tool_calls=[_tc("execute_command", {"command": NET_CMD}, "n2")]),
                ModelResponse(content="retry once more", tool_calls=[_tc("execute_command", {"command": NET_CMD}, "n3")]),
                ModelResponse(content="This is a network failure (could not resolve host). Retrying is futile; the environment has no network access, so the package cannot be fetched."),
            ],
        ),
        Scenario(
            id="t10_mcp_missing",
            title="MCP 工具不可用：诊断提示指向配置（合理失败）",
            task="用 mcp_playwright 打开网页并截图。",
            weakness="调用 mcp_ 工具但 MCP server 未启动",
            responses=[
                ModelResponse(content="navigate", tool_calls=[_tc("mcp_playwright_navigate", {"url": "https://example.com"}, "m1")]),
                ModelResponse(content="retry navigate", tool_calls=[_tc("mcp_playwright_navigate", {"url": "https://example.com"}, "m2")]),
                ModelResponse(content="retry again", tool_calls=[_tc("mcp_playwright_navigate", {"url": "https://example.com"}, "m3")]),
                ModelResponse(content="The mcp_playwright tool is unavailable. Per the diagnostic, the MCP server is not running / not configured (check mcp_config.json). I will not install dependencies; this task needs MCP setup first."),
            ],
        ),
        Scenario(
            id="t11_unfixable",
            title="无法在限度内修复：耗尽 repair 次数后失败收尾",
            task="让 broken.py 通过验证。",
            weakness="持续性错误，超过最大修复次数",
            responses=[
                ModelResponse(content="plan", tool_calls=[_tc("write_file", {"path": "broken.py", "content": "raise RuntimeError('always')\n"}, "w1")]),
                ModelResponse(content="run", tool_calls=[_tc("execute_command", {"command": "python3 broken.py # 1"}, "r1")]),
                ModelResponse(content="run", tool_calls=[_tc("execute_command", {"command": "python3 broken.py # 2"}, "r2")]),
                ModelResponse(content="run", tool_calls=[_tc("execute_command", {"command": "python3 broken.py # 3"}, "r3")]),
                ModelResponse(content="I cannot fix this within the repair limit: broken.py raises RuntimeError unconditionally by design."),
            ],
        ),
        Scenario(
            id="t12_hopeless_prose",
            title="彻底空谈的模型：框架优雅终止（卡在 plan）",
            task="写一个排序函数。",
            weakness="模型只会输出散文，连文本协议都不给——框架必须能优雅停下",
            responses=[
                ModelResponse(content="Sorting is an interesting topic with many algorithms."),
                ModelResponse(content="One could use quicksort or mergesort depending on constraints."),
                ModelResponse(content="In conclusion, sorting matters. (still no tool call)"),
            ],
        ),
    ]


def classify(result, task_state_phase: str, events: list[dict[str, Any]]) -> tuple[str, str]:
    tools_used = any(e["type"] in {"tool_call", "tool_call_recovered"} for e in events)
    if result.content == MAX_ITER_WARNING:
        return "iter_exhausted", "达到最大迭代上限仍未收敛"
    if task_state_phase == "failed":
        return "failed_clean", "进入 FAILED 后用文字明确解释失败原因（未死锁、未耗尽迭代）"
    if not tools_used:
        return "stuck_in_plan", "模型始终未发出可执行动作；框架在 nudge 上限后优雅终止"
    return "done", "任务完成并通过验证"


def run_scenarios(resilience_enabled: bool = True) -> tuple[list[TrajectoryResult], list[str]]:
    """Execute all scenarios; return (results, jsonl_lines) without writing files.

    When ``resilience_enabled`` is False the agent and tool registry run in the
    pre-fix "legacy" mode, used to produce the before/after control group.
    """

    policy = RetryPolicy(base_delay=0.0, max_attempts=2, sleep=lambda _s: None)
    results: list[TrajectoryResult] = []
    jsonl_lines: list[str] = []

    for scenario in build_scenarios():
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            if scenario.setup:
                scenario.setup(workspace)
            context = ToolContext(workspace=workspace, permission_mode=scenario.permission_mode)
            tools = ToolRegistry(
                tools=default_tools(context),
                context=context,
                retry_policy=policy,
                resilience_enabled=resilience_enabled,
            )
            agent = CodeAgent(
                ScriptedModel(scenario.responses),
                tools=tools,
                system_prompt="trajectory-harness",
                require_tool_use=True,
                max_tool_iterations=scenario.max_tool_iterations,
                resilience_enabled=resilience_enabled,
                task_state=TaskState(max_repairs=3) if scenario.id == "t11_unfixable" else None,
            )
            events: list[dict[str, Any]] = []
            result = agent.run_turn_with_trace(scenario.task, trace=events.append)
            phase = agent.task_state.phase.value

        outcome, reason = classify(result, phase, events)
        tools_used = [e["name"] for e in events if e["type"] == "tool_call"]
        recovered = [f"{e['name']}({e['source']})" for e in events if e["type"] == "tool_call_recovered"]
        notable = []
        if any(e["type"] == "tool_call_suppressed" or (e["type"] == "tool_result" and e.get("suppressed")) for e in events):
            notable.append("去重/抑制 生效")
        if any(e["type"] == "self_healing_repair" for e in events):
            notable.append("自修复 触发")
        if any(e["type"] == "tool_use_required" and e.get("forced_tool") for e in events):
            notable.append("强制 tool_choice 升级")
        if any(e["type"] == "self_healing_repair" and e.get("backoff_delay") for e in events):
            notable.append("退避等待")

        traj = TrajectoryResult(
            id=scenario.id,
            title=scenario.title,
            weakness=scenario.weakness,
            outcome=outcome,
            iterations=result.iterations,
            final=result.content,
            reason=reason,
            tools_used=tools_used,
            recovered=recovered,
            notable=notable,
        )
        results.append(traj)
        jsonl_lines.append(json.dumps({
            "id": scenario.id,
            "title": scenario.title,
            "weakness": scenario.weakness,
            "task": scenario.task,
            "outcome": outcome,
            "reason": reason,
            "iterations": result.iterations,
            "final": result.content,
            "tools_used": tools_used,
            "recovered": recovered,
            "notable": notable,
            "events": events,
        }, ensure_ascii=False))

    return results, jsonl_lines


def run() -> dict[str, list[TrajectoryResult]]:
    out_dir = Path(__file__).resolve().parent / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    resilient, resilient_jsonl = run_scenarios(resilience_enabled=True)
    legacy, legacy_jsonl = run_scenarios(resilience_enabled=False)

    (out_dir / "trajectories.jsonl").write_text("\n".join(resilient_jsonl) + "\n", encoding="utf-8")
    (out_dir / "trajectories_legacy.jsonl").write_text("\n".join(legacy_jsonl) + "\n", encoding="utf-8")

    (out_dir / "trajectory_report.md").write_text(render_report(resilient), encoding="utf-8")
    (out_dir / "trajectory_comparison.md").write_text(render_comparison(legacy, resilient), encoding="utf-8")

    resilient_dist = _distribution(resilient)
    legacy_dist = _distribution(legacy)
    (out_dir / "outcome_distribution.svg").write_text(render_donut_svg(resilient_dist), encoding="utf-8")
    _maybe_render_png(resilient_dist, out_dir / "outcome_distribution.png")
    (out_dir / "outcome_comparison.svg").write_text(
        render_comparison_svg(legacy_dist, resilient_dist), encoding="utf-8"
    )
    _maybe_render_comparison_png(legacy_dist, resilient_dist, out_dir / "outcome_comparison.png")
    return {"resilient": resilient, "legacy": legacy}


def render_comparison(legacy: list[TrajectoryResult], resilient: list[TrajectoryResult]) -> str:
    total = len(resilient) or 1
    legacy_dist = _distribution(legacy)
    resilient_dist = _distribution(resilient)
    lines = [
        "# InsightAgent V5 — 对照实验：修复前 (legacy) vs 修复后 (resilient)",
        "",
        f"同一组 {total} 个任务、同一批脚本化模型行为，分别在 legacy 模式与 resilient 模式下真实运行。",
        "",
        "## 结局分布对比",
        "",
        "| 结局 | 修复前 legacy | 修复后 resilient |",
        "| --- | --- | --- |",
    ]
    for key, label in OUTCOME_LABELS.items():
        lc = legacy_dist.get(key, 0)
        rc = resilient_dist.get(key, 0)
        lines.append(f"| {label} | {lc} ({lc / total * 100:.1f}%) | {rc} ({rc / total * 100:.1f}%) |")
    lines += ["", "## 逐任务对比", "", "| 任务 | 修复前 | 修复后 | 说明 |", "| --- | --- | --- | --- |"]
    legacy_by_id = {t.id: t for t in legacy}
    for traj in resilient:
        old = legacy_by_id.get(traj.id)
        old_label = OUTCOME_LABELS.get(old.outcome, "?") if old else "?"
        changed = "→ 改善" if old and old.outcome != traj.outcome and traj.outcome in {"done", "failed_clean"} and old.outcome in {"iter_exhausted", "stuck_in_plan"} else ""
        lines.append(f"| {traj.id} | {old_label} | {OUTCOME_LABELS[traj.outcome]} | {traj.weakness} {changed} |")
    return "\n".join(lines)


def _distribution(results: list[TrajectoryResult]) -> dict[str, int]:
    counts: dict[str, int] = {key: 0 for key in OUTCOME_LABELS}
    for traj in results:
        counts[traj.outcome] = counts.get(traj.outcome, 0) + 1
    return counts


def render_report(results: list[TrajectoryResult]) -> str:
    total = len(results)
    dist = _distribution(results)
    lines = [
        "# InsightAgent V5 — 12 任务测试轨迹报告",
        "",
        f"总任务数：{total}（确定性脚本化模型 + 真实运行时，无需联网）",
        "",
        "## 轨迹结局分布",
        "",
        "| 结局 | 数量 | 占比 |",
        "| --- | --- | --- |",
    ]
    for key, label in OUTCOME_LABELS.items():
        count = dist.get(key, 0)
        pct = (count / total * 100) if total else 0
        lines.append(f"| {label} | {count} | {pct:.1f}% |")
    lines += ["", "## 逐任务轨迹", ""]
    for traj in results:
        status = "✅ 成功" if traj.outcome == "done" else ("🟠 合理失败" if traj.outcome == "failed_clean" else "🔴 异常")
        lines += [
            f"### {traj.id} — {traj.title}",
            "",
            f"- 结局：**{OUTCOME_LABELS[traj.outcome]}** {status}",
            f"- 弱点模拟：{traj.weakness}",
            f"- 迭代轮数：{traj.iterations}",
            f"- 判定原因：{traj.reason}",
            f"- 执行工具：{', '.join(traj.tools_used) or '（无）'}",
            f"- 文本恢复：{', '.join(traj.recovered) or '（无）'}",
            f"- 关键机制：{', '.join(traj.notable) or '（无）'}",
            f"- 最终输出：{traj.final[:160]}",
            "",
        ]
    return "\n".join(lines)


def render_donut_svg(distribution: dict[str, int], size: int = 460) -> str:
    total = sum(distribution.values()) or 1
    cx = cy = 150
    radius = 95
    stroke = 46
    circumference = 2 * math.pi * radius
    segments = []
    offset = 0.0
    for key, label in OUTCOME_LABELS.items():
        count = distribution.get(key, 0)
        if count == 0:
            continue
        fraction = count / total
        dash = fraction * circumference
        segments.append(
            f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="{OUTCOME_COLORS[key]}" '
            f'stroke-width="{stroke}" stroke-dasharray="{dash:.3f} {circumference - dash:.3f}" '
            f'stroke-dashoffset="{-offset:.3f}" transform="rotate(-90 {cx} {cy})" />'
        )
        offset += dash
    legend = []
    ly = 60
    for key, label in OUTCOME_LABELS.items():
        count = distribution.get(key, 0)
        pct = count / total * 100
        legend.append(f'<rect x="300" y="{ly}" width="16" height="16" fill="{OUTCOME_COLORS[key]}" rx="3" />')
        legend.append(f'<text x="324" y="{ly + 13}" font-family="sans-serif" font-size="13" fill="#222">{label}: {count} ({pct:.1f}%)</text>')
        ly += 30
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size + 180}" height="{size - 100}" '
        f'viewBox="0 0 {size + 180} {size - 100}">'
        f'<rect width="100%" height="100%" fill="#ffffff" />'
        f'<text x="40" y="34" font-family="sans-serif" font-size="20" font-weight="bold" fill="#5B2A86">轨迹结局分布 (Trajectory Outcomes)</text>'
        + "".join(segments)
        + f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" font-family="sans-serif" font-size="22" font-weight="bold" fill="#222">{total}</text>'
        + f'<text x="{cx}" y="{cy + 18}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="#666">tasks</text>'
        + "".join(legend)
        + "</svg>"
    )


def _donut_svg_segments(distribution: dict[str, int], cx: int, cy: int, radius: int, stroke: int) -> str:
    total = sum(distribution.values()) or 1
    circumference = 2 * math.pi * radius
    out = []
    offset = 0.0
    for key in OUTCOME_LABELS:
        count = distribution.get(key, 0)
        if count == 0:
            continue
        dash = count / total * circumference
        out.append(
            f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="{OUTCOME_COLORS[key]}" '
            f'stroke-width="{stroke}" stroke-dasharray="{dash:.3f} {circumference - dash:.3f}" '
            f'stroke-dashoffset="{-offset:.3f}" transform="rotate(-90 {cx} {cy})" />'
        )
        offset += dash
    out.append(f'<text x="{cx}" y="{cy + 6}" text-anchor="middle" font-family="sans-serif" font-size="20" font-weight="bold" fill="#222">{total}</text>')
    return "".join(out)


def render_comparison_svg(legacy: dict[str, int], resilient: dict[str, int]) -> str:
    W, H = 820, 420
    radius, stroke = 90, 42
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<rect width="100%" height="100%" fill="#ffffff" />',
        '<text x="40" y="34" font-family="sans-serif" font-size="20" font-weight="bold" fill="#5B2A86">轨迹结局对照：修复前 vs 修复后</text>',
        '<text x="120" y="70" text-anchor="middle" font-family="sans-serif" font-size="15" fill="#C75D5D" font-weight="bold">修复前 (legacy)</text>',
        '<text x="430" y="70" text-anchor="middle" font-family="sans-serif" font-size="15" fill="#54B89A" font-weight="bold">修复后 (resilient)</text>',
        _donut_svg_segments(legacy, 120, 190, radius, stroke),
        _donut_svg_segments(resilient, 430, 190, radius, stroke),
    ]
    ly = 110
    for key, label in OUTCOME_LABELS.items():
        parts.append(f'<rect x="600" y="{ly}" width="16" height="16" fill="{OUTCOME_COLORS[key]}" rx="3" />')
        parts.append(f'<text x="624" y="{ly + 13}" font-family="sans-serif" font-size="13" fill="#222">{label}</text>')
        ly += 30
    total = sum(resilient.values()) or 1
    yb = 330
    for key, label in OUTCOME_LABELS.items():
        lc = legacy.get(key, 0)
        rc = resilient.get(key, 0)
        parts.append(
            f'<text x="40" y="{yb}" font-family="sans-serif" font-size="12" fill="#444">'
            f'{label}: {lc} → {rc}</text>'
        )
        yb += 20
    parts.append("</svg>")
    return "".join(parts)


def _load_cjk_font(size: int):
    from PIL import ImageFont

    for candidate in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_donut(draw, box, distribution, total):
    start = -90.0
    for key in OUTCOME_LABELS:
        count = distribution.get(key, 0)
        if count == 0:
            continue
        end = start + count / total * 360.0
        draw.pieslice(box, start, end, fill=OUTCOME_COLORS[key])
        start = end
    cx = (box[0] + box[2]) // 2
    cy = (box[1] + box[3]) // 2
    r = (box[2] - box[0]) // 4
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill="white")


def _maybe_render_comparison_png(legacy: dict[str, int], resilient: dict[str, int], path: Path) -> None:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return
    total = sum(resilient.values()) or 1
    W, H = 900, 430
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    font = _load_cjk_font(14)
    title_font = _load_cjk_font(20)
    label_font = _load_cjk_font(15)
    draw.text((30, 18), "轨迹结局对照：修复前 vs 修复后", fill="#5B2A86", font=title_font)
    draw.text((90, 60), "修复前 (legacy)", fill="#C75D5D", font=label_font)
    draw.text((360, 60), "修复后 (resilient)", fill="#54B89A", font=label_font)
    _draw_donut(draw, (50, 90, 250, 290), legacy, total)
    _draw_donut(draw, (330, 90, 530, 290), resilient, total)
    lt = sum(legacy.values()) or 1
    draw.text((135, 180), str(lt), fill="#222", font=title_font)
    draw.text((415, 180), str(total), fill="#222", font=title_font)
    ly = 95
    for key, label in OUTCOME_LABELS.items():
        lc = legacy.get(key, 0)
        rc = resilient.get(key, 0)
        draw.rectangle((590, ly, 608, ly + 18), fill=OUTCOME_COLORS[key])
        draw.text((618, ly + 1), f"{label}", fill="#222", font=font)
        draw.text((618, ly + 22), f"   {lc}  →  {rc}", fill="#666", font=font)
        ly += 56
    img.save(path)


def _maybe_render_png(distribution: dict[str, int], path: Path) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return
    total = sum(distribution.values()) or 1
    W, H = 820, 380
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    cjk_candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "DejaVuSans.ttf",
    ]
    font = title_font = None
    for candidate in cjk_candidates:
        try:
            font = ImageFont.truetype(candidate, 14)
            title_font = ImageFont.truetype(candidate, 20)
            break
        except Exception:
            continue
    if font is None:
        font = title_font = ImageFont.load_default()
    draw.text((30, 20), "Trajectory Outcome Distribution", fill="#5B2A86", font=title_font)
    box = (40, 70, 300, 330)
    start = -90.0
    for key in OUTCOME_LABELS:
        count = distribution.get(key, 0)
        if count == 0:
            continue
        end = start + count / total * 360.0
        draw.pieslice(box, start, end, fill=OUTCOME_COLORS[key])
        start = end
    draw.ellipse((110, 140, 230, 260), fill="white")
    draw.text((150, 188), str(total), fill="#222", font=title_font)
    ly = 90
    for key, label in OUTCOME_LABELS.items():
        count = distribution.get(key, 0)
        pct = count / total * 100
        draw.rectangle((360, ly, 378, ly + 18), fill=OUTCOME_COLORS[key])
        draw.text((388, ly + 1), f"{label}: {count} ({pct:.0f}%)", fill="#222", font=font)
        ly += 34
    img.save(path)


if __name__ == "__main__":
    both = run()
    legacy_dist = _distribution(both["legacy"])
    resilient_dist = _distribution(both["resilient"])
    print("Outcome distribution (legacy -> resilient):")
    for key, label in OUTCOME_LABELS.items():
        print(f"  {label}: {legacy_dist.get(key, 0)} -> {resilient_dist.get(key, 0)}")
    print(f"\nWrote artifacts to {Path(__file__).resolve().parent / 'out'}")
