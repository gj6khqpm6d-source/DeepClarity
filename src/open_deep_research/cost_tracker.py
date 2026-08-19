"""Lightweight token usage and cost tracking for deep research runs.

Attach a TokenCallback to the graph's config["callbacks"] to capture every LLM
call. Call tracker.report() at the end for a summary. Prices are approximate.
"""
from collections import defaultdict
from langchain_core.callbacks import BaseCallbackHandler

# Approximate prices per 1M tokens (USD). Update when providers change.
PRICING = {
    "deepseek-chat":   {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "gpt-4.1":         {"input": 2.00,  "output": 8.00},
    "gpt-4.1-mini":    {"input": 0.40,  "output": 1.60},
    "gpt-5":           {"input": 2.50,  "output": 10.00},
    "default":         {"input": 0.27,  "output": 1.10},
}

class CostTracker:
    def __init__(self):
        self.records = []

    def add(self, prompt_tokens, completion_tokens, model="unknown"):
        self.records.append({
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "model": model,
        })

    def summary(self):
        by_model = defaultdict(lambda: {"input": 0, "output": 0, "calls": 0, "cost_usd": 0.0})
        for r in self.records:
            m = r["model"]
            d = by_model[m]
            d["input"] += r["prompt"]
            d["output"] += r["completion"]
            d["calls"] += 1
            price = PRICING.get(m, PRICING["default"])
            d["cost_usd"] += (r["prompt"] * price["input"] + r["completion"] * price["output"]) / 1_000_000
        total_in = sum(d["input"] for d in by_model.values())
        total_out = sum(d["output"] for d in by_model.values())
        total_cost = sum(d["cost_usd"] for d in by_model.values())
        return {
            "by_model": dict(by_model),
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_cost_usd": round(total_cost, 4),
            "total_calls": len(self.records),
        }

    def report(self, tag=""):
        s = self.summary()
        lines = [f"=== Cost {tag} ==="]
        for m, d in s["by_model"].items():
            lines.append(f"  {m}: {d['calls']} calls, {d['input']} in + {d['output']} out = ${d['cost_usd']:.4f}")
        lines.append(f"  TOTAL: {s['total_input_tokens']} in + {s['total_output_tokens']} out = ${s['total_cost_usd']:.4f}")
        return "\n".join(lines)


class TokenCallback(BaseCallbackHandler):
    """LangChain callback that records every LLM call into a CostTracker."""

    def __init__(self, tracker: CostTracker):
        self.tracker = tracker

    def on_llm_end(self, response, **kwargs):
        try:
            out = response.llm_output or {}
            usage = out.get("token_usage") or {}
            prompt = usage.get("prompt_tokens", 0) or 0
            completion = usage.get("completion_tokens", 0) or 0
            model = out.get("model_name", "unknown") or "unknown"
            if prompt or completion:
                self.tracker.add(prompt, completion, model)
        except Exception:
            pass  # tracker is best-effort, never block the graph
