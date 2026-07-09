"""
자연어 → 구조화 JSON 변환 모듈 (Intent Parser)

이 모듈이 요청 이해 파이프라인의 핵심.
사용자가 채팅창에 "도쿄 2박3일 미식, 30만" 이라고 치면
이 모듈이 그걸 받아서 API 명세서 형식의 JSON으로 변환해줌:

{
    "parse_id": "p_1",
    "fields": {
        "origin": {"city": "서울", "iata": "ICN"},
        "destinations": [
            {
                "city": "도쿄",
                "city_en": "Tokyo",
                "country_code": "JP",
                "iata": "TYO",
                "nights": 2
            }
        ],
        "budget": 300000,
        "pax": {"adult": 1, "child": 0},
        "themes": ["미식"],
        "dates": {"start": "2026-03-10", "end": "2026-03-12"}
    },
    "assumed_fields": ["origin"],
    "missing_slots": ["pax"],
    "filled_from_profile": ["origin"],
    "warnings": ["예산이 인원 대비 낮음"]
}

작동 순서:
    1. Claude API에 사용자 입력 전달 → JSON 응답 받기
    2. normalizer로 수량 표현 보정 (Claude가 놓친 "80만" 같은 것)
    3. 유저 프로필로 누락 슬롯 자동 채우기 (출발지 등)
    4. API 명세서 형식으로 변환해서 반환
"""
import json
import re
import uuid

from agents.claude_client import ask_claude
from .normalizer import normalize_budget, normalize_duration, normalize_headcount


# Claude에게 전달하는 시스템 프롬프트
# 에이전트 역할과 출력 형식을 정의함
# 이 프롬프트가 파싱 품질을 결정하는 핵심이라서 신중하게 작성해야 해
SYSTEM_PROMPT = """
너는 여행 요청을 분석하는 파서야.
사용자의 자연어 입력을 받아서 아래 JSON 스키마로 변환해줘.
반드시 JSON만 출력해. 설명이나 마크다운 코드블록(```json) 없이 순수 JSON만.

출력 스키마:
{
    "origin": {
        "city": "출발 도시(한글)",
        "iata": "출발 공항코드(대문자 3자리)"
    },
    "destinations": [
        {
            "city": "도시명(한글)",
            "city_en": "도시명(영어)",
            "country_code": "국가코드(대문자 2자리, 예: JP, TH, TW)",
            "iata": "공항코드(대문자 3자리, 예: FUK, NRT, ICN)",
            "nights": 숙박일수(정수 또는 null)
        }
    ],
    "budget": 총예산(원 단위 정수 또는 null, 예: 300000),
    "pax": {
        "adult": 성인수(정수, 기본 1),
        "child": 어린이수(정수, 기본 0)
    },
    "themes": ["테마1", "테마2"],
    "dates": {
        "start": "YYYY-MM-DD 형식 또는 null",
        "end": "YYYY-MM-DD 형식 또는 null"
    },
    "assumed_fields": ["사용자가 말하지 않아서 기본값으로 채운 필드명 목록"],
    "missing_slots": ["파이프라인 실행 전에 사용자에게 되물어야 할 필드명 목록"]
}

규칙:
- origin을 말하지 않으면 → origin을 null로, assumed_fields에 "origin" 추가
- 인원을 말하지 않으면 → pax.adult를 1로, assumed_fields에 "pax" 추가
- 날짜/기간을 말하지 않으면 → missing_slots에 "dates" 추가
- 예산을 말하지 않으면 → missing_slots에 "budget" 추가
- 목적지를 말하지 않으면 → missing_slots에 "destinations" 추가
- 테마 예시: 쇼핑, 맛집, 관광, 휴양, 액티비티, 문화
- "80만"은 800000으로 변환
- "3박4일"은 destinations[0].nights=3으로 변환
- 목적지가 명확하게 있으면 반드시 destinations 배열에 넣어. 절대 비워두지 마.
- "후쿠오카"는 {"city": "후쿠오카", "city_en": "Fukuoka", "country_code": "JP", "iata": "FUK"}
- "도쿄"는 {"city": "도쿄", "city_en": "Tokyo", "country_code": "JP", "iata": "TYO"}
- "오사카"는 {"city": "오사카", "city_en": "Osaka", "country_code": "JP", "iata": "KIX"}
- "방콕"은 {"city": "방콕", "city_en": "Bangkok", "country_code": "TH", "iata": "BKK"}
"""


def parse_intent(user_input: str, user_profile: dict | None = None) -> dict:
    """
    자연어 입력을 API 명세서 형식의 구조화 JSON으로 변환하는 메인 함수.

    ChatInput 컴포넌트에서 사용자가 메시지를 보내면
    views.py가 이 함수를 호출해서 파싱 결과를 돌려받아.

    Args:
        user_input: 사용자가 채팅창에 입력한 자연어 문자열
                    예: "도쿄 2박3일 미식, 30만"
        user_profile: 로그인한 유저의 기본 설정값
                      예: {"origin_iata": "ICN", "nationality": "KR"}
                      None이면 프로필 채우기 스킵

    Returns:
        API 명세서 형식의 파싱 결과 dict

    Raises:
        ValueError: Claude 응답이 JSON 형식이 아닐 때
        Exception: Claude API 호출 자체가 실패했을 때 (키 오류, 네트워크 등)
    """
    # Step 1: Claude API 호출해서 자연어 → JSON 변환
    # SYSTEM_PROMPT에 역할과 출력 형식이 정의되어 있음
    raw_response = ask_claude(
        prompt=user_input,
        system=SYSTEM_PROMPT,
        max_tokens=1024,  # 파싱 결과는 짧으니까 1024면 충분
    )

    # Step 2: 응답 문자열에서 JSON 파싱
    # Claude가 가끔 ```json ... ``` 형식으로 감싸서 보내는 경우가 있어서 방어 처리
    claude_parsed = _extract_json(raw_response)

    # Step 3: normalizer로 수량 표현 보정
    # Claude가 "80만"을 800000으로 못 바꿨거나, 기간 계산이 틀렸을 때 수정
    claude_parsed = _apply_normalizer(user_input, claude_parsed)

    # Step 4: 유저 프로필 기본값으로 누락 슬롯 채우기
    # 예: 사용자가 출발지를 안 말했으면 프로필의 default_origin_iata(ICN)로 채움
    filled_from_profile = []  # 프로필로 채운 필드 추적 (API 명세서 응답에 포함)
    if user_profile:
        claude_parsed, filled_from_profile = _fill_from_profile(claude_parsed, user_profile)

    # Step 5: warnings 생성
    # 비현실적인 값이나 주의가 필요한 상황 감지
    warnings = _generate_warnings(claude_parsed)

    # Step 6: API 명세서 형식으로 최종 응답 구성
    # parse_id: 이 파싱 결과를 식별하는 고유 ID
    # 재질문 답변 병합 시 어떤 파싱 결과에 이어붙일지 식별하는 용도
    return {
        "parse_id": f"p_{uuid.uuid4().hex[:8]}",  # 예: "p_a3f2c1d4"
        "fields": {
            "origin":       claude_parsed.get("origin"),
            "destinations": claude_parsed.get("destinations", []),
            "budget":       claude_parsed.get("budget"),
            "pax":          claude_parsed.get("pax", {"adult": 1, "child": 0}),
            "themes":       claude_parsed.get("themes", []),
            "dates":        claude_parsed.get("dates", {"start": None, "end": None}),
        },
        "assumed_fields":    claude_parsed.get("assumed_fields", []),
        "missing_slots":     claude_parsed.get("missing_slots", []),
        "filled_from_profile": filled_from_profile,
        "warnings":          warnings,
    }


def _extract_json(text: str) -> dict:
    """
    Claude 응답 문자열에서 순수 JSON만 추출해서 dict로 변환.

    Claude가 지시를 잘 따르면 JSON만 오지만,
    가끔 ```json ... ``` 마크다운으로 감싸서 보내는 경우가 있어.
    그 경우를 대비해서 코드블록 마커를 제거하고 파싱.

    Args:
        text: Claude API 원본 응답 문자열

    Returns:
        파싱된 dict

    Raises:
        ValueError: JSON 파싱 실패 시 (원문 포함해서 에러 메시지 던짐)
    """
    # ```json 또는 ``` 제거
    text = re.sub(r"```json|```", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # 파싱 실패 시 원문도 같이 올려서 디버깅하기 쉽게
        raise ValueError(f"Claude 응답 JSON 파싱 실패: {e}\n원문:\n{text}")


def _apply_normalizer(user_input: str, parsed: dict) -> dict:
    """
    normalizer 함수들로 Claude 파싱 결과를 보정.

    Claude가 수량 표현을 놓쳤을 때만 보정하고,
    이미 값이 있으면 건드리지 않아.

    Args:
        user_input: 사용자 원본 입력 (normalizer에 넘겨서 직접 파싱)
        parsed: Claude가 반환한 파싱 결과 dict

    Returns:
        보정된 parsed dict
    """
    # 예산 보정: Claude가 budget을 null로 반환했을 때만 시도
    if not parsed.get("budget"):
        budget = normalize_budget(user_input)
        if budget:
            parsed["budget"] = budget
            # normalizer가 찾았으니 missing_slots에서 제거
            if "budget" in parsed.get("missing_slots", []):
                parsed["missing_slots"].remove("budget")

    # 기간 보정: destinations[0].nights가 없을 때만 시도
    destinations = parsed.get("destinations", [])
    if destinations and not destinations[0].get("nights"):
        duration = normalize_duration(user_input)
        if duration:
            # 첫 번째 목적지에 nights 추가
            destinations[0]["nights"] = duration["nights"]

    # 인원 보정: normalizer가 명확한 표현 찾으면 Claude 결과 덮어씌움
    # ("혼자", "둘이서" 같은 구어체를 Claude가 놓칠 수 있어서)
    headcount = normalize_headcount(user_input)
    if headcount:
        pax = parsed.setdefault("pax", {"adult": 1, "child": 0})
        pax["adult"] = headcount

    return parsed


def _fill_from_profile(parsed: dict, profile: dict) -> tuple[dict, list]:
    """
    유저 프로필의 기본값으로 누락된 슬롯을 채움.

    사용자가 출발지를 안 말하는 경우가 많은데,
    프로필에 default_origin_iata가 있으면 자동으로 채워줘.
    이렇게 채운 필드는 filled_from_profile에 기록해서
    API 응답에 포함시킴.

    Args:
        parsed: 현재까지 파싱된 dict
        profile: 유저 기본 설정 {"origin_iata": "ICN", "nationality": "KR"}

    Returns:
        (프로필 값이 채워진 parsed dict, 프로필로 채운 필드 목록)
    """
    filled = []  # 프로필로 채운 필드 목록

    # 출발지가 없고 프로필에 기본 출발지가 있으면 채우기
    if not parsed.get("origin") and profile.get("origin_iata"):
        parsed["origin"] = {
            "city": None,   # 도시명은 나중에 코드 매핑에서 채움
            "iata": profile["origin_iata"],
        }
        filled.append("origin")

        # assumed_fields 리스트에 "origin" 추가 (없으면 리스트 새로 만들기)
        assumed = parsed.setdefault("assumed_fields", [])
        if "origin" not in assumed:
            assumed.append("origin")

        # missing_slots에서는 제거 (이제 채웠으니까)
        missing = parsed.get("missing_slots", [])
        if "origin" in missing:
            missing.remove("origin")

    return parsed, filled

def _generate_warnings(parsed: dict) -> list:
    """
    파싱 결과에서 주의가 필요한 상황을 감지해서 경고 메시지 생성.

    비현실적인 값이나 주의가 필요한 상황을 탐지해서 warnings에 추가.
    파이프라인 실행을 막지는 않고 사용자에게 알려주는 용도.
    파싱 확인 카드에서 "이런 부분이 이상해요"로 보여줄 수 있음.

    Args:
        parsed: 파싱된 dict

    Returns:
        경고 메시지 문자열 리스트 (없으면 빈 리스트)
    """
    warnings = []

    budget = parsed.get("budget")
    pax = parsed.get("pax", {})
    adult = pax.get("adult", 1)
    child = pax.get("child", 0)
    total_pax = adult + child
    destinations = parsed.get("destinations", [])

    # ── 예산 검증 ────────────────────────────────────────────────────────

    if budget is not None:
        # 예산이 너무 낮은 경우 (1인 기준 최소 10만원)
        if budget < total_pax * 100000:
            warnings.append("예산이 인원 대비 낮음")

        # 예산이 비현실적으로 낮은 경우 (1만원 미만)
        if budget < 10000:
            warnings.append("예산이 너무 낮음 (1만원 미만)")

        # 예산이 비현실적으로 높은 경우 (1억 초과)
        if budget > 100000000:
            warnings.append("예산이 비현실적으로 높음 (1억 초과)")

    # ── 인원 검증 ────────────────────────────────────────────────────────

    # 성인이 0명인 경우
    if adult == 0:
        warnings.append("성인 인원이 0명임")

    # 인원이 비현실적으로 많은 경우 (100명 초과)
    if total_pax > 100:
        warnings.append("인원이 비현실적으로 많음 (100명 초과)")

    # 어린이만 있고 성인이 없는 경우
    if child > 0 and adult == 0:
        warnings.append("성인 없이 어린이만 있음")

    # ── 기간 검증 ────────────────────────────────────────────────────────

    nights = None
    if destinations:
        # destinations 안의 nights 합산
        nights = sum(d.get("nights", 0) or 0 for d in destinations)

    if nights is not None:
        # 기간이 0박인 경우
        if nights == 0:
            warnings.append("여행 기간이 0박임")

        # 기간이 비현실적으로 긴 경우 (60박 초과)
        if nights > 60:
            warnings.append("여행 기간이 비현실적으로 길음 (60박 초과)")

    # ── 목적지 검증 ──────────────────────────────────────────────────────

    # 목적지 없이 예산/기간만 있는 경우
    if not destinations and budget:
        warnings.append("목적지가 지정되지 않음")

    # ── 교차 검증 ────────────────────────────────────────────────────────

    # 예산 대비 기간이 너무 긴 경우
    # (1인 1박 최소 3만원 기준)
    if budget and nights and total_pax:
        min_required = total_pax * nights * 30000
        if budget < min_required:
            warnings.append(f"{total_pax}인 {nights}박 기준 예산이 너무 낮음 (최소 {min_required:,}원 권장)")

    return warnings