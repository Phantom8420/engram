"""Routing, input limits and the fork, without needing the full psf/black graph. The fork test runs only when a
FalkorDB is reachable (it skips otherwise; rerun with -rs to see which)."""
import pytest
from fastapi.testclient import TestClient

from engram import agent, interrogate, server, tools

MODULES = [("src/black/cache.py", 0.3, "code", 2), ("src/blib2to3/pgen2/tokenize.py", 0.5, "code", 3),
           ("blib2to3/pgen2/tokenize.py", 0.8, "code", 0), ("src/black/linegen.py", 0.9, "code", 5),
           ("docs/usage.md", 0.1, "doc", 1)]
PEOPLE = [("JelleZijlstra",), ("jellyfish42",), ("cobaltt7",)]


def fake_rows(g, q, **params):
    if "WHERE m.hub = false AND m.kind IN" in q:
        return MODULES
    if "MATCH (p:Person)-[k:KNOWS]->() WITH p, sum(k.score) AS s" in q:
        return PEOPLE
    return []


@pytest.fixture
def fake_graph(monkeypatch):
    monkeypatch.setattr(tools, "rows", fake_rows)
    monkeypatch.setattr(agent, "rows", fake_rows)


def test_find_modules_matches_word_variants(fake_graph):
    assert tools.find_modules(None, "how does caching work") == ["src/black/cache.py"]
    assert tools.find_modules(None, "who knows the tokenizer", 1) == ["src/blib2to3/pgen2/tokenize.py"]
    assert tools.find_modules(None, "who knows linegen") == ["src/black/linegen.py"]
    # interview answers only count names written as code, not English words
    assert tools.find_modules(None, "caching is subtle", strict=True) == []


def test_route_sends_departures_to_the_fork(fake_graph):
    assert agent.route(None, "what happens if Jelle leaves") == ("leave", {"handle": "JelleZijlstra"})
    assert agent.route(None, "who knows linegen.py") == ("who_knows", {"module": "src/black/linegen.py"})
    assert agent.route(None, "which docs are out of date")[0] == "stale_docs"


def test_unmatched_question_gets_examples(fake_graph, monkeypatch):
    monkeypatch.setattr(agent.T, "route_issue", lambda g, text: {"answer": "x", "data": [], "path": {"nodes": [], "edges": []}})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "Questions I can answer" in agent.ask(None, "zzzz qqqq")["answer"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "graph", lambda: None)
    return TestClient(server.app)


def test_answer_to_unknown_interview_is_404(client, monkeypatch):
    monkeypatch.setattr(server, "rows", lambda g, q, **p: [])
    r = client.post("/api/interview/answer", json={"sid": "s-nope", "question": {"id": "x", "text": "y", "modules": []}, "text": "z"})
    assert r.status_code == 404


def test_answer_needs_a_well_formed_question(client, monkeypatch):
    monkeypatch.setattr(server, "rows", lambda g, q, **p: [["s-1"]])
    r = client.post("/api/interview/answer", json={"sid": "s-1", "question": {"id": 3}, "text": "z"})
    assert r.status_code == 422


def test_interview_stops_taking_answers_at_the_cap(monkeypatch):
    monkeypatch.setattr(interrogate.loss, "scenario_meter", lambda g, sid: {"meter": 12.0})
    monkeypatch.setattr(interrogate, "rows", lambda g, q, **p: [[interrogate.MAX_CAPTURES]])
    r = interrogate.answer(None, "s-1", {"id": "mod:a.py", "kind": "module", "text": "q", "modules": ["a.py"]}, "long " * 40)
    assert r["rejected"] and r["meter"] == 12.0


def test_fork_never_touches_the_live_graph():
    from engram.db import graph
    from engram.future import fork
    try:
        g = graph("engram_test")
        g.query("CREATE (:Person {handle:'a'})-[:KNOWS {score:1.0}]->(:Module {path:'m.py', kind:'code'})")
    except Exception as e:
        pytest.skip(f"no FalkorDB reachable: {e}")
    try:
        f = fork(g)
        f.query("MATCH ()-[k:KNOWS]->() DELETE k")
        assert f.query("MATCH ()-[k:KNOWS]->() RETURN count(k)").result_set[0][0] == 0
        assert g.query("MATCH ()-[k:KNOWS]->() RETURN count(k)").result_set[0][0] == 1
        f.delete()
    finally:
        g.delete()


def test_who_knows_names_the_last_people_when_knowledge_has_faded(monkeypatch):
    past = [["ambv", 4, 1500000000]]
    monkeypatch.setattr(tools, "rows", lambda g, q, **p: past if "count(DISTINCT pr)" in q else [])
    r = tools.who_knows(None, "src/blib2to3/pgen2/tokenize.py")
    assert "Nobody has worked on" in r["answer"] and "@ambv (4 PRs" in r["answer"]


def test_activity_prior_lifts_people_active_everywhere():
    from engram.evaluate import top3
    k = {"a.py": {"x": 1.0, "y": 0.5, "z": 0.4}}
    assert top3(k, {"a.py"}) == ["x", "y", "z"]
    assert top3(k, {"a.py"}, {"z": 1.0})[0] == "z"
