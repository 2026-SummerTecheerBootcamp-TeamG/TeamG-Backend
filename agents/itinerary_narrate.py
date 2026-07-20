"""
itinerary_narrate.py - 날짜별 일정을 읽기 좋은 문장으로

itinerary.py와 파일 분리

"""

import json

from agents.budget_explain import DEFAULT_LANGUAGE  # 언어 기본값 재사용 (한 곳에서 관리)
from agents.claude_client import ask_claude

NARRATE_SYSTEM = (
    "당신은 여행 일정 플래너입니다. 주어진 '날짜별 방문지'와 이동 시간을 바탕으로, "
    "여행자가 보기 좋은 하루별 일정을 정리하세요. 각 날마다 오전/오후/저녁 흐름으로 "
    "방문지를 배치하고, 이동 시간이 있으면 자연스럽게 언급하세요. 맛집은 식사 시간대에 "
    "배치하면 좋습니다. travel_mode가 driving인 이동은 '택시로 약 N분'으로 표현하세요 "
    "(운전이 아니라 택시 이동입니다). 목록에 없는 장소를 지어내지 마세요. "
    # 실측(2026-07-16): 내러티브 생성이 30초로 전체 최대 병목 — 생성 시간은
    # 출력 길이에 비례하므로 분량을 줄이는 게 곧 속도다 (상세는 화면의
    # 일정 타임라인이 이미 보여주고, DAY별 설명은 접혀 있음)
    "날짜별로 2-3줄로 간결하게, 마지막 날 다음에 귀국일 안내 한 줄을 덧붙이세요. "
    "자연스러운 {language}로 작성하세요."
)


def narrate_day_plan(city: str, themes: list[str] | None, day_plan: list[dict],
                     language: str = DEFAULT_LANGUAGE) -> str:
    """날짜별 일정 데이터 -> 사람이 읽기 좋은 일정표 문장"""

    if not day_plan:
        return "(장소를 찾지 못해 일정을 만들 수 없습니다.)"
    
    context = {
        "여행지": city,
        "테마": themes or [],
        "날짜별_방문지": day_plan   # items의 place_detail/이동시간까지 통째로
    }
    # max_tokens를 일수에 비례해 늘림 (하루 2-3줄 ≈ 150~200토큰)
    # 고정 1400이었을 때 11박 여행에서 8일차쯤에서 서술이 잘리는 사고가 있었음
    # 상한 3000: 생성 시간은 출력 길이에 비례 — 분량 축소와 세트인 속도 가드
    max_tokens = min(3000, max(1000, 300 + 200 * len(day_plan)))
    return ask_claude(
        prompt=json.dumps(context, ensure_ascii=False, default=str),
        system=NARRATE_SYSTEM.format(language=language),
        max_tokens=max_tokens,
    )