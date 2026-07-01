"""Prepare SWE-bench Lite cases for the local SWE-style runner."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .swe_style import _safe_name


DEFAULT_DATASET_URL = (
    "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/resolve/main/"
    "data/test-00000-of-00001.parquet"
)

CommandRunner = Callable[[Sequence[str], str | None], None]


def load_swe_bench_lite_rows(dataset_url: str = DEFAULT_DATASET_URL) -> list[dict[str, Any]]:
    try:
        import pandas as pd
    except ImportError as error:
        raise RuntimeError(
            "Preparing SWE-bench Lite from HuggingFace parquet requires pandas and pyarrow."
        ) from error
    frame = pd.read_parquet(dataset_url)
    return [dict(row) for row in frame.to_dict(orient="records")]


def select_rows(
    rows: Iterable[dict[str, Any]],
    *,
    repo: str | None = None,
    instance_ids: set[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for row in rows:
        instance_id = str(row.get("instance_id") or "")
        if repo and row.get("repo") != repo:
            continue
        if instance_ids and instance_id not in instance_ids:
            continue
        selected.append(row)
        if limit is not None and len(selected) >= limit:
            break
    return selected


def case_record_from_row(
    row: dict[str, Any],
    *,
    checkout_root: Path | None = None,
    test_runner: str = "python -m pytest -q",
    test_patch_applied: bool = False,
) -> dict[str, Any]:
    instance_id = str(row["instance_id"])
    record: dict[str, Any] = {
        "instance_id": instance_id,
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "problem_statement": row["problem_statement"],
        "verification_command": build_verification_command(row, test_runner=test_runner),
        "FAIL_TO_PASS": _json_list(row.get("FAIL_TO_PASS")),
        "PASS_TO_PASS": _json_list(row.get("PASS_TO_PASS")),
        "environment_setup_commit": row.get("environment_setup_commit"),
        "test_patch_applied": test_patch_applied,
    }
    if checkout_root is not None:
        record["source_dir"] = str((checkout_root / _safe_name(instance_id)).resolve())
    return {key: value for key, value in record.items() if value not in (None, "")}


def build_verification_command(row: dict[str, Any], *, test_runner: str = "python -m pytest -q") -> str:
    selectors = _json_list(row.get("FAIL_TO_PASS"))
    if not selectors:
        return test_runner
    return _shell_join([*shlex.split(test_runner), *[str(selector) for selector in selectors]])


def write_cases_jsonl(records: Iterable[dict[str, Any]], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    return output_path


def prepare_checkout(
    row: dict[str, Any],
    checkout_root: Path,
    *,
    apply_test_patch: bool = True,
    disable_git_proxy: bool = False,
    clone_from_local: str | Path | None = None,
    command_runner: CommandRunner | None = None,
) -> Path:
    checkout_root = checkout_root.resolve()
    checkout_root.mkdir(parents=True, exist_ok=True)
    destination = (checkout_root / _safe_name(str(row["instance_id"]))).resolve()
    if checkout_root != destination and checkout_root not in destination.parents:
        raise ValueError(f"checkout destination escapes root: {destination}")
    runner = command_runner or _run_command
    git = _git_command(disable_git_proxy=disable_git_proxy)
    if not destination.exists():
        clone_source = str(Path(clone_from_local).resolve()) if clone_from_local else f"https://github.com/{row['repo']}.git"
        clone_options = [] if clone_from_local else ["--filter=blob:none", "--no-checkout"]
        runner([*git, "clone", *clone_options, clone_source, str(destination)], None)
    runner([*git, "-C", str(destination), "checkout", "--force", str(row["base_commit"])], None)
    runner([*git, "-C", str(destination), "clean", "-fdx"], None)
    test_patch = str(row.get("test_patch") or "")
    if apply_test_patch and test_patch:
        runner([*git, "-C", str(destination), "apply", "--whitespace=nowarn", "-"], test_patch)
    return destination


def prepare_cases(
    rows: Iterable[dict[str, Any]],
    *,
    output_path: Path,
    checkout_root: Path | None = None,
    clone: bool = False,
    disable_git_proxy: bool = False,
    clone_from_local: str | Path | None = None,
    test_runner: str = "python -m pytest -q",
) -> Path:
    records: list[dict[str, Any]] = []
    for row in rows:
        if clone:
            if checkout_root is None:
                raise ValueError("--clone requires --checkout-root")
            prepare_checkout(
                row,
                checkout_root,
                disable_git_proxy=disable_git_proxy,
                clone_from_local=clone_from_local,
            )
        records.append(
            case_record_from_row(
                row,
                checkout_root=checkout_root,
                test_runner=test_runner,
                test_patch_applied=clone,
            )
        )
    return write_cases_jsonl(records, output_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare SWE-bench Lite cases for insightagent.evals.swe_style.")
    parser.add_argument("--dataset-url", default=DEFAULT_DATASET_URL)
    parser.add_argument("--output", default="reports/swe_style/swebench-lite/cases.jsonl")
    parser.add_argument("--checkout-root", default="workspaces/evals/swebench_lite/checkouts")
    parser.add_argument("--repo", help="Filter by GitHub repo, for example psf/requests.")
    parser.add_argument("--instance-id", action="append", help="Filter to one or more SWE-bench instance ids.")
    parser.add_argument("--limit", type=int, help="Maximum number of selected rows.")
    parser.add_argument("--clone", action="store_true", help="Clone each repo at base_commit and apply test_patch.")
    parser.add_argument(
        "--git-no-proxy",
        action="store_true",
        help="Run git with empty http.proxy/https.proxy config for environments with stale local proxies.",
    )
    parser.add_argument(
        "--clone-from-local",
        help="Clone from an existing local checkout instead of GitHub; useful when preparing another case from the same repo.",
    )
    parser.add_argument("--test-runner", default="python -m pytest -q")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    rows = load_swe_bench_lite_rows(args.dataset_url)
    selected = select_rows(
        rows,
        repo=args.repo,
        instance_ids=set(args.instance_id or []) or None,
        limit=args.limit,
    )
    if not selected:
        raise SystemExit("no SWE-bench Lite rows selected")
    output_path = Path(args.output).resolve()
    checkout_root = Path(args.checkout_root).resolve() if args.checkout_root else None
    path = prepare_cases(
        selected,
        output_path=output_path,
        checkout_root=checkout_root,
        clone=args.clone,
        disable_git_proxy=args.git_no_proxy,
        clone_from_local=args.clone_from_local,
        test_runner=args.test_runner,
    )
    print(f"cases_jsonl: {path}")
    if args.clone:
        print(f"checkout_root: {checkout_root}")
    print(f"selected: {len(selected)}")


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else [parsed]
    return list(value)


def _shell_join(args: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(args))
    return shlex.join(list(args))


def _git_command(*, disable_git_proxy: bool) -> list[str]:
    if not disable_git_proxy:
        return ["git"]
    return ["git", "-c", "http.proxy=", "-c", "https.proxy="]


def _run_command(command: Sequence[str], stdin: str | None) -> None:
    completed = subprocess.run(
        list(command),
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "command failed: "
            + subprocess.list2cmdline(list(command))
            + f"\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


if __name__ == "__main__":
    main()
