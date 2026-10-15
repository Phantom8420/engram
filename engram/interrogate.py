"""The Interrogator: someone is leaving. Rank the questions that recover the most at-risk knowledge per answer,
write each answer back into the graph as a Capture, and flag answers that contradict a recorded decision."""
import re
import sys
import time
import uuid

from engram import loss
from engram.db import graph, rows
from engram.tools import find_modules

MIN_WORDS, MIN_DISTINCT = 12, 9   # shorter answers don't document anything
DISAGREE = re.compile(r"\b(no longer|not true|outdated|out of date|wrong|we changed|isn'?t the case|reverted|obsolete|"
                      r"don'?t do that anymore|not anymore)\b", re.I)


def start(g, handles):
    sid = "s-" + uuid.uuid4().hex[:8]
    g.query("""CREATE (s:Scenario {id:$s, created_at:$t}) WITH s
               UNWIND $h AS h MATCH (p:Person {handle:h}) CREATE (s)-[:REMOVES]->(p)""",
            {"s": sid, "t": int(time.time()), "h": list(handles)})
    return sid


def gains(r):
    """Meter points recovered by documenting each at-risk module (documenting cuts at_risk from lost to lost*(1-credit))."""
    return {i["module"]: 100 * i["importance"] * i["lost"] * loss.DOC_CREDIT * (1 - i["documented"]) / r["total"]
            for i in r["items"] if i["documented"] < 1}


def greedy(questions, gain, k):
    """Weighted max coverage: repeatedly take the question covering the most still-uncovered gain."""
    covered, picked = set(), []
    for _ in range(k):
        best = max(questions, key=lambda q: sum(gain.get(m, 0) for m in set(q["modules"]) - covered), default=None)
        if not best or sum(gain.get(m, 0) for m in set(best["modules"]) - covered) <= 0:
            break
        picked.append({**best, "gain": sum(gain.get(m, 0) for m in set(best["modules"]) - covered)})
        covered |= set(best["modules"])
    return picked


def candidates(g, sid, items):
    """Two kinds of question: about a decision the leaver took part in, and about a module they hold."""
    at_risk = {i["module"]: i for i in items if i["documented"] < 1}
    qs = []
    for did, quote, url, n, mods in rows(g, """
            MATCH (:Scenario {id:$s})-[:REMOVES]->(p:Person)
            MATCH (d:Decision)-[:DECIDED_IN]->(pr:PR)
            WHERE (d)-[:BY]->(p) OR (p)-[:AUTHORED|REVIEWED]->(pr)
            MATCH (d)-[:AFFECTS]->(m:Module {kind:'code'})
            WHERE d.valid_to IS NULL
            RETURN DISTINCT d.id, d.quote, d.source_url, pr.number, collect(DISTINCT m.path)""", s=sid):
        mods = [m for m in mods if m in at_risk]
        if mods:
            qs.append({"id": did, "kind": "decision", "modules": mods, "source": url,
                       "text": f"In #{n} the thread says: \"{quote}\" — is that still how {', '.join(mods)} should work, "
                               f"and what breaks if someone changes it?"})
    for m, i in at_risk.items():
        left = i["holders_left"]
        qs.append({"id": f"mod:{m}", "kind": "module", "modules": [m], "source": None,
                   "text": f"{m}: you hold {i['lost']:.0%} of the team's recent knowledge of it and "
                           f"{'only @' + left[0] + ' can' if len(left) == 1 else str(len(left)) + ' others can' if left else 'nobody else can'} "
                           f"answer. What would a newcomer get wrong there?"})
    return qs


def questions(g, sid, k=5):
    r = loss.scenario_meter(g, sid)
    answered = {q for (q,) in rows(g, "MATCH (:Scenario {id:$s})-[:HAS]->(c:Capture) RETURN c.question", s=sid)}
    qs = [q for q in candidates(g, sid, r["items"]) if q["id"] not in answered]
    picked = greedy(qs, gains(r), k)
    for q in picked:
        q["gain"] = round(q["gain"], 2)
    return {"meter": r["meter"], "questions": picked}


def answer(g, sid, question, text):
    """Write an answer back: Capture -DOCUMENTS-> modules (from the question plus any named in the answer),
    and -CONTRADICTS-> the decision if the answer says it no longer holds."""
    before = loss.scenario_meter(g, sid)["meter"]
    if problem := too_thin(text, question["text"]):
        return {"meter": before, "before": before, "contradicts": None, "documented": [], "rejected": problem}
    mods = sorted(set(question["modules"]) | set(find_modules(g, text, 3, strict=True)))
    cid = "c-" + uuid.uuid4().hex[:8]
    g.query("""MATCH (s:Scenario {id:$s})
               CREATE (s)-[:HAS]->(c:Capture {id:$c, question:$q, text:$t, at:$at})
               WITH c UNWIND $mods AS mp MATCH (m:Module {path:mp}) CREATE (c)-[:DOCUMENTS]->(m)""",
            {"s": sid, "c": cid, "q": question["id"], "t": text, "at": int(time.time()), "mods": mods})
    contradicts = None
    if question["kind"] == "decision":
        g.query("MATCH (c:Capture {id:$c}), (d:Decision {id:$d}) CREATE (c)-[:EXPLAINS]->(d)", {"c": cid, "d": question["id"]})
        if disagrees(g, question["id"], text):
            g.query("""MATCH (c:Capture {id:$c}), (d:Decision {id:$d})
                       CREATE (c)-[:CONTRADICTS]->(d)""", {"c": cid, "d": question["id"]})
            contradicts = question["id"]
    return {"meter": loss.scenario_meter(g, sid)["meter"], "before": before, "contradicts": contradicts,
            "documented": mods, "capture": cid}


def words(text):
    return re.findall(r"[a-z][a-z0-9_']+", text.lower())


def too_thin(text, question):
    """Why an answer documents nothing, or None. Echoing the question back does not count."""
    w = words(text)
    asked = set(words(question))
    if len(w) < MIN_WORDS or len(set(w) - asked) < MIN_DISTINCT:
        return f"Too short to hand over: at least {MIN_WORDS} words, {MIN_DISTINCT} of them not taken from the question."
    return None


def disagrees(g, decision_id, text):
    """A disagreement cue, plus the answer naming what the decision is about (two content words from its quote or
    choice), so a stray "no longer" about something else doesn't flag the decision."""
    if not DISAGREE.search(text):
        return False
    found = rows(g, "MATCH (d:Decision {id:$d}) RETURN d.quote, d.choice", d=decision_id)
    if not found:
        return False
    about = {w for w in words(" ".join(x or "" for x in found[0])) if len(w) >= 5}
    return len(about & set(words(text))) >= 2


def end(g, sid):
    g.query("MATCH (s:Scenario {id:$s}) OPTIONAL MATCH (s)-[:HAS]->(c:Capture) DETACH DELETE s, c", {"s": sid})


def contradictions(g):
    """Recorded decisions that a departing person's own answer says no longer hold, with both sides quoted."""
    return [{"decision": d, "quote": q, "source": u, "answer": a} for d, q, u, a in rows(g, """
        MATCH (c:Capture)-[:CONTRADICTS]->(d:Decision) RETURN d.id, d.quote, d.source_url, c.text""")]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    g = graph()
    sid = start(g, sys.argv[1:])
    r = questions(g, sid)
    print(f"scenario {sid}: knowledge at risk {r['meter']}%")
    for q in r["questions"]:
        print(f"  [-{q['gain']} pts] {q['text']}")
