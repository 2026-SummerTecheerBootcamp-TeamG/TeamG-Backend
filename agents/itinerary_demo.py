# =============================================================
#  itinerary_demo.py — 일정 에이전트 검증 (단일 + 다중 도시)
#  실행: 리포 루트에서  python -m agents.itinerary_demo
#  (Google 실호출 ~30번 — 월 무료 크레딧 내지만 반복 실행은 자제)
# =============================================================

import logging

from agents.itinerary import build_day_plan

logging.basicConfig(level=logging.INFO, format="%(message)s")


def show(result):
    print(f"\n🗺  {result['city']} · 활동 {result['plan_days']}일")
    for day in result["day_plan"]:
        print(f"📅 {day['day']}일차 [{day['city']}] ({len(day['items'])}곳)")
        for item in day["items"]:
            line = f"   {item['visit_order']}. {item['place_name']} (평점 {item['place_detail']['rating']})"
            if item["travel_min_to_next"]:
                line += f"  → 다음까지 {item['travel_min_to_next']}분 ({item['travel_mode']})"
            print(line)


print("=" * 60)
print("케이스 1) 단일 도시 — 후쿠오카 3박 쇼핑")
r1 = build_day_plan(
    destinations=[{"city": "후쿠오카", "city_en": "Fukuoka", "country_code": "JP", "nights": 3}],
    themes=["쇼핑"], start_date="2026-08-09",
)
show(r1)
assert r1["plan_days"] == 3

print("=" * 60)
print("케이스 2) 다중 도시 — 베이징 2박 + 상하이 2박 (PoC 검증 시나리오 재현)")
r2 = build_day_plan(
    destinations=[
        {"city": "베이징", "city_en": "Beijing", "country_code": "CN", "nights": 2},
        {"city": "상하이", "city_en": "Shanghai", "country_code": "CN", "nights": 2},
    ],
    themes=["관광"], start_date="2026-08-09",
)
show(r2)

# ── 멀티시티 핵심 검증 ──
assert r2["plan_days"] == 4, "2박+2박이면 활동 4일"
day_numbers = [d["day"] for d in r2["day_plan"]]
assert day_numbers == [1, 2, 3, 4], "일차 번호가 도시를 건너 연속이어야 함"
assert [d["city"] for d in r2["day_plan"]] == ["베이징", "베이징", "상하이", "상하이"], \
    "1~2일차=베이징, 3~4일차=상하이여야 함 (nights 배분)"
for day in r2["day_plan"]:
    assert day["items"], f"{day['day']}일차가 비어 있음"
    assert day["items"][-1]["travel_min_to_next"] is None, "마지막 장소 이동시간은 None"
print("\n✅ 단일·다중 도시 모두 통과 — 2/3 단계 완료")