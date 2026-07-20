#!/usr/bin/env python3
"""
viewcode — View source code from a specific git ref.

This is a self-contained re-implementation of PortGPT's viewcode tool logic
for use as an OpenCode custom tool. It does NOT depend on the PortGPT
Project class — all logic is implemented here directly.

Usage:
    python viewcode.py --repo /path/to/git/repo --ref <commit_hash> --path <file_path> --startline <N> --endline <M>

The script uses GitPython to read a file blob at a specific git ref
and returns the requested line range with boundary handling.
"""

import argparse
import io
import json
import sys

# Ensure stdout uses UTF-8 encoding (fixes Windows cp1252 issues)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from git import Repo


def viewcode(repo_dir: str, ref: str, path: str, startline: int, endline: int) -> str:
    """
    View a file from a specific ref of a git repository.
    Lines between startline and endline (inclusive, 1-indexed) are shown.

    Args:
        repo_dir: Path to the local git repository.
        ref: Git commit hash or ref to view the file from.
        path: Relative path of the file from the repository root.
        startline: The starting line number to display (1-indexed).
        endline: The ending line number to display (1-indexed).

    Returns:
        The content of the file between the specified startline and endline,
        with line numbers and boundary handling messages.
    """
    repo = Repo(repo_dir)

    # Try to access the file at the given ref
    try:
        file_blob = repo.tree(ref) / path
    except (KeyError, ValueError):
        return "This file doesn't exist in this commit."

    # Read and decode the file contents
    content = file_blob.data_stream.read().decode("utf-8", errors="ignore")
    lines = content.split("\n")

    ret = []

    if not lines:
        return "This file is empty.\n"

    # Swap if startline > endline
    if startline > endline:
        startline, endline = endline, startline

    # Clamp startline to at least 1
    startline = max(1, startline)

    # Handle boundary conditions
    if startline > len(lines):
        ret.append(
            f"This file only has {len(lines)} lines. Showing full file.\n"
        )
        startline = 1
        endline = len(lines)
    elif endline > len(lines):
        endline = len(lines)
        ret.append(
            f"This file only has {len(lines)} lines. Here are lines {startline} through {endline}.\n"
        )
    else:
        ret.append(f"Here are lines {startline} through {endline}.\n")

    # Extract the requested line range (1-indexed to 0-indexed)
    for i in range(startline - 1, endline):
        ret.append(lines[i])

    return (
        "\n".join(ret)
        + "\nBased on the previous information, think carefully do you see the target code? You may want to keep checking if you don't.\n"
    )


def main():
    parser = argparse.ArgumentParser(
        description="View source code from a specific git ref",
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
        help="Git commit hash or ref to view the file from",
    )
    parser.add_argument(
        "--path",
        type=str,
        required=True,
        help="Relative path of the file from the repository root",
    )
    parser.add_argument(
        "--startline",
        type=int,
        required=True,
        help="First line to display (1-indexed)",
    )
    parser.add_argument(
        "--endline",
        type=int,
        required=True,
        help="Last line to display (1-indexed)",
    )

    args = parser.parse_args()

    result = viewcode(args.repo, args.ref, args.path, args.startline, args.endline)
    print(result)


if __name__ == "__main__":
    main()
