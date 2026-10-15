"""HTTP API + the one-page UI. Every endpoint runs a curated query; nothing user-typed reaches Cypher as code."""
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from engram import agent, future, interrogate, loss
from engram import tools as T
from engram.db import graph, rows

app = FastAPI(title="Engram")
ROOT = Path(__file__).resolve().parent.parent
WEB, RESULTS = ROOT / "web", ROOT / "eval" / "results.json"


class Ask(BaseModel):
    q: str


class Start(BaseModel):
    handles: list[str]


class Answer(BaseModel):
    sid: str
    question: dict
    text: str


class What(BaseModel):
    kind: str
    target: str


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/app")
def dashboard():
    return FileResponse(WEB / "app.html")


@app.get("/about")
def about():
    return FileResponse(WEB / "about.html")


@app.get("/api/overview")
def overview():
    g = graph()
    counts = dict(rows(g, "MATCH (n) WHERE NOT n:Scenario AND NOT n:Capture RETURN labels(n)[0], count(n)"))
    people = [h for h, _ in rows(g, """MATCH (p:Person)-[k:KNOWS]->() RETURN p.handle, sum(k.score) AS s
                                       ORDER BY s DESC LIMIT 25""")]
    decisions = [{"id": i, "summary": s} for i, s in rows(g, """MATCH (d:Decision) WHERE d.valid_to IS NULL
                 RETURN d.id, d.summary ORDER BY d.valid_from DESC LIMIT 60""")]
    central = T.central_modules(g)
    held = dict(rows(g, """UNWIND $mods AS mp MATCH (m:Module {path:mp}) OPTIONAL MATCH (p:Person)-[k:KNOWS]->(m)
                           WHERE k.score >= $h RETURN m.path, count(p)""", mods=[c["module"] for c in central["data"]], h=loss.HOLDER))
    fragile = rows(g, """MATCH (m:Module {kind:'code'}) WHERE m.importance > 0.05
                         OPTIONAL MATCH (p:Person)-[k:KNOWS]->(m) WHERE k.score >= $h
                         WITH m, count(p) AS n WHERE n <= 1 RETURN count(m)""", h=loss.HOLDER)[0][0]
    share = rows(g, """MATCH (p:Person)-[k:KNOWS]->(:Module {kind:'code'}) WITH p, sum(k.score) AS s
                       RETURN p.handle, s ORDER BY s DESC LIMIT 10""")
    total = rows(g, "MATCH (:Person)-[k:KNOWS]->(:Module {kind:'code'}) RETURN sum(k.score)")[0][0] or 1
    return {"counts": counts, "people": people, "decisions": decisions, **central,
            "holders": [{"module": c["module"], "holders": held.get(c["module"], 0)} for c in central["data"]],
            "concentration": [{"handle": h, "share": round(100 * v / total, 1)} for h, v in share],
            "fragile": fragile, "evidence": json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else None}


@app.post("/api/ask")
def ask(body: Ask):
    return agent.ask(graph(), body.q[:2000])


@app.post("/api/interview/start")
def start(body: Start):
    g = graph()
    known = {h for (h,) in rows(g, "UNWIND $h AS h MATCH (p:Person {handle:h}) RETURN p.handle", h=body.handles)}
    if not known:
        raise HTTPException(404, "None of those handles appear in the graph.")
    sid = interrogate.start(g, sorted(known))
    r = loss.scenario_meter(g, sid)
    return {"sid": sid, "items": r["items"][:8], **interrogate.questions(g, sid)}


@app.get("/api/interview/{sid}")
def resume(sid: str):
    g = graph()
    if not rows(g, "MATCH (s:Scenario {id:$s}) RETURN s.id", s=sid):
        raise HTTPException(404, "That interview has expired.")
    r = loss.scenario_meter(g, sid)
    handles = [h for (h,) in rows(g, "MATCH (:Scenario {id:$s})-[:REMOVES]->(p:Person) RETURN p.handle", s=sid)]
    return {"sid": sid, "handles": handles, "items": r["items"][:8], **interrogate.questions(g, sid)}


@app.post("/api/interview/answer")
def answer(body: Answer):
    g = graph()
    res = interrogate.answer(g, body.sid, body.question, body.text[:5000])
    return {**res, "next": interrogate.questions(g, body.sid)["questions"],
            "contradictions": interrogate.contradictions(g)}


@app.post("/api/future")
def what_if(body: What):
    g = graph()
    if body.kind == "leave":
        return future.leave(g, body.target)
    if body.kind == "reverse":
        return future.reverse(g, body.target)
    raise HTTPException(400, "kind must be 'leave' or 'reverse'")
