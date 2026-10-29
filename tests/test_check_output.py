import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
from check_output import check  # noqa: E402


def write(tmp, name, data):
    p = tmp / name
    p.write_text(json.dumps(data))
    return str(p)


def test_ok(tmp_path):
    p = write(tmp_path, "for9a.json", [{"review_flags": []}, {"review_flags": ["llm_failed"]}])
    assert check(p)[0]


def test_llm_mostly_failed(tmp_path):
    p = write(tmp_path, "for9a.json", [{"review_flags": ["llm_failed"]}] * 3 + [{"review_flags": []}])
    assert not check(p)[0]


def test_empty_fails_unless_allowed_or_all_known(tmp_path):
    p = write(tmp_path, "for9a.json", [])
    assert not check(p)[0]
    assert check(p, allow_empty=True)[0]
    write(tmp_path, "db_report_for9a.json", {"dry_run": False, "unchanged": 5, "errors": 0})
    assert check(p)[0]


def test_db_errors_fail(tmp_path):
    p = write(tmp_path, "telegram.json", [{"review_flags": []}])
    write(tmp_path, "db_report_telegram.json", {"errors": 2, "problems": ["x"]})
    assert not check(p)[0]


def test_missing_file(tmp_path):
    assert not check(str(tmp_path / "nope.json"))[0]
    assert check(str(tmp_path / "nope.json"), allow_empty=True)[0]


def test_llm_skipped_counts_as_failure(tmp_path):
    p = write(tmp_path, "for9a.json", [{"review_flags": ["llm_skipped"]}] * 4)
    assert not check(p)[0]
