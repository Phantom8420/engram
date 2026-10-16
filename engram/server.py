"""HTTP API + the one-page UI. Every endpoint runs a curated query; nothing user-typed reaches Cypher as code."""
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from engram import REPOS, agent, future, graph_name, interrogate, loss, results_file
from engram import tools as T
from engram.db import connect, graph, rows

app = FastAPI(title="Engram")
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SNAPSHOT = ROOT / "public" / "snapshot.json"  # Vercel serves public/ from its CDN; this route covers local runs


def pick(repo):
    """Only repos that were loaded on purpose; the name never reaches Cypher."""
    if repo not in REPOS:
        raise HTTPException(404, f"Unknown repo. Loaded: {', '.join(REPOS)}")
    return graph(graph_name(repo))


@app.get("/api/repos")
def repos():
    loaded = set(connect().list_graphs())
    return [r for r in REPOS if graph_name(r) in loaded]


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


@app.get("/snapshot.json")
def snapshot():
    if not SNAPSHOT.exists():
        raise HTTPException(404)
    return FileResponse(SNAPSHOT)


@app.get("/api/overview")
def overview(repo: str = "psf/black"):
    g = pick(repo)
    results = ROOT / results_file(repo)
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
            "fragile": fragile, "evidence": json.loads(results.read_text(encoding="utf-8")) if results.exists() else None}


@app.post("/api/ask")
def ask(body: Ask, repo: str = "psf/black"):
    return agent.ask(pick(repo), body.q[:2000])


@app.post("/api/interview/start")
def start(body: Start, repo: str = "psf/black"):
    g = pick(repo)
    known = {h for (h,) in rows(g, "UNWIND $h AS h MATCH (p:Person {handle:h}) RETURN p.handle", h=body.handles[:5])}
    if not known:
        raise HTTPException(404, "None of those handles appear in the graph.")
    sid = interrogate.start(g, sorted(known))
    r = loss.scenario_meter(g, sid)
    return {"sid": sid, "items": r["items"][:8], **interrogate.questions(g, sid)}


@app.get("/api/interview/{sid}")
def resume(sid: str, repo: str = "psf/black"):
    g = pick(repo)
    if not rows(g, "MATCH (s:Scenario {id:$s}) RETURN s.id", s=sid):
        raise HTTPException(404, "That interview has expired.")
    r = loss.scenario_meter(g, sid)
    handles = [h for (h,) in rows(g, "MATCH (:Scenario {id:$s})-[:REMOVES]->(p:Person) RETURN p.handle", s=sid)]
    return {"sid": sid, "handles": handles, "items": r["items"][:8], **interrogate.questions(g, sid)}


@app.post("/api/interview/answer")
def answer(body: Answer, repo: str = "psf/black"):
    g = pick(repo)
    if not rows(g, "MATCH (s:Scenario {id:$s}) RETURN s.id", s=body.sid):
        raise HTTPException(404, "That interview has expired.")
    q = body.question
    if not (isinstance(q.get("id"), str) and isinstance(q.get("text"), str) and isinstance(q.get("modules"), list)):
        raise HTTPException(422, "question needs id, text and modules")
    q = {"id": q["id"][:200], "text": q["text"][:2000], "kind": q.get("kind"), "modules": [str(m)[:300] for m in q["modules"][:20]]}
    res = interrogate.answer(g, body.sid, q, body.text[:5000])
    return {**res, "next": interrogate.questions(g, body.sid)["questions"],
            "contradictions": interrogate.contradictions(g, body.sid)}


@app.post("/api/future")
def what_if(body: What, repo: str = "psf/black"):
    g = pick(repo)
    if body.kind == "leave":
        return future.leave(g, body.target)
    if body.kind == "reverse":
        return future.reverse(g, body.target)
    raise HTTPException(400, "kind must be 'leave' or 'reverse'")
