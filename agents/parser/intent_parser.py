"""
자연어 → 구조화 JSON 변환 모듈 (Intent Parser)

이 모듈이 요청 이해 파이프라인의 핵심.
사용자가 채팅창에 "후쿠오카 3박4일 쇼핑, 80만" 이라고 치면
이 모듈이 그걸 받아서 아래처럼 구조화된 JSON으로 변환해줌:

{
    "destination": [{"city": "후쿠오카", "city_en": "Fukuoka", 
                     "country": "일본", "iata": "FUK"}],
    "origin": {"city": "서울", "iata": "ICN"},
    "nights": 3,
    "days": 4,
    "start_date": null,
    "end_date": null,
    "adult": 1,
    "kid": 0,
    "budget_total": 800000,
    "themes": ["쇼핑"],
    "assumed_fields": ["origin", "adult"],  ← 사용자가 안 말했지만 기본값으로 채운 것
    "missing_fields": []                    ← 아직 모르는 것 (재질문 대상)
}

작동 순서:
    1. Claude API에 사용자 입력 전달 → JSON 응답 받기
    2. normalizer로 수량 표현 보정 (Claude가 놓친 "80만" 같은 것)
    3. 유저 프로필로 누락 슬롯 자동 채우기 (출발지 등)
"""
import json
import re

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
    "destination": [
        {
            "city": "도시명(한글)",
            "city_en": "도시명(영어)",
            "country": "국가명(한글)",
            "iata": "공항코드(대문자 3자리, 예: FUK, NRT, ICN)"
        }
    ],
    "origin": {
        "city": "출발 도시(한글)",
        "iata": "출발 공항코드"
    },
    "nights": 숙박일수(정수 또는 null),
    "days": 여행일수(정수 또는 null),
    "start_date": "YYYY-MM-DD 형식 또는 null",
    "end_date": "YYYY-MM-DD 형식 또는 null",
    "adult": 성인수(정수, 기본 1),
    "kid": 어린이수(정수, 기본 0),
    "budget_total": 총예산(원 단위 정수 또는 null, 예: 800000),
    "themes": ["테마1", "테마2"],
    "assumed_fields": ["사용자가 말하지 않아서 기본값으로 채운 필드명 목록"],
    "missing_fields": ["파이프라인 실행 전에 사용자에게 되물어야 할 필드명 목록"]
}

규칙:
- origin을 말하지 않으면 → origin을 null로, assumed_fields에 "origin" 추가
- 인원을 말하지 않으면 → adult를 1로, assumed_fields에 "adult" 추가
- 날짜/기간을 말하지 않으면 → missing_fields에 "nights" 추가
- 예산을 말하지 않으면 → missing_fields에 "budget_total" 추가
- 목적지를 말하지 않으면 → missing_fields에 "destination" 추가
- 테마 예시: 쇼핑, 맛집, 관광, 휴양, 액티비티, 문화
- "80만"은 800000으로 변환, "3박4일"은 nights=3, days=4로 변환
"""


def parse_intent(user_input: str, user_profile: dict | None = None) -> dict:
    """
    자연어 입력을 구조화 JSON으로 변환하는 메인 함수.

    ChatInput 컴포넌트에서 사용자가 메시지를 보내면
    views.py가 이 함수를 호출해서 파싱 결과를 돌려받아.

    Args:
        user_input: 사용자가 채팅창에 입력한 자연어 문자열
                    예: "후쿠오카 3박4일 쇼핑, 80만"
        user_profile: 로그인한 유저의 기본 설정값
                      예: {"origin_iata": "ICN", "nationality": "KR"}
                      None이면 프로필 채우기 스킵

    Returns:
        구조화된 파싱 결과 dict (스키마는 모듈 docstring 참고)

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
    parsed = _extract_json(raw_response)

    # Step 3: normalizer로 수량 표현 보정
    # Claude가 "80만"을 800000으로 못 바꿨거나, 기간 계산이 틀렸을 때 수정
    parsed = _apply_normalizer(user_input, parsed)

    # Step 4: 유저 프로필 기본값으로 누락 슬롯 채우기
    # 예: 사용자가 출발지를 안 말했으면 프로필의 default_origin_iata(ICN)로 채움
    if user_profile:
        parsed = _fill_from_profile(parsed, user_profile)

    return parsed


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
    # 예산 보정: Claude가 budget_total을 null로 반환했을 때만 시도
    if not parsed.get("budget_total"):
        budget = normalize_budget(user_input)
        if budget:
            parsed["budget_total"] = budget
            # normalizer가 찾았으니 missing_fields에서 제거
            if "budget_total" in parsed.get("missing_fields", []):
                parsed["missing_fields"].remove("budget_total")

    # 기간 보정: nights가 없을 때만 시도
    if not parsed.get("nights"):
        duration = normalize_duration(user_input)
        if duration:
            parsed["nights"] = duration["nights"]
            parsed["days"] = duration["days"]

    # 인원 보정: normalizer가 명확한 표현 찾으면 Claude 결과 덮어씌움
    # ("혼자", "둘이서" 같은 구어체를 Claude가 놓칠 수 있어서)
    headcount = normalize_headcount(user_input)
    if headcount:
        parsed["adult"] = headcount

    return parsed


def _fill_from_profile(parsed: dict, profile: dict) -> dict:
    """
    유저 프로필의 기본값으로 누락된 슬롯을 채움.

    사용자가 출발지를 안 말하는 경우가 많은데,
    프로필에 default_origin_iata가 있으면 자동으로 채워줘.
    이렇게 채운 필드는 assumed_fields에 기록해서
    파싱 확인 카드에서 "이렇게 가정했어요"로 보여줄 수 있어.

    Args:
        parsed: 현재까지 파싱된 dict
        profile: 유저 기본 설정 {"origin_iata": "ICN", "nationality": "KR"}

    Returns:
        프로필 값이 채워진 parsed dict
    """
    # 출발지가 없고 프로필에 기본 출발지가 있으면 채우기
    if not parsed.get("origin") and profile.get("origin_iata"):
        parsed["origin"] = {
            "city": None,  # 도시명은 나중에 코드 매핑에서 채움
            "iata": profile["origin_iata"],
        }
        # assumed_fields 리스트에 "origin" 추가 (없으면 리스트 새로 만들기)
        assumed = parsed.setdefault("assumed_fields", [])
        if "origin" not in assumed:
            assumed.append("origin")

        # missing_fields에서는 제거 (이제 채웠으니까)
        missing = parsed.get("missing_fields", [])
        if "origin" in missing:
            missing.remove("origin")

    return parsed