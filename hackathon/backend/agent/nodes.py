"""
Agent nodes — each function maps to one step in the LangGraph pipeline.
All action nodes use LLM.bind_tools() for real tool calling.
4 paths supported: happy, low-confidence (clarify), failure, correction.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Literal

from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

_BACKEND = str(Path(__file__).parent.parent)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import mock_db
import scoring as _scoring
from .schemas import AgentState, IntentResult
from .prompts import (
    CLASSIFY_SYSTEM, SEARCH_SYSTEM, CORRECTION_SYSTEM,
    CHANGE_SYSTEM, CANCEL_SYSTEM, QA_SYSTEM, CLARIFY_SYSTEM, FAILURE_SYSTEM,
)
from .tools import search_hotels, get_booking, calculate_refund, check_available_dates


# ── LLM FACTORY ──────────────────────────────────────────────────────────────

def _llm(env_key: str = "OPENAI_MODEL_FAST") -> ChatOpenAI:
    return ChatOpenAI(model=os.getenv(env_key, "gpt-4o-mini"), temperature=0)


# ── CLASSIFY NODE ─────────────────────────────────────────────────────────────

async def classify_node(state: AgentState) -> AgentState:
    llm = _llm().with_structured_output(IntentResult)
    try:
        result: IntentResult = await llm.ainvoke([
            SystemMessage(content=CLASSIFY_SYSTEM),
            HumanMessage(content=state["user_message"]),
        ])
        entities = result.entities.model_dump(exclude_none=True)

        # For correction: merge with previous_entities so downstream has full picture
        prev = state.get("previous_entities", {})
        if result.intent == "correction" and prev:
            entities = {**prev, **{k: v for k, v in entities.items() if v is not None}}

        steps = _thinking_steps(result.intent, entities)
        return {
            **state,
            "intent": result.intent,
            "confidence": result.confidence,
            "entities": entities,
            "clarification_needed": result.clarification_needed,
            "thinking_steps": steps,
            "error": None,
        }
    except Exception as e:
        return _fallback_classify(state, str(e))


def _thinking_steps(intent: str, entities: dict) -> list:
    steps = []
    if entities.get("date_range"):
        steps.append({"text": entities["date_range"], "ok": True})
    elif entities.get("nights"):
        steps.append({"text": f"{entities['nights']} đêm", "ok": True})
    elif intent in ("search", "correction"):
        steps.append({"text": "chưa rõ thời gian", "ok": False})

    if entities.get("guest_count"):
        label = f"{entities['guest_count']} người"
        if entities.get("trip_type") == "family":
            label += " (gia đình)"
        elif entities.get("trip_type") == "couple":
            label += " (cặp đôi)"
        steps.append({"text": label, "ok": True})
    elif intent in ("search", "correction"):
        steps.append({"text": "chưa rõ số người", "ok": False})

    if entities.get("vibe"):
        vibe_map = {"beach": "nghỉ dưỡng biển", "city": "city break",
                    "nature": "thiên nhiên", "culture": "văn hóa"}
        steps.append({"text": vibe_map.get(entities["vibe"], entities["vibe"]), "ok": True})

    if entities.get("destination"):
        steps.append({"text": f"địa điểm: {entities['destination']}", "ok": True})
    elif intent in ("search", "correction"):
        steps.append({"text": "chưa có địa điểm cụ thể", "ok": False})

    if entities.get("budget"):
        steps.append({"text": f"ngân sách ~{entities['budget'] // 1_000_000}M", "ok": True})

    if entities.get("booking_ref"):
        steps.append({"text": f"booking #{entities['booking_ref']}", "ok": True})

    return steps


_DEST_ALIASES: dict[str, str] = {
    "phú quốc": "Phú Quốc", "phu quoc": "Phú Quốc",
    "nha trang": "Nha Trang",
    "hội an": "Hội An", "hoi an": "Hội An",
    "quảng nam": "Hội An", "quang nam": "Hội An",
    "hạ long": "Hạ Long", "ha long": "Hạ Long",
    "quảng ninh": "Hạ Long", "quang ninh": "Hạ Long",
    "hà tĩnh": "Hà Tĩnh", "ha tinh": "Hà Tĩnh",
    "cửa sót": "Hà Tĩnh", "cua sot": "Hà Tĩnh",
    "nghệ an": "Nghệ An", "nghe an": "Nghệ An",
    "cửa hội": "Nghệ An", "cua hoi": "Nghệ An",
    "bắc ninh": "Bắc Ninh", "bac ninh": "Bắc Ninh",
    "đà nẵng": "Hội An", "da nang": "Hội An",
}


def _detect_destination(text: str) -> str | None:
    lower = text.lower().strip()
    if lower in _DEST_ALIASES:
        return _DEST_ALIASES[lower]
    for alias, name in _DEST_ALIASES.items():
        if alias in lower:
            return name
    return None


def _fallback_classify(state: AgentState, _: str) -> AgentState:
    msg = state["user_message"].lower()
    entities = {}
    intent = "unclear"

    # Priority 1: standalone destination name → always search
    dest_name = _detect_destination(state["user_message"])
    if dest_name and len(state["user_message"].split()) <= 4:
        intent = "search"
        entities["destination"] = dest_name
    elif any(k in msg for k in ["hủy", "cancel", "huỷ"]):
        intent = "cancel"
    elif any(k in msg for k in ["đổi", "thay", "dời", "change"]):
        intent = "change"
    elif any(k in msg for k in ["không phải", "nhầm", "sửa", "thực ra", "ý tôi"]):
        intent = "correction"
    elif "?" in msg or any(k in msg for k in ["có không", "bao nhiêu", "mấy giờ", "có gì"]):
        intent = "qa"
    elif any(k in msg for k in ["tìm", "muốn", "book", "đặt", "resort"]):
        intent = "search"

    if not dest_name and (dest := _detect_destination(msg)):
        entities["destination"] = dest
        if intent == "unclear":
            intent = "search"

    if "gia đình" in msg or "family" in msg:
        entities["trip_type"] = "family"
    if "biển" in msg or "beach" in msg:
        entities["vibe"] = "beach"
    for n in [1, 2, 3, 4, 5, 6]:
        if f"{n} người" in msg:
            entities["guest_count"] = n
    ref_match = re.search(r"VNP-\d+-\d+", state["user_message"].upper())
    if ref_match:
        entities["booking_ref"] = ref_match.group()

    needs_clarify = (
        intent == "unclear"
        or (intent in ("change", "cancel") and not entities.get("booking_ref"))
        or (intent in ("search", "correction") and not entities.get("destination")
            and not entities.get("vibe") and not entities.get("trip_type"))
    )

    return {
        **state,
        "intent": intent,
        "confidence": 0.65,
        "entities": entities,
        "clarification_needed": needs_clarify,
        "thinking_steps": _thinking_steps(intent, entities),
        "error": None,
    }


# ── ROUTER ────────────────────────────────────────────────────────────────────

def router_node(state: AgentState) -> Literal["search", "change", "cancel", "qa", "clarify", "error"]:
    if state.get("error"):
        return "error"
    if state.get("clarification_needed"):
        return "clarify"
    intent = state.get("intent", "unclear")
    # correction re-uses search_node (with merged entities already applied in classify)
    if intent in ("search", "correction"):
        return "search"
    if intent in ("change", "cancel", "qa"):
        return intent
    return "clarify"   # unclear with no clarification_needed → ask anyway


# ── CLARIFY NODE (Low-confidence path) ───────────────────────────────────────

async def clarify_node(state: AgentState) -> AgentState:
    """Generate one focused clarifying question. Covers low-confidence + unclear intents."""
    user_message = state["user_message"]
    intent = state.get("intent", "unclear")
    entities = state.get("entities", {})

    llm = _llm("OPENAI_MODEL_FAST")

    context = (
        f'User nói: "{user_message}"\n'
        f"Intent: {intent}\n"
        f"Thông tin đã có: {json.dumps(entities, ensure_ascii=False) if entities else 'không có'}"
    )

    response = await llm.ainvoke([
        SystemMessage(content=CLARIFY_SYSTEM),
        HumanMessage(content=context),
    ])
    question = response.content.strip()

    # Rule-based quick replies based on what's missing
    quick_replies: list[str] = []
    if intent in ("search", "correction", "unclear"):
        if not entities.get("destination"):
            quick_replies = ["Phú Quốc", "Nha Trang", "Đà Nẵng", "Hạ Long"]
        elif not entities.get("guest_count"):
            quick_replies = ["1 người", "2 người", "4 người (gia đình)", "Nhóm 6+"]
        elif not entities.get("date_range") and not entities.get("nights"):
            quick_replies = ["Cuối tuần này", "Tuần tới", "Tháng sau"]
    elif intent in ("change", "cancel"):
        quick_replies = ["Tìm booking tự động", "Nhập mã booking"]

    return {
        **state,
        "ui_state": "ask_clarification",
        "llm_response": question,
        "result_data": {
            "question": question,
            "quick_replies": quick_replies,
            "intent_hint": intent,
            "llm_message": question,
        },
    }


# ── SEARCH NODE (Happy + Correction paths) ───────────────────────────────────

async def search_node(state: AgentState) -> AgentState:
    user_message = state["user_message"]
    entities = state.get("entities", {})
    intent = state.get("intent", "search")
    previous_entities = state.get("previous_entities", {})

    llm = _llm()
    llm_with_tools = llm.bind_tools([search_hotels])

    # Correction: use the CORRECTION_SYSTEM with previous context
    system = (
        CORRECTION_SYSTEM.format(
            previous_entities=json.dumps(previous_entities, ensure_ascii=False),
            user_message=user_message,
        )
        if intent == "correction" and previous_entities
        else SEARCH_SYSTEM
    )

    messages = [
        SystemMessage(content=system),
        HumanMessage(content=user_message),
    ]

    ai_msg = await llm_with_tools.ainvoke(messages)

    tool_msgs = []
    called_args: dict = {}

    for tc in (ai_msg.tool_calls or []):
        if tc["name"] == "search_hotels":
            called_args = tc["args"]
            result_str = search_hotels.invoke(tc["args"])
            tool_msgs.append(ToolMessage(content=result_str, tool_call_id=tc["id"]))

    # Merge LLM-extracted args into entities
    merged = {**entities}
    for k in ("vibe", "trip_type", "guest_count", "destination", "budget", "nights"):
        if called_args.get(k) is not None:
            merged[k] = called_args[k]

    # Fallback: LLM skipped tool call but we have enough entities → call directly
    if not tool_msgs and (merged.get("destination") or merged.get("vibe") or merged.get("trip_type")):
        tool_args = {k: v for k, v in merged.items()
                     if k in ("vibe", "trip_type", "guest_count", "destination", "budget", "nights") and v is not None}
        result_str = search_hotels.invoke(tool_args)
        # Fake tool turn so LLM can generate a proper response
        from langchain_core.messages import AIMessage as _AIMessage
        fake_ai = _AIMessage(
            content="",
            tool_calls=[{"name": "search_hotels", "args": tool_args, "id": "direct-0"}],
        )
        tool_msgs = [ToolMessage(content=result_str, tool_call_id="direct-0")]
        ai_msg = fake_ai

    # Generate natural-language summary from tool results
    llm_response = ""
    if tool_msgs:
        messages = messages + [ai_msg] + tool_msgs
        final = await llm.ainvoke(messages)
        llm_response = final.content.strip()

    if not llm_response:
        dest_hint = merged.get("destination", "")
        llm_response = f"Tôi tìm thấy các địa điểm phù hợp{' ở ' + dest_hint if dest_hint else ''}! 🏖"

    full_dests = _scoring.query_and_score_destinations(merged)

    return {
        **state,
        "ui_state": "show_destinations",
        "llm_response": llm_response,
        "entities": merged,   # save enriched entities for next correction
        "result_data": {
            "understanding_tags": _understanding_tags(merged),
            "destinations": full_dests,
            "llm_message": llm_response,
        },
    }


# ── CHANGE NODE ───────────────────────────────────────────────────────────────

async def change_node(state: AgentState) -> AgentState:
    user_message = state["user_message"]
    user_id = state.get("user_id", "demo_user")
    entities = state.get("entities", {})
    booking_ref = entities.get("booking_ref")

    llm = _llm()
    llm_with_tools = llm.bind_tools([get_booking, check_available_dates])

    messages = [SystemMessage(content=CHANGE_SYSTEM), HumanMessage(content=user_message)]
    ai_msg = await llm_with_tools.ainvoke(messages)

    tool_msgs = []
    for tc in (ai_msg.tool_calls or []):
        args = {**tc["args"]}
        if "user_id" not in args:
            args["user_id"] = user_id
        if booking_ref and "booking_ref" not in args:
            args["booking_ref"] = booking_ref
        if tc["name"] == "get_booking":
            result = get_booking.invoke(args)
        elif tc["name"] == "check_available_dates":
            result = check_available_dates.invoke(args)
        else:
            continue
        tool_msgs.append(ToolMessage(content=result, tool_call_id=tc["id"]))

    # LLM response from tool results
    llm_response = ""
    if tool_msgs:
        messages = messages + [ai_msg] + tool_msgs
        final = await llm.ainvoke(messages)
        llm_response = final.content.strip()

    full_data = _scoring.get_booking_and_availability(user_message, user_id, entities)

    if not full_data:
        return await _failure_node(state, "booking_not_found", user_message)

    if not llm_response:
        p = full_data["change_policy"]
        llm_response = (
            f"Tôi tìm thấy booking của bạn rồi! "
            f"Miễn phí đổi nếu đổi trước {p['free_change_before_days']} ngày check-in, "
            f"sau đó phí {p['change_fee']:,}đ/lần. 📅"
        )

    policy_text = (
        f"Miễn phí đổi nếu đổi trước "
        f"{full_data['change_policy']['free_change_before_days']} ngày check-in. "
        f"Sau đó {full_data['change_policy']['change_fee']:,}đ/lần."
    )
    return {
        **state,
        "ui_state": "show_change_booking",
        "llm_response": llm_response,
        "result_data": {
            "booking": full_data["booking"],
            "change_policy": full_data["change_policy"],
            "policy_text": policy_text,
            "available_dates": full_data["available_dates"],
            "llm_message": llm_response,
        },
    }


# ── CANCEL NODE ───────────────────────────────────────────────────────────────

async def cancel_node(state: AgentState) -> AgentState:
    user_message = state["user_message"]
    user_id = state.get("user_id", "demo_user")
    entities = state.get("entities", {})
    booking_ref = entities.get("booking_ref")

    llm = _llm()
    llm_with_tools = llm.bind_tools([calculate_refund])

    messages = [SystemMessage(content=CANCEL_SYSTEM), HumanMessage(content=user_message)]
    ai_msg = await llm_with_tools.ainvoke(messages)

    tool_msgs = []
    for tc in (ai_msg.tool_calls or []):
        args = {**tc["args"]}
        if "user_id" not in args:
            args["user_id"] = user_id
        if booking_ref and "booking_ref" not in args:
            args["booking_ref"] = booking_ref
        if tc["name"] == "calculate_refund":
            result = calculate_refund.invoke(args)
            tool_msgs.append(ToolMessage(content=result, tool_call_id=tc["id"]))

    llm_response = ""
    if tool_msgs:
        messages = messages + [ai_msg] + tool_msgs
        final = await llm.ainvoke(messages)
        llm_response = final.content.strip()

    full_data = _scoring.get_booking_and_refund(user_message, user_id, entities)

    if not full_data:
        return await _failure_node(state, "booking_not_found", user_message)

    if not llm_response:
        r = full_data["refund"]
        llm_response = (
            f"Tôi kiểm tra booking của bạn rồi. "
            f"Hủy trước {r['days_until_checkin']} ngày nên phí {r['fee_pct']}% "
            f"({r['fee_amount']:,}đ). Bạn sẽ nhận lại {r['refund_amount']:,}đ "
            f"trong {r['refund_timeline']}."
        )

    return {
        **state,
        "ui_state": "show_cancel_booking",
        "llm_response": llm_response,
        "result_data": {
            "booking": full_data["booking"],
            "refund": full_data["refund"],
            "policy_tiers": full_data["policy_tiers"],
            "policy_explanation": llm_response,
            "llm_message": llm_response,
        },
    }


# ── QA NODE ───────────────────────────────────────────────────────────────────

async def qa_node(state: AgentState) -> AgentState:
    llm = _llm("OPENAI_MODEL_SMART")
    response = await llm.ainvoke([
        SystemMessage(content=QA_SYSTEM),
        HumanMessage(content=state["user_message"]),
    ])
    llm_response = response.content.strip()

    # Detect if LLM is escalating to CSKH (policy not in data)
    escalating = any(k in llm_response for k in ["liên hệ CSKH", "chưa có trong dữ liệu", "chính sách này"])
    ui_state = "show_failure" if escalating else "show_message"

    return {
        **state,
        "ui_state": ui_state,
        "llm_response": llm_response,
        "result_data": {
            "message": llm_response,
            "llm_message": llm_response,
            "failure_reason": "policy_not_found" if escalating else None,
            "source_hint": "vinpearl.com/chinh-sach" if escalating else None,
        },
    }


# ── FAILURE NODE (Graceful — Failure path) ────────────────────────────────────

async def _failure_node(state: AgentState, reason: str, user_message: str) -> AgentState:
    """Gracefully handle data-not-found with empathetic LLM message + CSKH escalation."""
    llm = _llm("OPENAI_MODEL_FAST")
    response = await llm.ainvoke([
        SystemMessage(content=FAILURE_SYSTEM),
        HumanMessage(content=user_message),
    ])
    llm_response = response.content.strip()

    return {
        **state,
        "ui_state": "show_failure",
        "llm_response": llm_response,
        "error": None,  # handled gracefully — no hard error
        "result_data": {
            "failure_reason": reason,
            "llm_message": llm_response,
            "cskh": {
                "phone": "1800 1234",
                "chat_url": "https://www.vinpearlresort.com",
                "policy_url": "https://www.vinpearlresort.com/chinh-sach",
            },
        },
    }


async def error_node(state: AgentState) -> AgentState:
    return {
        **state,
        "ui_state": "show_error",
        "llm_response": "Xin lỗi, có lỗi xảy ra. Vui lòng thử lại nhé!",
        "result_data": {"error_type": state.get("error", "unknown")},
    }


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _understanding_tags(entities: dict) -> list[str]:
    vibe_map = {"beach": "🏖 Biển", "city": "🌆 Thành phố", "nature": "⛰ Thiên nhiên", "culture": "🏛 Văn hóa"}
    trip_map = {"family": "👨‍👩‍👧‍👦 Gia đình", "couple": "💑 Cặp đôi", "solo": "🧳 Solo", "group": "👥 Nhóm"}
    tags = []
    if v := entities.get("vibe"):
        tags.append(vibe_map.get(v, v))
    if t := entities.get("trip_type"):
        tags.append(trip_map.get(t, t))
    if d := entities.get("date_range"):
        tags.append(f"📅 {d}")
    if g := entities.get("guest_count"):
        tags.append(f"👥 {g} người")
    if b := entities.get("budget"):
        tags.append(f"💰 ~{b // 1_000_000}M")
    if dest := entities.get("destination"):
        tags.append(f"📍 {dest}")
    return tags or ["Đang tìm kiếm..."]
