import json
import memory
import tools


def step(observation):
    state = dict(observation.get("state", {}))
    if observation.get("pending_action"):
        return {"action": observation["pending_action"], "state": state}
    phase = state.get("phase", "discover")
    if phase == "discover":
        state["phase"] = "read"
        return {"action": {"tool": "list_files"}, "state": state}
    if phase == "read":
        files = observation["last"].get("files", [])
        state["phase"] = "solve"
        return {"action": {"tool": "read_file", "path": tools.choose_file(files)}, "state": state}
    if state.get("asked") and observation["last"].get("written"):
        if observation["policy"].get("verify_before_finish", False):
            state["verified"] = True
            return {"action": {"tool": "run_tests"}, "state": state}
        return {"action": {"tool": "finish"}, "state": state}
    if state.get("verified") and observation["last"].get("all_passed"):
        return {"action": {"tool": "finish"}, "state": state}
    state["asked"] = True
    request = tools.build_request(observation["task"], memory.render(observation["trace"],
                                  observation["policy"].get("max_context_chars", 12000)),
                                  observation.get("skills", []))
    return {"action": {"tool": "ask_model", "request": request}, "state": state}


def propose(context):
    request = {
        "system": "Improve the agent program using only the supplied training evidence. "
                  "Return a JSON mutation: summary, rationale, edits (path/content). "
                  "Keep step(observation) and propose(context). Do not embed solutions or task IDs. "
                  "Improve general mechanisms; obey the permitted component and import contract.",
        "payload": {
            "component": context["component"],
            "allowed_files": context["allowed_files"],
            "allowed_imports": context["allowed_imports"],
            "genome": context["genome"],
            "training_evidence": context["training_evidence"],
            "action_contract": context["action_contract"],
        },
    }
    return {"request": request}
