import zipfile
from datetime import datetime

from scripts.export_executor_logs import discover_log_files, export_logs, zip_export_file


def test_discover_log_files_returns_oldest_backup_first(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "executor.log").write_text("current\n", encoding="utf-8")
    (log_dir / "executor.log.1").write_text("newer backup\n", encoding="utf-8")
    (log_dir / "executor.log.3").write_text("oldest backup\n", encoding="utf-8")
    (log_dir / "executor.log.tmp").write_text("ignore\n", encoding="utf-8")

    files = discover_log_files(log_dir, "executor.log")

    assert [path.name for path in files] == [
        "executor.log.3",
        "executor.log.1",
        "executor.log",
    ]


def test_export_logs_builds_single_analysis_file_in_order(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    oldest = log_dir / "executor.log.2"
    middle = log_dir / "executor.log.1"
    current = log_dir / "executor.log"
    oldest.write_text("oldest-line\n", encoding="utf-8")
    middle.write_text("middle-line\n", encoding="utf-8")
    current.write_text("current-line", encoding="utf-8")
    output = log_dir / "analysis.txt"

    exported = export_logs([oldest, middle, current], output)

    text = exported.read_text(encoding="utf-8")
    assert "FILE_COUNT: 3" in text
    assert text.index("oldest-line") < text.index("middle-line") < text.index("current-line")
    assert text.endswith("\n")


def test_export_logs_filters_from_marker_timestamp(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    current = log_dir / "executor.log"
    current.write_text(
        "\n".join(
            [
                "2026-06-01 10:00:00,000 - executor - INFO - old order",
                "old continuation",
                "2026-06-01 11:00:00,000 - executor - INFO - new order",
                "new continuation",
            ]
        ),
        encoding="utf-8",
    )
    output = log_dir / "analysis.txt"

    exported = export_logs(
        [current],
        output,
        marker_time=datetime(2026, 6, 1, 10, 30, 0),
        marker_path=log_dir / "executor_analysis_start.marker",
    )

    text = exported.read_text(encoding="utf-8")
    assert "FILTERED_FROM_MARKER: 2026-06-01T10:30:00.000" in text
    assert "old order" not in text
    assert "old continuation" not in text
    assert "new order" in text
    assert "new continuation" in text


def test_zip_export_file_creates_archive_with_export(tmp_path):
    exported = tmp_path / "executor_analysis.txt"
    exported.write_bytes(b"analysis\n")

    zip_path = zip_export_file(exported)

    assert zip_path == tmp_path / "executor_analysis.zip"
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == ["executor_analysis.txt"]
        assert archive.read("executor_analysis.txt").decode("utf-8") == "analysis\n"
