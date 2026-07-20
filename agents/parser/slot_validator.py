"""
필수 슬롯 검증 모듈 (파이프라인 게이트)

Duffel(항공), LiteAPI(숙소) 같은 외부 API는 호출할 때마다 비용이 발생.
필수 정보가 없는 상태에서 파이프라인을 실행하면 API만 낭비되고 실패.
그래서 파이프라인 실행 전에 필수 슬롯이 다 있는지 먼저 확인하는 게이트 역할.

누락이 있으면 → 재질문 메시지 반환 (파이프라인 실행 안 함)
누락이 없으면 → ok: True 반환 (파이프라인 실행 허가)

API 명세서 기준:
    parse_intent() 반환값의 "missing_slots" 키를 기준으로 검증.
    (기존 "missing_fields" → "missing_slots" 로 변경됨)
"""

# 파이프라인 실행에 반드시 필요한 슬롯 목록
# API 명세서의 fields 구조 기준으로 이름 맞춤
# 이 중 하나라도 없으면 항공/숙소 검색 불가
REQUIRED_SLOTS = ["destinations", "budget"]

# 누락 슬롯별 재질문 메시지 템플릿
# 사용자에게 자연스럽게 물어보는 말투로 작성
REASK_MESSAGES = {
    "destinations": "어디로 여행 가고 싶으세요? 😊",
    "budget":       "총 예산은 얼마 정도 생각하고 계세요? (예: 80만원)",
    "dates":        "여행 기간이 어떻게 되나요? (예: 3박 4일)",
    "pax":          "인원이 몇 명인가요? (예: 성인 2명)",
}


def validate_slots(parsed: dict) -> dict:
    """
    파싱 결과에서 필수 슬롯이 다 있는지 확인.

    parse_intent()로 파싱한 결과를 받아서
    파이프라인을 실행해도 되는지 판단해줌.

    API 명세서 기준으로 parsed 구조:
    {
        "parse_id": "p_xxx",
        "fields": { ... },
        "missing_slots": ["budget", "dates"],  ← 여기서 체크
        ...
    }

    Args:
        parsed: intent_parser.parse_intent()의 반환값

    Returns:
        검증 결과 dict:
        {
            "ok": True,              ← 파이프라인 실행 가능
            "missing": [],
            "reask_message": None
        }
        또는
        {
            "ok": False,             ← 재질문 필요, 파이프라인 실행 금지
            "missing": ["budget"],   ← 누락된 슬롯 목록
            "reask_message": "총 예산은 얼마 정도 생각하고 계세요?"
        }
    """
    # parsed의 missing_slots에서 REQUIRED_SLOTS에 해당하는 것만 추림
    # dates, pax 같은 건 required가 아니라서 무시
    # (누락돼도 파이프라인 실행은 가능, 나중에 가정값으로 처리)
    missing_in_parsed = parsed.get("missing_slots", [])
    critical_missing = [f for f in REQUIRED_SLOTS if f in missing_in_parsed]

    # 필수 슬롯이 다 있으면 통과
    if not critical_missing:
        return {
            "ok": True,
            "missing": [],
            "reask_message": None,
        }

    # 누락이 있으면 첫 번째 누락 슬롯 기준으로 재질문 메시지 생성
    # (여러 개가 빠졌어도 한 번에 하나씩 물어보는 게 UX상 더 자연스러움)
    first_missing = critical_missing[0]
    reask_message = REASK_MESSAGES.get(
        first_missing,
        f"{first_missing}을(를) 알려주세요."  # 템플릿에 없는 슬롯이면 기본 메시지
    )

    return {
        "ok": False,
        "missing": critical_missing,
        "reask_message": reask_message,
    }