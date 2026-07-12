import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from datetime import date
from agents.accommodation.services.city_date_splitter import (
    split_city_dates,
    to_search_params,
    CityDateSplitError,
)

# 케이스 1: 2개 도시 (오사카 2박 -> 교토 1박)
print("[케이스1: 오사카 2박 -> 교토 1박, 시작일 2026-08-01]")
stays = split_city_dates(
    trip_start_date=date(2026, 8, 1),
    city_nights=[{"city": "오사카", "nights": 2}, {"city": "교토", "nights": 1}],
)
for s in stays:
    print(f"  {s.city}: {s.checkin} ~ {s.checkout}")
assert stays[0].city == "오사카"
assert stays[0].checkin == date(2026, 8, 1)
assert stays[0].checkout == date(2026, 8, 3)
assert stays[1].city == "교토"
assert stays[1].checkin == date(2026, 8, 3)  # 오사카 체크아웃 = 교토 체크인
assert stays[1].checkout == date(2026, 8, 4)
print("  -> 통과 (도시 간 날짜 경계 정확)\n")

# 케이스 2: 도시 1개만
print("[케이스2: 도쿄 3박만]")
stays2 = split_city_dates(
    trip_start_date=date(2026, 9, 10),
    city_nights=[{"city": "도쿄", "nights": 3}],
)
print(f"  도쿄: {stays2[0].checkin} ~ {stays2[0].checkout}")
assert stays2[0].checkout == date(2026, 9, 13)
print("  -> 통과\n")

# 케이스 3: 도시 3개 연속 검증
print("[케이스3: 3개 도시 연속 - 날짜 끊김없이 이어지는지]")
stays3 = split_city_dates(
    trip_start_date=date(2026, 8, 1),
    city_nights=[
        {"city": "A", "nights": 1},
        {"city": "B", "nights": 2},
        {"city": "C", "nights": 1},
    ],
)
for s in stays3:
    print(f"  {s.city}: {s.checkin} ~ {s.checkout}")
# 이전 도시 체크아웃 == 다음 도시 체크인 이어야 함
for i in range(len(stays3) - 1):
    assert stays3[i].checkout == stays3[i + 1].checkin, "날짜 경계가 끊어짐"
print("  -> 통과 (날짜 경계 끊김없이 이어짐)\n")

# 케이스 4: to_search_params 변환 확인
print("[케이스4: hotel_search용 딕셔너리 변환]")
params = to_search_params(stays)
print(f"  변환 결과: {params}")
assert params[0] == {"city": "오사카", "checkin": "2026-08-01", "checkout": "2026-08-03"}
print("  -> 통과\n")

# 케이스 5: 예외 - 빈 도시 목록
print("[케이스5: 예외 처리]")
try:
    split_city_dates(date(2026, 8, 1), [])
    raise AssertionError("예외가 발생했어야 함")
except CityDateSplitError as e:
    print(f"  빈 목록 -> 정상 예외: {e}")

try:
    split_city_dates(date(2026, 8, 1), [{"city": "오사카", "nights": 0}])
    raise AssertionError("예외가 발생했어야 함")
except CityDateSplitError as e:
    print(f"  0박 -> 정상 예외: {e}")
print("  -> 통과\n")

print("모든 테스트 통과")