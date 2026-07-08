'''
budget_demo.py - 예산 에이전트 동작 확인용 데모
실행: backend 폴더 위치에서 python -m agents.budget_demo
'''

import json
import logging

from budget import allocate_budget

logging.basicConfig(level=logging.INFO, format="%(message)s")   # 로그를 화면에 보이게

# 가짜 후보 데이터 - PoC 후쿠오카 데모와 같은 구조
FLIGHTS = [
    {"label": "Duffel Airways 자정 출발", "krw": 139_000, "utility": 60.0,
     "raw": {"expires_at": "2026-08-01T12:00:00Z"}},
    {"label": "Hahn Air 낮 출발 직항", "krw": 210_000, "utility": 96.0, "raw": {}},
]
HOTELS = [
    {"label": "비즈니스 호텔 2성", "krw": 280_000, "utility": 66.0, "raw": {}},
    {"label": "Nishitetsu 4성", "krw": 410_000, "utility": 82.0, "raw": {}},
    {"label": "ANA 크라운플라자 5성", "krw": 640_000, "utility": 90.0, "raw": {}},
]

print("\n케이스 1) 80만원  3박4일  1명 -> fit + 업그레이드 기대")
r1 = allocate_budget(800_000, FLIGHTS, HOTELS, days=4, travelers=1)
print(json.dumps(r1, ensure_ascii=False, indent=2, default=str))
assert r1["status"] == "fit"
assert r1["total_cost"] <= r1["spendable"]      # 예산 초과 금지
assert set(r1["breakdown"]) == {"flight_krw", "hotel_krw", "activity_krw"}  # 키 이름 오타 방지

print("\n케이스 2) 30만원 -> insufficient(부족액 명시) 기대")
r2 = allocate_budget(300_000, FLIGHTS, HOTELS, days=4, travelers=1)
print("상태:", r2["status"], "/ 부족액:", f"{r2['shortfall']:,}원")
assert r2["status"] == "insufficient"

print("\n케이스 3) 항공 후보 0건 -> no_flights 기대")
r3 = allocate_budget(800_000, [], HOTELS, days=4, travelers=1)
print("상태:", r3["status"], "/", r3["message"])
assert r3["status"] == "no_flights"

print("\n케이스 3개 모두 통과")