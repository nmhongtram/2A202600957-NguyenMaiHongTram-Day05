from typing import TypedDict, Optional, Literal
from pydantic import BaseModel, Field


class AgentState(TypedDict):
    user_message: str
    user_id: str
    intent: str           # search | change | cancel | qa | correction | unclear
    confidence: float
    entities: dict
    previous_entities: dict   # last search entities — for correction flow
    clarification_needed: bool
    thinking_steps: list
    llm_response: str     # natural-language answer shown in chat bubble
    ui_state: str
    # show_destinations | show_change_booking | show_cancel_booking
    # show_message | show_failure | ask_clarification | show_error
    result_data: dict
    error: Optional[str]


class EntityExtraction(BaseModel):
    trip_type: Optional[str] = Field(None, description="family | couple | solo | group")
    vibe: Optional[str] = Field(None, description="beach | city | nature | culture")
    guest_count: Optional[int] = Field(None, description="Số người")
    nights: Optional[int] = Field(None, description="Số đêm")
    date_range: Optional[str] = Field(None, description="Mô tả thời gian")
    budget: Optional[int] = Field(None, description="Ngân sách tổng (VND)")
    destination: Optional[str] = Field(None, description="Tên địa điểm")
    booking_ref: Optional[str] = Field(None, description="Mã booking VNP-XXXX-XXXX")
    change_type: Optional[str] = Field(None, description="date | room")


class IntentResult(BaseModel):
    intent: Literal["search", "change", "cancel", "qa", "correction", "unclear"] = Field(
        description=(
            "search=tìm/đặt chỗ ở mới. "
            "change=đổi ngày/phòng booking đã có. "
            "cancel=hủy booking. "
            "qa=hỏi thông tin chung (chính sách, tiện ích, giờ check-in...). "
            "correction=user sửa thông tin vừa cung cấp ('không phải X, là Y', 'nhầm rồi', 'ý tôi là'). "
            "unclear=câu quá ngắn hoặc hoàn toàn không liên quan."
        )
    )
    confidence: float = Field(description="Mức độ tự tin 0.0–1.0")
    entities: EntityExtraction
    clarification_needed: bool = Field(
        False,
        description=(
            "Đặt True khi: "
            "(1) confidence < 0.6, hoặc "
            "(2) intent=search nhưng không có destination VÀ không có vibe VÀ không có trip_type, hoặc "
            "(3) intent=change/cancel nhưng không có booking_ref, hoặc "
            "(4) intent=unclear."
        ),
    )
