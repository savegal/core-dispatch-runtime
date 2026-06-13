"""Build one analysis-friendly file from rotated executor logs.

The executor keeps bounded rotating logs for safety. This helper creates a
single chronological snapshot that is easier to attach for diagnostics.
"""

from __future__ import annotations

import argparse
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_OUTPUT_PREFIX = "executor_analysis"
DEFAULT_MARKER_NAME = "executor_analysis_start.marker"
LOG_LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})\b")


def _default_executor_log_path() -> Path:
    try:
        from config.settings import get_settings

        return Path(get_settings().EXECUTOR_LOG_FILE)
    except Exception:
        return Path(os.environ.get("EXECUTOR_LOG_FILE", "logs/executor.log"))


def _rotation_index(path: Path, base_name: str) -> int | None:
    if path.name == base_name:
        return 0
    prefix = f"{base_name}."
    if not path.name.startswith(prefix):
        return None
    suffix = path.name[len(prefix) :]
    if not suffix.isdigit():
        return None
    return int(suffix)


def discover_log_files(log_dir: Path, base_name: str) -> list[Path]:
    """Return rotated logs in chronological order: oldest backup -> current."""
    log_dir = Path(log_dir)
    indexed: list[tuple[int, Path]] = []
    for path in log_dir.glob(f"{base_name}*"):
        if not path.is_file():
            continue
        index = _rotation_index(path, base_name)
        if index is None:
            continue
        indexed.append((index, path))

    indexed.sort(key=lambda item: (item[0] == 0, -item[0]))
    return [path for _, path in indexed]


def _file_header(path: Path) -> str:
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    return (
        "\n"
        + "=" * 96
        + "\n"
        + f"FILE: {path.resolve()}\n"
        + f"SIZE_BYTES: {stat.st_size}\n"
        + f"MODIFIED: {modified}\n"
        + "=" * 96
        + "\n"
    )


def _read_marker_time(marker_path: Path) -> datetime | None:
    try:
        raw = marker_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def write_marker(marker_path: Path) -> Path:
    marker_path = Path(marker_path)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(datetime.now().isoformat(timespec="milliseconds"), encoding="utf-8")
    return marker_path


def _line_timestamp(line: str) -> datetime | None:
    match = LOG_LINE_TS_RE.match(line)
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group(1)}.{match.group(2)}",
            "%Y-%m-%d %H:%M:%S.%f",
        )
    except ValueError:
        return None


def _iter_lines_since_marker(path: Path, marker_time: datetime | None) -> Iterable[str]:
    include = marker_time is None
    with path.open("r", encoding="utf-8", errors="replace") as src:
        for line in src:
            line_time = _line_timestamp(line)
            if line_time is not None:
                include = marker_time is None or line_time >= marker_time
            if include:
                yield line


def export_logs(
    log_files: Iterable[Path],
    output_path: Path,
    *,
    marker_time: datetime | None = None,
    marker_path: Path | None = None,
) -> Path:
    files = [Path(path) for path in log_files]
    if not files:
        raise FileNotFoundError("No executor log files found to export")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_bytes = sum(path.stat().st_size for path in files)
    created_at = datetime.now().isoformat(timespec="seconds")

    with output_path.open("w", encoding="utf-8", errors="replace", newline="\n") as out:
        out.write("# Executor Log Export\n")
        out.write(f"CREATED_AT: {created_at}\n")
        out.write(f"FILE_COUNT: {len(files)}\n")
        out.write(f"TOTAL_BYTES: {total_bytes}\n")
        out.write("ORDER: oldest rotated log first, current log last\n")
        if marker_time is not None:
            out.write(f"FILTERED_FROM_MARKER: {marker_time.isoformat(timespec='milliseconds')}\n")
            if marker_path is not None:
                out.write(f"MARKER_FILE: {Path(marker_path).resolve()}\n")
        out.write("\nFILES:\n")
        for path in files:
            out.write(f"- {path.resolve()}\n")

        for path in files:
            out.write(_file_header(path))
            last_ended_with_newline = True
            for line in _iter_lines_since_marker(path, marker_time):
                out.write(line)
                last_ended_with_newline = line.endswith("\n")
            if not last_ended_with_newline:
                out.write("\n")

    return output_path


def zip_export_file(exported_path: Path) -> Path:
    exported_path = Path(exported_path)
    zip_path = exported_path.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(exported_path, arcname=exported_path.name)
    return zip_path


def build_default_output_path(log_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(log_dir) / f"{DEFAULT_OUTPUT_PREFIX}_{stamp}.txt"


def main() -> int:
    default_log_path = _default_executor_log_path()
    parser = argparse.ArgumentParser(
        description="Export rotated executor logs into one analysis-friendly text file."
    )
    parser.add_argument("--log-dir", default=str(default_log_path.parent))
    parser.add_argument("--base-name", default=default_log_path.name)
    parser.add_argument("--output", default=None)
    parser.add_argument(
        "--marker",
        default=None,
        help=(
            "Marker file with ISO timestamp. If omitted, "
            f"{DEFAULT_MARKER_NAME} in the log directory is used when present."
        ),
    )
    parser.add_argument(
        "--ignore-marker",
        action="store_true",
        help="Export all discovered executor logs even if a marker file exists.",
    )
    parser.add_argument(
        "--mark-start",
        action="store_true",
        help="Write a fresh marker timestamp and exit without exporting logs.",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help="Also create a .zip archive next to the exported .txt file.",
    )
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    marker_path = Path(args.marker) if args.marker else log_dir / DEFAULT_MARKER_NAME
    if args.mark_start:
        written = write_marker(marker_path)
        print(f"Log capture marker written: {written}")
        print("Run your orders, then use export_logs_for_analysis.bat to export only logs after this marker.")
        return 0

    marker_time = None if args.ignore_marker else _read_marker_time(marker_path)
    output = Path(args.output) if args.output else build_default_output_path(log_dir)
    log_files = discover_log_files(log_dir, args.base_name)
    exported = export_logs(
        log_files,
        output,
        marker_time=marker_time,
        marker_path=marker_path if marker_time is not None else None,
    )
    print(f"Exported {len(log_files)} log file(s) to {exported}")
    if marker_time is not None:
        print(f"Filtered logs from marker: {marker_time.isoformat(timespec='milliseconds')}")
    if args.zip:
        zip_path = zip_export_file(exported)
        print(f"Created archive: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
