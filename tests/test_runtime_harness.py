from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from insightagent.agent.core import CodeAgent
from insightagent.api.messages import Message, ModelResponse, ToolCall
from insightagent.api.providers import ModelClient
from insightagent.api.resilience import RetryPolicy
from insightagent.runtime.tool_context import ToolContext
from insightagent.tools import ToolRegistry


def _no_delay_policy(max_attempts: int = 1) -> RetryPolicy:
    return RetryPolicy(max_attempts=max_attempts, sleep=lambda _seconds: None)


class FakeModelClient(ModelClient):
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.calls.append(list(messages))
        return self.responses.pop(0)


def load_runtime_api():
    try:
        from insightagent.runtime.command_validation import CommandKind, CommandValidator
        from insightagent.runtime.failure_classifier import FailureClassifier, FailureKind
        from insightagent.runtime.permissions import PermissionEnforcer
        from insightagent.runtime.types import ToolPermission, ToolRisk, ToolSpec
        from insightagent.telemetry.trace import JsonlTraceRecorder
    except ModuleNotFoundError as error:
        raise AssertionError(f"runtime harness module is missing: {error}") from error
    except ImportError as error:
        raise AssertionError(f"runtime harness API is incomplete: {error}") from error
    return (
        CommandKind,
        CommandValidator,
        FailureClassifier,
        FailureKind,
        PermissionEnforcer,
        ToolPermission,
        ToolRisk,
        ToolSpec,
        JsonlTraceRecorder,
    )


class RuntimeHarnessTests(unittest.TestCase):
    def test_command_validator_classifies_intent_and_blocks_destructive_commands(self) -> None:
        CommandKind, CommandValidator, *_ = load_runtime_api()
        validator = CommandValidator()

        self.assertEqual(validator.classify("python3 -m unittest discover -s tests").kind, CommandKind.TEST)
        self.assertEqual(validator.classify("curl https://example.com").kind, CommandKind.NETWORK)
        self.assertEqual(validator.classify("rm -rf .").kind, CommandKind.DESTRUCTIVE)

        decision = validator.validate("rm -rf .", permission_mode="workspace-write")
        self.assertFalse(decision.allowed)
        self.assertIn("destructive", decision.reason)

    def test_permission_enforcer_uses_tool_specs_before_execution(self) -> None:
        (
            _CommandKind,
            _CommandValidator,
            _FailureClassifier,
            _FailureKind,
            PermissionEnforcer,
            ToolPermission,
            ToolRisk,
            ToolSpec,
            _JsonlTraceRecorder,
        ) = load_runtime_api()
        context = ToolContext(workspace=Path.cwd(), permission_mode="read-only")
        spec = ToolSpec(
            name="write_file",
            description="write",
            input_schema={"type": "object"},
            required_permission=ToolPermission.WORKSPACE_WRITE,
            risk=ToolRisk.MEDIUM,
            mutates_workspace=True,
        )

        decision = PermissionEnforcer().check(spec, context, {"path": "x.txt"})

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.required_permission, ToolPermission.WORKSPACE_WRITE)
        self.assertIn("read-only", decision.reason)

    def test_registry_exposes_tool_specs_and_structured_execution_results(self) -> None:
        _CommandKind, _CommandValidator, _FailureClassifier, FailureKind, *_ = load_runtime_api()
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))

            spec = registry.spec("execute_command")
            result = registry.execute(
                "execute_command",
                {
                    "command": "python3 -c \"raise SyntaxError('bad syntax')\"",
                    "cwd": str(directory),
                },
            )

        self.assertEqual(spec.name, "execute_command")
        self.assertTrue(result.is_error)
        self.assertEqual(result.failure_kind, FailureKind.CODE_ERROR)
        self.assertIn("SyntaxError", result.content)
        self.assertFalse(result.retryable)

    def test_workspace_mutation_invalidates_readonly_dedup_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "calc.py"
            path.write_text("value = 'old'\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            first_read = registry.execute("read_file", {"path": "calc.py"})
            edit = registry.execute(
                "edit_file",
                {"path": "calc.py", "old": "old", "new": "new"},
            )
            second_read = registry.execute("read_file", {"path": "calc.py"})

        self.assertFalse(first_read.is_error)
        self.assertFalse(edit.is_error)
        self.assertFalse(second_read.is_error)
        self.assertFalse(second_read.suppressed)
        self.assertIn("new", second_read.content)
        self.assertNotIn("刚刚执行过", second_read.content)

    def test_read_file_supports_line_ranges_for_large_file_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "large.py"
            path.write_text("one\n# target\ntwo\nthree\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute("read_file", {"path": "large.py", "start_line": 2, "max_lines": 2})

        self.assertFalse(result.is_error)
        self.assertIn("large.py lines 2-3", result.content)
        self.assertIn("2: # target", result.content)
        self.assertIn("3: two", result.content)
        self.assertNotIn("1: one", result.content)

    def test_read_file_large_file_without_range_returns_navigation_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "large.py"
            path.write_text("".join(f"line {index}\n" for index in range(1, 452)), encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute("read_file", {"path": "large.py"})

        self.assertFalse(result.is_error)
        self.assertIn("large.py has 451 lines", result.content)
        self.assertIn("read_file", result.content)
        self.assertIn("start_line", result.content)
        self.assertIn("grep_search", result.content)
        self.assertNotIn("line 451", result.content)

    def test_grep_search_glob_matches_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "src" / "pkg"
            source.mkdir(parents=True)
            (source / "blueprints.py").write_text("class Blueprint(Scaffold):\n    pass\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "grep_search",
                {"pattern": r"class Blueprint\(", "glob": "**/*.py"},
            )

        self.assertFalse(result.is_error)
        self.assertIn("src/pkg/blueprints.py:1:class Blueprint(Scaffold):", result.content)

    def test_grep_search_glob_matches_src_package_suffix_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "src" / "flask"
            source.mkdir(parents=True)
            (source / "blueprints.py").write_text("class Blueprint(Scaffold):\n    pass\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            direct = registry.execute(
                "grep_search",
                {"pattern": r"class Blueprint\(", "glob": "flask/*.py"},
            )
            recursive = registry.execute(
                "grep_search",
                {"pattern": r"class Blueprint\(", "glob": "flask/**/*.py"},
            )

        self.assertFalse(direct.is_error)
        self.assertFalse(recursive.is_error)
        self.assertIn("src/flask/blueprints.py:1:class Blueprint(Scaffold):", direct.content)
        self.assertIn("src/flask/blueprints.py:1:class Blueprint(Scaffold):", recursive.content)

    def test_failure_classifier_marks_network_errors_as_non_retryable_environment_failures(self) -> None:
        _CommandKind, _CommandValidator, FailureClassifier, FailureKind, *_ = load_runtime_api()
        content = "exit_code: 6\nstdout:\n\nstderr:\ncurl: (6) Could not resolve host: example.test"

        classification = FailureClassifier().classify("execute_command", content, is_error=True)

        self.assertEqual(classification.kind, FailureKind.NETWORK_ERROR)
        self.assertFalse(classification.retryable)
        self.assertIn("network", classification.repair_guidance.lower())

    def test_failure_classifier_guides_format_string_index_errors(self) -> None:
        _CommandKind, _CommandValidator, FailureClassifier, FailureKind, *_ = load_runtime_api()
        content = (
            "exit_code: 1\nstdout:\nFAILED tests/test_cli.py::TestRoutes::test_host\n"
            "E       AssertionError: assert 1 == 0\n"
            "E        +  where 1 = <Result IndexError('Replacement index 2 out of range for positional args tuple')>.exit_code"
        )

        classification = FailureClassifier().classify("run_verification", content, is_error=True)

        self.assertEqual(classification.kind, FailureKind.CODE_ERROR)
        self.assertIn("format string", classification.repair_guidance)
        self.assertIn("placeholders", classification.repair_guidance)

    def test_failure_classifier_guides_missing_expected_exception_at_call_site(self) -> None:
        _CommandKind, _CommandValidator, FailureClassifier, FailureKind, *_ = load_runtime_api()
        content = (
            "exit_code: 1\nstdout:\n"
            "FAILED tests/test_blueprints.py::test_dotted_name_not_allowed\n"
            "    def test_dotted_name_not_allowed(app, client):\n"
            ">       with pytest.raises(ValueError):\n"
            "E       Failed: DID NOT RAISE <class 'ValueError'>\n"
            "        flask.Blueprint(\"app.ui\", __name__)\n"
        )

        classification = FailureClassifier().classify("run_verification", content, is_error=True)

        self.assertEqual(classification.kind, FailureKind.TEST_FAILURE)
        self.assertIn("call site", classification.repair_guidance)
        self.assertIn("constructor", classification.repair_guidance)

    def test_failure_classifier_guides_assertion_to_value_error_conversion(self) -> None:
        _CommandKind, _CommandValidator, FailureClassifier, FailureKind, *_ = load_runtime_api()
        content = (
            "exit_code: 1\nstdout:\n"
            "with pytest.raises(ValueError):\n"
            "    bp.add_url_rule('/', view_func=view)\n"
            "E           AssertionError: Blueprint view function name should not contain dots\n"
        )

        classification = FailureClassifier().classify("run_verification", content, is_error=True)

        self.assertEqual(classification.kind, FailureKind.TEST_FAILURE)
        self.assertIn("explicit ValueError", classification.repair_guidance)
        self.assertIn("assert", classification.repair_guidance)

    def test_edit_file_old_text_not_found_guides_symbol_search(self) -> None:
        _CommandKind, _CommandValidator, FailureClassifier, FailureKind, *_ = load_runtime_api()

        classification = FailureClassifier().classify("edit_file", "ValueError: old text not found", is_error=True)

        self.assertEqual(classification.kind, FailureKind.CODE_ERROR)
        self.assertIn("grep_search", classification.repair_guidance)
        self.assertIn("unique", classification.repair_guidance)

    def test_edit_file_exception_preserves_old_text_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "cli.py").write_text("def routes_command():\n    pass\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "edit_file",
                {"path": "cli.py", "old": "headers = (...)", "new": "headers = (..., 'Subdomain')"},
            )

        self.assertTrue(result.is_error)
        self.assertIn("old text not found", result.content)
        self.assertIn("grep_search", result.repair_guidance)
        self.assertIn("narrow file section", result.repair_guidance)

    def test_edit_file_accepts_unique_quote_style_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "blueprints.py").write_text(
                "    def add_url_rule(self, endpoint):\n"
                "        if endpoint:\n"
                "            assert \".\" not in endpoint, \"Blueprint endpoints should not contain dots\"\n",
                encoding="utf-8",
            )
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "edit_file",
                {
                    "path": "blueprints.py",
                    "old": (
                        "        if endpoint:\n"
                        "            assert '.' not in endpoint, 'Blueprint endpoints should not contain dots'\n"
                    ),
                    "new": (
                        "        if endpoint and '.' in endpoint:\n"
                        "            raise ValueError('Blueprint endpoint cannot contain dots')\n"
                    ),
                },
            )

            edited = (workspace / "blueprints.py").read_text(encoding="utf-8")

        self.assertFalse(result.is_error)
        self.assertIn("raise ValueError", edited)
        self.assertNotIn("assert", edited)

    def test_edit_file_accepts_unique_whitespace_insensitive_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "cli.py"
            path.write_text(
                "def routes_command():\n"
                "    widths = (\n"
                "        max(len(rule.endpoint) for rule in rules),\n"
                "        max(len(methods) for methods in rule_methods),\n"
                "        max(len(rule.rule) for rule in rules),\n"
                "    )\n"
                "    return widths\n",
                encoding="utf-8",
            )
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "edit_file",
                {
                    "path": "cli.py",
                    "old": (
                        "widths = (max(len(rule.endpoint) for rule in rules), "
                        "max(len(methods) for methods in rule_methods), max(len(rule.rule) for rule in rules))"
                    ),
                    "new": (
                        "widths = (\n"
                        "        max(len(rule.endpoint) for rule in rules),\n"
                        "        max(len(methods) for methods in rule_methods),\n"
                        "        max(len(rule.rule) for rule in rules),\n"
                        "        max(len(rule.subdomain or '') for rule in rules),\n"
                        "    )"
                    ),
                },
            )

            edited = path.read_text(encoding="utf-8")

        self.assertFalse(result.is_error)
        self.assertIn("match=whitespace_insensitive", result.content)
        self.assertIn("rule.subdomain", edited)
        self.assertEqual(edited.count("widths = ("), 1)

    def test_edit_file_indents_multiline_replacement_to_matched_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "cli.py"
            path.write_text(
                "def routes_command():\n"
                "    headers = (\"Endpoint\", \"Methods\", \"Rule\")\n"
                "    widths = (\n"
                "        max(len(rule.endpoint) for rule in rules),\n"
                "        max(len(methods) for methods in rule_methods),\n"
                "        max(len(rule.rule) for rule in rules),\n"
                "    )\n"
                "    return headers, widths\n",
                encoding="utf-8",
            )
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "edit_file",
                {
                    "path": "cli.py",
                    "old": (
                        "headers = (\"Endpoint\", \"Methods\", \"Rule\") widths = "
                        "(max(len(rule.endpoint) for rule in rules), "
                        "max(len(methods) for methods in rule_methods), max(len(rule.rule) for rule in rules))"
                    ),
                    "new": (
                        "headers = (\"Endpoint\", \"Methods\", \"Rule\", \"Subdomain\")\n"
                        "widths = (\n"
                        "    max(len(rule.endpoint) for rule in rules),\n"
                        "    max(len(methods) for methods in rule_methods),\n"
                        "    max(len(rule.rule) for rule in rules),\n"
                        "    max(len(rule.subdomain or '') for rule in rules),\n"
                        ")"
                    ),
                },
            )

            edited = path.read_text(encoding="utf-8")

        self.assertFalse(result.is_error)
        self.assertIn('    headers = ("Endpoint", "Methods", "Rule", "Subdomain")', edited)
        self.assertIn("    widths = (", edited)
        self.assertIn("        max(len(rule.subdomain or '') for rule in rules),", edited)

    def test_edit_file_accepts_replacement_with_existing_context_indent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "app.py"
            path.write_text(
                "class App:\n"
                "    def register_blueprint(self, blueprint, options):\n"
                "        blueprint.register(self, options)\n",
                encoding="utf-8",
            )
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "edit_file",
                {
                    "path": "app.py",
                    "old": "blueprint.register(self, options)",
                    "new": (
                        "        if '.' in blueprint.name:\n"
                        "            raise ValueError('Blueprint name cannot contain dots.')\n"
                        "        blueprint.register(self, options)"
                    ),
                },
            )

            edited = path.read_text(encoding="utf-8")

        self.assertFalse(result.is_error)
        self.assertIn("        if '.' in blueprint.name:", edited)
        self.assertNotIn("                if '.' in blueprint.name:", edited)

    def test_edit_file_does_not_double_indent_preindented_replacement_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "blueprints.py"
            path.write_text(
                "class Blueprint:\n"
                "    def __init__(\n"
                "        self,\n"
                "        name,\n"
                "    ):\n"
                "        super().__init__()\n"
                "        self.name = name\n",
                encoding="utf-8",
            )
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "edit_file",
                {
                    "path": "blueprints.py",
                    "old": (
                        "def __init__(\n"
                        "        self,\n"
                        "        name,\n"
                        "    ):\n"
                        "        super().__init__()"
                    ),
                    "new": (
                        "def __init__(\n"
                        "        self,\n"
                        "        name,\n"
                        "    ):\n"
                        "        if '.' in name:\n"
                        "            raise ValueError('bad name')\n"
                        "        super().__init__()"
                    ),
                },
            )

            edited = path.read_text(encoding="utf-8")

        self.assertFalse(result.is_error)
        self.assertIn("        if '.' in name:", edited)
        self.assertNotIn("            if '.' in name:", edited)

    def test_edit_file_rejects_python_syntax_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "cli.py"
            original = "def routes_command():\n    row = '{}'\n    print(row)\n"
            path.write_text(original, encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "edit_file",
                {
                    "path": "cli.py",
                    "old": "    print(row)\n",
                    "new": "print(row)\n    print(row)\n",
                },
            )

            edited = path.read_text(encoding="utf-8")

        self.assertTrue(result.is_error)
        self.assertIn("invalid Python syntax", result.content)
        self.assertEqual(edited, original)

    def test_write_file_rejects_python_syntax_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "blueprints.py"
            original = "class Blueprint:\n    def __init__(self):\n        self.name = 'ok'\n"
            path.write_text(original, encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "write_file",
                {"path": "blueprints.py", "content": "    def __init__(self):\n        pass\n"},
            )

            edited = path.read_text(encoding="utf-8")

        self.assertTrue(result.is_error)
        self.assertIn("invalid Python syntax", result.content)
        self.assertEqual(edited, original)

    def test_edit_file_rejects_super_init_keyword_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "blueprints.py"
            original = (
                "class Blueprint:\n"
                "    def __init__(self, import_name, url_prefix=None):\n"
                "        super().__init__(\n"
                "            import_name=import_name,\n"
                "        )\n"
                "        self.url_prefix = url_prefix\n"
            )
            path.write_text(original, encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "edit_file",
                {
                    "path": "blueprints.py",
                    "old": "            import_name=import_name,\n",
                    "new": "            import_name=import_name,\n            url_prefix=url_prefix,\n",
                },
            )

            edited = path.read_text(encoding="utf-8")

        self.assertTrue(result.is_error)
        self.assertIn("super().__init__ keyword expansion", result.content)
        self.assertEqual(edited, original)

    def test_edit_file_converts_multiline_assert_with_message_to_raise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "blueprints.py"
            path.write_text(
                "class Blueprint:\n"
                "    def add_url_rule(self, endpoint, view_func):\n"
                "        if view_func and hasattr(view_func, \"__name__\"):\n"
                "            assert (\n"
                "                \".\" not in view_func.__name__\n"
                "            ), \"Blueprint view function name should not contain dots\"\n"
                "        self.record(endpoint)\n",
                encoding="utf-8",
            )
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "edit_file",
                {
                    "path": "blueprints.py",
                    "old": 'assert ("." not in view_func.__name__)',
                    "new": (
                        'if "." in view_func.__name__:\n'
                        '    raise ValueError("Blueprint view function name should not contain dots")'
                    ),
                },
            )

            edited = path.read_text(encoding="utf-8")

        self.assertFalse(result.is_error)
        self.assertIn('            if "." in view_func.__name__:', edited)
        self.assertIn(
            '                raise ValueError("Blueprint view function name should not contain dots")',
            edited,
        )
        self.assertNotIn('), "Blueprint view function name should not contain dots"', edited)

    def test_edit_file_rejects_noop_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "cli.py"
            original = "def routes_command():\n    return 'old'\n"
            path.write_text(original, encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "edit_file",
                {"path": "cli.py", "old": "return 'old'", "new": "return 'old'"},
            )

            edited = path.read_text(encoding="utf-8")

        self.assertTrue(result.is_error)
        self.assertIn("no-op edit_file", result.content)
        self.assertEqual(edited, original)

    def test_edit_file_rejects_header_row_format_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "cli.py"
            original = (
                "def routes_command():\n"
                "    headers = (\"Endpoint\", \"Methods\", \"Rule\")\n"
                "    widths = (8, 7, 4)\n"
                "    row = \"{{0:<{0}}}  {{1:<{1}}}  {{2:<{2}}}\".format(*widths)\n"
                "    return row.format(*headers)\n"
            )
            path.write_text(original, encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "edit_file",
                {
                    "path": "cli.py",
                    "old": 'headers = ("Endpoint", "Methods", "Rule")',
                    "new": 'headers = ("Subdomain", "Host", "Endpoint", "Methods", "Rule")',
                },
            )

            edited = path.read_text(encoding="utf-8")

        self.assertTrue(result.is_error)
        self.assertIn("row format has 3 columns", result.content)
        self.assertIn("headers defines 5", result.content)
        self.assertEqual(edited, original)

    def test_edit_file_rejects_widths_row_format_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "cli.py"
            original = (
                "def routes_command():\n"
                "    headers = (\"Endpoint\", \"Methods\", \"Rule\")\n"
                "    widths = (8, 7, 4)\n"
                "    row = \"{{0:<{0}}}  {{1:<{1}}}  {{2:<{2}}}\".format(*widths)\n"
                "    return row.format(*headers)\n"
            )
            path.write_text(original, encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=workspace))

            result = registry.execute(
                "edit_file",
                {
                    "path": "cli.py",
                    "old": "widths = (8, 7, 4)",
                    "new": "widths = (8, 7, 4, 9)",
                },
            )

            edited = path.read_text(encoding="utf-8")

        self.assertTrue(result.is_error)
        self.assertIn("widths defines 4", result.content)
        self.assertIn("row format has 3 columns", result.content)
        self.assertEqual(edited, original)

    def test_agent_suppresses_repeated_non_retryable_tool_calls(self) -> None:
        _CommandKind, _CommandValidator, _FailureClassifier, FailureKind, *_ = load_runtime_api()
        repeated_call = ToolCall(
            id="call_network",
            name="execute_command",
            arguments={
                "command": (
                    "python3 -c \"import sys; "
                    "sys.stderr.write('Could not resolve host: example.test\\n'); sys.exit(6)\""
                )
            },
        )
        client = FakeModelClient(
            [
                ModelResponse(tool_calls=[repeated_call]),
                ModelResponse(tool_calls=[ToolCall(id="call_network_again", name=repeated_call.name, arguments=repeated_call.arguments)]),
                ModelResponse(content="Stopped retrying the network failure."),
            ]
        )
        events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as directory:
            agent = CodeAgent(
                client,
                # max_attempts=1 means: after the first network failure, an identical
                # retry is suppressed instead of executed again (B2 bounded backoff).
                tools=ToolRegistry(
                    context=ToolContext(workspace=Path(directory)),
                    retry_policy=_no_delay_policy(max_attempts=1),
                ),
                max_tool_iterations=5,
            )

            result = agent.run_turn_with_trace("Try the same failing network command twice", trace=events.append)

        self.assertEqual(result.content, "Stopped retrying the network failure.")
        suppressed = [event for event in events if event["type"] == "tool_call_suppressed"]
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0]["failure_kind"], FailureKind.NETWORK_ERROR.value)
        self.assertFalse(suppressed[0]["retryable"])

    def test_jsonl_trace_recorder_persists_structured_events(self) -> None:
        *_, JsonlTraceRecorder = load_runtime_api()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            recorder = JsonlTraceRecorder(path)

            recorder({"type": "tool_result", "tool": "execute_command", "failure_kind": "network_error"})
            recorder.close()

            lines = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["type"], "tool_result")
        self.assertEqual(json.loads(lines[0])["failure_kind"], "network_error")


if __name__ == "__main__":
    unittest.main()
