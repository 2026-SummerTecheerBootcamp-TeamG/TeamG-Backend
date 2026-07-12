import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from agents.accommodation.services.location_enricher import (
    haversine_km,
    estimate_walking_minutes,
    enrich_hotel_with_location,
    enrich_candidates_with_location,
)

# -----------------------------------------------------------------
# 케이스 1: 거리 계산 정확성 (오사카 신사이바시 근처, 위도 0.0036도 차이 = 약 400m)
# -----------------------------------------------------------------
print("[케이스1: haversine 거리 계산]")
shinsaibashi = (34.6746, 135.5010)
hotel_near = (34.6746 + 0.0036, 135.5010)  # 위도로 약 400m 북쪽

distance = haversine_km(*hotel_near, *shinsaibashi)
print(f"  계산된 거리: {distance:.3f}km (예상: 약 0.4km)")
assert 0.35 < distance < 0.45, "거리 계산이 예상 범위를 벗어남"
print("  -> 통과\n")

# -----------------------------------------------------------------
# 케이스 2: 도보 시간 환산
# -----------------------------------------------------------------
print("[케이스2: 도보 시간 환산]")
minutes = estimate_walking_minutes(0.4)  # 0.4km, 시속 4.8km 기준
print(f"  0.4km -> {minutes}분 (예상: 5분)")
assert minutes == 5
print("  -> 통과\n")

# -----------------------------------------------------------------
# 케이스 3: 정상 케이스 - 가까운 호텔의 근거 문구 생성
# -----------------------------------------------------------------
print("[케이스3: 근거 문구 생성 - 실사용 시나리오]")
pois = [
    {"name": "신사이바시 쇼핑거리", "latitude": 34.6746, "longitude": 135.5010},
    {"name": "오사카성", "latitude": 34.6873, "longitude": 135.5262},  # 훨씬 멀리
]
insight = enrich_hotel_with_location(
    hotel_id="h1",
    hotel_lat=34.6746 + 0.0036,
    hotel_lon=135.5010,
    pois=pois,
)
print(f"  결과: {insight.to_dict()}")
assert insight.nearest_poi_name == "신사이바시 쇼핑거리"  # 오사카성보다 훨씬 가까움
assert insight.reason == "신사이바시 쇼핑거리 도보 6분"  # 실거리 0.4003km -> 정확히는 6분
print("  -> 통과 (여러 POI 중 가장 가까운 것 선택 확인)\n")

# -----------------------------------------------------------------
# 케이스 4: 방어 코드 - 좌표 없음 / POI 없음
# -----------------------------------------------------------------
print("[케이스4: 방어 코드]")
no_coord = enrich_hotel_with_location("h2", None, None, pois)
print(f"  좌표 없음: {no_coord.reason}")
assert no_coord.nearest_poi_name is None

no_poi = enrich_hotel_with_location("h3", 34.67, 135.50, [])
print(f"  POI 없음: {no_poi.reason}")
assert no_poi.nearest_poi_name is None
print("  -> 통과 (에러 없이 안전하게 처리됨)\n")

# -----------------------------------------------------------------
# 케이스 5: 여러 호텔 일괄 처리
# -----------------------------------------------------------------
print("[케이스5: 여러 호텔 일괄 처리]")
hotels = [
    {"hotel_id": "h1", "latitude": 34.6746 + 0.0036, "longitude": 135.5010},
    {"hotel_id": "h2", "latitude": None, "longitude": None},
]
results = enrich_candidates_with_location(hotels, pois)
print(f"  h1: {results[0].reason}")
print(f"  h2: {results[1].reason}")
assert results[0].hotel_id == "h1" and results[0].nearest_poi_name is not None
assert results[1].hotel_id == "h2" and results[1].nearest_poi_name is None
print("  -> 통과 (순서 유지 + 개별 처리 확인)\n")

print("모든 테스트 통과")