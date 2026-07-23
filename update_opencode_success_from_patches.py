#!/usr/bin/env python3
"""Mark OpenCode backport results successful using an OpenCode AI review.

The script reads rows from tenser-flow-results.csv where
opencode_backport_success is 0, maps each CSV Index to
opencode-backport-logs/row-####, compares expected.patch and generated.patch,
and asks OpenCode whether the generated patch is technically acceptable.

Rows stay 0 only when the generated patch is totally different from the expected
fix or incorrect. Otherwise, the row is updated to 1.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


SUCCESS_COLUMN = "opencode_backport_success"
INDEX_COLUMN_CANDIDATES = ("Index", "index", "", "Unnamed: 0")


def find_index_column(fieldnames: list[str]) -> str | None:
    for candidate in INDEX_COLUMN_CANDIDATES:
        if candidate in fieldnames:
            return candidate
    return None


def log_dir_for_row(
    logs_dir: Path,
    row: dict[str, str],
    index_column: str | None,
    row_number: int,
) -> Path:
    index = int(row[index_column]) if index_column is not None else row_number
    return logs_dir / f"row-{index:04d}"


def opencode_command_prefix(opencode_command: str) -> list[str]:
    resolved = shutil.which(opencode_command) or opencode_command
    if sys.platform == "win32" and resolved.lower().endswith(".ps1"):
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            resolved,
        ]
    return [resolved]


def opencode_patch_judgement(
    opencode_command: str,
    expected_patch: Path,
    generated_patch: Path,
    *,
    model: str | None,
    timeout: int,
) -> bool:
    row_name = expected_patch.parent.name
    cwd = Path.cwd()
    try:
        expected_prompt_path = expected_patch.relative_to(cwd)
    except ValueError:
        expected_prompt_path = expected_patch
    try:
        generated_prompt_path = generated_patch.relative_to(cwd)
    except ValueError:
        generated_prompt_path = generated_patch

    prompt = f"""You are judging {row_name}.

Decision rule:
- Answer 1 if the generated patch is technically the same fix, equivalent, partly different but still correct, or acceptable.
- Answer 0 only if the generated patch is totally different from the expected fix or the generated patch is incorrect.

Read exactly these two files:
- EXPECTED_REFERENCE_PATCH: {expected_prompt_path}
- GENERATED_BACKPORT_PATCH: {generated_prompt_path}

The expected.patch file is the correct target/reference backport context.
The generated.patch file is the generated backport patch being judged.

Focus on the code added and removed. Ignore commit hashes, hunk line numbers, diff metadata, formatting-only differences, or context-only differences.

Return exactly one character: 1 or 0. Do not explain.
"""
    command = [
        *opencode_command_prefix(opencode_command),
        "run",
        prompt,
    ]
    if model:
        command.extend(["--model", model])

    attempts = 3
    for attempt in range(1, attempts + 1):
        result = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        combined_output = f"{result.stdout}\n{result.stderr}"
        if result.returncode == 0 or "database is locked" not in combined_output.lower():
            break
        if attempt < attempts:
            time.sleep(5 * attempt)

    if result.returncode != 0:
        raise RuntimeError(
            f"opencode failed for {expected_patch.parent.name} with exit code {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    output = result.stdout.strip()
    match = re.search(r"\b([01])\b", output)
    if not match:
        raise RuntimeError(
            f"opencode did not return 1 or 0 for {expected_patch.parent.name}: {output!r}"
        )
    return match.group(1) == "1"


def update_csv(
    csv_path: Path,
    logs_dir: Path,
    *,
    dry_run: bool,
    backup: bool,
    opencode_command: str,
    model: str | None,
    timeout: int,
) -> tuple[int, int, list[str], Path]:
    with csv_path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        fieldnames = reader.fieldnames

    if not fieldnames:
        raise RuntimeError(f"No CSV header found in {csv_path}")

    if SUCCESS_COLUMN not in fieldnames:
        raise RuntimeError(f"Missing required CSV column: {SUCCESS_COLUMN}")
    index_column = find_index_column(fieldnames)

    checked = 0
    updated = 0
    skipped: list[str] = []

    for row_number, row in enumerate(rows, start=1):
        if row.get(SUCCESS_COLUMN, "").strip() != "0":
            continue

        checked += 1
        try:
            log_dir = log_dir_for_row(logs_dir, row, index_column, row_number)
        except ValueError:
            value = row.get(index_column) if index_column is not None else str(row_number)
            skipped.append(f"Index {value!r}: invalid row index")
            continue

        expected_patch = log_dir / "expected.patch"
        generated_patch = log_dir / "generated.patch"
        if not expected_patch.is_file() or not generated_patch.is_file():
            skipped.append(f"{log_dir.name}: missing expected.patch or generated.patch")
            continue

        print(f"Checking {log_dir.name} with OpenCode...")
        try:
            should_mark_success = opencode_patch_judgement(
                opencode_command,
                expected_patch,
                generated_patch,
                model=model,
                timeout=timeout,
            )
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            skipped.append(f"{log_dir.name}: {error}")
            continue

        if should_mark_success:
            row[SUCCESS_COLUMN] = "1"
            updated += 1

    output_path = csv_path

    if updated and not dry_run:
        if backup:
            shutil.copy2(csv_path, csv_path.with_suffix(csv_path.suffix + ".bak"))

        temp_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
        with temp_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        try:
            os.replace(temp_path, csv_path)
        except PermissionError:
            output_path = csv_path.with_suffix(csv_path.suffix + ".updated.csv")
            if output_path.exists():
                output_path.unlink()
            temp_path.replace(output_path)
            skipped.append(
                f"{csv_path.name}: permission denied while replacing file; "
                f"wrote updated CSV to {output_path.name}"
            )

    return checked, updated, skipped, output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Update tenser-flow-results.csv opencode_backport_success from 0 to 1 "
            "when OpenCode judges generated.patch acceptable compared with expected.patch."
        )
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("tenser-flow-results.csv"),
        help="Path to the results CSV. Default: tenser-flow-results.csv",
    )
    parser.add_argument(
        "--logs",
        type=Path,
        default=Path("opencode-backport-logs"),
        help="Path to the OpenCode log directory. Default: opencode-backport-logs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many rows would change without writing the CSV.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create tenser-flow-results.csv.bak before writing.",
    )
    parser.add_argument(
        "--opencode",
        default="opencode",
        help="OpenCode command to run. Default: opencode",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional OpenCode model, for example provider/model.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Seconds to wait for each OpenCode judgement. Default: 180",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    csv_path = args.csv.resolve()
    logs_dir = args.logs.resolve()

    checked, updated, skipped, output_path = update_csv(
        csv_path,
        logs_dir,
        dry_run=args.dry_run,
        backup=not args.no_backup,
        opencode_command=args.opencode,
        model=args.model,
        timeout=args.timeout,
    )

    action = "Would update" if args.dry_run else "Updated"
    print(f"Checked {checked} rows marked 0.")
    print(f"{action} {updated} rows to {SUCCESS_COLUMN}=1.")
    if not args.dry_run and output_path != csv_path:
        print(f"Original CSV was locked. Updated file: {output_path}")

    if skipped:
        print(f"Skipped {len(skipped)} rows:")
        for item in skipped:
            print(f"  - {item}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
