"""
=====================================================================
후보 평가 (p0) - 만족도 산정
=====================================================================

주기능: 만족도 산정
설명: 성급, 관광/쇼핑 테마 가중
비고: p1에서 리뷰 점수 반영 예정

[참고한 실데이터 패턴] (후쿠오카 LiteAPI 실행 결과)
    Hotel Torifito Hakata Gion   (4성) = 440,103원 · 만족도 90.0점
    Hotel Monte Hermana Fukuoka  (4성) = 463,527원 · 만족도 90.0점
    lyf Tenjin Fukuoka           (3성) = 338,068원 · 만족도 80.0점
    THE LIVELY FUKUOKA HAKATA    (3성) = 386,092원 · 만족도 80.0점
    Comfort Hotel Hakata         (3성) = 438,459원 · 만족도 80.0점

    -> 가격과 무관하게 "성급"에 정확히 비례해서 만족도가 매겨져 있음
       (3성=80.0, 4성=90.0 -> 10점 단위 정비례 패턴)
       이 파일의 기본 스코어링 공식이 이 실데이터 패턴을 그대로 재현하도록 설계함.

[MCP TRAVEL PLANNER POC(모스크바 리포트) 참고 포인트]
    예산 에이전트(한계효용 그리디)가 동작하려면, 숙소/항공/활동 같은
    "서브 에이전트"가 후보마다 {label, price, utility, 근거}를 리턴해줘야 함.
    이 파일의 score_candidates()가 바로 그 "숙소 서브 에이전트의 utility 계산" 역할.
    -> 여기서 만든 CandidateScore를 나중에 예산 에이전트에 그대로 넘기면 됨.

[아직 안 한 것 / 다음 단계]
    - 리뷰 점수 반영 (p1, 명세서에 명시됨)
    - 관광/쇼핑 테마 가중치는 "편의시설 키워드 매칭" 방식으로 우선 구현.
      실제 리뷰/위치 기반 가중치는 location_enricher.py(p1)에서 보강 예정.
=====================================================================
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict


# ---------------------------------------------------------------------------
# 정책 상수
# ---------------------------------------------------------------------------

# 성급 -> 기본 만족도 점수 매핑.
# 후쿠오카 실데이터(3성=80, 4성=90)를 그대로 재현하는 공식: base = 50 + star*10
# (1성=60, 2성=70, 3성=80, 4성=90, 5성=100)
def _base_score_from_star(star_rating: Optional[int]) -> tuple[float, str]:
    """
    성급을 받아서 (기본 점수, 산정 근거 문자열)을 돌려주는 내부 함수.

    star_rating이 None이거나 유효 범위(1~5) 밖이면, "정보 없음" 취급해서
    중간값(70점, 2~3성 사이 정도)을 기본값으로 준다.
    -> 이렇게 하는 이유: 성급 정보가 없다고 무조건 0점을 주면
       (예: 데이터 누락된 신규 호텔이) 부당하게 순위 밖으로 밀려나므로,
       "정보 없을 때의 안전한 중간값"을 쓰는 게 더 합리적.
    """
    if star_rating is None or not (1 <= star_rating <= 5):
        return 70.0, "성급 정보 없음 (기본값 70점 적용)"

    score = 50.0 + (star_rating * 10.0)
    return score, f"{star_rating}성급 기준 기본 점수 {score:.1f}점"


# 테마별로 "이 편의시설 키워드가 있으면 가산점을 준다"는 매핑.
# 지금은 호텔 편의시설(hotelFacilities) 텍스트에 키워드가 포함되는지로
# 단순 매칭하는 방식. 나중에 위치 기반(location_enricher.py)으로 고도화 예정.
THEME_FACILITY_KEYWORDS: Dict[str, List[str]] = {
    "관광": ["Tour desk", "Sightseeing", "City center", "Tourist information"],
    "쇼핑": ["Shopping", "Shopping mall", "Shopping area", "Gift shop"],
}

THEME_BONUS_PER_MATCH = 3.0  # 테마 키워드 1개 매칭될 때마다 더해줄 가산점
THEME_BONUS_MAX = 10.0       # 테마 가산점 상한 (한 후보가 테마 보너스로 너무 튀지 않게)

MAX_UTILITY_SCORE = 100.0
MIN_UTILITY_SCORE = 0.0


def _theme_bonus(theme: Optional[str], facilities: List[str]) -> tuple[float, Optional[str]]:
    """
    테마(예: '관광', '쇼핑')와 호텔의 편의시설 리스트를 비교해서
    (가산점, 근거 문자열 또는 None)을 돌려주는 내부 함수.

    facilities가 비어있거나 theme이 None이면 가산점 없이 (0, None) 반환.
    """
    if not theme or theme not in THEME_FACILITY_KEYWORDS:
        return 0.0, None

    if not facilities:
        return 0.0, f"'{theme}' 테마 가중치 미적용 (편의시설 데이터 없음)"

    keywords = THEME_FACILITY_KEYWORDS[theme]
    # 대소문자 구분 없이 비교하기 위해 전부 소문자로 통일
    facility_text = " ".join(facilities).lower()
    matched = [kw for kw in keywords if kw.lower() in facility_text]

    if not matched:
        return 0.0, f"'{theme}' 테마 관련 편의시설 없음"

    bonus = min(len(matched) * THEME_BONUS_PER_MATCH, THEME_BONUS_MAX)
    reason = f"'{theme}' 테마 편의시설 {len(matched)}개 매칭({', '.join(matched)}) +{bonus:.1f}점"
    return bonus, reason


# ---------------------------------------------------------------------------
# 결과 자료구조
# ---------------------------------------------------------------------------

@dataclass
class CandidateScore:
    """
    후보 하나에 대한 평가 결과.

    utility_score: 최종 만족도 점수 (0~100 범위로 클램프됨)
    reasons: 점수가 왜 이렇게 나왔는지 설명하는 문자열 리스트
             (나중에 예산 에이전트/UI에서 "왜 이 호텔이 추천됐는지" 보여줄 때 사용)
    name/latitude/longitude: LiteAPI 정적 정보(/data/hotels)에서 온 표시용 필드.
             점수 계산에는 안 쓰이지만, 최종 선택된 후보를 DB에 저장할 때
             (trips/services.py) 실제 이름/좌표로 채우려면 여기까지 들고 와야 함.
    """
    hotel_id: str
    price_krw: float
    utility_score: float
    reasons: List[str] = field(default_factory=list)
    name: Optional[str] = None
    star_rating: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    def to_dict(self) -> dict:
        """예산 에이전트 등 다른 서비스에 넘길 때 쓰는 딕셔너리 변환"""
        return {
            "hotel_id": self.hotel_id,
            "krw": self.price_krw,
            "utility": self.utility_score,
            "reasons": self.reasons,
            "name": self.name,
            "star_rating": self.star_rating,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


# ---------------------------------------------------------------------------
# 메인 함수
# ---------------------------------------------------------------------------

def score_candidate(
    hotel_id: str,
    price_krw: float,
    star_rating: Optional[int],
    theme: Optional[str] = None,
    facilities: Optional[List[str]] = None,
    name: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> CandidateScore:
    """
    호텔 후보 1개에 대한 만족도 점수를 계산한다.

    Args:
        hotel_id: 호텔 ID
        price_krw: 이 후보의 가격 (원화)
        star_rating: 성급 (1~5). 정보 없으면 None
        theme: 여행 테마 ('관광', '쇼핑' 등). 없으면 None
        facilities: 호텔 편의시설 리스트 (LiteAPI hotelFacilities). 없으면 None
        name/latitude/longitude: 표시용 정적 정보. 점수 계산에는 안 쓰이고
            결과에 그대로 실어 보내기만 함 (DB 저장 시 필요)

    Returns:
        CandidateScore (최종 점수 0~100 범위로 클램프됨)
    """
    facilities = facilities or []
    reasons: List[str] = []

    # 1) 성급 기반 기본 점수
    base_score, base_reason = _base_score_from_star(star_rating)
    reasons.append(base_reason)

    # 2) 테마 가중치 가산점
    bonus, bonus_reason = _theme_bonus(theme, facilities)
    if bonus_reason:
        reasons.append(bonus_reason)

    # 3) 합산 후 0~100 범위로 클램프
    #    (테마 보너스가 겹쳐서 100점을 넘거나, 이론상 음수가 되는 걸 방지)
    final_score = base_score + bonus
    final_score = max(MIN_UTILITY_SCORE, min(MAX_UTILITY_SCORE, final_score))

    return CandidateScore(
        hotel_id=hotel_id,
        price_krw=price_krw,
        utility_score=final_score,
        reasons=reasons,
        name=name,
        star_rating=star_rating,
        latitude=latitude,
        longitude=longitude,
    )


def score_candidates(
    candidates: List[dict],
    theme: Optional[str] = None,
) -> List[CandidateScore]:
    """
    여러 후보를 한 번에 평가해서 만족도 점수 내림차순으로 정렬해 돌려준다.

    Args:
        candidates: 각 원소가 다음 키를 가진 딕셔너리인 리스트
            {
                "hotel_id": str,
                "price_krw": float,
                "star_rating": Optional[int],
                "facilities": Optional[List[str]],
                "name": Optional[str],
                "latitude": Optional[float],
                "longitude": Optional[float],
            }
            (hotel_search.HotelCandidate + clients.HotelStaticInfo를
             hotel_id 기준으로 합쳐서 이 형태로 만들어 넘기면 됨)
        theme: 여행 테마

    Returns:
        CandidateScore 리스트, utility_score 내림차순 정렬
        (동점이면 원래 순서 유지 - 파이썬 sort는 안정 정렬이라 보장됨)
    """
    scored = [
        score_candidate(
            hotel_id=c["hotel_id"],
            price_krw=c["price_krw"],
            star_rating=c.get("star_rating"),
            theme=theme,
            facilities=c.get("facilities"),
            name=c.get("name"),
            latitude=c.get("latitude"),
            longitude=c.get("longitude"),
        )
        for c in candidates
    ]
    # key에 음수를 씌우는 트릭으로 "내림차순" 정렬 (reverse=True 안 써도 됨)
    scored.sort(key=lambda s: -s.utility_score)
    return scored


def merge_candidates_with_static_info(
    hotel_candidates: list,  # List[hotel_search.HotelCandidate], 순환import 방지 위해 타입힌트 생략
    static_info_by_id: Dict[str, "object"],  # Dict[str, clients.liteapi_client.HotelStaticInfo]
) -> List[dict]:
    """
    hotel_search.py가 만든 HotelCandidate(가격 정보)와
    clients/liteapi_client.py가 만든 HotelStaticInfo(성급/편의시설 정보)를
    hotel_id 기준으로 합쳐서 score_candidates()에 넘길 수 있는 형태로 만든다.

    이렇게 "합치는 함수"를 따로 두는 이유:
        가격 조회(hotel_search)와 정적정보 조회(liteapi_client)가
        서로 다른 API 호출이라 결과가 따로따로 나오는데,
        평가(candidate_scorer) 입장에서는 두 정보가 합쳐진 하나의
        딕셔너리로 받는 게 다루기 편하기 때문.

    static_info_by_id에 없는 hotel_id는 성급/편의시설을 None/빈 리스트로 채움
    (정적 정보 조회가 실패하거나 일부 호텔만 매칭 안 된 경우를 대비한 방어 코드).
    """
    merged = []
    for candidate in hotel_candidates:
        static_info = static_info_by_id.get(candidate.hotel_id)
        merged.append({
            "hotel_id": candidate.hotel_id,
            "price_krw": candidate.min_price_amount,
            "star_rating": static_info.star_rating if static_info else None,
            "facilities": static_info.facilities if static_info else [],
            "name": static_info.name if static_info else None,
            "latitude": static_info.latitude if static_info else None,
            "longitude": static_info.longitude if static_info else None,
        })
    return merged