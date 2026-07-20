#!/usr/bin/env python3
"""Run OpenCode backport attempts from a TensorFlow CVE CSV.

For each CSV row, this script:
  1. Resolves the oldest-version patch commit from the CSV link.
  2. Checks out that commit's first parent in a temporary worktree, so the
     benchmark starts from the codebase before the backport was applied.
  3. Starts a fresh non-interactive OpenCode session and gives it only the latest
     version diff plus a backport instruction.
  4. Captures OpenCode's generated git diff.
  5. Compares that generated diff with the row's expected oldest-version diff by
     applying both to the same base version and comparing canonical git diffs.
  6. Writes result columns back to a CSV.

The main TensorFlow checkout is not modified.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable


SUCCESS_COLUMN = "opencode_backport_success"
CODE_CHANGE_COLUMN = "opencode_generated_code_change"
MESSAGE_COLUMN = "opencode_final_message"
ERROR_COLUMN = "opencode_error"
PATCH_COMMIT_COLUMN = "opencode_oldest_patch_commit"
BASE_COMMIT_COLUMN = "opencode_base_commit"
INPUT_TOKENS_COLUMN = "opencode_input_tokens"
CACHED_INPUT_TOKENS_COLUMN = "opencode_cache_read_tokens"
CACHE_WRITE_TOKENS_COLUMN = "opencode_cache_write_tokens"
OUTPUT_TOKENS_COLUMN = "opencode_output_tokens"
REASONING_OUTPUT_TOKENS_COLUMN = "opencode_reasoning_output_tokens"
TOTAL_TOKENS_COLUMN = "opencode_total_tokens"
MODEL_COLUMN = "opencode_model_used"
COST_COLUMN = "opencode_estimated_cost_usd"


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        cmd = " ".join(args)
        raise RuntimeError(
            f"command failed ({result.returncode}): {cmd}\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )
    return result


def require_command(command: str) -> None:
    if shutil.which(command) is None:
        raise RuntimeError(f"required command not found on PATH: {command}")


def resolve_opencode_bin(command: str) -> str:
    found = shutil.which(command)
    if found:
        return found

    raise RuntimeError(
        f"required command not found on PATH: {command}\n"
        "Install OpenCode, add it to PATH, or pass --opencode-bin with the full "
        "path to the OpenCode executable."
    )


def validate_opencode_cli(opencode_bin: str) -> None:
    result = run([opencode_bin, "run", "--help"])
    help_text = result.stdout + result.stderr
    if result.returncode != 0:
        raise RuntimeError(
            "Could not run OpenCode CLI. "
            f"{(result.stderr or result.stdout).strip()}"
        )
    missing_flags = [
        flag for flag in ["--dir", "--format", "--auto"] if flag not in help_text
    ]
    if missing_flags:
        raise RuntimeError(
            "OpenCode CLI is too old for this script; its `run` command is missing "
            f"{', '.join(missing_flags)}. Upgrade OpenCode and try again."
        )


def commit_exists(repo: Path, commit: str) -> bool:
    result = run(["git", "-C", str(repo), "rev-parse", "--verify", f"{commit}^{{commit}}"])
    return result.returncode == 0


def fetch_commit(repo: Path, commit: str) -> None:
    result = run(["git", "-C", str(repo), "fetch", "--no-tags", "origin", commit])
    if result.returncode != 0:
        raise RuntimeError(
            f"Commit not found locally and fetch failed for {commit}: "
            f"{(result.stderr or result.stdout).strip()}"
        )


def ensure_commit_available(repo: Path, commit: str) -> None:
    if not commit_exists(repo, commit):
        fetch_commit(repo, commit)
    if not commit_exists(repo, commit):
        raise RuntimeError(f"Commit not found: {commit}")


def follow_redirect(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.geturl()
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.geturl()


def extract_commit_from_link(link: str) -> str:
    link = (link or "").strip()
    if not link:
        raise RuntimeError("CSV row has no oldest version link.")

    candidates = [link]
    if link.startswith(("http://", "https://")):
        try:
            candidates.append(follow_redirect(link))
        except Exception:
            pass

    for candidate in candidates:
        match = re.search(r"/commit/([0-9a-fA-F]{7,40})(?:\b|[/?#])", candidate)
        if match:
            return match.group(1)

    for candidate in candidates:
        matches = re.findall(r"(?<![0-9a-fA-F])([0-9a-fA-F]{40})(?![0-9a-fA-F])", candidate)
        if matches:
            return matches[-1]

    raise RuntimeError(f"Could not find a commit SHA in oldest version link: {link}")


def first_parent_commit(repo: Path, commit: str) -> str:
    ensure_commit_available(repo, commit)
    result = run(["git", "-C", str(repo), "rev-parse", f"{commit}^"], check=True)
    parent = result.stdout.strip()
    ensure_commit_available(repo, parent)
    return parent


def git_clean_reset(worktree: Path) -> None:
    run(["git", "-C", str(worktree), "reset", "--hard"], check=True)
    run(["git", "-C", str(worktree), "clean", "-fdx"], check=True)


def add_intent_to_add_for_untracked(worktree: Path) -> None:
    status = run(
        ["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all"],
        check=True,
    ).stdout
    untracked = [
        line[3:]
        for line in status.splitlines()
        if line.startswith("?? ") and line[3:].strip()
    ]
    if untracked:
        run(["git", "-C", str(worktree), "add", "-N", "--", *untracked], check=True)


def current_diff(worktree: Path) -> str:
    add_intent_to_add_for_untracked(worktree)
    return run(
        ["git", "-C", str(worktree), "diff", "--binary", "--no-ext-diff", "HEAD"],
        check=True,
    ).stdout


def is_inside_path(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def remove_stale_path(path: Path, allowed_root: Path) -> None:
    if not path.exists():
        return
    if path.resolve(strict=False) == allowed_root.resolve(strict=False):
        raise RuntimeError(f"Refusing to remove work root itself: {path}")
    if not is_inside_path(path, allowed_root):
        raise RuntimeError(f"Refusing to remove path outside work root: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def prepare_worktree_path(repo: Path, worktree: Path, work_root: Path) -> None:
    run(["git", "-C", str(repo), "worktree", "prune"], check=True)
    if not worktree.exists():
        return

    result = run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)])
    if result.returncode == 0:
        return

    output = f"{result.stdout}\n{result.stderr}"
    if "is not a working tree" not in output:
        raise RuntimeError(
            f"command failed ({result.returncode}): git -C {repo} worktree remove --force {worktree}\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )
    remove_stale_path(worktree, work_root)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def read_rows(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise RuntimeError(f"CSV has no header: {csv_path}")
        return list(reader.fieldnames), list(reader)


def read_input_rows(csv_path: Path, output_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if output_path != csv_path and output_path.exists():
        print(f"resuming from existing output CSV: {output_path}")
        return read_rows(output_path)
    return read_rows(csv_path)


def write_rows(csv_path: Path, fieldnames: list[str], rows: Iterable[dict[str, str]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp_path.replace(csv_path)


def format_token_count(value: int | None) -> str:
    return "" if value is None else f"{value:,}"


def format_cost(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.6f}".rstrip("0").rstrip(".")


def effective_model_name(model: str | None) -> str:
    return model or "opencode default"


def parse_opencode_token_usage(jsonl_text: str) -> dict[str, int]:
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
    }
    found_usage = False
    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        if event.get("type") != "step_finish":
            continue
        part = event.get("part")
        tokens = part.get("tokens") if isinstance(part, dict) else None
        if not isinstance(tokens, dict):
            continue

        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        values = {
            "input_tokens": tokens.get("input"),
            "cached_input_tokens": cache.get("read"),
            "cache_write_tokens": cache.get("write"),
            "output_tokens": tokens.get("output"),
            "reasoning_output_tokens": tokens.get("reasoning"),
        }
        for key, value in values.items():
            if isinstance(value, int):
                usage[key] += value
        found_usage = True

    if not found_usage:
        return {}
    usage["total_tokens"] = sum(
        usage[key]
        for key in [
            "input_tokens",
            "cached_input_tokens",
            "cache_write_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ]
    )
    return usage


def parse_opencode_cost(jsonl_text: str) -> float | None:
    total_cost = 0.0
    found_cost = False
    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "step_finish":
            continue
        part = event.get("part")
        cost = part.get("cost") if isinstance(part, dict) else None
        if isinstance(cost, (int, float)):
            total_cost += float(cost)
            found_cost = True

    return total_cost if found_cost else None


def parse_opencode_final_message(jsonl_text: str) -> str:
    messages: list[str] = []
    for line in jsonl_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part")
        text = part.get("text") if isinstance(part, dict) else None
        if isinstance(text, str) and text.strip():
            messages.append(text.strip())
    return "\n\n".join(messages)


def parse_opencode_errors(jsonl_text: str) -> str:
    errors: list[str] = []
    for line in jsonl_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "error":
            continue
        error = event.get("error")
        if isinstance(error, dict):
            data = error.get("data")
            message = data.get("message") if isinstance(data, dict) else None
            message = message or error.get("message")
            errors.append(
                str(message)
                if message
                else json.dumps(error, ensure_ascii=False)
            )
        elif error:
            errors.append(str(error))
    return "\n".join(errors)


def render_opencode_event_log(jsonl_text: str) -> str:
    sections: list[str] = []
    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")
        part = event.get("part")
        if event_type == "text" and isinstance(part, dict):
            text = part.get("text")
            if text:
                sections.append(str(text).rstrip())
        elif event_type == "tool_use" and isinstance(part, dict):
            state = part.get("state")
            state = state if isinstance(state, dict) else {}
            block = [f"tool: {part.get('tool', 'unknown')}"]
            tool_input = state.get("input")
            if tool_input is not None:
                block.append(json.dumps(tool_input, ensure_ascii=False, indent=2))
            output = state.get("output") or state.get("error")
            if output:
                block.append(str(output).rstrip())
            sections.append("\n".join(block))
        elif event_type == "step_finish" and isinstance(part, dict):
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                cache = tokens.get("cache")
                cache = cache if isinstance(cache, dict) else {}
                sections.append(
                    "tokens used\n"
                    f"input: {format_token_count(tokens.get('input'))}\n"
                    f"cache read: {format_token_count(cache.get('read'))}\n"
                    f"cache write: {format_token_count(cache.get('write'))}\n"
                    f"output: {format_token_count(tokens.get('output'))}\n"
                    f"reasoning output: {format_token_count(tokens.get('reasoning'))}"
                )
        elif event_type == "error":
            error = event.get("error")
            if error:
                sections.append(f"error: {json.dumps(error, ensure_ascii=False)}")
    return "\n\n".join(section for section in sections if section).strip() + ("\n" if sections else "")

def build_prompt(
    row: dict[str, str],
    *,
    original_commit: str,
    backport_commit: str,
    backport_parent: str,
) -> str:
    title = row.get("Title", "").strip() or "TensorFlow backport"
    original_version = row.get("latest version", "").strip() or "the source TensorFlow version"
    backport_version = row.get("oldest version", "").strip() or "the target TensorFlow version"
    backport_type = row.get("Corrected Type", "").strip() or "unknown"
    new_version_patch = row["latest version changes"].strip()

    prompt_header = textwrap.dedent(
        f"""\
        You are an expert TensorFlow security backporting engineer.

        ## Task

        Your goal is to backport one TensorFlow security fix from the newer
        TensorFlow release `{original_version}` to the older TensorFlow release
        `{backport_version}`.

        The repository has already been checked out to the target base commit:

        ```text
        {backport_parent}
        ```

        This is the parent of the known backport commit and represents the
        target release before the fix was applied. Do not switch branches, do not
        check out any other ref, and do not commit your changes.

        ## Row information

        | Field | Value |
        | --- | --- |
        | Project | TensorFlow |
        | CVE / title | `{title}` |
        | Newer source release | `{original_version}` |
        | Newer source commit | `{original_commit}` |
        | Older target release | `{backport_version}` |
        | Target base commit | `{backport_parent}` |
        | Known backport commit | `{backport_commit}` |
        | Backport type | `{backport_type}` |

        The known backport commit is ground truth for evaluation only. Do not
        inspect it, do not check it out, and do not infer anything from it.

        ## New-version patch to backport

        The following is the exact unified diff introduced by the newer source
        commit `{original_commit}` on TensorFlow `{original_version}`. This diff
        is your sole source of truth for what changed.

        ```diff
        """
    )
    prompt_footer = textwrap.dedent(
        f"""\
        ```

        Do not look at commits after `{original_commit}` on the newer release to
        infer extra changes. Use git/source tools only to understand surrounding
        context in the checked-out older target release.

        ## Available custom tools

        Use these OpenCode tools when they help you make a precise backport:

        - `git_show(ref, context?)`: view commit message, stats, and patch
          context for a commit. You may use this on `{original_commit}` only.
        - `viewcode(ref, path, startline, endline)`: inspect source code at a
          specific ref. Use `ref="{backport_parent}"` for target-side code.
        - `locate_symbol(ref, symbol)`: find functions, classes, methods,
          kernels, ops, tests, or other symbols in TensorFlow at a ref.
        - `git_history(filepath, start_line, end_line, start_commit,
          end_commit)`: trace code-region history when a hunk may have moved or
          changed shape.
        - `validate(ref, patch, mode="hunk", revise_context?)`: validate that a
          generated hunk applies to `{backport_parent}`. Use hunk mode only.

        ## Workflow

        1. Study every file and hunk in the newer-version patch.
        2. Use the tools to locate the equivalent TensorFlow code in the checked
           out older target version. For renamed or moved logic, prefer
           `locate_symbol`, `viewcode`, and targeted history checks over broad
           searching.
        3. Construct the smallest correct backport that preserves the intent of
           `{original_commit}` while matching the APIs, file layout, and coding
           style of TensorFlow `{backport_version}`.
        4. Use `validate` with `mode="hunk"` and `ref="{backport_parent}"` for
           hunks whose context is uncertain. Do not run `validate` in full mode.
        5. Apply the final source changes directly in the current repository
           checkout. The benchmark will capture your resulting `git diff`.

        ## Constraints

        - Do not inspect or use the expected older-version patch.
        - Do not inspect or use `{backport_commit}`.
        - Do not change unrelated files.
        - Do not run expensive full builds or full validation.
        - Do not commit the changes.
        - If a hunk truly has no equivalent in this target version, leave it out
          and explain why in your final notes.

        ## Final response format

        After applying the source changes, end with this YAML block:

        ```yaml
        backport_result:
          status: success
          patch: |
            <full unified diff that you applied, or empty if need_not_ported>
          notes: >
            <one-line explanation of what was done or why it failed>
        ```

        Valid status values are `success`, `partial`, `failed`, and
        `need_not_ported`.

        Begin now.
        """
    )
    return prompt_header + new_version_patch + "\n" + prompt_footer


def run_opencode(
    *,
    worktree: Path,
    prompt: str,
    model: str | None,
    agent: str,
    variant: str | None,
    timeout: int,
    opencode_bin: str,
    log_dir: Path,
    row_number: int,
) -> tuple[int, str, str, str, dict[str, int], float | None]:
    command = [
        opencode_bin,
        "run",
        "--dir",
        str(worktree),
        "--agent",
        agent,
        "--format",
        "json",
        "--auto",
        "--title",
        f"backport-row-{row_number:04d}",
    ]
    if model:
        command.extend(["--model", model])
    if variant:
        command.extend(["--variant", variant])

    result = run(command, input_text=prompt, timeout=timeout)
    stdout_path = log_dir / "opencode-stdout.txt"
    stderr_path = log_dir / "opencode-stderr.txt"
    write_text(stdout_path, result.stdout)
    write_text(log_dir / "opencode-events.jsonl", result.stdout)
    write_text(
        log_dir / "opencode-readable-log.txt",
        render_opencode_event_log(result.stdout),
    )
    write_text(stderr_path, result.stderr)
    final_message = parse_opencode_final_message(result.stdout)
    error_output = result.stderr or parse_opencode_errors(result.stdout)
    return (
        result.returncode,
        result.stdout,
        error_output,
        final_message,
        parse_opencode_token_usage(result.stdout),
        parse_opencode_cost(result.stdout),
    )


def canonical_diff_after_applying(worktree: Path, patch_text: str, patch_path: Path) -> tuple[bool, str, str]:
    git_clean_reset(worktree)
    write_text(patch_path, patch_text)
    apply_result = run(
        ["git", "-C", str(worktree), "apply", "--whitespace=nowarn", str(patch_path)]
    )
    if apply_result.returncode != 0:
        return False, "", apply_result.stderr or apply_result.stdout
    return True, current_diff(worktree), ""


def normalize_diff_text(diff_text: str) -> str:
    normalized_lines: list[str] = []
    for raw_line in diff_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith("index "):
            continue
        if raw_line == r"\ No newline at end of file":
            continue
        normalized_lines.append(raw_line)
    return "\n".join(normalized_lines).strip()


def compare_to_expected(
    *,
    worktree: Path,
    generated_diff: str,
    expected_diff: str,
    log_dir: Path,
    row_number: int,
) -> tuple[bool, str]:
    if not generated_diff.strip():
        return False, "OpenCode produced no git diff."
    if not expected_diff.strip():
        return False, "CSV row has no expected oldest-version diff."

    generated_patch = log_dir / "generated.patch"
    expected_patch = log_dir / "expected.patch"
    write_text(generated_patch, generated_diff)
    write_text(expected_patch, expected_diff)

    expected_ok, expected_canonical, expected_error = canonical_diff_after_applying(
        worktree, expected_diff, expected_patch
    )
    if not expected_ok:
        if normalize_diff_text(generated_diff) == normalize_diff_text(expected_diff):
            return True, ""
        return (
            False,
            "Could not apply expected oldest-version diff, and generated diff does not "
            f"textually match it: {expected_error.strip()}",
        )

    generated_ok, generated_canonical, generated_error = canonical_diff_after_applying(
        worktree, generated_diff, generated_patch
    )
    if not generated_ok:
        return False, f"Could not re-apply OpenCode generated diff: {generated_error.strip()}"

    write_text(log_dir / "expected-canonical.patch", expected_canonical)
    write_text(log_dir / "generated-canonical.patch", generated_canonical)

    if expected_canonical == generated_canonical:
        return True, ""
    return False, "Generated code change differs from the expected oldest-version change."


def selected_indexes(total: int, start: int, limit: int | None) -> range:
    start_index = max(start - 1, 0)
    stop = total if limit is None else min(total, start_index + limit)
    return range(start_index, stop)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run OpenCode backport attempts for rows in tenser-flow.csv."
    )
    parser.add_argument("--csv", default="tenser-flow.csv", type=Path)
    parser.add_argument("--repo", default="tensorflow", type=Path)
    parser.add_argument("--output", type=Path, help="Output CSV path. Defaults to updating --csv.")
    parser.add_argument(
        "--model",
        default=os.environ.get("OPENCODE_MODEL"),
        help=(
            "OpenCode model in provider/model format. Defaults to OPENCODE_MODEL, "
            "then OpenCode's configured default."
        ),
    )
    parser.add_argument(
        "--agent",
        default=os.environ.get("OPENCODE_AGENT", "build"),
        help="OpenCode primary agent to use (default: build).",
    )
    parser.add_argument(
        "--variant",
        default=os.environ.get("OPENCODE_VARIANT"),
        help="Optional provider-specific model variant/reasoning effort.",
    )
    parser.add_argument(
        "--opencode-bin",
        default=os.environ.get("OPENCODE_BIN", "opencode"),
    )
    parser.add_argument("--work-root", default=Path(".opencode-backport-worktrees"), type=Path)
    parser.add_argument("--log-dir", default=Path("opencode-backport-logs"), type=Path)
    parser.add_argument("--start", type=int, default=1, help="1-based CSV row number to start at.")
    parser.add_argument("--limit", type=int, help="Maximum number of rows to process.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Per-row OpenCode timeout in seconds.",
    )
    parser.add_argument(
        "--rerun-completed",
        action="store_true",
        help="Run rows even if the success/code-change columns are already populated.",
    )
    parser.add_argument(
        "--keep-worktrees",
        action="store_true",
        help="Do not remove per-row temporary worktrees after processing.",
    )
    args = parser.parse_args()

    require_command("git")
    opencode_bin = resolve_opencode_bin(args.opencode_bin)
    validate_opencode_cli(opencode_bin)
    if args.model and (
        "/" not in args.model
        or args.model.startswith("/")
        or args.model.endswith("/")
    ):
        parser.error("--model must use OpenCode's provider/model format")

    csv_path = args.csv.resolve()
    output_path = (args.output or args.csv).resolve()
    repo = args.repo.resolve()
    work_root = args.work_root.resolve()
    log_dir = args.log_dir.resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)

    fieldnames, rows = read_input_rows(csv_path, output_path)
    required_columns = [
        "latest version",
        "lastest version link",
        "oldest version",
        "oldest version link",
        "latest version changes",
        "oldest version changes",
    ]
    missing = [name for name in required_columns if name not in fieldnames]
    if missing:
        raise RuntimeError(f"CSV is missing required columns: {', '.join(missing)}")

    for column in [
        SUCCESS_COLUMN,
        CODE_CHANGE_COLUMN,
        MESSAGE_COLUMN,
        ERROR_COLUMN,
        PATCH_COMMIT_COLUMN,
        BASE_COMMIT_COLUMN,
        INPUT_TOKENS_COLUMN,
        CACHED_INPUT_TOKENS_COLUMN,
        CACHE_WRITE_TOKENS_COLUMN,
        OUTPUT_TOKENS_COLUMN,
        REASONING_OUTPUT_TOKENS_COLUMN,
        TOTAL_TOKENS_COLUMN,
        MODEL_COLUMN,
        COST_COLUMN,
    ]:
        if column not in fieldnames:
            fieldnames.append(column)

    for row_index in selected_indexes(len(rows), args.start, args.limit):
        row = rows[row_index]
        row_number = row_index + 1
        worktree: Path | None = None
        token_columns = [
            INPUT_TOKENS_COLUMN,
            CACHED_INPUT_TOKENS_COLUMN,
            CACHE_WRITE_TOKENS_COLUMN,
            OUTPUT_TOKENS_COLUMN,
            REASONING_OUTPUT_TOKENS_COLUMN,
            TOTAL_TOKENS_COLUMN,
        ]
        metadata_columns = [
            MODEL_COLUMN,
            COST_COLUMN,
        ]
        if (
            not args.rerun_completed
            and row.get(SUCCESS_COLUMN, "").strip()
            and row.get(CODE_CHANGE_COLUMN, "").strip()
            and all(row.get(column, "").strip() for column in token_columns)
            and all(row.get(column, "").strip() for column in metadata_columns)
        ):
            print(f"[{row_number}/{len(rows)}] already populated; skipping")
            continue

        title = row.get("Title", "").strip()
        print(f"[{row_number}/{len(rows)}] {title or row.get('oldest version', '').strip()}")

        try:
            row_log_dir = log_dir / f"row-{row_number:04d}"
            row_log_dir.mkdir(parents=True, exist_ok=True)

            original_commit = extract_commit_from_link(row["lastest version link"])
            backport_commit = extract_commit_from_link(row["oldest version link"])
            base_commit = first_parent_commit(repo, backport_commit)
            row[PATCH_COMMIT_COLUMN] = backport_commit
            row[BASE_COMMIT_COLUMN] = base_commit
            print(f"  base: {base_commit[:12]} (parent of patch {backport_commit[:12]})")

            worktree = work_root / f"row-{row_number:04d}-{base_commit[:12]}"
            prepare_worktree_path(repo, worktree, work_root)
            run(
                ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), base_commit],
                check=True,
            )

            prompt = build_prompt(
                row,
                original_commit=original_commit,
                backport_commit=backport_commit,
                backport_parent=base_commit,
            )
            code, _stdout, stderr, final_message, token_usage, cost = run_opencode(
                worktree=worktree,
                prompt=prompt,
                model=args.model,
                agent=args.agent,
                variant=args.variant,
                timeout=args.timeout,
                opencode_bin=opencode_bin,
                log_dir=row_log_dir,
                row_number=row_number,
            )
            generated_diff = current_diff(worktree)
            success, error = compare_to_expected(
                worktree=worktree,
                generated_diff=generated_diff,
                expected_diff=row["oldest version changes"],
                log_dir=row_log_dir,
                row_number=row_number,
            )
            if code != 0:
                success = False
                suffix = stderr.strip() or f"OpenCode exited with status {code}."
                error = f"{error} {suffix}".strip()

            row[SUCCESS_COLUMN] = "1" if success else "0"
            row[CODE_CHANGE_COLUMN] = generated_diff
            row[MESSAGE_COLUMN] = final_message
            row[ERROR_COLUMN] = error
            row[INPUT_TOKENS_COLUMN] = format_token_count(token_usage.get("input_tokens"))
            row[CACHED_INPUT_TOKENS_COLUMN] = format_token_count(token_usage.get("cached_input_tokens"))
            row[CACHE_WRITE_TOKENS_COLUMN] = format_token_count(token_usage.get("cache_write_tokens"))
            row[OUTPUT_TOKENS_COLUMN] = format_token_count(token_usage.get("output_tokens"))
            row[REASONING_OUTPUT_TOKENS_COLUMN] = format_token_count(token_usage.get("reasoning_output_tokens"))
            row[TOTAL_TOKENS_COLUMN] = format_token_count(token_usage.get("total_tokens"))
            row[MODEL_COLUMN] = effective_model_name(args.model)
            row[COST_COLUMN] = format_cost(cost)
            print(f"  result: {row[SUCCESS_COLUMN]}{f' ({error})' if error else ''}")
        except subprocess.TimeoutExpired:
            row[SUCCESS_COLUMN] = "0"
            row[CODE_CHANGE_COLUMN] = ""
            row[MESSAGE_COLUMN] = ""
            row[ERROR_COLUMN] = f"OpenCode timed out after {args.timeout} seconds."
            row[INPUT_TOKENS_COLUMN] = ""
            row[CACHED_INPUT_TOKENS_COLUMN] = ""
            row[CACHE_WRITE_TOKENS_COLUMN] = ""
            row[OUTPUT_TOKENS_COLUMN] = ""
            row[REASONING_OUTPUT_TOKENS_COLUMN] = ""
            row[TOTAL_TOKENS_COLUMN] = ""
            row[MODEL_COLUMN] = effective_model_name(args.model)
            row[COST_COLUMN] = ""
            print(f"  result: 0 ({row[ERROR_COLUMN]})")
        except Exception as exc:
            row[SUCCESS_COLUMN] = "0"
            row[CODE_CHANGE_COLUMN] = ""
            row[MESSAGE_COLUMN] = ""
            row[ERROR_COLUMN] = str(exc)
            row[INPUT_TOKENS_COLUMN] = ""
            row[CACHED_INPUT_TOKENS_COLUMN] = ""
            row[CACHE_WRITE_TOKENS_COLUMN] = ""
            row[OUTPUT_TOKENS_COLUMN] = ""
            row[REASONING_OUTPUT_TOKENS_COLUMN] = ""
            row[TOTAL_TOKENS_COLUMN] = ""
            row[MODEL_COLUMN] = effective_model_name(args.model)
            row[COST_COLUMN] = ""
            print(f"  result: 0 ({exc})")
        finally:
            if worktree is not None and worktree.exists() and not args.keep_worktrees:
                run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)])

        write_rows(output_path, fieldnames, rows)

    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
