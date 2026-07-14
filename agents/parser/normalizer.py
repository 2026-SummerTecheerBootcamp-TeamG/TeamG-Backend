"""
한국어 수량 표현 정규화 모듈

Claude가 자연어를 파싱할 때 "80만원", "3박4일" 같은
한국어 특유의 수량 표현을 숫자로 못 바꾸는 경우가 있음
이 모듈은 그런 표현들을 정규식으로 직접 잡아서 숫자로 변환해줌.

Claude 파싱 결과를 보정하는 역할이라서,
intent_parser.py가 Claude 호출 후 이 모듈을 불러서 사용함.
"""
import re


def normalize_budget(text: str) -> int | None:
    """
    사용자 입력에서 예산 표현을 찾아 원(₩) 단위 정수로 변환.

    Claude가 "80만원"을 800000으로 못 바꿨을 때 이 함수가 대신 처리함.
    
    처리하는 패턴:
        "80만"      → 800000   (만 단위)
        "80만원"    → 800000   (만 단위 + 원)
        "100만원"   → 1000000  (100만)
        "15만 5천"  → 155000   (만 + 천 조합)
        "800000"    → 800000   (이미 숫자면 그대로)

    Args:
        text: 사용자 원본 입력 문자열
              예: "후쿠오카 3박4일 쇼핑, 80만원"

    Returns:
        변환된 정수 (원 단위)
        패턴을 못 찾으면 None 반환 → intent_parser에서 missing으로 처리
    """
    # 쉼표, 공백 제거해서 "80만 원" 같은 표현도 처리
    text = text.replace(",", "").replace(" ", "")

    # "N만 M천" 패턴 먼저 체크 (더 구체적인 패턴 우선)
    # 예: "15만5천" → 15*10000 + 5*1000 = 155000
    match = re.search(r"(\d+)만\s*(\d+)천", text)
    if match:
        return int(match.group(1)) * 10000 + int(match.group(2)) * 1000

    # "N만" 패턴
    # 예: "80만" → 80*10000 = 800000
    match = re.search(r"(\d+)만", text)
    if match:
        return int(match.group(1)) * 10000

    # 순수 숫자는 "만/천" 단위 표현이 없을 때만 시도
    # "3박4일 80만" 같은 입력에서 3을 예산으로 잘못 읽는 버그 방지
    # text는 위에서 이미 공백/쉼표 제거된 상태라 원본 text 따로 체크
    if "만" not in text and "천" not in text:
        match = re.search(r"(\d+)", text)
        if match:
            return int(match.group(1))

def normalize_duration(text: str) -> dict | None:
    """
    여행 기간 표현을 nights(숙박일수)와 days(여행일수)로 변환.

    TripRequest 테이블에는 기간이 nights로 저장되므로
    "3박4일" 같은 표현을 분리해서 저장해야 함.

    처리하는 패턴:
        "3박4일" → {"nights": 3, "days": 4}
        "2박3일" → {"nights": 2, "days": 3}
        "3박"    → {"nights": 3, "days": 4}  ← days는 nights+1로 자동 계산

    Args:
        text: 사용자 원본 입력 문자열

    Returns:
        {"nights": int, "days": int} 딕셔너리
        기간 표현을 못 찾으면 None 반환
    """
    # "N박M일" 패턴 — 둘 다 명시된 경우
    match = re.search(r"(\d+)박\s*(\d+)일", text)
    if match:
        return {
            "nights": int(match.group(1)),
            "days": int(match.group(2)),
        }

    # "N박"만 있는 경우 — days는 nights + 1로 계산
    # 예: "3박" → 3박4일로 간주
    match = re.search(r"(\d+)박", text)
    if match:
        nights = int(match.group(1))
        return {"nights": nights, "days": nights + 1}

    return None


def normalize_headcount(text: str) -> int | None:
    """
    인원 표현을 정수로 변환.

    "성인 2명"처럼 명시적인 경우뿐 아니라
    "혼자", "둘이서" 같은 구어체 표현도 처리.

    처리하는 패턴:
        "혼자", "나 혼자", "1인" → 1
        "둘이", "둘이서", "2인"  → 2
        "성인 2명", "2명"        → 2
        "3명"                   → 3

    Args:
        text: 사용자 원본 입력 문자열

    Returns:
        인원 수 정수
        인원 표현을 못 찾으면 None 반환
        → None이면 intent_parser에서 assumed_fields에 "adult" 추가하고 기본값 1 사용
    """
    # 혼자 관련 표현
    if any(w in text for w in ["혼자", "나 혼자", "1인"]):
        return 1

    # 둘이 관련 표현
    if any(w in text for w in ["둘이", "둘이서", "2인"]):
        return 2

    # "N명" 패턴
    match = re.search(r"(\d+)\s*명", text)
    if match:
        return int(match.group(1))

    return None