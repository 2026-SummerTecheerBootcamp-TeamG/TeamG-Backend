"""
자연어 → 구조화 JSON 변환 모듈 (Intent Parser)

이 모듈이 요청 이해 파이프라인의 핵심.
사용자가 채팅창에 "도쿄 2박3일 미식, 30만" 이라고 치면
이 모듈이 그걸 받아서 API 명세서 형식의 JSON으로 변환함:

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
    1. Claude API에 사용자 입력 전달 → JSON 응답 받기 (실패 시 최대 2회 재시도)
    2. 여행과 무관한 입력 감지 → 안내 메시지 반환
    3. normalizer로 수량 표현 보정 (Claude가 놓친 "80만" 같은 것)
    4. 유저 프로필로 누락 슬롯 자동 채우기 (출발지 등)
    5. 비현실 값 감지 → warnings 생성
    6. API 명세서 형식으로 변환해서 반환
"""
import json
import re
import uuid

from agents.claude_client import ask_claude
from .exceptions import ClaudeResponseError, NotTravelRelatedError
from .normalizer import normalize_budget, normalize_duration, normalize_headcount


# Claude에게 전달하는 시스템 프롬프트.
# 에이전트 역할과 출력 형식을 정의함.
# 이 프롬프트가 파싱 품질을 결정하는 핵심이라서 신중하게 작성해야 함.
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

# Claude 호출 실패(JSON 파싱 실패) 시 최대 재시도 횟수 (총 시도 = 1 + 이 값)
MAX_RETRIES = 2

# ── warnings 판단 기준값 ────────────────────────────────────────────────
# 기존엔 _generate_warnings 안에 하드코딩돼 있던 값들.
# 기준이 바뀔 때 여기만 고치면 되게 상수로 뺌.
MIN_BUDGET_PER_PERSON = 100_000       # 1인 기준 최소 예산 (미만이면 "낮음" 경고)
MIN_BUDGET_ABSOLUTE = 10_000          # 이 밑이면 비현실적으로 낮은 예산
MAX_BUDGET_ABSOLUTE = 100_000_000     # 이 위면 비현실적으로 높은 예산
MAX_PAX = 100                         # 이 초과면 인원이 비현실적으로 많음
MAX_NIGHTS = 60                       # 이 초과면 여행 기간이 비현실적으로 긴
MIN_BUDGET_PER_PERSON_PER_NIGHT = 30_000  # 인원x박수 대비 최소 필요 예산 단가


def parse_intent(user_input: str, user_profile: dict | None = None) -> dict:
    """
    자연어 입력을 API 명세서 형식의 구조화 JSON으로 변환하는 메인 함수.

    ChatInput 컴포넌트에서 사용자가 메시지를 보내면
    views.py가 이 함수를 호출해서 파싱 결과를 돌려받음.

    Args:
        user_input: 사용자가 채팅창에 입력한 자연어 문자열
                    예: "도쿄 2박3일 미식, 30만"
        user_profile: 로그인한 유저의 기본 설정값
                      예: {"origin_iata": "ICN", "nationality": "KR"}
                      None이면 프로필 채우기 스킵

    Returns:
        API 명세서 형식의 파싱 결과 dict

    Raises:
        ClaudeResponseError: Claude 응답이 JSON 형식이 아니어서
                              재시도까지 다 실패했을 때 (→ views.py에서 422)
        NotTravelRelatedError: 여행과 무관한 입력일 때 (→ views.py에서 422)
        Exception: Claude API 호출 자체가 실패했을 때 (키 오류, 네트워크 등)
                   (→ views.py에서 500)
    """
    # ── Step 1. Claude API 호출 + JSON 파싱 (재시도 포함) ────────────────
    claude_parsed = _call_claude_and_parse(user_input)

    # ── Step 2. 여행과 무관한 입력 감지 ─────────────────────────────────
    _assert_travel_related(claude_parsed)

    # ── Step 3. normalizer로 수량 표현 보정 ──────────────────────────────
    claude_parsed = _apply_normalizer(user_input, claude_parsed)

    # ── Step 4. 유저 프로필 기본값으로 누락 슬롯 채우기 ─────────────────
    filled_from_profile = []
    if user_profile:
        claude_parsed, filled_from_profile = _fill_from_profile(
            claude_parsed, user_profile
        )

    # ── Step 5. warnings 생성 ────────────────────────────────────────────
    warnings = _generate_warnings(claude_parsed)

    # ── Step 6. API 명세서 형식으로 최종 응답 구성 ───────────────────────
    # parse_id: 이 파싱 결과를 식별하는 고유 ID.
    # 재질문 답변 병합(parse/answer/)이나
    # 파싱 확인(parse/{parse_id}/)에서 어떤 파싱 결과인지 식별하는 용도.
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
        "assumed_fields":      claude_parsed.get("assumed_fields", []),
        "missing_slots":       claude_parsed.get("missing_slots", []),
        "filled_from_profile": filled_from_profile,
        "warnings":            warnings,
    }


# ── Step 1 helper ────────────────────────────────────────────────────────

def _call_claude_and_parse(user_input: str) -> dict:
    """
    Claude API를 호출하고 응답을 JSON dict로 파싱.

    JSON 파싱에 실패하면(Claude가 형식을 안 지켰을 때) 최대 MAX_RETRIES회
    재시도하되, 재시도 시 프롬프트에 JSON 강제 힌트를 추가함.
    최종 실패 시 사용자용 에러 메시지로 변환해서 raise.

    Args:
        user_input: 사용자 원본 입력

    Returns:
        Claude 응답을 파싱한 dict

    Raises:
        ClaudeResponseError: 재시도를 모두 소진해도 JSON 파싱에 실패했을 때.
                              message는 API 명세서 422 응답의 error 문구와 그대로 일치함
                              (예: "Claude 응답 JSON 파싱 실패: ..."),
                              user_message는 챗봇에 보여줄 친절한 안내 문구.
    """
    last_error = None
    prompt = user_input

    for attempt in range(MAX_RETRIES + 1):
        try:
            raw_response = ask_claude(
                prompt=prompt,
                system=SYSTEM_PROMPT,
                max_tokens=1024,  # 파싱 결과는 짧으니까 1024면 충분
            )
            return _extract_json(raw_response)

        except ValueError as e:
            last_error = e
            if attempt < MAX_RETRIES:
                # 재시도 전에 프롬프트에 힌트를 추가해서
                # 다음 시도에서 JSON 형식을 강제함
                prompt = f"{prompt}\n(반드시 JSON 형식으로만 응답해줘)"
                continue

    # 최대 재시도 횟수를 모두 소진했을 때.
    # message는 원본 파싱 에러를 그대로 살려서 API 명세서 422 응답과 맞추고,
    # user_message는 챗봇 UI에서 보여줄 친절한 문구로 따로 둠.
    raise ClaudeResponseError(
        f"Claude 응답 JSON 파싱 실패: {last_error}",
        user_message=(
            "입력을 이해하지 못했어요. 다시 한번 입력해 주세요.\n"
            "예: '후쿠오카 3박4일 쇼핑, 80만원'"
        ),
    ) from last_error


def _extract_json(text: str) -> dict:
    """
    Claude 응답 문자열에서 순수 JSON만 추출해서 dict로 변환.

    Claude가 지시를 잘 따르면 JSON만 오지만,
    가끔 ```json ... ``` 마크다운으로 감싸거나
    "네 알겠습니다" 같은 설명을 앞뒤로 붙여서 보내는 경우가 있음.
    1차: 코드블록 마커만 제거하고 바로 파싱 시도.
    2차(fallback): 첫 '{' ~ 마지막 '}' 구간만 잘라서 다시 파싱 시도.

    Args:
        text: Claude API 원본 응답 문자열

    Returns:
        파싱된 dict

    Raises:
        ValueError: 두 시도 모두 JSON 파싱 실패 시 (원문 포함해서 에러 메시지 던짐)
    """
    # ```json 또는 ``` 제거
    cleaned = re.sub(r"```json|```", "", text).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass  # fallback으로 넘어감

    # fallback: 앞뒤에 설명 텍스트가 붙어있는 경우 대비,
    # 가장 바깥쪽 { ... } 구간만 추출해서 재시도
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and start < end:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"Claude 응답 JSON 파싱 실패: {e}\n원문:\n{text}")

    raise ValueError(f"Claude 응답에서 JSON을 찾지 못함\n원문:\n{text}")


# ── Step 2 helper ────────────────────────────────────────────────────────

def _assert_travel_related(parsed: dict) -> None:
    """
    여행과 무관한 입력인지 확인하고, 무관하면 ValueError를 던짐.

    destinations, budget, themes 중 아무것도 없으면
    여행 요청이 아닌 것으로 판단.
    예: "안녕", "오늘 날씨 어때?" 같은 무관한 입력 차단.

    Args:
        parsed: Claude 파싱 결과 dict

    Raises:
        NotTravelRelatedError: 여행과 무관한 입력일 때
    """
    is_travel_related = (
        parsed.get("destinations") or
        parsed.get("budget") or
        parsed.get("themes")
    )
    if not is_travel_related:
        raise NotTravelRelatedError(
            "여행과 무관한 입력",
            user_message=(
                "여행 관련 요청을 입력해 주세요.\n"
                "예: '후쿠오카 3박4일 쇼핑, 80만원'"
            ),
        )


# ── Step 3 helper ────────────────────────────────────────────────────────

def _apply_normalizer(user_input: str, parsed: dict) -> dict:
    """
    normalizer 함수들로 Claude 파싱 결과를 보정.

    Claude가 수량 표현을 놓쳤을 때만 보정하고,
    이미 값이 있으면 건드리지 않음.

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

    # 인원 보정: normalizer가 명확한 표현을 찾으면 Claude 결과를 덮어씌움.
    # "혼자", "둘이서" 같은 구어체를 Claude가 놓칠 수 있어서 추가 처리함.
    headcount = normalize_headcount(user_input)
    if headcount:
        pax = parsed.setdefault("pax", {"adult": 1, "child": 0})
        pax["adult"] = headcount

    return parsed


# ── Step 4 helper ────────────────────────────────────────────────────────

def _fill_from_profile(parsed: dict, profile: dict) -> tuple[dict, list]:
    """
    유저 프로필의 기본값으로 누락된 슬롯을 채움.

    사용자가 출발지를 안 말하는 경우가 많은데,
    프로필에 default_origin_iata가 있으면 자동으로 채워줌.
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


# ── Step 5 helpers ───────────────────────────────────────────────────────
# 기존 _generate_warnings() 하나에 다 몰려있던 걸
# 검증 항목별로 쪼갬. 각 함수는 독립적으로 테스트 가능.

def _check_budget_warnings(budget: int | None, total_pax: int) -> list[str]:
    """예산 관련 경고 (인원 대비 낮음 / 절대적으로 낮음·높음)."""
    if budget is None:
        return []

    warnings = []
    if budget < total_pax * MIN_BUDGET_PER_PERSON:
        warnings.append("예산이 인원 대비 낮음")
    if budget < MIN_BUDGET_ABSOLUTE:
        warnings.append("예산이 너무 낮음 (1만원 미만)")
    if budget > MAX_BUDGET_ABSOLUTE:
        warnings.append("예산이 비현실적으로 높음 (1억 초과)")
    return warnings


def _check_pax_warnings(adult: int, child: int, total_pax: int) -> list[str]:
    """인원 관련 경고 (성인 0명 / 인원 과다 / 성인 없이 어린이만)."""
    warnings = []
    if adult == 0:
        warnings.append("성인 인원이 0명임")
    if total_pax > MAX_PAX:
        warnings.append(f"인원이 비현실적으로 많음 ({MAX_PAX}명 초과)")
    if child > 0 and adult == 0:
        warnings.append("성인 없이 어린이만 있음")
    return warnings


def _check_duration_warnings(nights: int | None) -> list[str]:
    """전체 여행 기간 관련 경고 (0박 / 비현실적으로 긴 기간)."""
    if nights is None:
        return []

    warnings = []
    if nights == 0:
        warnings.append("여행 기간이 0박임")
    if nights > MAX_NIGHTS:
        warnings.append(f"여행 기간이 비현실적으로 길음 ({MAX_NIGHTS}박 초과)")
    return warnings


def _check_multi_destination_warnings(destinations: list[dict]) -> list[str]:
    """다중 목적지 관련 경고 (nights 누락 / 총 박수 과다)."""
    if len(destinations) <= 1:
        return []

    warnings = []

    missing_nights = [
        d.get("city", "알 수 없는 도시")
        for d in destinations
        if not d.get("nights")
    ]
    if missing_nights:
        warnings.append(
            f"숙박 일수가 지정되지 않은 목적지 있음: {', '.join(missing_nights)}"
        )

    total_nights = sum(d.get("nights", 0) or 0 for d in destinations)
    if total_nights > MAX_NIGHTS:
        warnings.append(f"총 여행 기간이 너무 길음 ({total_nights}박)")

    return warnings


def _check_destination_warnings(destinations: list[dict], budget: int | None) -> list[str]:
    """목적지 없이 예산/기간만 있는 경우 경고."""
    if not destinations and budget:
        return ["목적지가 지정되지 않음"]
    return []


def _check_cross_warnings(
    budget: int | None, nights: int | None, total_pax: int
) -> list[str]:
    """
    예산 대비 기간이 너무 짧은지 교차 검증.

    1인 1박 최소 MIN_BUDGET_PER_PERSON_PER_NIGHT원을 기준으로
    최소 필요 예산을 계산함.
    """
    if not (budget and nights and total_pax):
        return []

    min_required = total_pax * nights * MIN_BUDGET_PER_PERSON_PER_NIGHT
    if budget < min_required:
        return [
            f"{total_pax}인 {nights}박 기준 예산이 너무 낮음 "
            f"(최소 {min_required:,}원 권장)"
        ]
    return []


def _generate_warnings(parsed: dict) -> list:
    """
    파싱 결과에서 주의가 필요한 상황을 감지해서 경고 메시지 생성.

    비현실적인 값이나 주의가 필요한 상황을 탐지해서 warnings에 추가함.
    파이프라인 실행을 막지는 않고 사용자에게 알려주는 용도.
    파싱 확인 카드에서 "이런 부분이 이상해요"로 보여줄 수 있음.

    실제 검증 로직은 항목별 _check_*_warnings 함수에 위임하고,
    여기서는 결과를 모아서 반환하는 역할만 함.

    Args:
        parsed: 파싱된 dict

    Returns:
        경고 메시지 문자열 리스트 (없으면 빈 리스트)
    """
    budget = parsed.get("budget")
    pax = parsed.get("pax", {})
    adult = pax.get("adult", 1)
    child = pax.get("child", 0)
    total_pax = adult + child
    destinations = parsed.get("destinations", [])

    nights = None
    if destinations:
        nights = sum(d.get("nights", 0) or 0 for d in destinations)

    warnings = []
    warnings += _check_budget_warnings(budget, total_pax)
    warnings += _check_pax_warnings(adult, child, total_pax)
    warnings += _check_duration_warnings(nights)
    warnings += _check_multi_destination_warnings(destinations)
    warnings += _check_destination_warnings(destinations, budget)
    warnings += _check_cross_warnings(budget, nights, total_pax)
    return warnings