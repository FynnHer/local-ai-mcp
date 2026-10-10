#!/usr/bin/env python3
"""Benchmark a local model the way the MCP uses it.

  python3 bench.py coding            # alias from models.json (or a raw Ollama model name)
  python3 bench.py coding --ask-only # skip the slower Claude Code delegate scenarios

ask scenarios call Ollama directly (same as local_ask) and report Ollama's own timings.
delegate scenarios go through server.local_delegate, so they show up in the dashboard too.
"""
import json, os, shutil, sys, tempfile, time, urllib.request, uuid
from pathlib import Path

import server

HERE = Path(__file__).resolve().parent


def chat(model, prompt, think=False, num_predict=None):
    body = {"model": model, "stream": False, "think": think, "messages": [{"role": "user", "content": prompt}]}
    if num_predict:
        body["options"] = {"num_predict": num_predict}
    t = time.time()
    with server.ollama("/api/chat", body) as r:
        d = json.load(r)
    wall = time.time() - t
    prompt_n, cached = d.get("prompt_eval_count", 0), d.get("prompt_eval_cached_count", 0)
    pe_s, ev_s = d.get("prompt_eval_duration", 0) / 1e9, d.get("eval_duration", 0) / 1e9
    return {
        "prompt": prompt_n, "cached": cached, "out": d.get("eval_count", 0),
        "prefill_tps": (prompt_n - cached) / pe_s if pe_s else 0,  # only the uncached part is evaluated
        "gen_tps": d.get("eval_count", 0) / ev_s if ev_s else 0,
        "ttft": d.get("load_duration", 0) / 1e9 + pe_s, "wall": wall,
    }


def delegate(model_alias, task, cwd, allow_edits=False):
    t = time.time()
    answer = server.local_delegate(task, cwd, model_alias, allow_edits)
    r = server.stats()["recent"][0]
    live = server.run_detail(r["id"])["live"]
    return {"prompt": r["prompt_tokens"], "cached": r["cached_tokens"], "out": r["gen_tokens"],
            "turns": live.count("── turn "), "wall": time.time() - t, "answer": answer}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    alias = args[0] if args else None
    model = server.resolve(alias) if alias in server.load_models() or not alias else alias
    ask_only = "--ask-only" in sys.argv

    if [r for r in server.stats()["running"]]:
        print("warning: other local AI calls are running, numbers will be skewed\n")

    code = (HERE / "server.py").read_text()
    big = code + (HERE / "dashboard.html").read_text()
    nonce = lambda: f"[run {uuid.uuid4()}]\n"  # unique first line = no prompt cache hit
    asks = [
        ("short Q, short A", lambda: chat(model, "What is the capital of France? One word.")),
        ("long answer (~500 tok)", lambda: chat(model, "Write about 400 words on the history of the printing press.",
                                                num_predict=700)),
        ("reasoning, thinking on", lambda: chat(model, "A bat and a ball cost 1.10 in total. The bat costs 1.00 more "
                                                "than the ball. How much is the ball? Answer briefly.", think=True)),
    ]
    with server.ollama("/api/show", {"model": model}, timeout=10) as r:
        params = json.load(r).get("parameters", "")
    ctx = int(next((l.split()[1] for l in params.splitlines() if l.startswith("num_ctx")), 32768))
    sizes = [("~7k", code), ("~12k", big)] + ([("~36k", big * 3)] if ctx >= 60000 else [])  # only if it fits
    for label, text in sizes:
        fixed = nonce() + text + "\n\nSummarise this code in 3 bullet points."
        asks += [
            (f"read {label} prompt, cold", lambda p=fixed: chat(model, p, num_predict=150)),
            (f"same prompt again, cached", lambda p=fixed: chat(model, p, num_predict=150)),
        ]

    print(f"model: {model}\n")
    print(f"{'ask scenario':28} {'prompt':>7} {'cached':>7} {'out':>5} {'read tok/s':>10} {'gen tok/s':>9} "
          f"{'1st token':>9} {'total':>7}")
    for label, fn in asks:
        r = fn()
        print(f"{label:28} {r['prompt']:>7,} {r['cached']:>7,} {r['out']:>5,} {r['prefill_tps']:>10,.0f} "
              f"{r['gen_tps']:>9.1f} {r['ttft']:>8.1f}s {r['wall']:>6.1f}s")

    if ask_only:
        return
    work = Path(tempfile.mkdtemp(prefix="bench-"))
    (work / "stats_util.py").write_text("def mean(xs):\n    return sum(xs) / len(xs)\n")
    delegates = [
        ("read 1 file", lambda: delegate(alias or model, "How many entries does models.json have, and what are "
                                         "their aliases? One sentence.", str(HERE))),
        ("read 1 file again (warm)", lambda: delegate(alias or model, "How many entries does models.json have, and "
                                                      "what are their aliases? One sentence.", str(HERE))),
        ("explore code (grep+read)", lambda: delegate(alias or model, "In server.py, which functions write to the "
                                                      "runs table in SQLite? List function names with line numbers, "
                                                      "nothing else.", str(HERE))),
        ("small edit", lambda: delegate(alias or model, "Add a function median(xs) to stats_util.py, next to mean. "
                                        "Handle even and odd lengths. Change nothing else. Reply 'done'.",
                                        str(work), allow_edits=True)),
    ]
    print(f"\n{'delegate scenario':28} {'turns':>5} {'prompt':>7} {'cached':>7} {'out':>5} {'total':>7}")
    try:
        for label, fn in delegates:
            r = fn()
            print(f"{label:28} {r['turns']:>5} {r['prompt']:>7,} {r['cached']:>7,} {r['out']:>5,} {r['wall']:>6.0f}s"
                  f"   → {r['answer'].strip().splitlines()[0][:70] if r['answer'].strip() else '(empty)'}")
        ns = {}
        exec((work / "stats_util.py").read_text(), ns)  # does the edit actually work?
        ok = ns["median"]([3, 1, 2]) == 2 and ns["median"]([4, 1, 3, 2]) == 2.5
        print(f"\nsmall edit check: median() {'works' if ok else 'is WRONG'}")
    except Exception as e:
        print(f"\nsmall edit check failed: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
