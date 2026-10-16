import os

REPO = os.environ.get("ENGRAM_REPO", "psf/black")  # owner/name; each repo gets its own graph and cache
REPOS = {"psf/black": "engram", "pytest-dev/pytest": "engram_pytest"}


def graph_name(repo=REPO):
    return REPOS.get(repo) or "engram_" + repo.replace("/", "_").replace("-", "_")


def results_file(repo=REPO):
    return "eval/results.json" if repo == "psf/black" else f"eval/results_{repo.split('/')[1]}.json"
