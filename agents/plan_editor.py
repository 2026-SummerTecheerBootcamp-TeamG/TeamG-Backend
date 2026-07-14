"""
국소 수정 편집기 - Claude는 "각 날짜에 남길 장소 이름과 순서"만 결정함

왜 이름만 받나
    PoC는 Claude가 장소 데이터 전체를 돌려줌
    그 방식은 LLM이 데이터를 복사하는 과정에서 훼손/누락될 수 있음
    본 구현에서는:
      Claude = 판단
      코드 = 데이터
    부수 효과: 원본에 없는 이름은 재조립 때 자동 탈락 -> 할루시네이션 차단
"""

import json

import anthropic

from agents.claude_client import DEFAULT_MODEL
from agents import trace

_client = anthropic.Anthropic()

EDIT_TOOL = {
    "name": "save_edited_itinerary",
    "description": "수정 요청을 반영한 날짜별 '남길 장소 이름 목록'을 저장합니다. 반드시 이 도구로 응답하세요.",
    "input_schema": {
        "type": "object",
        "properties": {
            "days": {
                "type": "array",
                "description": "모든 날짜를 빠짐없이 포함 (수정 없는 날도 원래 이름들 그대로)",
                "items": {
                    "type": "object",
                    "properties": {
                        "day": {"type": "integer", "description": "몇 일차"},
                        "place_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "그 날 방문할 장소 이름들 (방문 순서대로, 현재 일정에 있는 이름만)",
                        },
                    },
                    "required": ["day", "place_names"],
                },
            },
            "summary": {"type": "string", "description": "무엇을 어떻게 바꿨는지 한국어 한 줄 요약"},
        },
        "required": ["days", "summary"],
    },
}

EDITOR_SYSTEM = (
    "당신은 여행 일정 편집자입니다. 현재 일정과 수정 요청을 보고, "
    "날짜별로 '남길 장소 이름 목록'을 save_edited_itinerary 도구로 돌려주세요.\n"
    "\n"
    "당신이 할 수 있는 것 (이 세 가지뿐입니다):\n"
    "A) 어떤 날의 장소를 빼기 (여유롭게/쉬고 싶다 → 평점 낮은 곳부터 뺌)\n"
    "B) 같은 날 안에서 순서 바꾸기\n"
    "C) 한 날짜의 장소를 다른 날짜로 옮기기 (빡빡하게/더 많이 → 다른 날에서 가져옴)\n"
    "\n"
    "할 수 없는 것:\n"
    "- 현재 일정에 없는 장소를 만들기 (새 장소가 필요한 요청이면 있는 장소 안에서 "
    "최선을 다하고, summary에 '새 장소 추가는 재검색이 필요하다'고 적으세요)\n"
    "\n"
    "판단 규칙:\n"
    "1) 언급되지 않은 날짜는 원래 이름·순서 그대로 유지\n"
    "2) 요청이 모호하면 작게 바꾸고, summary에 어떻게 해석했는지 명시\n"
    "3) 모든 날짜를 응답에 포함\n"
)


def edit_day_plan(run_id, day_plan, edit_request):
    """
    day_plan: [{"day", "city", "items": [{"place_name", "place_detail", ...}]}]
    반환: {"days" [{"day", "place_names": [...]}], "summary": str}
    """

    # Claude에게는 판단에 필요한 최소 정보만
    slim = [
        {
            "day": d["day"],
            "city": d.get("city"),
            "places": [
                {
                    "name": item["place_name"],
                    "rating": (item.get("place_detail") or {}).get("rating"),
                }
                for item in d["items"]
            ],
        }
        for d in day_plan
    ]

    trace.publish(run_id, "llm", "일정편집기", "편집 요청",
                  f"{len(day_plan)}일치 · \"{edit_request[:60]}\"")
    
    response = _client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=1500,
        system=EDITOR_SYSTEM,
        tools=[EDIT_TOOL],
        tool_choice={"type": "tool", "name": "save_edited_itinerary"},
        messages=[{
            "role": "user",
            "content": json.dumps(
                {"현재_일정": slim, "수정_요청": edit_request},
                ensure_ascii=False,
            ),
        }],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "save_edited_itinerary":
            return block.input
        
    raise RuntimeError("일정 편집기: Claude가 도구를 호출하지 않았습니다.")
