"""
SSE streaming generator — calls nodes directly for fine-grained control.
Yields server-sent event strings consumed by FastAPI StreamingResponse.
"""

import asyncio
import json
from .schemas import AgentState
from .nodes import (
    classify_node, search_node, change_node, cancel_node,
    qa_node, clarify_node, error_node,
)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _blank_state(message: str, user_id: str, context: dict | None = None) -> AgentState:
    ctx = context or {}
    return {
        "user_message": message,
        "user_id": user_id,
        "intent": "",
        "confidence": 0.0,
        "entities": {},
        "previous_entities": ctx.get("previous_entities", {}),  # correction flow
        "clarification_needed": False,
        "thinking_steps": [],
        "llm_response": "",
        "ui_state": "",
        "result_data": {},
        "error": None,
    }


async def stream_agent_events(
    message: str,
    user_id: str = "demo_user",
    context: dict | None = None,
):
    """
    Async generator of SSE strings.

    Event sequence:
      thinking_start
      thinking_item  {text, ok}
      thinking_progress {value: 0-100}
      result  {intent, ui_state, result_data, entities, llm_response, thinking_steps}
      error   {message}

    ui_state values:
      show_destinations | show_change_booking | show_cancel_booking
      show_message | show_failure | ask_clarification | show_error
    """
    yield _sse({"type": "thinking_start"})
    await asyncio.sleep(0.05)

    state = _blank_state(message, user_id, context)

    # ── 1. Classify ───────────────────────────────────────────────────────────
    try:
        state = await classify_node(state)
    except Exception as e:
        yield _sse({"type": "error", "message": f"Classify error: {e}"})
        return

    for step in state.get("thinking_steps", []):
        yield _sse({"type": "thinking_item", "text": step["text"], "ok": step["ok"]})
        await asyncio.sleep(0.25)

    yield _sse({"type": "thinking_progress", "value": 40})

    # ── 2. Route to action node ───────────────────────────────────────────────
    intent = state.get("intent", "unclear")
    clarify_needed = state.get("clarification_needed", False)

    if clarify_needed or intent == "unclear":
        label, node_fn = ("❓ Cần thêm thông tin...", clarify_node)
    else:
        action_map = {
            "search":     ("🔍 Đang tìm kiếm khách sạn phù hợp...", search_node),
            "correction": ("🔄 Đang cập nhật kết quả tìm kiếm...", search_node),
            "change":     ("📅 Đang kiểm tra booking và ngày trống...", change_node),
            "cancel":     ("💰 Đang tính toán phí hủy và hoàn tiền...", cancel_node),
            "qa":         ("💬 Đang chuẩn bị câu trả lời...", qa_node),
        }
        label, node_fn = action_map.get(intent, ("🔍 Đang xử lý...", search_node))

    yield _sse({"type": "thinking_item", "text": label, "ok": True})
    await asyncio.sleep(0.15)

    try:
        state = await node_fn(state)
    except Exception as e:
        yield _sse({"type": "error", "message": f"Node error ({intent}): {e}"})
        return

    yield _sse({"type": "thinking_progress", "value": 100})
    await asyncio.sleep(0.08)

    # ── 3. Yield final result ─────────────────────────────────────────────────
    if state.get("error"):
        yield _sse({"type": "error", "message": state["error"]})
    else:
        yield _sse({
            "type": "result",
            "intent": state.get("intent", ""),
            "ui_state": state.get("ui_state", ""),
            "result_data": state.get("result_data", {}),
            "entities": state.get("entities", {}),
            "thinking_steps": state.get("thinking_steps", []),
            "llm_response": state.get("llm_response", ""),
        })


async def run_agent(
    message: str,
    user_id: str = "demo_user",
    context: dict | None = None,
) -> dict:
    """Non-streaming: run full graph and return final state dict."""
    from .graph import agent_graph
    return await agent_graph.ainvoke(_blank_state(message, user_id, context))
