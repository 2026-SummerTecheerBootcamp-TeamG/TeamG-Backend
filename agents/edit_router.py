"""
수정 요청 라우터 - 사용자의 수정 요청을 3종으로 분류함

    국소수정: 일정(방문지 수/순서/여유도)만 바꾸면 됨 -> plan_editor가 처리
    예산영향: 숙소/항공 등급 변경 등 비용이 달라짐 -> 재검색+재배분 필요 (후속)
    재계획: 날짜/목적지/인원 변경 -> 파이프라인 재실행 필요 (후속)
    
분류는 단순 작업이라 빠르고 저렴한 Haiku로 충분
tool_choice로 도구 호출을 강제 - 자유 문장 대신 enum 3개 중 하나만 나옴
"""

import re

import anthropic

from agents import trace

ROUTER_MODEL = "claude-haiku-4-5-20251001"

_client = anthropic.Anthropic()

# 분류 규칙 + 예시 (실사고 2회 반영: "숙소를 가까운 곳으로/마블마운틴 근처로"가
# 국소수정으로 오분류 → 편집기가 아무것도 못 바꾸고 말로만 응답)
ROUTER_SYSTEM = (
    "당신은 여행 플랜 수정 요청 분류기입니다. 반드시 route_edit 도구로만 답하세요.\n"
    "\n"
    "분류 규칙 (위에서부터 순서대로 적용):\n"
    "1. 날짜/목적지/인원을 바꾸면 → 재계획\n"
    "2. 숙소(호텔)나 항공(비행기) '자체'를 바꾸거나 예산을 조정하면 → 예산영향.\n"
    "   바꾸려는 이유는 무엇이든 상관없다 (등급, 가격, 위치, 시간대, 브랜드...).\n"
    "3. 방문 장소와 일정만 바꾸면 → 국소수정\n"
    "\n"
    "예시:\n"
    '- "숙소를 마블마운틴 근처로 바꿔줘" → 예산영향 (숙소 자체를 교체)\n'
    '- "숙소를 일정 장소들과 가까운 곳으로 골라줘" → 예산영향\n'
    '- "더 싼 호텔로 바꿔줘" → 예산영향\n'
    '- "아침에 출발하는 비행기로 바꿔줘" → 예산영향\n'
    '- "예산을 400만원으로 늘려줘" → 예산영향\n'
    '- "숙소 근처 맛집을 일정에 추가해줘" → 국소수정 (바꾸는 건 일정, 숙소는 기준점일 뿐)\n'
    '- "2일차를 여유롭게 해줘" → 국소수정\n'
    '- "날짜를 다음 주로 옮겨줘" → 재계획\n'
)

# ── 결정론 안전망: LLM이 또 놓쳐도 코드가 잡는다 ──────────────────────────
# "숙소를/호텔을/비행기로"처럼 조사가 '바로 붙어' 그 대상 자체를 가리키고
# ("숙소 근처 맛집"의 '숙소 '는 띄어쓰기라 매치 안 됨 = 기준점 표현 보호),
# 바꾸다 계열 동사가 있으며, 고정 의도(그대로/유지)가 아닐 때만 보정한다.
_BUDGET_TARGET = re.compile(r"(숙소|호텔|항공편|항공권|항공|비행기)(를|을|은|는|로|으로)")
_CHANGE_VERB = re.compile(r"(바꾸|바꿔|변경|교체|업그레이드|다운그레이드|골라|잡아|추천|찾아)")
_KEEP_INTENT = re.compile(r"(그대로|유지)")


def _safety_override(edit_request, category):
    """국소수정으로 분류됐지만 숙소/항공 변경 패턴이면 예산영향으로 보정."""
    if category != "국소수정":
        return None
    if (_BUDGET_TARGET.search(edit_request)
            and _CHANGE_VERB.search(edit_request)
            and not _KEEP_INTENT.search(edit_request)):
        return "예산영향"
    return None

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
                    "국소수정: '방문 장소'의 순서/개수/여유도/교체 등 일정만 바꾸는 요청. "
                    "예산영향: 숙소(호텔)나 항공(비행기) 자체를 바꾸거나 예산을 조정하는 "
                    "모든 요청 — 등급·가격뿐 아니라 위치·시간대·브랜드 등 이유가 무엇이든 "
                    "'숙소/항공을 바꿔달라'면 여기 (실사고: '숙소를 일정과 가까운 곳으로 "
                    "바꿔줘'가 국소수정으로 잘못 분류되어 아무것도 못 바꿨음). "
                    "예: '숙소를 일정 장소들과 가까운 곳으로', '더 싼 호텔로', "
                    "'아침에 출발하는 비행기로'. "
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
        system=ROUTER_SYSTEM,       # 규칙 + 실사고 기반 예시
        tools=[ROUTER_TOOL],
        # tool_choice: 반드시 이 도구로 답하라 강제
        tool_choice={"type": "tool", "name": "route_edit"},
        messages=[{"role": "user", "content": f"수정 요청: {edit_request}"}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "route_edit":
            result = block.input

            # LLM이 놓쳐도 코드가 잡는 안전망 (실사고 재발 방지의 최종 방어선)
            override = _safety_override(edit_request, result.get("category"))
            if override:
                trace.publish(run_id, "rule", "수정라우터",
                              f"안전망 보정: {result.get('category')} -> {override}",
                              "숙소/항공 대상 변경 패턴 감지")
                result = {
                    "category": override,
                    "reason": (result.get("reason", "")
                               + " (안전망 보정: 숙소/항공 변경 패턴)"),
                }

            trace.publish(run_id, "rule", "수정라우터",
                          f"분류: {result['category']}", result.get("reason", ""))
            return result
        
    # 도구 호출이 없었던 극단적 경우 - 영향 범위가 가장 작은 쪽으로 안전 처리
    return {"category": "국소수정", "reason": "(분류 실패 - 기본값)"}
