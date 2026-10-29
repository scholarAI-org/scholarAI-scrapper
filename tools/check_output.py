"""Fail the CI run when a scrape silently produced nothing useful.

usage: python tools/check_output.py output/for9a.json output/telegram.json --allow-empty output/telegram.json

FAIL when
  - a JSON output is missing/empty (unless listed in --allow-empty, e.g. Telegram has no new
    posts; for9a may be empty when everything was already stored: db report 'unchanged' > 0),
  - more than 50% of its records have the review flag 'llm_failed' or 'llm_skipped' (dead keys,
    provider outage, secrets not configured),
  - the matching output/db_report_<spider>.json shows database write errors.
"""
import argparse
import json
import os
import sys

MAX_LLM_FAILED = 0.5


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check(path, allow_empty=False):
    """-> (ok, message)"""
    spider = os.path.splitext(os.path.basename(path))[0]
    report_path = os.path.join(os.path.dirname(path), f"db_report_{spider}.json")
    report = _load(report_path) if os.path.exists(report_path) else None
    problems = []

    if report and report.get("errors"):
        problems.append(f"{report['errors']} database write errors: {report.get('problems', [])[:3]}")

    try:
        items = _load(path)
    except FileNotFoundError:
        items = []
        if not allow_empty:
            problems.append("output file missing")
    except Exception as e:
        return False, f"FAIL {path}: cannot read ({e})"

    if not items:
        everything_known = bool(report and not report.get("dry_run") and report.get("unchanged"))
        if not (allow_empty or everything_known):
            problems.append("no records")
        msg = "no new records"
    else:
        failed = sum(bool({"llm_failed", "llm_skipped"} & set(i.get("review_flags") or [])) for i in items)
        ratio = failed / len(items)
        if ratio > MAX_LLM_FAILED:
            problems.append(f"LLM failed or skipped on {failed}/{len(items)} records")
        msg = f"{len(items)} records, LLM failed/skipped {failed}"
    if report:
        msg += (f"; db inserted {report.get('inserted')}, updated {report.get('updated')}, "
                f"unchanged {report.get('unchanged')}, protected {report.get('skipped_reviewed')}, "
                f"invalid {report.get('invalid')}")
    return (not problems), (f"FAIL {path}: {'; '.join(problems)} ({msg})" if problems else f"ok {path}: {msg}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--allow-empty", nargs="*", default=[])
    args = ap.parse_args(argv)
    bad = False
    for p in args.paths:
        ok, msg = check(p, allow_empty=p in args.allow_empty)
        print(msg)
        bad |= not ok
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
