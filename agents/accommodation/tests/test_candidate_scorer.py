import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from agents.accommodation.services.candidate_scorer import (
    score_candidate,
    score_candidates,
    merge_candidates_with_static_info,
)
from agents.accommodation.clients.liteapi_client import HotelStaticInfo


# -----------------------------------------------------------------
# 케이스 1: 후쿠오카 실데이터 패턴 재현 (이미지로 받은 실제 값과 일치하는지)
# -----------------------------------------------------------------
print("[케이스1: 후쿠오카 실데이터 패턴 재현]")

real_data = [
    ("Hotel Torifito Hakata Gion", 4, 440103, 90.0),
    ("Hotel Monte Hermana Fukuoka", 4, 463527, 90.0),
    ("lyf Tenjin Fukuoka", 3, 338068, 80.0),
    ("THE LIVELY FUKUOKA HAKATA", 3, 386092, 80.0),
    ("Comfort Hotel Hakata", 3, 438459, 80.0),
]

for name, star, price, expected_score in real_data:
    result = score_candidate(
        hotel_id=name, price_krw=price, star_rating=star, theme=None, facilities=[]
    )
    status = "일치" if result.utility_score == expected_score else "불일치!!"
    print(f"  {name} ({star}성): 계산값={result.utility_score} / 실데이터={expected_score} -> {status}")
    assert result.utility_score == expected_score, f"{name} 점수 불일치"

print("  -> 전부 통과, 실데이터 패턴 정확히 재현됨\n")


# -----------------------------------------------------------------
# 케이스 2: 테마 가중치 적용
# -----------------------------------------------------------------
print("[케이스2: 테마 가중치]")

no_theme = score_candidate(
    hotel_id="h1", price_krw=400000, star_rating=3,
    theme=None, facilities=["Shopping mall", "Free WiFi"],
)
with_theme = score_candidate(
    hotel_id="h1", price_krw=400000, star_rating=3,
    theme="쇼핑", facilities=["Shopping mall", "Free WiFi"],
)
print(f"  테마 미지정: {no_theme.utility_score}점 (근거: {no_theme.reasons})")
print(f"  '쇼핑' 테마 지정: {with_theme.utility_score}점 (근거: {with_theme.reasons})")
assert with_theme.utility_score > no_theme.utility_score, "테마 가중치가 반영되지 않음"
print("  -> 통과 (테마 지정 시 점수가 더 높음)\n")


# -----------------------------------------------------------------
# 케이스 3: 성급 정보 없는 경우 기본값 처리
# -----------------------------------------------------------------
print("[케이스3: 성급 정보 없음]")
no_star = score_candidate(hotel_id="h2", price_krw=300000, star_rating=None)
print(f"  성급 없음 -> {no_star.utility_score}점 (근거: {no_star.reasons})")
assert no_star.utility_score == 70.0
print("  -> 통과 (기본값 70점 적용)\n")


# -----------------------------------------------------------------
# 케이스 4: score_candidates - 여러 후보 일괄 평가 + 내림차순 정렬
# -----------------------------------------------------------------
print("[케이스4: 일괄 평가 + 정렬]")
raw_candidates = [
    {"hotel_id": "h_3star", "price_krw": 300000, "star_rating": 3, "facilities": []},
    {"hotel_id": "h_5star", "price_krw": 900000, "star_rating": 5, "facilities": []},
    {"hotel_id": "h_4star", "price_krw": 500000, "star_rating": 4, "facilities": []},
]
scored = score_candidates(raw_candidates, theme=None)
order = [s.hotel_id for s in scored]
print(f"  정렬 결과: {order}")
assert order == ["h_5star", "h_4star", "h_3star"], "만족도 내림차순 정렬 실패"
print("  -> 통과\n")


# -----------------------------------------------------------------
# 케이스 5: merge_candidates_with_static_info - 가격정보+정적정보 병합
# -----------------------------------------------------------------
print("[케이스5: 가격정보 + 정적정보 병합]")

class FakeHotelCandidate:
    """hotel_search.HotelCandidate를 흉내낸 가짜 객체 (순환 import 없이 테스트하기 위함)"""
    def __init__(self, hotel_id, min_price_amount):
        self.hotel_id = hotel_id
        self.min_price_amount = min_price_amount

fake_candidates = [
    FakeHotelCandidate("h1", 400000),
    FakeHotelCandidate("h2", 350000),  # 정적정보 조회에서 누락된 케이스
]
static_info = {
    "h1": HotelStaticInfo(hotel_id="h1", name="Hotel A", star_rating=4, facilities=["WiFi"]),
    # h2는 일부러 안 넣음 -> 방어 코드(None 처리) 확인용
}
merged = merge_candidates_with_static_info(fake_candidates, static_info)
print(f"  병합 결과: {merged}")
assert merged[0]["star_rating"] == 4
assert merged[1]["star_rating"] is None  # 정적정보 없는 호텔은 None으로 안전 처리
print("  -> 통과 (정적정보 누락 호텔도 에러 없이 처리됨)\n")

print("모든 테스트 통과")