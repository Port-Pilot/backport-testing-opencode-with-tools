#!/usr/bin/env python3
"""
locate_symbol — Locate a symbol in a git ref.

This is a self-contained re-implementation of PortGPT's locate_symbol tool logic
for use as an OpenCode custom tool. It uses ctags to build a symbol map and
Levenshtein distance to find similar symbols if the exact one is missing.

Usage:
    python locate_symbol.py --repo /path/to/git/repo --ref <commit_hash> --symbol <symbol_name>
"""

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from git import Repo

# Ensure stdout uses UTF-8 encoding (fixes Windows cp1252 issues)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def levenshtein_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            replace_cost = previous[j - 1] + (left_char != right_char)
            current.append(min(insert_cost, delete_cost, replace_cost))
        previous = current
    return previous[-1]


def run_git_grep(repo_dir: Path, ref: str, symbol: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "grep", "-n", "-I", "--no-color", "-w", symbol, ref],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return (
            f"git grep timed out after 30 seconds while searching for {symbol}. "
            "Use viewcode/grep with a narrower file path, or increase the "
            "locate_symbol timeout for whole-repository symbol search."
        )
    if result.returncode not in (0, 1):
        return None

    candidates = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) != 4:
            continue
        _, file_path, lineno_text, content = parts
        try:
            lineno = int(lineno_text)
        except ValueError:
            continue

        stripped = content.strip()
        score = -1
        escaped = re.escape(symbol)
        if re.search(rf"\b(class|struct|enum)\s+{escaped}\b", stripped):
            score = 100
        elif re.search(rf"\b{escaped}\s*\(", stripped):
            score = 70 if stripped.endswith(";") else 90
        elif re.search(rf"\b{escaped}\b", stripped):
            score = 20

        if score >= 0 and not stripped.startswith(("//", "/*", "*")):
            candidates.append((score, file_path, lineno))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    seen = set()
    lines = []
    for _, file_path, lineno in candidates:
        key = (file_path, lineno)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{file_path}:{lineno}")
        if len(lines) >= 20:
            break

    return "\n".join(lines)


def find_ctags() -> str | None:
    return shutil.which("ctags")


def add_temp_worktree(repo_dir: Path, ref: str) -> Path:
    temp_root = Path(tempfile.mkdtemp(prefix="opencode-locate-symbol-"))
    worktree = temp_root / "repo"
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "add", "--detach", str(worktree), ref],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        shutil.rmtree(temp_root, ignore_errors=True)
        output = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Failed to create temporary worktree for ref {ref}: {output}")
    return worktree


def remove_temp_worktree(repo_dir: Path, worktree: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "remove", "--force", str(worktree)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    shutil.rmtree(worktree.parent, ignore_errors=True)


def locate_symbol(repo_dir: str, ref: str, symbol: str) -> str:
    repo_path = Path(repo_dir).resolve()
    try:
        Repo(repo_path)
    except Exception as e:
        return f"Invalid git repository {repo_path}: {e}"

    grep_result = run_git_grep(repo_path, ref, symbol)
    if grep_result:
        return grep_result

    ctags_path = find_ctags()
    if not ctags_path:
        return (
            "Failed to find ctags. Install Universal Ctags and make sure ctags.exe "
            "is on PATH, or install it with `winget install --id UniversalCtags.Ctags`."
        )

    try:
        worktree = add_temp_worktree(repo_path, ref)
    except Exception as e:
        return str(e)

    try:
        ctags = subprocess.run(
            [ctags_path, "--excmd=number", "-R", "."],
            stdout=subprocess.PIPE,
            cwd=worktree,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if ctags.returncode != 0:
            return f"Failed to run ctags at {ctags_path}: {(ctags.stderr or ctags.stdout).strip()}"

        symbol_map = {}
        tags_file = worktree / "tags"
        if not tags_file.exists():
            return "Failed to generate tags file."

        with tags_file.open("rb") as f:
            for line in f.readlines():
                text = line.decode("utf-8", errors="ignore")
                if text and not text.startswith("!_TAG_"):
                    try:
                        parts = text.strip().split(';"')[0].split("\t")
                        sym, file_path, lineno = parts[0], parts[1], int(parts[2])
                        if sym not in symbol_map:
                            symbol_map[sym] = []
                        symbol_map[sym].append((file_path, lineno))
                    except Exception:
                        continue

        if symbol in symbol_map:
            res = symbol_map[symbol]
            return "\n".join([f"{f}:{line}" for f, line in res])

        most_similar = None
        smallest_distance = float("inf")
        for symbol_i in symbol_map.keys():
            distance = levenshtein_distance(symbol, symbol_i)
            if distance < smallest_distance:
                smallest_distance = distance
                most_similar = symbol_i

        if most_similar:
            res = symbol_map[most_similar]
            ret = f"The symbol {symbol} you are looking for does not exist in the current ref.\n"
            ret += f"But here is a symbol similar to it. It's `{most_similar}`.\n"
            ret += "The file where this symbol is located is: \n"
            ret += "\n".join([f"{f}:{line}" for f, line in res])
            ret += "\nPlease be careful to check that this symbol indicates the same thing as the previous symbol.\n"
            return ret

        return f"No similar symbols found for {symbol}."
    except subprocess.TimeoutExpired:
        return (
            f"No exact git-grep matches found for {symbol}. ctags fallback timed out "
            "after 60 seconds while indexing this repository. Use grep/viewcode for "
            "targeted search or increase the locate_symbol timeout for full indexing."
        )
    finally:
        remove_temp_worktree(repo_path, worktree)


def main():
    parser = argparse.ArgumentParser(
        description="Locate a symbol in a specific git ref",
    )
    parser.add_argument(
        "--repo",
        type=str,
        required=True,
        help="Path to the local git repository",
    )
    parser.add_argument(
        "--ref",
        type=str,
        required=True,
        help="Git commit hash or ref to search in",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        required=True,
        help="The symbol name to locate",
    )

    args = parser.parse_args()
    result = locate_symbol(args.repo, args.ref, args.symbol)
    print(result)


if __name__ == "__main__":
    main()
