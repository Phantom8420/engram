"""Ask the brain a question. Claude picks the curated tool when ANTHROPIC_API_KEY is set; otherwise a keyword
router does. Either way the answer is the tool's own text and path, so every claim stays sourced."""
import os
import re
import sys

from engram import tools as T
from engram.db import graph
from engram.decisions import TOPICS, topic_of

TOOL_SPECS = [
    {"name": "who_knows", "description": "People who actually know a module (time-decayed authoring/review), with evidence PRs.",
     "input_schema": {"type": "object", "properties": {"module": {"type": "string", "description": "module path or file name"}},
                      "required": ["module"]}},
    {"name": "why", "description": "Decision lineage with verbatim quotes, for a module or a topic.",
     "input_schema": {"type": "object", "properties": {"module": {"type": "string"},
                                                       "topic": {"type": "string", "enum": list(TOPICS)}}}},
    {"name": "stale_docs", "description": "Docs edited before newer decisions on the code they describe.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "route_issue", "description": "Suggest reviewers for an issue from its text.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "central_modules", "description": "Most central code by PageRank over co-change.",
     "input_schema": {"type": "object", "properties": {}}},
]


def resolve(g, name):
    """Accept 'linegen' or 'src/black/linegen.py'."""
    return (T.find_modules(g, name, 1) or [name])[0] if name else None


def call(g, name, args):
    if name == "who_knows":
        return T.who_knows(g, resolve(g, args["module"]))
    if name == "why":
        return T.why(g, module=resolve(g, args.get("module"))) if args.get("module") else T.why(g, topic=args.get("topic"))
    if name == "stale_docs":
        return T.stale_docs(g)
    if name == "route_issue":
        return T.route_issue(g, args["text"])
    if name == "central_modules":
        return T.central_modules(g)
    raise ValueError(f"unknown tool {name}")


def route(g, q):
    """Deterministic fallback: pick a tool from the question's wording."""
    s = q.lower()
    mod = (T.find_modules(g, q, 1) or [None])[0]
    if re.search(r"\b(stale|outdated|out of date)\b", s):
        return "stale_docs", {}
    if re.search(r"\b(central|important|core|critical)\b", s):
        return "central_modules", {}
    if re.search(r"\b(who|expert|knows|ask|owner)\b", s) and mod and "review" not in s:
        return "who_knows", {"module": mod}
    if re.search(r"\b(why|decid|decision|history|reason)\w*", s):
        return ("why", {"module": mod}) if mod else ("why", {"topic": topic_of(q)})
    return "route_issue", {"text": q}


def ask_claude(g, q):
    import anthropic
    client = anthropic.Anthropic()
    msgs, results = [{"role": "user", "content": q}], []
    for _ in range(4):
        r = client.beta.messages.create(
            model="claude-opus-5-5", max_tokens=4000, tools=TOOL_SPECS, messages=msgs,
            system="You answer questions about the psf/black repo using only the tools. Quote tool output; never invent names or links.",
            output_config={"effort": "low"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        if r.stop_reason != "tool_use":
            text = "".join(b.text for b in r.content if b.type == "text")
            break
        msgs.append({"role": "assistant", "content": r.content})
        out = []
        for b in r.content:
            if b.type == "tool_use":
                res = call(g, b.name, b.input)
                results.append(res)
                out.append({"type": "tool_result", "tool_use_id": b.id, "content": res["answer"]})
        msgs.append({"role": "user", "content": out})
    else:
        text = results[-1]["answer"] if results else ""
    nodes, edges = {}, []
    for res in results:
        nodes.update({n["id"]: n for n in res["path"]["nodes"]})
        edges += res["path"]["edges"]
    return {"answer": text, "tools": len(results), "path": {"nodes": list(nodes.values()), "edges": edges}}


def ask(g, q):
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ask_claude(g, q)
    name, args = route(g, q)
    return {**call(g, name, args), "tool": name}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(ask(graph(), " ".join(sys.argv[1:]))["answer"])
