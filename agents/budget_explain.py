"""
budget_explain.py - 예산 배분 결과를 사람 말로 설명 (LLM 담당)

budget.py와 일부러 파일을 분리한 이유
 - budget.py는 결정론적 계산임 (같은 입력 -> 같은 출력, LLM 없음)

PoC의 explain_allocation 프롬프트를 검증된 그대로 이식함
"""

import json

# API 키 로드, 모델 선택(sonnet-5), thinking 블록 처리까지 다 해줌
from agents.claude_client import ask_claude


# 시스템 프롬프트
# 너는 누구고 어떤 규칙으로 답해야 하는가를 정하는 역할 지정서
EXPLAIN_SYSTEM = (
    "당신은 여행 예산 조율 전문가입니다. "
    "주어진 '한계효용 기반 예산 배분 결과'(JSON)를 여행자에게 설명하세요: "
    "1. 기본(최저가) 배분에서 시작해 "
    "2. 남은 예산을 어떤 순서로 어디에 더 썼는지(업그레이드와 그 이유), "
    "3. 최종 배분과 여유/부족. "
    "예산이 부족(insufficient)이면 현실적 대안(예산 증액, 여행 시기 변경, 박 수 축소)을 제안하세요. "
    "표 없이 4-6문장, 자연스러운 {language}로 작성하세요."
)
NATIONALITY_LANGUAGE = {
    "KR": "한국어",
    "JP": "일본어",
    "CN": "중국어",
    "TW": "중국어(번체)",
    "US": "영어",
    "GB": "영어",
}
DEFAULT_LANGUAGE = "한국어"


def language_for_nationality(code: str | None) -> str:
    """국적 코드로 설명 언어를 고른다.
    
    - None/빈값 -> 한국어
    - 표에 없는 국적 -> 영어
    """
    if not code:
        return DEFAULT_LANGUAGE
    return NATIONALITY_LANGUAGE.get(code.upper(), "영어")


def explain_allocation(request_summary: dict, allocation: dict, language: str = DEFAULT_LANGUAGE) -> str:
    """배분 결과를 Claude가 읽기 좋은 한국어 설명으로 변환한다.
    
    Args:
        request_summary: 여행 요청 요약. 파서 출력이든 TripRequest 행이든,
            호출하는 쪽이 {"목적지", "기간", "인원", "테마", "총예산_KRW"} 정도의
            간단한 딕셔너리로 만들어 넘긴다.
        allocation: allocate_budget()의 반환 dict 그대로.
        language: 설명 언어. 국적 코드에서 고르려면 language_for_nationality를 넘길 것.
    
    Returns:
        설명 문자열. 후보 0건이면 LLM 호출 없이 안내 메시지를 돌려준다.
    """

    # 후보 0건 처리
    if allocation.get("status") in ("no_flights", "no_hotels"):
        return allocation.get("message", "검색 후보가 부족해 배분을 만들지 못했습니다.")
    
    # Claude에게 넘길 재료를 JSON 문자열로 포장
    context = {
        "여행요청": request_summary,
        "예산배분결과": allocation,
    }
    prompt = json.dumps(context, ensure_ascii=False, default=str)

    # max_tokens=1024: 900이면 충분, 여유분 포함
    return ask_claude(prompt=prompt, system=EXPLAIN_SYSTEM.format(language=language), max_tokens=1024)

