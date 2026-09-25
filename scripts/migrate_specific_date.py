"""
One-time migration: maps GitHub issues created on a SPECIFIC date into
ServiceNow cases (short_description + description only for now), then
closes each case using the latest available comment on that GitHub issue.

Every matching issue is closed regardless of whether it's still open on
GitHub -- this migration assumes all of these are being closed out in bulk.

Usage:
    export SNOW_INSTANCE=... SNOW_USER=... SNOW_PASSWORD=... SNOW_TABLE=...
    export GITHUB_TOKEN=... GH_REPO=owner/repo
    export TARGET_DATE=2026-06-15
    python scripts/migrate_specific_date.py

Optional:
    DRY_RUN=true          -- log what would happen, don't call ServiceNow
    SLEEP_SECONDS=0.4     -- delay between records
"""

import os
import sys
import time
from datetime import datetime, timezone

import requests

# ---------- config ----------
SNOW_INSTANCE = os.environ["SNOW_INSTANCE"]
SNOW_USER = os.environ["SNOW_USER"]
SNOW_PASSWORD = os.environ["SNOW_PASSWORD"]
SNOW_TABLE = os.environ.get("SNOW_TABLE", "sn_customerservice_case")

GH_TOKEN = os.environ["GITHUB_TOKEN"]
GH_REPO = os.environ["GH_REPO"]

TARGET_DATE = os.environ["TARGET_DATE"]  # e.g. "2026-06-15"
TARGET_DT = datetime.strptime(TARGET_DATE, "%Y-%m-%d").date()

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
SLEEP_SECONDS = float(os.environ.get("SLEEP_SECONDS", "0.4"))

SNOW_BASE = f"https://{SNOW_INSTANCE}/api/now/table/{SNOW_TABLE}"
SNOW_AUTH = (SNOW_USER, SNOW_PASSWORD)
SNOW_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

GH_API_BASE = f"https://api.github.com/repos/{GH_REPO}"
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
}


# ---------- GitHub helpers ----------
def get_effective_date(issue):
    """Use a 'date:YYYY-MM-DD' label as a test override if present,
    otherwise fall back to the issue's real GitHub creation date."""
    for label in issue.get("labels", []):
        name = label["name"]
        if name.startswith("date:"):
            try:
                return datetime.strptime(name[5:], "%Y-%m-%d").date()
            except ValueError:
                print(f"  Warning: issue #{issue['number']} has an unparsable "
                      f"'{name}' label, ignoring it.")
    return datetime.strptime(issue["created_at"], "%Y-%m-%dT%H:%M:%SZ").date()


def list_issues_on_target_date():
    issues = []
    page = 1
    while True:
        resp = requests.get(
            f"{GH_API_BASE}/issues",
            headers=GH_HEADERS,
            params={"state": "all", "per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        issues.extend([i for i in batch if "pull_request" not in i])
        print(f"Fetched page {page} ({len(batch)} items, running total {len(issues)})")
        page += 1
        time.sleep(0.2)

    matching = [i for i in issues if get_effective_date(i) == TARGET_DT]
    print(f"{len(matching)} of {len(issues)} issues have an effective date of {TARGET_DATE}")
    return matching


def get_last_comment(issue_number):
    resp = requests.get(
        f"{GH_API_BASE}/issues/{issue_number}/comments",
        headers=GH_HEADERS,
        params={"per_page": 100},
    )
    resp.raise_for_status()
    comments = resp.json()
    if not comments:
        return "(No comments on the GitHub issue.)"
    return comments[-1]["body"]


# ---------- ServiceNow helpers ----------
def find_existing_case(issue_number):
    params = {
        "sysparm_query": f"correlation_id={issue_number}^correlation_display={GH_REPO}",
        "sysparm_limit": 1,
    }
    resp = requests.get(SNOW_BASE, auth=SNOW_AUTH, headers=SNOW_HEADERS, params=params)
    resp.raise_for_status()
    results = resp.json().get("result", [])
    return results[0] if results else None


def create_case(issue):
    payload = {
        "short_description": (issue["title"] or "")[:160],
        "description": (
            f"{issue.get('body') or ''}\n\n"
            f"---\n"
            f"Source: GitHub issue {issue['html_url']}\n"
            f"Repo: {GH_REPO}\n"
            f"Migrated from GitHub."
        ),
        "correlation_id": str(issue["number"]),
        "correlation_display": GH_REPO,
    }
    if DRY_RUN:
        print(f"[DRY RUN] Would create case for issue #{issue['number']}")
        return {"sys_id": "dry-run", "number": "DRY-RUN"}

    resp = requests.post(SNOW_BASE, auth=SNOW_AUTH, headers=SNOW_HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()["result"]


def close_case(case_sys_id, issue_number, resolution_note):
    payload = {
        "state": "Closed",
        "close_notes": resolution_note,
        "work_notes": (
            f"Migrated and closed from GitHub issue #{issue_number}. "
            f"Latest available comment:\n\n{resolution_note}"
        ),
    }
    if DRY_RUN:
        print(f"[DRY RUN] Would close case for issue #{issue_number}")
        return

    params = {"sysparm_input_display": "true"}
    resp = requests.patch(
        f"{SNOW_BASE}/{case_sys_id}",
        auth=SNOW_AUTH,
        headers=SNOW_HEADERS,
        json=payload,
        params=params,
    )
    resp.raise_for_status()


# ---------- main ----------
def main():
    print(f"Fetching issues from {GH_REPO} created on {TARGET_DATE} ...")
    issues = list_issues_on_target_date()

    created, skipped, closed, errors = 0, 0, 0, 0

    for idx, issue in enumerate(issues, start=1):
        number = issue["number"]
        try:
            existing = find_existing_case(number)
            if existing:
                print(f"[{idx}/{len(issues)}] #{number}: case already exists "
                      f"({existing.get('number')}) -- skipping create.")
                case = existing
                skipped += 1
            else:
                case = create_case(issue)
                print(f"[{idx}/{len(issues)}] #{number}: created case {case.get('number')}")
                created += 1

            last_comment = get_last_comment(number)
            close_case(case["sys_id"], number, last_comment)
            closed += 1

        except requests.exceptions.HTTPError as e:
            print(f"[{idx}/{len(issues)}] #{number}: ERROR -- {e}")
            errors += 1

        time.sleep(SLEEP_SECONDS)

    print("\n----- Migration summary -----")
    print(f"Target date:             {TARGET_DATE}")
    print(f"Total matching issues:   {len(issues)}")
    print(f"New cases created:       {created}")
    print(f"Already existed:        {skipped}")
    print(f"Cases closed:            {closed}")
    print(f"Errors:                  {errors}")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
