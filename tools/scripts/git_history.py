#!/usr/bin/env python3
"""
git_history — Get change history for a code region.

This is a self-contained re-implementation of PortGPT's git_history tool logic
for use as an OpenCode custom tool. Since OpenCode tools are stateless, the LLM
must provide the file path, line range, and commit range to query.

Usage (explicit start commit):
    python git_history.py --repo /path/to/git/repo --filepath <path> --start_line <N> --end_line <M> --start_commit <ref> --end_commit <ref>

Usage (auto merge-base, mirrors _git_history exactly):
    python git_history.py --repo /path/to/git/repo --filepath <path> --start_line <N> --end_line <M> --target_release <ref> --end_commit <ref>

When --target_release is provided, the script computes the merge base between
--target_release and --end_commit automatically, just like _git_history() does in
project.py. This is the recommended usage to avoid passing a wrong start commit.
"""

import argparse
import io
import sys

from git import Repo

# Ensure stdout uses UTF-8 encoding (fixes Windows cp1252 issues)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def get_git_history(
    repo_dir: str,
    filepath: str,
    start_line: int,
    end_line: int,
    end_commit: str,
    start_commit: str | None = None,
    target_release: str | None = None,
) -> str:
    """
    Get the git log -L history for a code region.

    Mirrors _git_history() in project.py.  Either start_commit or target_release
    must be supplied:

    - target_release: the script computes merge_base(target_release, end_commit)
      automatically, which is identical to what _git_history() does.
    - start_commit: used directly as the range start (caller is responsible for
      providing the correct value, ideally the merge base).
    """
    repo = Repo(repo_dir)

    # Resolve the range start — prefer automatic merge-base computation.
    if target_release is not None:
        merge_bases = repo.merge_base(target_release, end_commit)
        if not merge_bases:
            return f"Failed to compute merge base between {target_release} and {end_commit}."
        range_start = merge_bases[0].hexsha
    elif start_commit is not None:
        range_start = start_commit
    else:
        return "Either --start_commit or --target_release must be provided."

    try:
        log_message = repo.git.log(
            "--oneline",
            f"-L {start_line},{end_line}:{filepath}",
            f"{range_start}..{end_commit}",
        )
    except Exception as e:
        return f"Failed to get git history: {e}"

    if not log_message:
        return "No history found for this code region."

    # Return logic matches _git_history in project.py exactly.
    ret = log_message[len(log_message) - 5001 : -1]
    ret += "\nYou need to do the following analysis based on the information in the last commit:\n"
    ret += "Analyze the code logic of the context of the patch to be ported in this commit step by step.\n"
    ret += "If code logic already existed before this commit, the patch context can be assumed to remain in a similar location. Use `locate` and `viewcode` to check your results.\n"
    ret += "If code logic were added in this commit, then you need to `git_show` for further details.\n"
    return ret


def main():
    parser = argparse.ArgumentParser(
        description="Get change history for a code region in a git repository",
    )
    parser.add_argument(
        "--repo",
        type=str,
        required=True,
        help="Path to the local git repository",
    )
    parser.add_argument(
        "--filepath",
        type=str,
        required=True,
        help="Path of the file to trace",
    )
    parser.add_argument(
        "--start_line",
        type=int,
        required=True,
        help="Start line number of the code region",
    )
    parser.add_argument(
        "--end_line",
        type=int,
        required=True,
        help="End line number of the code region",
    )
    parser.add_argument(
        "--end_commit",
        type=str,
        required=True,
        help="End commit hash (e.g. new patch parent)",
    )

    # Mutually exclusive: auto merge-base vs. explicit start commit.
    start_group = parser.add_mutually_exclusive_group(required=True)
    start_group.add_argument(
        "--target_release",
        type=str,
        help=(
            "Target release ref. The script will compute merge_base(target_release, end_commit) "
            "automatically, mirroring _git_history() in project.py. Preferred over --start_commit."
        ),
    )
    start_group.add_argument(
        "--start_commit",
        type=str,
        help="Explicit start commit hash. Use the merge base for correct results.",
    )

    args = parser.parse_args()
    result = get_git_history(
        args.repo,
        args.filepath,
        args.start_line,
        args.end_line,
        args.end_commit,
        start_commit=args.start_commit,
        target_release=args.target_release,
    )
    print(result)


if __name__ == "__main__":
    main()
