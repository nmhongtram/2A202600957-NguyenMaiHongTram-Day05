"""
LangChain Tools — LLM calls these via bind_tools().
Each tool wraps the pure scoring/mock_db logic and returns JSON strings
so the LLM can read results and generate natural-language responses.
"""

import json
import sys
from pathlib import Path

# Parent dir (backend/) is on sys.path when main.py runs, but be explicit for imports within package
_BACKEND_DIR = str(Path(__file__).parent.parent)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import mock_db
import scoring
from langchain_core.tools import tool


@tool
def search_hotels(
    vibe: str = None,
    trip_type: str = None,
    guest_count: int = 2,
    budget: int = None,
    destination: str = None,
    nights: int = None,
) -> str:
    """
    Tìm và xếp hạng khách sạn/resort Vinpearl phù hợp với nhu cầu của khách.

    Args:
        vibe: Phong cách kỳ nghỉ — beach (biển), city (thành phố), nature (thiên nhiên), culture (văn hóa)
        trip_type: Loại nhóm — family (gia đình), couple (cặp đôi), solo (một mình), group (nhóm bạn)
        guest_count: Tổng số người (người lớn + trẻ em)
        budget: Ngân sách tổng chuyến đi tính bằng VND
        destination: Địa điểm cụ thể nếu đã biết (Phú Quốc, Đà Nẵng, Nha Trang, Hạ Long, Huế)
        nights: Số đêm lưu trú dự kiến

    Returns:
        JSON với top 3 điểm đến phù hợp nhất, điểm match và lý do gợi ý
    """
    entities = {k: v for k, v in {
        "vibe": vibe, "trip_type": trip_type, "guest_count": guest_count,
        "budget": budget, "destination": destination, "nights": nights,
    }.items() if v is not None}

    destinations = scoring.query_and_score_destinations(entities)

    summary = [
        {
            "name": d["name"],
            "match_score": d["match_score"],
            "reasons": d.get("reasons", [])[:3],
            "rating": d.get("rating"),
            "region": d.get("region", ""),
            "description": d.get("description", "")[:80],
        }
        for d in destinations
    ]
    return json.dumps({"destinations": summary, "count": len(summary)}, ensure_ascii=False)


@tool
def get_booking(user_id: str = "demo_user", booking_ref: str = None) -> str:
    """
    Lấy thông tin booking hiện tại của khách hàng.

    Args:
        user_id: ID người dùng (mặc định: demo_user)
        booking_ref: Mã booking cụ thể (vd: VNP-2024-8821). Nếu không có sẽ lấy booking mới nhất.

    Returns:
        JSON với thông tin booking: tên resort, ngày check-in/out, số khách, tổng tiền
    """
    booking = None
    if booking_ref:
        booking = mock_db.get_booking_by_ref(booking_ref, user_id)
    if not booking:
        bookings = mock_db.get_user_bookings(user_id)
        booking = bookings[0] if bookings else None

    if not booking:
        return json.dumps({"error": "Không tìm thấy booking nào cho tài khoản này"}, ensure_ascii=False)

    return json.dumps({
        "ref": booking["ref"],
        "resort_name": booking["resort_name"],
        "destination": booking["destination"],
        "room_type": booking["room_type"],
        "checkin": booking["checkin"],
        "checkout": booking["checkout"],
        "nights": booking["nights"],
        "guests": booking["guests"],
        "total_paid": booking["total_paid"],
        "status": booking["status"],
    }, ensure_ascii=False)


@tool
def calculate_refund(user_id: str = "demo_user", booking_ref: str = None) -> str:
    """
    Tính toán chi tiết số tiền hoàn lại khi hủy booking theo chính sách Vinpearl.

    Args:
        user_id: ID người dùng
        booking_ref: Mã booking (nếu không có sẽ dùng booking mới nhất)

    Returns:
        JSON với: số ngày đến check-in, % phí hủy, số tiền bị trừ, số tiền hoàn, thời gian hoàn tiền
    """
    booking = None
    if booking_ref:
        booking = mock_db.get_booking_by_ref(booking_ref, user_id)
    if not booking:
        bookings = mock_db.get_user_bookings(user_id)
        booking = bookings[0] if bookings else None

    if not booking:
        return json.dumps({"error": "Không tìm thấy booking"}, ensure_ascii=False)

    refund = mock_db.compute_refund(booking["total_paid"], booking["checkin"])
    policy = mock_db.CANCELLATION_POLICIES["default"]

    return json.dumps({
        "booking_ref": booking["ref"],
        "resort_name": booking["resort_name"],
        "checkin": booking["checkin"],
        "total_paid": booking["total_paid"],
        "days_until_checkin": refund["days_until_checkin"],
        "fee_pct": refund["fee_pct"],
        "fee_amount": refund["fee_amount"],
        "refund_amount": refund["refund_amount"],
        "tier_label": refund["tier_label"],
        "refund_timeline": refund["refund_timeline"],
        "next_tier_deadline_days": refund.get("next_tier_deadline_days"),
        "policy_tiers": policy["tiers"],
    }, ensure_ascii=False)


@tool
def check_available_dates(user_id: str = "demo_user", booking_ref: str = None) -> str:
    """
    Kiểm tra các ngày có thể chọn để đổi check-in và phí tương ứng.

    Args:
        user_id: ID người dùng
        booking_ref: Mã booking

    Returns:
        JSON với danh sách ngày trống ±7 ngày từ check-in hiện tại, cùng phí thay đổi
    """
    booking = None
    if booking_ref:
        booking = mock_db.get_booking_by_ref(booking_ref, user_id)
    if not booking:
        bookings = mock_db.get_user_bookings(user_id)
        booking = bookings[0] if bookings else None

    if not booking:
        return json.dumps({"error": "Không tìm thấy booking"}, ensure_ascii=False)

    dates = mock_db.get_availability_grid(booking["resort_id"], booking["room_id"], booking["checkin"])
    policy = mock_db.CANCELLATION_POLICIES["default"]["change_policy"]

    available = [d for d in dates if d["available"] and not d["is_current"]]

    return json.dumps({
        "booking_ref": booking["ref"],
        "current_checkin": booking["checkin"],
        "current_checkout": booking["checkout"],
        "available_dates": available[:6],
        "change_policy": {
            "free_before_days": policy["free_change_before_days"],
            "fee": policy["change_fee"],
            "max_changes": policy["max_changes"],
        },
    }, ensure_ascii=False)


# Exported list for easy import
ALL_TOOLS = [search_hotels, get_booking, calculate_refund, check_available_dates]
