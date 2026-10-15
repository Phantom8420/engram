"""Find decisions in PR threads, keep only those whose quote is verbatim in the source, link them into the graph."""
import json
import re
import sys
from pathlib import Path

from engram import llm
from engram.db import LIVE, graph, rows
from engram.fetch import load_prs
from engram.load import human, module_of, ts

CACHE = Path("data/cache/decisions")
SHORTLIST = 80
MAX_CHARS = 7000   # shortcut: threads are cut to fit a 4B local model's context; raise when using Claude
CUES = re.compile(r"\b(we decided|decided to|decision|instead of|rather than|the reason|because|trade-?off|consensus|"
                  r"agreed|let'?s go with|we chose|prefer|deprecat\w*|revert\w*|stable style|preview)\b", re.I)
STRONG = re.compile(r"\b(we decided|decided to|instead of|rather than|let'?s go with|we chose|agreed|consensus)\b", re.I)
TOPICS = {
    "string processing": ["string", "quote", "f-string"],
    "line splitting": ["line length", "split", "wrap", "long line"],
    "magic trailing comma": ["trailing comma"],
    "parentheses": ["parenthes", "paren", "brackets"],
    "comments": ["comment", "fmt: off", "fmt: skip"],
    "docstrings": ["docstring"],
    "blank lines": ["blank line", "empty line", "newline"],
    "stability policy": ["preview", "stable style", "stability", "unstable"],
    "python versions": ["python 3", "py3", "target version", "target-version"],
    "parser": ["blib2to3", "grammar", "parser", "tokeniz"],
    "configuration": ["pyproject", "config", "option", "flag"],
    "jupyter": ["jupyter", "notebook", "ipynb"],
    "performance": ["cache", "performance", "speed", "mypyc"],
    "blackd": ["blackd"],
}
SYSTEM = ("You extract engineering decisions from a GitHub pull request thread of the Python formatter Black. "
          "A decision is a choice the maintainers made or accepted, ideally with a reason. Reply with JSON only.")
PROMPT = """Thread:
{thread}

Return {{"decisions": [...]}} with at most 3 items, each:
{{"summary": "one sentence", "topic": one of {topics}, "choice": "the option chosen, 2-6 words",
  "why": "the stated reason or null", "quote": "an exact sentence copied verbatim from the thread", "confidence": 0.0-1.0}}
Return {{"decisions": []}} if the thread contains no real decision."""


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def segments(pr):
    segs = [(human(pr["author"]), pr["createdAt"], f"{pr['title']}\n{pr['body'] or ''}")]
    segs += [(human(c["author"]), c.get("createdAt") or pr["createdAt"], c["body"] or "") for c in pr["comments"]["nodes"]]
    segs += [(human(r["author"]), r.get("submittedAt") or pr["createdAt"], r["body"] or "") for r in pr["reviews"]["nodes"]]
    segs += [(i["author"]["login"] if i.get("author") else None, i["createdAt"], f"{i['title']}\n{i['body'] or ''}")
             for i in pr["closingIssuesReferences"]["nodes"]]
    return [s for s in segs if s[2].strip()]


def thread_text(segs):
    return "\n\n".join(f"[@{a or 'unknown'} {at[:10]}]\n{t}" for a, at, t in segs)[:MAX_CHARS]


def shortlist(prs, n=SHORTLIST):
    scored = [(len(CUES.findall(thread_text(segments(p)))), p) for p in prs]
    return [p for s, p in sorted(scored, key=lambda x: -x[0]) if s >= 2][:n]


def topic_of(text):
    t = text.lower()
    return next((k for k, words in TOPICS.items() if any(w in t for w in words)), "other")


REASON = re.compile(r"\b(because|so that|since|the reason|in order to|to avoid)\b", re.I)
NOT_DECISION = re.compile(r"\b(incorrectly|bug|crash\w*|traceback|would sometimes|fails?|failing|error)\b|^\W*(fix|fixes)\b", re.I)


def heuristic(segs, title):
    """Sentences with a strong decision cue, preferring ones that give a reason; bug reports are skipped."""
    scored = []
    for _, _, text in segs:
        for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
            sent = sent.strip()
            if not (30 <= len(sent) <= 300) or not STRONG.search(sent) or NOT_DECISION.search(sent) or sent.startswith(">"):
                continue
            why = bool(REASON.search(sent))
            scored.append((2 + why, {"summary": title, "topic": topic_of(sent + " " + title), "choice": None,
                                     "why": sent if why else None, "quote": sent, "confidence": 0.5 if why else 0.4}))
    return [d for _, d in sorted(scored, key=lambda x: -x[0])][:2]


def verify(dec, segs):
    """Keep a decision only if its quote appears verbatim in one segment; return that segment's author."""
    q = norm(dec.get("quote"))
    if len(q) < 20:
        return None
    for author, at, text in segs:
        if q in norm(text):
            return author, at
    return None


def extract(pr, which):
    path = CACHE / f"pr_{pr['number']}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    segs = segments(pr)
    if which:
        resp = llm.ask_json(SYSTEM, PROMPT.format(thread=thread_text(segs), topics=list(TOPICS) + ["other"]), which)
        raw = resp.get("decisions", []) if isinstance(resp, dict) else []
    else:
        raw = heuristic(segs, pr["title"])
    kept, rejected = [], 0
    for d in raw if isinstance(raw, list) else []:
        if not isinstance(d, dict) or not (hit := verify(d, segs)):
            rejected += 1
            continue
        d["by"], d["said_at"] = hit
        d["topic"] = d.get("topic") if d.get("topic") in TOPICS else topic_of(f"{d.get('summary', '')} {d['quote']}")
        try:
            d["confidence"] = max(0.0, min(1.0, float(d.get("confidence") or 0.5)))
        except (TypeError, ValueError):
            d["confidence"] = 0.5
        kept.append(d)
    result = {"number": pr["number"], "extractor": which or "heuristic", "decisions": kept, "rejected": rejected}
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result), encoding="utf-8")
    return result


def link(g, prs):
    """Write cached decisions into the graph with DECIDED_IN, AFFECTS, BY, ABOUT and SUPERSEDES."""
    by_num = {p["number"]: p for p in prs}
    rows_ = []
    for f in CACHE.glob("pr_*.json"):
        res = json.loads(f.read_text(encoding="utf-8"))
        pr = by_num.get(res["number"])
        if not pr:
            continue
        mods = sorted({module_of(x["path"]) for x in pr["files"]["nodes"]})
        for i, d in enumerate(res["decisions"]):
            rows_.append({"id": f"pr{pr['number']}-{i}", "n": pr["number"], "summary": d.get("summary") or pr["title"],
                          "topic": d["topic"], "choice": d.get("choice"), "why": d.get("why"), "quote": d["quote"],
                          "url": pr["url"], "confidence": d["confidence"], "extractor": res["extractor"],
                          "valid_from": ts(pr["mergedAt"]), "by": d.get("by"), "mods": mods})
    g.query("MATCH (d:Decision) DETACH DELETE d")
    g.query("MATCH (t:Topic) DETACH DELETE t")
    g.query("""UNWIND $rows AS r
               MATCH (pr:PR {number:r.n})
               CREATE (d:Decision {id:r.id, summary:r.summary, topic:r.topic, choice:r.choice, why:r.why, quote:r.quote,
                       source_url:r.url, confidence:r.confidence, extractor:r.extractor, valid_from:r.valid_from,
                       status:'active'})-[:DECIDED_IN]->(pr)
               MERGE (t:Topic {name:r.topic}) CREATE (d)-[:ABOUT]->(t)
               WITH d, r
               OPTIONAL MATCH (p:Person {handle:r.by}) FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END | CREATE (d)-[:BY]->(p))
               WITH d, r
               UNWIND r.mods AS mp
               MATCH (m:Module {path:mp}) WHERE m.hub = false AND m.kind IN ['code', 'doc']
               CREATE (d)-[:AFFECTS]->(m)""", {"rows": rows_})
    # explicit supersession: a later PR that references an earlier one (#123) and decides on the same topic
    refs = []
    for r in rows_:
        text = " ".join(t for _, _, t in segments(by_num[r["n"]]))
        refs += [{"new": r["id"], "old": int(m)} for m in set(re.findall(r"#(\d{2,5})\b", text)) if int(m) != r["n"]]
    g.query("""UNWIND $rows AS r
               MATCH (new:Decision {id:r.new})-[:DECIDED_IN]->(:PR), (old:Decision)-[:DECIDED_IN]->(:PR {number:r.old})
               WHERE old.topic = new.topic AND old.valid_from < new.valid_from AND old.topic <> 'other'
               MERGE (new)-[:SUPERSEDES]->(old)
               SET old.valid_to = new.valid_from, old.status = 'superseded'""", {"rows": refs})
    return len(rows_)


if __name__ == "__main__":
    prs = load_prs()
    which = None if "--heuristic" in sys.argv else llm.backend()
    picks = shortlist(prs, SHORTLIST if which else 400)  # the deterministic pass is free, so it reads more threads
    print(f"backend={which or 'heuristic'} shortlist={len(picks)}", flush=True)
    kept = rej = 0
    for i, pr in enumerate(picks):
        try:
            res = extract(pr, which)
        except Exception as e:  # one bad thread must not stop the run; it is retried next time
            print(f"#{pr['number']} failed: {e}", flush=True)
            continue
        kept, rej = kept + len(res["decisions"]), rej + res["rejected"]
        print(f"[{i + 1}/{len(picks)}] #{pr['number']}: {len(res['decisions'])} kept, {res['rejected']} rejected", flush=True)
    g = graph(LIVE)
    print("linked", link(g, prs), "decisions; kept", kept, "rejected", rej)
    print(rows(g, "MATCH (d:Decision) RETURN d.status, count(d)"))
