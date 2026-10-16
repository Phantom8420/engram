# Engram

A company brain for an open-source project, built on one temporal FalkorDB graph. It is loaded from 1,977 merged pull
requests of [psf/black](https://github.com/psf/black) (and, as a second repo, 6,173 from
[pytest-dev/pytest](https://github.com/pytest-dev/pytest)) and looks at the project three ways:

- **Now (the brain).** Who actually knows a module, as opposed to who touched it once. Why the code is the way it is,
  as decisions with verbatim quotes and links. Which docs drifted behind newer decisions. Who should review a new issue.
- **Someone leaves (the interrogator).** A loss meter for how much important, unwritten knowledge goes with a person.
  Then an exit interview whose questions are ranked by how much of that loss each answer recovers. Answers are written
  back into the graph and the meter drops. An answer that says a recorded decision no longer holds is flagged.
- **What if (the future).** The graph is forked with `GRAPH.COPY` and a hypothetical is applied to the fork: a
  person stops contributing, or a decision is reversed. The same queries run on the fork and are diffed against the
  live graph: modules left with one person or nobody, docs to rewrite, later decisions built on the reversed one,
  decisions that come back into force. The live graph is never touched.

Every answer comes with the graph path it rests on, and the UI draws that path.

## Run it

```bash
docker compose up -d falkordb
pip install -r requirements.txt
python -m engram.build --heuristic    # fetch (cached under data/raw), load, extract decisions, derive KNOWS
uvicorn engram.server:app --port 8000 # landing page at http://localhost:8000, dashboard at /app
```

`engram.build` needs a GitHub token (`GITHUB_TOKEN`, or a logged-in `gh`). The first run fetches about 2,000 PRs
through the GraphQL API and makes a blobless clone for doc dates. Later runs reuse the cache.

With `ANTHROPIC_API_KEY` set, drop `--heuristic`: decisions are then extracted by Claude, and the "Now" box becomes a
Claude tool-use loop over the same curated tools. Delete `data/cache/decisions` to re-extract.

`docker compose up` runs the API in a container too, against the cached `./data`.

### Another repo

`ENGRAM_REPO=owner/name` points the pipeline at any GitHub repo. Each repo gets its own graph and its own cache under
`data/raw/owner__name`, and `evaluate` writes `eval/results_<name>.json`. The server only serves repos listed in
`engram.REPOS` whose graph exists; the dashboard's repo menu switches between them (`/app?repo=pytest-dev/pytest`).
The decision topics are tuned to Black, so other repos get mostly untopiced decisions, and decision precision has only
been hand-labelled for Black.

### Deploy on Vercel

Vercel runs the API and pages (`main.py` is the entrypoint), but it cannot host the database. Use a hosted FalkorDB
(FalkorDB Cloud has a free tier), load the graph into it from your machine, then point Vercel at it:

```bash
FALKORDB_HOST=<host> FALKORDB_PORT=<port> FALKORDB_USERNAME=<user> FALKORDB_PASSWORD=<password> python -m engram.build --heuristic
```

Set the same four variables in the Vercel project settings and deploy. `data/` stays local (`.vercelignore`).

Command-line entry points:

```bash
python -m engram.agent who knows linegen
python -m engram.loss cobaltt7               # loss meter if this person left
python -m engram.interrogate cobaltt7        # ranked exit-interview questions
python -m engram.future leave cobaltt7
python -m engram.future reverse pr5262-0
```

## Graph model

| Node | Key properties |
|---|---|
| `Person` | `handle`, `first_seen`, `last_seen` |
| `PR` | `number`, `title`, `url`, `created_at`, `merged_at` |
| `Issue` | `number`, `title`, `url`, `created_at` |
| `Module` (+ `Doc`) | `path` (first three path components), `kind` code/doc/test/other, `hub`, `importance`; docs also have `updated_at` from git |
| `Decision` | `id`, `summary`, `topic`, `quote` (verbatim), `source_url`, `confidence`, `valid_from`, `valid_to`, `status` |
| `Topic` | `name` |
| `Scenario`, `Capture` | an exit interview and its written-back answers |

| Edge | Meaning |
|---|---|
| `(Person)-[:AUTHORED\|REVIEWED\|COMMENTED {at}]->(PR)` | activity |
| `(Person)-[:MERGED {at}]->(PR)` | who pressed merge; not counted in KNOWS |
| `(Person)-[:OWNS {share}]->(Module)` | derived: at least a quarter of the module's merges in the last two years (3+ merges) |
| `(PR)-[:TOUCHES]->(Module)`, `(PR)-[:FIXES]->(Issue)` | structure |
| `(Module)-[:CO_CHANGES {n}]->(Module)` | edited in the same PR `n` times |
| `(Doc)-[:DESCRIBES {n}]->(Module)` | doc repeatedly edited together with that code |
| `(Person)-[:KNOWS {score, last_seen}]->(Module)` | derived, time-decayed |
| `(Decision)-[:DECIDED_IN]->(PR)`, `-[:AFFECTS]->(Module)`, `-[:BY]->(Person)`, `-[:ABOUT]->(Topic)` | lineage |
| `(Decision)-[:SUPERSEDES]->(Decision)` | a later PR references the earlier one on the same topic; the old one gets `valid_to` |
| `(Scenario)-[:REMOVES]->(Person)`, `(Scenario)-[:HAS]->(Capture)-[:DOCUMENTS]->(Module)` | interview |
| `(Capture)-[:EXPLAINS\|CONTRADICTS]->(Decision)` | interview answers about a decision |

The loaded graph has about 4,000 nodes and 17,000 edges.

## The queries that matter

**Who knows a module.** KNOWS is a time-decayed sum over activity: authored 1.0, reviewed 0.5, commented 0.2, times
`exp(-age / 365 days)`, so knowledge halves in about eight months without touching the code.

```cypher
MATCH (p:Person)-[a:AUTHORED|REVIEWED|COMMENTED]->(pr:PR)-[:TOUCHES]->(m:Module)
WHERE pr.created_at <= $asof AND m.hub = false
WITH p, m, max(pr.created_at) AS last,
     sum(CASE type(a) WHEN 'AUTHORED' THEN 1.0 WHEN 'REVIEWED' THEN 0.5 ELSE 0.2 END
         * exp(-toFloat($asof - pr.created_at) / $decay)) AS s
WHERE s > 0.05
MERGE (p)-[k:KNOWS]->(m) SET k.score = s, k.last_seen = last
```

**Who owns it vs who knows it.** Merges are gatekeeping, not knowledge, so they become a separate edge. `who_knows`
shows both, and calls out an owner who approves changes without being among the top three knowers.

```cypher
MATCH (p:Person)-[:MERGED]->(pr:PR)-[:TOUCHES]->(m:Module)
WHERE pr.created_at <= $asof AND pr.created_at > $asof - 2 * 365 * 86400 AND m.hub = false
WITH m, p, count(pr) AS n
WITH m, collect([p, n]) AS ps, sum(n) AS total
UNWIND ps AS x
WITH m, x[0] AS p, x[1] AS n, total WHERE total >= 3 AND toFloat(n) / total >= 0.25
MERGE (p)-[o:OWNS]->(m) SET o.merges = n, o.share = toFloat(n) / total
```

**Stale docs.** A doc is stale when decisions about the code it describes were made after it was last edited, and the
doc actually talks about the decision's topic. Stub docs under 200 bytes are skipped.

```cypher
MATCH (doc:Doc)-[:DESCRIBES]->(m:Module)<-[:AFFECTS]-(d:Decision)
WHERE d.valid_from > doc.updated_at AND d.valid_to IS NULL AND d.topic IN doc.topics
RETURN doc.path, count(DISTINCT d) AS newer_decisions ORDER BY newer_decisions DESC
```

**Decision lineage.**

```cypher
MATCH (d:Decision)-[:AFFECTS]->(:Module {path: $module})
MATCH (d)-[:DECIDED_IN]->(pr:PR)
OPTIONAL MATCH (d)-[:SUPERSEDES]->(old:Decision)
OPTIONAL MATCH (d)-[:BY]->(who:Person)
RETURN d.summary, d.quote, d.status, pr.url, who.handle, collect(old.id)
```

**Exit-interview candidates.** Decisions the leaver took part in, on code that would be left at risk.

```cypher
MATCH (:Scenario {id: $s})-[:REMOVES]->(p:Person)
MATCH (d:Decision)-[:DECIDED_IN]->(pr:PR)
WHERE (d)-[:BY]->(p) OR (p)-[:AUTHORED|REVIEWED]->(pr)
MATCH (d)-[:AFFECTS]->(m:Module {kind: 'code'})
WHERE d.valid_to IS NULL
RETURN d.id, d.quote, pr.number, collect(DISTINCT m.path)
```

**Counterfactual.** `graph.copy("fork_…")`, then for example
`MATCH (:Person {handle: $h})-[k:KNOWS]->() DELETE k` on the fork, and the holder counts per module are compared
with the live graph.

All queries live in `engram/tools.py`, `loss.py`, `interrogate.py` and `future.py`. They are fixed and parameterised.
Nothing a user types is turned into Cypher.

## Algorithms

- **PageRank** (`algo.pageRank`) over `CO_CHANGES` finds the structurally central code.
- **Module importance** is 0.5 × PageRank + 0.3 × PRs in the last year + 0.2 × active decisions, each scaled to 0..1.
- **Loss meter.** For each code module, the share of KNOWS score held by the leavers. Written knowledge earns credit:
  a current doc or an interview answer 1.0, a stale doc 0.5. The share is cut by 70% × credit, because writing only
  partly replaces a person. Interview answers under 12 words, or that mostly repeat the question, are rejected. The result is weighted by
  importance and summed into a percentage. A module is fragile when at most one person with a score of 0.3 or more
  is left.
- **Question ranking** is greedy weighted max-coverage. Each question covers some at-risk modules, and the next
  question is the one that recovers the most meter points that are still uncovered. A test checks it against brute
  force (`tests/test_core.py`).
- **Decision extraction** (the default, deterministic path) keeps sentences with explicit decision cues ("instead
  of", "rather than", "we decided", "agreed"…). It prefers sentences that state a reason and skips bug reports. Any
  extractor, Claude included, only keeps a decision if its quote appears verbatim in the thread.

## Evaluation

`python -m engram.evaluate` writes `eval/results.json`. Every test forks the graph, deletes everything after a cut-off
date in the fork, re-derives KNOWS as of that day, and scores the prediction against what really happened next.

| Test | Engram | Baseline |
|---|---|---|
| **Real departures.** Every contributor with 15+ PRs who then went quiet for a year: 8 people, 72 modules they held. Who actually did the most work on each module in the following year? | KNOWS names the heir first in **51%**, in its top two in **72%** | busiest remaining person: 31% |
| **Next author**, four yearly cut-offs (199 PRs). Is the author of each PR in the KNOWS top 3 for the code it touches? | 47% | busiest three on code: 39%; busiest three overall: **50%** |
| **Next reviewer**, same cut-offs (289 PRs) | 83% | busiest three on code: 78%; busiest three overall: **93%** |
| **Decision precision.** Held-out sample of 40 extracted decisions, labelled by hand | **58%** real decisions | — |

Departures are where KNOWS earns its keep: when someone leaves, the busiest person on the project is usually not the
one who picks up their modules. For predicting the next PR, simply naming the three busiest people overall does as well
or better, because a few maintainers review almost everything. Both results are reported as they came out. Adding
overall activity to KNOWS as a prior (weight fixed at 1 before scoring) did not close the gap: 44% for authors, 84% for
reviewers (`blend_hit@3`). The
departures are picked by rule (`evaluate.departed`), not by hand. The decision filters were tuned on a separate sample
(seed 7) before the held-out one (seed 2026) was labelled. Issue routing is not scored on its own: only 15 issues in the
last year were fixed by an existing contributor.

### Second repo: pytest-dev/pytest

Same code, same rules, no tuning: 6,173 merged PRs, written to `eval/results_pytest.json`.

| Test | Engram | Baseline |
|---|---|---|
| Real departures: 7 people, 91 modules | heir first in 34%, in its top two in **67%** | busiest remaining person: **37%** |
| Next author, four cut-offs (406 PRs) | **73%** | busiest three: 72% |
| Next reviewer, same cut-offs (850 PRs) | **87%** | busiest three: 87% |

On pytest KNOWS ties the baseline for the next PR, and for departures its first pick is slightly worse than naming
the busiest person, though its top two find the heir twice as often. pytest is run by a small core that touches
everything, which is the case where "ask the busiest person" is already a good answer. The method carries over to a
new repo; the departure edge measured on Black is smaller here.

## Honesty notes

- The decisions in this build come from the deterministic extractor, not an LLM. About four in ten are a design remark
  rather than a ruling (see Evaluation). Every one links to its verbatim source, so a wrong one is easy to spot.
- Contradiction detection is keyword-based ("no longer", "since changed", "reverted", …) and also needs the answer to
  share words with the decision's quote. A bare "we changed X" is how people describe the decision itself, so it does
  not count. With an LLM backend it could compare meanings. That is not built.
- `SUPERSEDES` only comes from explicit `#123` references on the same topic, so it misses silent reversals.
- Modules are the first three path components (`src/black/linegen.py`). Files touched by more than a quarter of all
  PRs, such as `CHANGES.md`, are treated as hubs and left out of KNOWS and co-change.
- The decision labels in `eval/decision_labels.json` were made with AI assistance, by reading each linked source thread.

## Data

Public GitHub data only: PR titles, bodies, reviews, comments, linked issues and file paths from psf/black. No emails
are stored. People appear by GitHub handle, and the UI hides handles behind stable aliases unless "Show handles" is
ticked.

## AI disclosure

AI assistance was used for this project: Claude Code helped design, write and test the code and wrote most of this
README.
