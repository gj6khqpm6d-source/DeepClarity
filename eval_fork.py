import asyncio, time, json, sys, os, sqlite3
from datetime import datetime
from collections import defaultdict
sys.path.insert(0, ".")
import open_deep_research.deep_researcher as dr
from open_deep_research.cost_tracker import CostTracker, TokenCallback
from langgraph.checkpoint.memory import MemorySaver

graph = dr.deep_researcher_builder.compile(checkpointer=MemorySaver())
CC_DB = os.path.expanduser("~/.cc-switch/cc-switch.db")

QUERIES = [
    "用通俗语言解释LSTM的遗忘门和输入门有什么区别",
    "What is transformer architecture and how does it differ from RNN?",
]


def get_cc_cost(start_ts, end_ts, model_filter="deepseek-v4-flash"):
    """Pull token/cost data from cc-switch proxy_request_logs for a time window."""
    if not os.path.exists(CC_DB):
        return None
    conn = sqlite3.connect(CC_DB)
    rows = conn.execute(
        "SELECT input_tokens, output_tokens, cache_read_tokens, "
        "input_cost_usd, output_cost_usd, cache_read_cost_usd, "
        "total_cost_usd, latency_ms "
        "FROM proxy_request_logs "
        "WHERE created_at BETWEEN ? AND ? AND model = ?",
        (int(start_ts), int(end_ts), model_filter),
    ).fetchall()
    conn.close()
    if not rows:
        return None
    s = defaultdict(float)
    for inp, out, cache, ic, oc, cc, tc, lat in rows:
        s["input"] += inp or 0
        s["output"] += out or 0
        s["cache"] += cache or 0
        s["cost"] += float(tc or 0)
        s["calls"] += 1
        s["latency"] += lat or 0
    s["avg_latency"] = s["latency"] / s["calls"] if s["calls"] else 0
    return dict(s)


async def run_one(query, mode, idx):
    tracker = CostTracker()
    cfg = {
        "configurable": {
            "thread_id": f"eval-{mode}-{idx}",
            "allow_clarification": (mode == "full"),
        },
        "callbacks": [TokenCallback(tracker)],
    }
    tag = f"{mode}/q{idx}"
    print(f"\n--- {tag} : {query[:35]} ---", flush=True)
    start = time.time()
    report = None
    async for event in graph.astream(
        {"messages": [{"role": "user", "content": query}]},
        cfg, stream_mode="updates",
    ):
        for nn, out in (event or {}).items():
            if out is not None:
                fr = out.get("final_report")
                if fr:
                    report = fr
    elapsed = time.time() - start
    cc = get_cc_cost(start, time.time())
    cc_cost = cc["cost"] if cc else 0
    cc_calls = cc["calls"] if cc else 0
    print(f"    {elapsed:.0f}s | report={len(report or '')} chars | "
          f"cc_cost=${cc_cost:.4f} ({cc_calls} LLM calls)", flush=True)
    return report or "", {"elapsed": elapsed, "cc_cost": cc_cost, "cc_calls": cc_calls}


JUDGE_RUBRIC = """Score this research report on 3 dimensions (1-5 integer each).
Output ONLY JSON: {"completeness": N, "relevance": N, "structure": N}
- completeness: covers main aspects of the query comprehensively
- relevance: directly addresses what was asked
- structure: well-organized with clear sections
"""


async def judge(query, report):
    from langchain.chat_models import init_chat_model
    m = init_chat_model(
        "anthropic:deepseek-v4-flash", max_tokens=200,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
    )
    m = m.with_config({"kwargs": {"base_url": "http://127.0.0.1:15721"}})
    content = f"Query: {query}\n\nReport (first 3000 chars):\n{report[:3000]}\n\nScore it."
    start = time.time()
    resp = await m.ainvoke([
        {"role": "system", "content": JUDGE_RUBRIC},
        {"role": "user", "content": content},
    ])
    elapsed = time.time() - start
    cc = get_cc_cost(start, time.time())
    try:
        scores = json.loads(resp.content)
    except Exception:
        scores = {"completeness": 0, "relevance": 0, "structure": 0, "raw": str(resp.content)[:100]}
    judge_cost = cc["cost"] if cc else 0
    print(f"    judge: {scores} (${judge_cost:.5f}, {elapsed:.1f}s)", flush=True)
    return scores, judge_cost


async def main():
    results = []
    for qi, query in enumerate(QUERIES):
        for mode in ["full", "baseline"]:
            report, meta = await run_one(query, mode, qi)
            scores, judge_cost = await judge(query, report)
            results.append({
                "query": query[:40], "mode": mode,
                "report_len": len(report), "scores": scores,
                "run_cost": meta["cc_cost"], "judge_cost": judge_cost,
                "elapsed": meta["elapsed"], "llm_calls": meta["cc_calls"],
            })

    print("\n" + "=" * 70)
    print("COMPARISON TABLE")
    print("=" * 70)
    for qi, query in enumerate(QUERIES):
        full = next(r for r in results if r["query"][:40] == query[:40] and r["mode"] == "full")
        base = next(r for r in results if r["query"][:40] == query[:40] and r["mode"] == "baseline")
        fs, bs = full["scores"], base["scores"]
        print(f"\nQuery: {query[:50]}")
        print(f"  {'metric':<15} {'full':>8} {'baseline':>10} {'delta':>8}")
        print(f"  {'-'*45}")
        for dim in ["completeness", "relevance", "structure"]:
            fv, bv = fs.get(dim, 0), bs.get(dim, 0)
            d = f"+{(fv-bv)/max(bv,1)*100:.0f}%" if fv != bv else "="
            print(f"  {dim:<15} {fv:>5}/5   {bv:>5}/5   {d:>7}")
        print(f"  {'report_len':<15} {full['report_len']:>8} {base['report_len']:>10} "
              f"{(full['report_len']-base['report_len'])/max(base['report_len'],1)*100:>+6.0f}%")
        print(f"  {'run_cost':<15} ${full['run_cost']:>.4f}  ${base['run_cost']:>.4f}  "
              f"{(full['run_cost']-base['run_cost'])/max(base['run_cost'],.0001)*100:>+6.1f}%")
        print(f"  {'time':<15} {full['elapsed']:>6.0f}s  {base['elapsed']:>6.0f}s")

    print(f"\nTotal eval cost (cc-switch): ${sum(r['run_cost']+r['judge_cost'] for r in results):.4f}")
    with open("eval_results.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("Results saved to eval_results.json")


if __name__ == "__main__":
    asyncio.run(main())
