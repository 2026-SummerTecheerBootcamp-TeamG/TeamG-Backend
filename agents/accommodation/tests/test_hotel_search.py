import sys
import os
# tests/ -> accommodation/ -> agents/ -> (프로젝트 루트, manage.py가 있는 곳)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from agents.accommodation.clients.liteapi_client import LiteAPIClient
from agents.accommodation.services.hotel_search import (
    search_hotel_candidates,
    NoHotelCandidatesError,
)


def make_hotel(hotel_id, price_amounts):
    """price_amounts: 이 호텔의 offer별 가격 리스트. 빈 리스트면 '가격 없는 호텔'."""
    room_types = []
    for i, amount in enumerate(price_amounts):
        room_types.append({
            "offerId": f"{hotel_id}-offer{i}",
            "offerRetailRate": {"amount": amount, "currency": "KRW"},
        })
    return {"hotelId": hotel_id, "roomTypes": room_types}


class FakeLiteAPIClient(LiteAPIClient):
    """실제 네트워크 호출 없이 미리 정해둔 응답을 리턴하는 목 클라이언트"""

    def __init__(self, fake_response):
        self.fake_response = fake_response
        self.last_payload = None

    def get_rates(self, payload):
        self.last_payload = payload
        return self.fake_response


# 케이스 1: 정상 - 5개 호텔 중 2개는 가격 없음 -> 3개만 후보로, 가격 오름차순 정렬
fake_response_1 = {
    "data": [
        make_hotel("h1", [150000, 120000]),   # 최저가 120000
        make_hotel("h2", []),                  # 가격 없음 -> 제외
        make_hotel("h3", [90000]),
        make_hotel("h4", []),                  # 가격 없음 -> 제외
        make_hotel("h5", [200000]),
    ]
}
client1 = FakeLiteAPIClient(fake_response_1)
result1 = search_hotel_candidates(
    client1, city_name="Seoul", country_code="KR",
    checkin="2026-08-01", checkout="2026-08-03",
    occupancies=[{"adults": 2}], target_count=10,
)
print("[케이스1: 정상 필터링+정렬]")
for c in result1:
    print(f"  {c.hotel_id}: {c.min_price_amount}{c.currency}")
assert [c.hotel_id for c in result1] == ["h3", "h1", "h5"], "정렬/필터링 결과 불일치"
assert client1.last_payload["limit"] == min(10 * 3, 200), "과조회 limit 계산 오류"
print("  -> 통과 (가격없는 h2,h4 제외, h3<h1<h5 순 정렬 확인)")

# 케이스 2: target_count보다 후보가 많으면 target_count만큼만 반환
fake_response_2 = {
    "data": [make_hotel(f"h{i}", [100000 + i]) for i in range(10)]
}
client2 = FakeLiteAPIClient(fake_response_2)
result2 = search_hotel_candidates(
    client2, city_name="Busan", country_code="KR",
    checkin="2026-09-01", checkout="2026-09-02",
    occupancies=[{"adults": 1}], target_count=3,
)
print("\n[케이스2: target_count 제한]")
print(f"  요청 target_count=3, 실제 반환 개수={len(result2)}")
assert len(result2) == 3
print("  -> 통과")

# 케이스 3: 전부 가격 없음 -> NoHotelCandidatesError (후보 부재 라우팅 트리거)
fake_response_3 = {
    "data": [make_hotel("h1", []), make_hotel("h2", [])]
}
client3 = FakeLiteAPIClient(fake_response_3)
try:
    search_hotel_candidates(
        client3, city_name="Ulsan", country_code="KR",
        checkin="2026-08-01", checkout="2026-08-02",
        occupancies=[{"adults": 2}], target_count=5,
    )
    raise AssertionError("예외가 발생했어야 함")
except NoHotelCandidatesError as e:
    print("\n[케이스3: 후보 부재]")
    print(f"  예외 정상 발생: {e}")
    print("  -> 통과")

print("\n모든 테스트 통과")