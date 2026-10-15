"""Structural load: people, PRs, issues, modules and how they connect. No LLM involved."""
import subprocess
from collections import Counter
from datetime import datetime
from itertools import combinations
from pathlib import Path

from engram.db import LIVE, batched, graph, rows
from engram.fetch import RAW, load_prs

BOTS = {"pre-commit-ci", "dependabot", "github-actions", "codecov"}
HUB_SHARE = 0.25      # modules touched by more than this share of PRs (CHANGES.md) are hubs
MAX_PR_MODULES = 20   # bigger PRs are mass edits; they don't say which modules belong together


def ts(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def human(author):
    if not author or author.get("__typename") == "Bot":
        return None
    login = author["login"]
    return None if login.endswith("[bot]") or login in BOTS else login


def module_of(path):
    return "/".join(path.split("/")[:3])


def kind_of(module):
    if module.endswith((".md", ".rst")):
        return "doc"
    if module.startswith("tests/"):
        return "test"
    if module.startswith("src/") or module.endswith(".py"):
        return "code"
    return "other"


def doc_dates(paths, before=None):
    repo = RAW / "repo"
    out = {}
    for p in paths:
        if before is None and not (repo / p).exists():
            continue  # deleted docs can't go stale
        cmd =["git", "-C", str(repo), "log", "-1", "--format=%ct"] + ([f"--before=@{before}"] if before else []) + ["--", p]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.stdout.strip():
            out[p] = int(r.stdout.strip())
    return out


def build(prs, cutoff=None):
    """Turn raw PRs into node/edge rows. With cutoff, everything after it is dropped (as-of graphs)."""
    keep = lambda t: cutoff is None or t <= cutoff
    people, pr_rows, issues, events, touches, fixes = {}, [], {}, [], [], []
    mod_count, pr_mods = Counter(), []

    def seen(login, t):
        lo, hi = people.get(login, (t, t))
        people[login] = (min(lo, t), max(hi, t))

    for pr in prs:
        created = ts(pr["createdAt"])
        if not keep(created):
            continue
        pr_rows.append({"number": pr["number"], "title": pr["title"], "url": pr["url"],
                        "created_at": created, "merged_at": ts(pr["mergedAt"])})
        if a := human(pr["author"]):
            seen(a, created)
            events.append({"p": a, "n": pr["number"], "type": "AUTHORED", "at": created})
        for kind, items, key in (("REVIEWED", pr["reviews"]["nodes"], "submittedAt"),
                                 ("COMMENTED", pr["comments"]["nodes"], "createdAt")):
            for it in items:
                who, at = human(it["author"]), ts(it[key]) if it.get(key) else created
                if who and keep(at) and who != human(pr["author"]):
                    seen(who, at)
                    events.append({"p": who, "n": pr["number"], "type": kind, "at": at})
        mods = sorted({module_of(f["path"]) for f in pr["files"]["nodes"]})
        mod_count.update(mods)
        pr_mods.append(mods)
        touches += [{"n": pr["number"], "m": m} for m in mods]
        for iss in pr["closingIssuesReferences"]["nodes"]:
            if keep(ts(iss["createdAt"])):
                issues[iss["number"]] = {"number": iss["number"], "title": iss["title"], "url": iss["url"],
                                         "created_at": ts(iss["createdAt"])}
                fixes.append({"n": pr["number"], "i": iss["number"]})

    hubs = {m for m, c in mod_count.items() if c > HUB_SHARE * max(len(pr_rows), 1)}
    co, doc_code = Counter(), Counter()
    for mods in pr_mods:
        mods = [m for m in mods if m not in hubs]
        if len(mods) > MAX_PR_MODULES:
            continue
        co.update(combinations(mods, 2))
        doc_code.update((d, c) for d in mods for c in mods if kind_of(d) == "doc" and kind_of(c) == "code")

    # a doc DESCRIBES the code it is repeatedly edited together with (top 3, at least twice)
    describes = []
    for d in {d for d, _ in doc_code}:
        best = sorted(((n, c) for (dd, c), n in doc_code.items() if dd == d and n >= 2), reverse=True)[:3]
        describes += [{"d": d, "c": c, "n": n} for n, c in best]

    modules = [{"path": m, "kind": kind_of(m), "hub": m in hubs, "prs": c} for m, c in mod_count.items()]
    return {
        "people": [{"handle": h, "first_seen": lo, "last_seen": hi} for h, (lo, hi) in people.items()],
        "prs": pr_rows, "issues": list(issues.values()), "events": events, "touches": touches,
        "fixes": fixes, "modules": modules, "describes": describes,
        "co": [{"a": a, "b": b, "n": n} for (a, b), n in co.items()],
    }


def load(g, data, dates):
    for idx in ("Person(handle)", "PR(number)", "Issue(number)", "Module(path)", "Decision(id)",
                "Scenario(id)", "Capture(id)"):
        try:
            g.query(f"CREATE INDEX FOR (n:{idx.split('(')[0]}) ON (n.{idx.split('(')[1][:-1]})")
        except Exception:
            pass  # index already exists
    batched(g, "UNWIND $rows AS r MERGE (p:Person {handle:r.handle}) SET p.first_seen=r.first_seen, p.last_seen=r.last_seen", data["people"])
    batched(g, "UNWIND $rows AS r MERGE (x:PR {number:r.number}) SET x.title=r.title, x.url=r.url, x.created_at=r.created_at, x.merged_at=r.merged_at", data["prs"])
    batched(g, "UNWIND $rows AS r MERGE (x:Issue {number:r.number}) SET x.title=r.title, x.url=r.url, x.created_at=r.created_at", data["issues"])
    batched(g, "UNWIND $rows AS r MERGE (m:Module {path:r.path}) SET m.kind=r.kind, m.hub=r.hub, m.prs=r.prs", data["modules"])
    doc_rows = [{"path": m["path"], "at": dates[m["path"]]} for m in data["modules"] if m["kind"] == "doc" and m["path"] in dates]
    batched(g, "UNWIND $rows AS r MATCH (m:Module {path:r.path}) SET m:Doc, m.updated_at=r.at", doc_rows)
    for kind in ("AUTHORED", "REVIEWED", "COMMENTED"):
        ev = [e for e in data["events"] if e["type"] == kind]
        batched(g, f"UNWIND $rows AS r MATCH (p:Person {{handle:r.p}}), (x:PR {{number:r.n}}) MERGE (p)-[e:{kind}]->(x) SET e.at=r.at", ev)
    batched(g, "UNWIND $rows AS r MATCH (x:PR {number:r.n}), (m:Module {path:r.m}) MERGE (x)-[:TOUCHES]->(m)", data["touches"])
    batched(g, "UNWIND $rows AS r MATCH (x:PR {number:r.n}), (i:Issue {number:r.i}) MERGE (x)-[:FIXES]->(i)", data["fixes"])
    batched(g, "UNWIND $rows AS r MATCH (a:Module {path:r.a}), (b:Module {path:r.b}) MERGE (a)-[c:CO_CHANGES]->(b) SET c.n=r.n", data["co"])
    batched(g, "UNWIND $rows AS r MATCH (d:Module {path:r.d}), (c:Module {path:r.c}) MERGE (d)-[e:DESCRIBES]->(c) SET e.n=r.n", data["describes"])


def run(name=LIVE, cutoff=None):
    g = graph(name)
    try:
        g.delete()
    except Exception:
        pass
    data = build(load_prs(), cutoff)
    load(g, data, doc_dates([m["path"] for m in data["modules"] if m["kind"] == "doc"], cutoff))
    return g, data


if __name__ == "__main__":
    g, data = run()
    print({k: len(v) for k, v in data.items()})
    print(rows(g, "MATCH (n) RETURN labels(n)[0], count(n) ORDER BY count(n) DESC"))
    print(rows(g, "MATCH ()-[e]->() RETURN type(e), count(e) ORDER BY count(e) DESC"))
