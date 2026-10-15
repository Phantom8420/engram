"""Full pipeline: fetch (cached) -> structural load -> decisions -> KNOWS + importance."""
import sys

from engram import decisions, fetch, knows, llm, load
from engram.db import rows

if __name__ == "__main__":
    fetch.clone_repo()
    fetch.fetch_prs()
    g, data = load.run()
    decisions.run(g, fetch.load_prs(), None if "--heuristic" in sys.argv else llm.backend())
    knows.derive(g)
    print(rows(g, "MATCH (n) RETURN labels(n)[0], count(n) ORDER BY count(n) DESC"))
    print(rows(g, "MATCH ()-[e]->() RETURN type(e), count(e) ORDER BY count(e) DESC"))
