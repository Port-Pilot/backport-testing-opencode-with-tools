#!/usr/bin/env python3
"""
validate - Validate a generated patch against a target git ref.

This script is intentionally standalone for the TensorFlow backport dataset.
The earlier version depended on PortGPT's `tools.utils` package and a Java/kernel
benchmark layout (`build.sh`, `test.sh`, `poc.sh`, Docker image). Those
dependencies are not available in this OpenCode setup and caused validation to
fail before `git apply` was reached.

Usage:
    python validate.py --repo /path/to/git/repo --ref <target_ref> \
        --patch_file <path_to_patch> --mode <hunk|full> [--err_msg <text>]

Modes:
    hunk: Run `git apply --check` for a patch/hunk against <ref> in an isolated
          temporary worktree. This is the mode used by the TensorFlow runner.
    full: Apply the complete patch in an isolated temporary worktree, then run
          optional validation commands supplied by environment variables:
          VALIDATE_BUILD_CMD, VALIDATE_TEST_CMD, VALIDATE_POC_CMD.
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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


def run_shell(command: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        shell=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def command_output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "").strip()


def ensure_git_repo(repo_dir: Path) -> None:
    result = run(["git", "-C", str(repo_dir), "rev-parse", "--git-dir"])
    if result.returncode != 0:
        raise RuntimeError(f"Not a git repository: {repo_dir}")


def ensure_ref(repo_dir: Path, ref: str) -> None:
    result = run(["git", "-C", str(repo_dir), "rev-parse", "--verify", f"{ref}^{{commit}}"])
    if result.returncode != 0:
        raise RuntimeError(f"Target ref is not a commit in this repo: {ref}")


def add_temp_worktree(repo_dir: Path, ref: str) -> Path:
    temp_root = Path(tempfile.mkdtemp(prefix="opencode-validate-"))
    worktree = temp_root / "repo"
    result = run(
        ["git", "-C", str(repo_dir), "worktree", "add", "--detach", str(worktree), ref]
    )
    if result.returncode != 0:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise RuntimeError(f"Failed to create temporary worktree: {command_output(result)}")
    return worktree


def remove_temp_worktree(repo_dir: Path, worktree: Path) -> None:
    run(["git", "-C", str(repo_dir), "worktree", "remove", "--force", str(worktree)])
    shutil.rmtree(worktree.parent, ignore_errors=True)


def summarize_apply_failure(patch: str, git_error: str) -> str:
    touched_files: list[str] = []
    for line in patch.splitlines():
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            path = line[6:].strip()
            if path != "/dev/null" and path not in touched_files:
                touched_files.append(path)

    ret = []
    ret.append("This patch does not apply to the target TensorFlow ref.")
    ret.append("Git apply output:")
    ret.append(git_error or "<no git error output>")
    if touched_files:
        ret.append("Touched files:")
        ret.extend(f"- {path}" for path in touched_files)
    ret.append(
        "Fix the patch context against the older TensorFlow version, then run "
        "validate again in hunk mode."
    )
    return "\n".join(ret) + "\n"


def git_apply_check(worktree: Path, patch: str) -> tuple[bool, str]:
    result = run(["git", "apply", "--check", "--verbose", "-"], cwd=worktree, input_text=patch)
    return result.returncode == 0, command_output(result)


def git_apply(worktree: Path, patch: str) -> tuple[bool, str]:
    result = run(["git", "apply", "--verbose", "-"], cwd=worktree, input_text=patch)
    return result.returncode == 0, command_output(result)


def validate_hunk(worktree: Path, patch: str) -> str:
    ok, output = git_apply_check(worktree, patch)
    if ok:
        return "Patch applied successfully\n"
    return summarize_apply_failure(patch, output)


def run_optional_step(name: str, env_var: str, worktree: Path, timeout: int) -> tuple[bool, str]:
    command = os.environ.get(env_var, "").strip()
    if not command:
        return True, f"No {name} command configured ({env_var}); skipped.\n"

    try:
        result = run_shell(command, worktree, timeout)
    except subprocess.TimeoutExpired:
        return False, f"The {name} command timed out after {timeout} seconds: {command}\n"

    output = command_output(result)
    if result.returncode != 0:
        return False, f"The {name} command failed: {command}\n{output}\n"
    return True, f"The {name} command passed: {command}\n{output}\n"


def validate_full(worktree: Path, patch: str, err_msg: str) -> str:
    ok, output = git_apply(worktree, patch)
    if not ok:
        return summarize_apply_failure(patch, output)

    ret = ["Patch applied successfully\n"]
    for name, env_var, timeout in [
        ("build", "VALIDATE_BUILD_CMD", 90 * 60),
        ("test", "VALIDATE_TEST_CMD", 60 * 60),
        ("PoC", "VALIDATE_POC_CMD", 30 * 60),
    ]:
        ok, message = run_optional_step(name, env_var, worktree, timeout)
        ret.append(message)
        if not ok:
            return "".join(ret)
        if name == "PoC" and err_msg and err_msg in message:
            ret.append("Existing PoC could still trigger the expected bug message.\n")
            return "".join(ret)

    return "".join(ret)


def validate(
    repo_dir: str,
    ref: str,
    patch_file: str,
    mode: str,
    err_msg: str,
    revise_context: bool,
) -> str:
    repo_path = Path(repo_dir).resolve()
    patch_path = Path(patch_file).resolve()
    ensure_git_repo(repo_path)
    ensure_ref(repo_path, ref)

    patch = patch_path.read_text(encoding="utf-8", errors="replace")
    if "need not ported" in patch:
        return "Patch marked as 'need not ported'. Validated successfully.\n"
    if revise_context:
        return (
            "revise_context is not supported by the standalone TensorFlow validator. "
            "Run without revise_context and correct the patch context directly.\n"
        )

    worktree = add_temp_worktree(repo_path, ref)
    try:
        if mode == "hunk":
            return validate_hunk(worktree, patch)
        if mode == "full":
            return validate_full(worktree, patch, err_msg)
        return f"Unknown mode {mode}\n"
    finally:
        remove_temp_worktree(repo_path, worktree)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a patch against a git ref")
    parser.add_argument("--repo", type=str, required=True, help="Path to the local git repository")
    parser.add_argument("--ref", type=str, required=True, help="Target git ref")
    parser.add_argument("--patch_file", type=str, required=True, help="Path to file containing the patch string")
    parser.add_argument("--mode", type=str, choices=["hunk", "full"], required=True, help="Validation mode")
    parser.add_argument("--err_msg", type=str, default="", help="Expected error message from PoC")
    parser.add_argument("--revise_context", action="store_true", help="Unsupported compatibility flag")

    args = parser.parse_args()
    try:
        result = validate(
            args.repo,
            args.ref,
            args.patch_file,
            args.mode,
            args.err_msg,
            args.revise_context,
        )
    except Exception as exc:
        result = f"Error executing validate: {exc}\n"
    print(result)


if __name__ == "__main__":
    main()
