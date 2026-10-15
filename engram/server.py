"""HTTP API + the one-page UI. Every endpoint runs a curated query; nothing user-typed reaches Cypher as code."""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from engram import agent, future, interrogate, loss
from engram import tools as T
from engram.db import graph, rows

app = FastAPI(title="Engram")
WEB = Path(__file__).resolve().parent.parent / "web"


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


@app.get("/api/overview")
def overview():
    g = graph()
    counts = dict(rows(g, "MATCH (n) WHERE NOT n:Scenario AND NOT n:Capture RETURN labels(n)[0], count(n)"))
    people = [h for h, _ in rows(g, """MATCH (p:Person)-[k:KNOWS]->() RETURN p.handle, sum(k.score) AS s
                                       ORDER BY s DESC LIMIT 25""")]
    decisions = [{"id": i, "summary": s} for i, s in rows(g, """MATCH (d:Decision) WHERE d.valid_to IS NULL
                 RETURN d.id, d.summary ORDER BY d.valid_from DESC LIMIT 60""")]
    return {"counts": counts, "people": people, "decisions": decisions, **T.central_modules(g)}


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
