#!/usr/bin/env python3
"""
git_show — Show commit details and context abstract for a specific ref.

Stateless re-implementation of _git_show() in project.py for use as an
OpenCode custom tool.

_git_show() relies on three pieces of class state that the LLM must supply:
  - ref          : the commit hash to show (taken from hunk_log_info[-1])
  - context      : the last-seen context lines from the current hunk (last_context)
  - add_percent  : fraction of '+' lines in the last git-history result (add_percent)

Usage:
    python git_show.py --repo /path/to/repo --ref <commit_hash> \\
        [--context <newline-separated context lines>] \\
        [--add_percent <float 0.0-1.0>]
"""

import argparse
import io
import re
import sys
from typing import Generator, List, Tuple

# Ensure stdout uses UTF-8 encoding (fixes Windows cp1252 issues)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BLACKLIST = [
    ".rst",
    ".yaml",
    ".yml",
    ".md",
    ".tcl",
    "CHANGES",
    "ANNOUNCE",
    "NEWS",
    ".pem",
    ".js",
    ".sha1",
    ".sha256",
    ".uuid",
    ".test",
    "manifest",
    ".xml",
    "_test.go",
    ".json",
    ".golden",
    ".txt",
    ".mdx",
]


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


def find_most_similar_block(
    pattern: List[str], main: List[str], p_len: int, dline_flag: bool = False
) -> Tuple[int, int]:
    min_distance = float("inf")
    best_start_index = 1

    if p_len <= 0 or not main:
        return best_start_index, 0

    for i in range(max(len(main) - p_len + 1, 1)):
        distance = levenshtein_distance(
            "\n".join(main[i : i + p_len]), "\n".join(pattern)
        )
        if distance < min_distance and not (
            dline_flag and i < len(main) and (main[i].startswith("+") or main[i].startswith("-"))
        ):
            min_distance = distance
            best_start_index = i + 1

    if not dline_flag:
        offset_flag = False
        offset = float("inf")
        lineno = best_start_index
        for i in range(min(p_len, len(pattern))):
            if len(pattern[i].strip()) < 3:
                continue
            for j in range(-5, 6):
                main_index = lineno - 1 + j
                if 0 <= main_index < len(main) and pattern[i].strip() == main[main_index].strip():
                    offset_flag = True
                    if abs(j - i) < abs(offset):
                        offset = j - i
            if offset_flag:
                best_start_index += offset
                break

    if min_distance == float("inf"):
        return best_start_index, 0
    return best_start_index, int(min_distance)


def extract_context(lines: list[str]) -> Tuple[list[str], int, list[str], int]:
    processed_lines = []
    add_lines = []
    for line in lines:
        if line.startswith(" "):
            processed_lines.append(line[1:])
        elif line.startswith("-"):
            processed_lines.append(line[1:])
        elif line.startswith("+"):
            add_lines.append(line[1:])
    return processed_lines, len(processed_lines), add_lines, len(add_lines)


def split_patch(patch: str, flag_commit: bool) -> Generator[str, None, None]:
    def split_block(lines: list[str]) -> Generator[str, None, None]:
        if len(lines) < 2:
            return
        file_path_line_a = lines[0]
        file_path_line_b = lines[1]
        last_line = -1
        for line_no in range(2, len(lines)):
            if lines[line_no].startswith("@@"):
                if last_line != -1:
                    yield (
                        file_path_line_a
                        + "\n"
                        + file_path_line_b
                        + "\n"
                        + "\n".join(lines[last_line:line_no])
                    )
                last_line = line_no
        if last_line != -1:
            yield (
                file_path_line_a
                + "\n"
                + file_path_line_b
                + "\n"
                + "\n".join(lines[last_line:])
            )

    lines = patch.splitlines()
    message = ""
    last_line = -1
    for line_no, line in enumerate(lines):
        if line.startswith("--- a/"):
            if last_line >= 0:
                block_end = line_no - 2 if flag_commit else line_no
                yield from split_block(lines[last_line:block_end])
            if last_line == -1 and flag_commit:
                message = "\n".join(lines[: max(line_no - 2, 0)])
            last_line = -2 if any(line.endswith(item) for item in BLACKLIST) else line_no
        elif line.startswith("--- /dev/null"):
            if last_line >= 0:
                block_end = line_no - 3 if flag_commit else line_no
                yield from split_block(lines[last_line:block_end])
            if last_line == -1 and flag_commit:
                message = "\n".join(lines[: max(line_no - 3, 0)])
            next_line = lines[line_no + 1] if line_no + 1 < len(lines) else ""
            last_line = -2 if any(next_line.endswith(item) for item in BLACKLIST) else line_no
    if last_line >= 0:
        for block in split_block(lines[last_line:]):
            yield message + block


def git_show(repo_dir: str, ref: str, context: str | None = None, add_percent: float = 1.0) -> str:
    """
    Show commit details and generate a context abstract, mirroring _git_show() in project.py.

    Args:
        repo_dir    : Path to the local git repository.
        ref         : Commit hash to show (the last entry from hunk_log_info).
        context     : Newline-separated string of context lines from the hunk (last_context).
                      If omitted, only the stat + raw diff snippet is returned.
        add_percent : Fraction of '+' lines in the last git_history result (add_percent).
                      Default 1.0 (all added lines — the normal/optimistic assumption).
                      Pass the actual value from the git_history step when available.

    Returns:
        Formatted string matching _git_show() output.
    """
    from git import Repo
    repo = Repo(repo_dir)

    try:
        log = repo.git.show(f"{ref}")
        stat = repo.git.show("--stat", f"{ref}")

        # --- Step 1: stat header (same as _git_show: stat[0:min(len,3000)]) ---
        ret = ""
        ret += stat[0 : min(len(stat), 3000)]
        ret += "\n"

        # --- Step 2: if no context, fall back gracefully ---
        if not context:
            ret += "\n--- Commit Details ---\n"
            ret += log[0:4000]
            if len(log) > 4000:
                ret += "\n... (commit truncated due to length) ...\n"
            return ret

        # --- Step 3: find most similar block in the commit (mirrors _git_show loop) ---
        pps = split_patch(log, False)
        dist = float("inf")
        last_context = context.split("\n")   # equivalent to self.last_context (already a list)
        last_context_len = len(last_context)
        best_context = []
        file_path = ""
        file_no = 0

        for idx, pp in enumerate(pps):
            try:
                file_path_i = re.findall(r"--- a/(.*)", pp)[0]
                chunks = re.findall(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@(.*)", pp)[0]
                contexts, _, _, _ = extract_context(pp.split("\n")[3:])
                if (int(chunks[1]) - int(chunks[3])) < last_context_len:
                    continue
                lineno, dist_i = find_most_similar_block(
                    last_context, contexts, last_context_len, False
                )
                if dist_i < dist:
                    best_context = contexts[lineno - 1 : lineno - 1 + last_context_len]
                    dist = dist_i
                    file_path = file_path_i
                    file_no = int(chunks[0]) + lineno - 1
            except:
                continue

        # --- Step 4: three-branch output — identical to _git_show ---
        if add_percent < 0.6:
            # _git_show branch 1: code was not purely added in this commit
            ret += f"[IMPORTANT] The relevant code shown by `git_history` is not fully `+` lines.\n"
            ret += f"[IMPORTANT] This means that the code in question was not added or migrated in this commit.\n"
            ret += f"[IMPORTANT] Please think step by step and check the abstract below carefully. If error exists in abstract, please ignore the info below.\n"
        elif best_context:
            # _git_show branch 2: found a matching context block
            ret += f"Because the commit's code change maybe too long, so I generate the abstract of the code change to show you how code changed in this commit.\n"
            ret += f"Commit shows that the patch code in old version maybe in the file {file_path} around line number {file_no} to {file_no + last_context_len}. The code is below\n"
            code_snippets = "\n".join(best_context)
            ret += f"{code_snippets}"
            ret += f"\nYou can call `viewcode` and `locate_symbol` to find the relevant code based on this information step by step."
        else:
            # _git_show branch 3: code is likely new
            ret += f"This commit shows that there is a high probability that this code is new, so the corresponding code segment cannot be found in the old version.\n"
            ret += f"You can call `viewcode` and `locate_symbol` to further check the results step by step. For newly introduced code, we consider that this hunk `need not ported`.\n"

        return ret

    except:
        return "Something error, maybe you don't use git_history before or git_history is empty."


def main():
    parser = argparse.ArgumentParser(
        description="Show commit details and context abstract for a specific ref",
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
        help="Git commit hash or ref to show (last entry from hunk_log_info)",
    )
    parser.add_argument(
        "--context",
        type=str,
        required=False,
        help=(
            "Newline-separated context lines from the current hunk (last_context). "
            "Pass the context lines you are trying to locate in the commit."
        ),
    )
    parser.add_argument(
        "--add_percent",
        type=float,
        required=False,
        default=1.0,
        help=(
            "Fraction of '+' lines in the last git_history result (0.0–1.0). "
            "When below 0.6, the IMPORTANT warning branch is taken, identical to _git_show(). "
            "Defaults to 1.0 (all added lines)."
        ),
    )

    args = parser.parse_args()

    # Handle literal \\n if context is passed via command line
    context = args.context.replace("\\n", "\n") if args.context else None

    result = git_show(args.repo, args.ref, context, args.add_percent)
    print(result)


if __name__ == "__main__":
    main()
