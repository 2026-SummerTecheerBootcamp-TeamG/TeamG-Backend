# =============================================================
#  google_client_demo.py — Google 클라이언트 3종 검증
#  실행: 리포 루트에서  python -m agents.google_client_demo
#  (실호출 4번 — Google 무료 크레딧 내라 부담 없음)
# =============================================================

import logging

from agents.google_client import geocode, get_travel_time, haversine_km, search_places

logging.basicConfig(level=logging.INFO, format="%(message)s")

print("=" * 60)
print("[1] Geocoding — 코펜하겐 NY 사고 회귀 테스트")
geo = geocode("Copenhagen", country_code="DK")   # 영문 + 국가 제한 (사고 예방 규칙)
print("   →", geo)
# 덴마크 코펜하겐은 북위 55.6도 / 사고 때 나온 미국 뉴욕주 Copenhagen은 43.9도.
# 국가 제한이 작동하면 절대 40도대가 나올 수 없다.
assert geo and 54 < geo["lat"] < 57, "국가 제한 실패 — 미국 Copenhagen이 잡혔을 가능성!"

print("=" * 60)
print("[2] Places — 후쿠오카 쇼핑 (좌표 제한 ON)")
fuk = geocode("Fukuoka", country_code="JP")
places = search_places("Fukuoka 쇼핑", latitude=fuk["lat"], longitude=fuk["lng"], max_results=5)
for p in places:
    print(f"   - {p['name']} (평점 {p['rating']}, 리뷰 {p['user_ratings']:,})")
assert places and places[0]["lat"], "장소 결과가 비었거나 좌표 누락"

print("=" * 60)
print("[3] Routes — 후쿠오카공항 → 캐널시티 (대중교통, 폴백 체인)")
t = get_travel_time((33.5859, 130.4507), (33.5903, 130.4113), mode="transit")
print("   →", t)
assert t and t["duration_min"] > 0, "경로 계산 실패"

print("=" * 60)
print(f"[4] 직선거리 검산: {haversine_km((33.5859, 130.4507), (33.5903, 130.4113)):.1f} km")
print("\n✅ 4가지 전부 통과 — 1/3 단계 완료")