# =============================================================
#  itinerary_narrate_demo.py — 내러티브 생성 검증 (LLM 1회 = 소액 과금)
#  실행: 리포 루트에서  python -m agents.itinerary_narrate_demo
#
#  ⭐ 픽스처(고정 데이터)를 쓰는 이유: 내러티브만 검증하는데 Google API를
#     10번 태울 필요가 없다 — 2/3 데모의 실출력에서 따온 형태의 가짜 데이터로
#     LLM 부분만 격리 테스트. (팀원의 "픽스처 우선 개발"과 같은 발상)
# =============================================================

from agents.itinerary_narrate import narrate_day_plan

FIXTURE_DAY_PLAN = [
    {"day": 1, "city": "후쿠오카", "items": [
        {"visit_order": 1, "place_name": "캐널시티 하카타",
         "place_detail": {"rating": 4.2, "user_ratings": 54371},
         "travel_min_to_next": 17, "travel_mode": "transit"},
        {"visit_order": 2, "place_name": "텐진 지하상가",
         "place_detail": {"rating": 4.1, "user_ratings": 12000},
         "travel_min_to_next": 7, "travel_mode": "walking"},
        {"visit_order": 3, "place_name": "모토무라 규카츠",
         "place_detail": {"rating": 4.4, "user_ratings": 8300},
         "travel_min_to_next": None, "travel_mode": None},
    ]},
    {"day": 2, "city": "후쿠오카", "items": [
        {"visit_order": 1, "place_name": "오호리 공원",
         "place_detail": {"rating": 4.5, "user_ratings": 31000},
         "travel_min_to_next": 13, "travel_mode": "driving"},   # ← "택시" 표현 검증용
        {"visit_order": 2, "place_name": "후쿠오카 타워",
         "place_detail": {"rating": 4.2, "user_ratings": 19000},
         "travel_min_to_next": None, "travel_mode": None},
    ]},
]

print("=" * 60)
narrative = narrate_day_plan("후쿠오카", ["쇼핑"], FIXTURE_DAY_PLAN)
print(narrative)
print("=" * 60)
print("👀 눈으로 확인: ① 2일차에 '택시'라는 단어가 나오는지 (driving→택시 규칙)")
print("               ② 규카츠집이 식사 시간대에 배치됐는지  ③ 귀국일 안내 한 줄")