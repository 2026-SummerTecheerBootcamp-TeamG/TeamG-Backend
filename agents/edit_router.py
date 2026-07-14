"""
수정 요청 라우터 - 사용자의 수정 요청을 3종으로 분류함

    국소수정: 일정(방문지 수/순서/여유도)만 바꾸면 됨 -> plan_editor가 처리
    예산영향: 숙소/항공 등급 변경 등 비용이 달라짐 -> 재검색+재배분 필요 (후속)
    재계획: 날짜/목적지/인원 변경 -> 파이프라인 재실행 필요 (후속)
    
분류는 단순 작업이라 빠르고 저렴한 Haiku로 충분
tool_choice로 도구 호출을 강제 - 자유 문장 대신 enum 3개 중 하나만 나옴
"""

import anthropic

from agents import trace

ROUTER_MODEL = "claude-haiku-4-5-20251001"

_client = anthropic.Anthropic()

ROUTER_TOOL = {
    "name": "route_edit",
    "description": "사용자의 여행 플랜 수정 요청을 세 가지 중 하나로 분류합니다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["국소수정", "예산영향", "재계획"],
                "description": (
                    "국소수정: 일정(방문지 순서/개수/여유도)만 바꾸면 되는 요청. "
                    "예산영향: 숙소 및 항공 등급/옵션 변경 등 비용이 달라져 예산 재정산이 필요한 요청. "
                    "재계획: 날짜/목적지/인원 변경 등 처음부터 다시 계획해야 하는 요청."
                ),
            },
            "reason": {"type": "string", "description": "그렇게 분류한 이유 한 줄"},
        },
        "required": ["category", "reason"],
    },
}


def route_edit_request(run_id, edit_request):
    """반환: {"category": "국소수정"|"예산영향"|"재계획", "reason": str}"""
    trace.publish(run_id, "llm", "수정라우터", "분류 요청", edit_request[:80])

    response = _client.messages.create(
        model=ROUTER_MODEL,
        max_tokens=200,
        tools=[ROUTER_TOOL],
        # tool_choice: 반드시 이 도구로 답하라 강제
        tool_choice={"type": "tool", "name": "route_edit"},
        messages=[{"role": "user", "content": f"수정 요청: {edit_request}"}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "route_edit":
            result = block.input
            trace.publish(run_id, "rule", "수정라우터",
                          f"분류: {result['category']}", result.get("reason", ""))
            return result
        
    # 도구 호출이 없었던 극단적 경우 - 영향 범위가 가장 작은 쪽으로 안전 처리
    return {"category": "국소수정", "reason": "(분류 실패 - 기본값)"}
