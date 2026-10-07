def render(trace, max_chars=12000):
    return [{"action": entry["action"], "result": str(entry["result"])[:400]}
            for entry in trace[-2:]]
