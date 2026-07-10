"""
항공 에이전트 → 예산 에이전트 연결 테스트 (픽스처)
실제 SerpApi 호출 없이, 가짜 항공 후보로 예산 배분이 도는지 확인한다.
"""

from agents.flight import make_candidate
from agents.budget import allocate_budget


# 가짜 항공 후보 5개 (가격·직항여부·시간 다양하게)
flight_options = [
    make_candidate("이스타항공", 442378, is_direct=True,  departure_hour=10, arrival_hour=14),
    make_candidate("에어서울",   449400, is_direct=True,  departure_hour=9,  arrival_hour=13),
    make_candidate("제주항공",   480000, is_direct=True,  departure_hour=7,  arrival_hour=11),
    make_candidate("경유편A",    390000, is_direct=False, departure_hour=6,  arrival_hour=20),
    make_candidate("경유편B",    370000, is_direct=False, departure_hour=23, arrival_hour=9),
]

# 가짜 숙소 후보 (예산 에이전트가 항공+숙소 둘 다 필요해서)
hotel_options = [
    {"label": "호텔A 3성", "krw": 338068, "utility": 80.0, "raw": {}},
    {"label": "호텔B 4성", "krw": 440103, "utility": 90.0, "raw": {}},
]


if __name__ == "__main__":
    result = allocate_budget(
        total_budget=2_000_000,
        flight_options=flight_options,
        hotel_options=hotel_options,
        days=4,
        travelers=2,
    )

    print("상태:", result["status"])
    print("선택된 항공:", result["selection"]["flight"]["label"])
    print("총 비용:", f"{result['total_cost']:,}원")
    print("여유:", f"{result['surplus']:,}원")