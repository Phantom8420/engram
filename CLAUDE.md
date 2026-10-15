# Graph Hacks (WeMakeDevs x FalkorDB) — Track 3: Company Brain

Window: 15 Oct 2026 00:01 IST -> 18 Oct 23:59 IST. Aim to submit by 18:00 on 18 Oct.
Rule 8: main coding only after the window opens. Public repo, README with graph model + Cypher
+ algorithms, demo video, AI disclosure in README. FalkorDB must do real work (no vector-store-with-a-graph-drawn-on-top).

## Plans on disk (read these first)
- `Graph-Hacks-Track3-Plan.pdf` — "Lineage": company brain on a real OSS org. Graph model, core Cypher, demo script.
- `Engram-Build-Plan.pdf` — "Engram": loss meter, ranked exit interview, contradiction check, backtest. Milestones M0-M10.
Extract text: `PYTHONIOENCODING=utf-8 python -c "import pypdf as P; [print(p.extract_text()) for p in P.PdfReader('X.pdf').pages]"`
(Read tool's PDF mode needs poppler, which is not installed.)

## Status (15 Oct 2026) — all phases built and pushed (private repo Phantom8420/engram)
Org = psf/black. Code map: `engram/` fetch -> load -> decisions -> knows (pipeline `python -m engram.build --heuristic`),
`tools.py` (brain queries), `agent.py` (keyword router / Claude tool loop), `loss.py`, `interrogate.py`, `future.py`
(GRAPH.COPY fork), `server.py` + `web/index.html` (one page; preview server "engram" in ../.claude/launch.json).
README has graph model, Cypher, algorithms, honesty notes, AI disclosure.
Left for the user: demo video, make repo public, submit form. No ANTHROPIC_API_KEY on this machine, so decisions are
heuristic; local Ollama models OOM.
Added later: MERGED/OWNS edges, topic-gated stale docs, stale-doc partial credit, thin-answer rejection,
`engram/evaluate.py` -> `eval/results.json` (departure heirs 40% vs 17%, decision precision 58%), README Evaluation.
UI = 1:1 copy of the user's Figma "Hyper ✦ Charts templates" dark widget board (small/medium/large "Sales Report"
cards on #27282D, ": CAPTION" labels, glowing pill bars). Keep that look; don't revert to a generic dashboard.
Still open: data dump so judges run without a GitHub token; tests for server/tools/future; mock test of Claude path.
Gotcha: `rows()` returns lists — single-column unpack is `for (x,) in`, two columns `for x, _ in`.

## Product = one temporal graph, three lenses
- **Present — the Brain (Lineage):** docs + tickets + chats + code in one graph. Who owns vs who actually knows,
  decision lineage with quotes, stale-doc flags, issue routing. Answers always return the graph path as source.
- **Past — the Interrogator (Engram):** takes a decision/module/person-leaving, walks back to sources, finds gaps and
  contradictions, then interviews the knowledge holder with questions ranked by greedy coverage gain. Answers written back as edges.
- **Future — (accepted by user):** counterfactual simulation. Fork the graph (GRAPH.COPY / scenario overlay), apply a
  hypothetical (person leaves, decision reversed, module deprecated), propagate along edges, show what breaks and which
  docs/decisions go stale. Backtest on a real departure is the proof it predicts. Stretch only; do not start before Present + Past work.

All three share one ingest (Engram M0-M3 == Lineage structural load). Build that once.

## Build order
1. Shared: repo, docker compose, GitHub ingest (cached, resumable), structural load, KNOWS scores.
2. Present: decision extraction with verified quotes, lineage + stale + routing queries, agent with curated Cypher tools.
3. Past: scenario nodes, loss meter, ranked questions, answer write-back, contradiction query.
4. Future: scenario fork + propagation; backtest.
5. README, demo video, submit.
Never cut: citations, decision lineage, loss meter, ranked questions with write-back, README with model + queries.

## Open decisions
- Track 3 only, or name Track 1 too.
- Whether to add an ANTHROPIC_API_KEY and re-extract decisions before recording the demo.

## Rules of this repo
- Public data only, handles only, no emails. Anonymise by default in demo.
- Read-only, curated Cypher templates; no free-form text-to-Cypher.
- Every submission text ends with an AI-disclosure line (AI assistance was used for ...).
- No Claude co-author trailers. Push every commit to origin (GitHub account Phantom8420).
- Run tests before every push.
