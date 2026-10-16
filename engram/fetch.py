"""Fetch merged PRs (files, reviews, comments, linked issues) from GitHub GraphQL.

Each page is cached to data/raw/prs/page_NNN.json, so a rerun only fetches what is missing.
Also makes a blobless clone of the repo for doc paths and their last-edit dates.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from engram import REPO

RAW = Path("data/raw") if REPO == "psf/black" else Path("data/raw") / REPO.replace("/", "__")
PAGES = RAW / "prs"
QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:MERGED, first:50, after:$cursor, orderBy:{field:CREATED_AT, direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body url createdAt mergedAt
        author { __typename login }
        files(first:100) { nodes { path } }
        reviews(first:30) { nodes { author { __typename login } state body submittedAt } }
        comments(first:50) { nodes { author { __typename login } body createdAt } }
        closingIssuesReferences(first:5) { nodes { number title body url createdAt author { login } } }
      }
    }
  }
}
"""


def token():
    return os.environ.get("GITHUB_TOKEN") or subprocess.check_output(["gh", "auth", "token"], text=True).strip()


def fetch_prs():
    PAGES.mkdir(parents=True, exist_ok=True)
    owner, name = REPO.split("/")
    pages = sorted(PAGES.glob("page_*.json"))
    cursor, has_next = None, True
    if pages:
        last = json.loads(pages[-1].read_text(encoding="utf-8"))["pageInfo"]
        cursor, has_next = last["endCursor"], last["hasNextPage"]
    headers = {"Authorization": f"bearer {token()}"}
    with httpx.Client(timeout=90, headers=headers) as client:
        while has_next:
            for attempt in range(5):
                r = client.post("https://api.github.com/graphql",
                                json={"query": QUERY, "variables": {"owner": owner, "name": name, "cursor": cursor}})
                body = r.json() if r.status_code == 200 else {}
                if body.get("data"):
                    break
                print(f"retry {attempt}: {r.status_code} {str(body.get('errors'))[:200]}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
            else:
                sys.exit("GitHub fetch failed after retries; rerun to resume")
            conn = body["data"]["repository"]["pullRequests"]
            path = PAGES / f"page_{len(pages):03d}.json"
            path.write_text(json.dumps(conn), encoding="utf-8")
            pages.append(path)
            cursor, has_next = conn["pageInfo"]["endCursor"], conn["pageInfo"]["hasNextPage"]
            print(f"{path.name}: {len(conn['nodes'])} PRs")


MERGERS = RAW / "merged_by.json"
MERGED_QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:MERGED, first:100, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { number mergedBy { login } }
    }
  }
}
"""


def fetch_mergers():
    """PR number -> who pressed merge. A separate small file so the main page cache stays valid."""
    if MERGERS.exists():
        return
    owner, name = REPO.split("/")
    out, cursor, has_next = {}, None, True
    with httpx.Client(timeout=90, headers={"Authorization": f"bearer {token()}"}) as client:
        while has_next:
            r = client.post("https://api.github.com/graphql",
                            json={"query": MERGED_QUERY, "variables": {"owner": owner, "name": name, "cursor": cursor}})
            r.raise_for_status()
            conn = r.json()["data"]["repository"]["pullRequests"]
            out.update({n["number"]: (n["mergedBy"] or {}).get("login") for n in conn["nodes"]})
            cursor, has_next = conn["pageInfo"]["endCursor"], conn["pageInfo"]["hasNextPage"]
    MERGERS.write_text(json.dumps(out), encoding="utf-8")


def load_mergers():
    return {int(k): v for k, v in json.loads(MERGERS.read_text(encoding="utf-8")).items()} if MERGERS.exists() else {}


def clone_repo():
    dest = RAW / "repo"
    if not dest.exists():
        subprocess.check_call(["git", "clone", "-q", "--filter=blob:none", f"https://github.com/{REPO}.git", str(dest)])


def load_prs():
    """All cached PRs, in creation order."""
    out = []
    for p in sorted(PAGES.glob("page_*.json")):
        out.extend(json.loads(p.read_text(encoding="utf-8"))["nodes"])
    return out


if __name__ == "__main__":
    clone_repo()
    fetch_prs()
    fetch_mergers()
    print(f"{len(load_prs())} PRs cached")
