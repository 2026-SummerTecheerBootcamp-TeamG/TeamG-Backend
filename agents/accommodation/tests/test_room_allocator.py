import sys
import os
# tests/ -> accommodation/ -> agents/ -> (프로젝트 루트, manage.py가 있는 곳)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from agents.accommodation.services.room_allocator import (
    allocate_rooms,
    to_liteapi_occupancies,
    RoomAllocationError,
)


def show(label, rooms):
    print(f"\n[{label}]")
    for i, r in enumerate(rooms, 1):
        print(f"  방{i}: 성인 {r.adults}명, 아동 {r.children}")
    print("  LiteAPI occupancies:", to_liteapi_occupancies(rooms))


# 케이스 1: 성인 2명, 아동 없음 -> 방 1개
show("성인2", allocate_rooms(adults=2))

# 케이스 2: 성인 5명, 아동 없음 -> 방 3개 (2/2/1)
show("성인5", allocate_rooms(adults=5))

# 케이스 3: 성인 2명 + 아동 2명(4,7세) -> 방 1개
show("성인2+아동2", allocate_rooms(adults=2, children_ages=[4, 7]))

# 케이스 4: 성인 4명 + 아동 5명 -> 아동 기준으로 방 3개 필요
show("성인4+아동5", allocate_rooms(adults=4, children_ages=[2, 3, 5, 7, 9]))

# 케이스 5: 성인 1명 + 아동 2명 -> 방 1개 (성인1, 아동2)
show("성인1+아동2", allocate_rooms(adults=1, children_ages=[3, 5]))

# 케이스 6: 성인 3명 + 아동 1명 -> 방 2개
show("성인3+아동1", allocate_rooms(adults=3, children_ages=[6]))

# 케이스 7: 예외 - 성인 0명, 아동만
try:
    allocate_rooms(adults=0, children_ages=[5])
except RoomAllocationError as e:
    print("\n[예외 케이스] 정상적으로 에러 발생:", e)

print("\n모든 테스트 실행 완료")