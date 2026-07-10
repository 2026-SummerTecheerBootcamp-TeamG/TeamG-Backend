"""
숙소 검색 (p0) - 호텔 후보 조회

주기능: 호텔 후보 조회
상세기능: LiteAPI 목록 가격 조회 후 정렬
설명: 목록에 있어도 가격 없는 호텔 존재 -> 후보 제외, 전부 없으면 후보 부재 처리
비고:
    - 0건 시 후보 부재 라우팅
    - 제외 감안해 목록 과조회 필요

LiteAPI 실제 스펙 (docs.liteapi.travel 확인):
    - POST /hotels/rates 하나로 "지역 검색 + 가격 조회"를 동시에 할 수 있음
      (cityName+countryCode 또는 coordinates 또는 hotelIds로 지역 지정 가능)
    - limit 파라미터는 "가격을 조회할 호텔 개수"이지 "가격이 있는 호텔 개수"를
      보장하지 않음. 기본 200, 최대 5000까지 확장 가능.
    - 응답의 hotel 항목 중 roomTypes가 비어있으면 = 해당 호텔은 가격 없음
      (재고 없음/마감 등) -> 후보에서 제외해야 함
"""

from dataclasses import dataclass
from typing import List, Optional

# 공용 클라이언트로 이동함 (clients/liteapi_client.py) - 리팩터링
from agents.accommodation.clients.liteapi_client import (
    LiteAPIClient,
    LiteAPIRequestError,
    DEFAULT_TIMEOUT_SECONDS,
)

DEFAULT_OVERQUERY_MULTIPLIER = 3  # 목표 수량의 3배로 넉넉하게 조회
MAX_HOTEL_LIMIT = 200  # 기본 상한 (필요시 최대 5000까지 늘릴 수 있음)


class NoHotelCandidatesError(Exception):
    """
    필터링 후 후보가 0건인 경우.
    상위 레이어(라우팅)에서 이 예외를 잡아 "숙소 후보 없음" 응답/대체 흐름으로 연결한다.
    """

    def __init__(self, city_name: str, country_code: str, searched_count: int):
        self.city_name = city_name
        self.country_code = country_code
        self.searched_count = searched_count
        super().__init__(
            f"'{city_name}({country_code})'에서 {searched_count}개 호텔을 조회했으나 "
            f"가격이 있는 후보가 하나도 없습니다."
        )


@dataclass
class HotelCandidate:
    hotel_id: str
    min_price_amount: float
    currency: str
    offer_id: str
    raw_offer: dict  # 다음 단계(후보 평가)에서 필요한 원본 데이터 보존


def _extract_min_price(hotel_entry: dict) -> Optional[HotelCandidate]:
    """
    LiteAPI 응답의 개별 호텔 항목에서 최저가 offer를 뽑아 HotelCandidate로 변환.
    roomTypes가 없거나 비어있으면 None 반환 (= 가격 없는 호텔, 후보 제외 대상).
    """
    hotel_id = hotel_entry.get("hotelId")
    room_types = hotel_entry.get("roomTypes") or []

    if not room_types:
        return None

    best = None
    for room_type in room_types:
        offer_id = room_type.get("offerId")
        # offerRetailRate: {"amount": ..., "currency": ...} - offer 전체 합산 금액
        # (LiteAPI 공식 문서 "Hotel Rates API JSON Data Structure" 기준 필드명)
        offer_total = room_type.get("offerRetailRate")
        if not isinstance(offer_total, dict):
            continue

        amount = offer_total.get("amount")
        currency = offer_total.get("currency")
        if amount is None:
            continue

        if best is None or amount < best.min_price_amount:
            best = HotelCandidate(
                hotel_id=hotel_id,
                min_price_amount=amount,
                currency=currency,
                offer_id=offer_id,
                raw_offer=room_type,
            )

    return best


def search_hotel_candidates(
    client: LiteAPIClient,
    city_name: str,
    country_code: str,
    checkin: str,
    checkout: str,
    occupancies: List[dict],
    currency: str = "KRW",
    guest_nationality: str = "KR",
    target_count: int = 20,
    overquery_multiplier: int = DEFAULT_OVERQUERY_MULTIPLIER,
    max_hotel_limit: int = MAX_HOTEL_LIMIT,
) -> List[HotelCandidate]:
    """
    도시 기준으로 호텔 후보를 조회한다.

    Args:
        client: LiteAPIClient 인스턴스
        city_name, country_code: 검색 지역
        checkin, checkout: "YYYY-MM-DD"
        occupancies: room_allocator.to_liteapi_occupancies() 결과
        target_count: 최종적으로 원하는 후보 수
        overquery_multiplier: 가격 없는 호텔 제외를 감안해 몇 배로 과조회할지
        max_hotel_limit: LiteAPI limit 파라미터 상한

    Returns:
        가격 오름차순 정렬된 HotelCandidate 리스트 (최대 target_count개)

    Raises:
        NoHotelCandidatesError: 필터링 후 후보가 0건인 경우
        LiteAPIRequestError: API 호출 자체가 실패한 경우
    """
    search_limit = min(target_count * overquery_multiplier, max_hotel_limit)

    payload = {
        "cityName": city_name,
        "countryCode": country_code,
        "checkin": checkin,
        "checkout": checkout,
        "occupancies": occupancies,
        "currency": currency,
        "guestNationality": guest_nationality,
        "limit": search_limit,
        "timeout": DEFAULT_TIMEOUT_SECONDS,
    }

    response = client.get_rates(payload)
    hotel_entries = response.get("data") or []

    candidates: List[HotelCandidate] = []
    for entry in hotel_entries:
        candidate = _extract_min_price(entry)
        if candidate is not None:
            candidates.append(candidate)

    if not candidates:
        raise NoHotelCandidatesError(
            city_name=city_name,
            country_code=country_code,
            searched_count=len(hotel_entries),
        )

    candidates.sort(key=lambda c: c.min_price_amount)
    return candidates[:target_count]