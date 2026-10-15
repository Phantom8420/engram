"""JSON-returning LLM call: Claude if ANTHROPIC_API_KEY is set, a local Ollama model if OLLAMA_MODEL is set, else None
(callers then fall back to deterministic extraction)."""
import json
import os

import httpx

CLAUDE_MODEL = "claude-opus-5-5"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL")


def backend():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    return "ollama" if OLLAMA_MODEL else None


def _parse(text):
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1]) if start >= 0 else {}


def ask_json(system, prompt, which=None):
    which = which or backend()
    if which == "claude":
        import anthropic
        client = anthropic.Anthropic()
        r = client.beta.messages.create(
            model=CLAUDE_MODEL, max_tokens=16000, system=system,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
        if r.stop_reason == "refusal":
            return {}
        return _parse("".join(b.text for b in r.content if b.type == "text"))
    if which == "ollama":
        r = httpx.post(f"{OLLAMA_URL}/api/chat", timeout=600, json={
            "model": OLLAMA_MODEL, "stream": False, "format": "json",
            "options": {"temperature": 0, "num_ctx": 4096},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        })
        r.raise_for_status()
        return _parse(r.json()["message"]["content"])
    return None
