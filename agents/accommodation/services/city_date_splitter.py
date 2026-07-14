"""
=====================================================================
도시 기반 검색 조건 (p1)
=====================================================================

주기능: 도시별 체크인/아웃 분할
상세기능: 여러 곳 방문 시 박 수 -> 날짜 경계 계산

[이 파일이 하는 일]
    "오사카 2박, 교토 1박"처럼 도시별 숙박 일수만 입력받으면,
    여행 시작일 하나를 기준으로 각 도시의 실제 체크인/체크아웃 날짜를
    순서대로 계산해줌.

    예시: 여행 시작일 2026-08-01, [오사카 2박, 교토 1박]로 입력하면
        -> 오사카: 체크인 2026-08-01, 체크아웃 2026-08-03
        -> 교토:   체크인 2026-08-03, 체크아웃 2026-08-04
    로 계산됨. (오사카 체크아웃 날짜 = 교토 체크인 날짜. 같은 날 이동한다고 가정함)

[왜 필요한가]
    hotel_search.py의 search_hotel_candidates()는 도시 하나당
    checkin/checkout 날짜를 요구함. 그런데 사용자는 보통
    "오사카 2박, 교토 1박"처럼 도시별 박 수로 말하지, 도시마다 정확한
    날짜를 직접 말하지는 않음. 이 변환 작업을 자동화하기 위해 만듦.
=====================================================================
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import List


class CityDateSplitError(ValueError):
    """
    도시별 박 수 정보가 잘못됐을 때 발생시키는 예외임.
    예: 방문할 도시가 하나도 없음, 숙박 일수가 0박 이하로 들어옴 등.

    ValueError를 상속받은 이유는, "입력값 자체가 잘못됐다"는 의미를
    명확히 하기 위함임. 이렇게 해두면 이 함수를 호출하는 쪽(예: MCP tool,
    Django view)에서 `except CityDateSplitError`로 구체적으로 잡아서
    "사용자에게 잘못된 입력이라고 안내" 같은 처리를 할 수 있게 됨.
    """


@dataclass
class CityStay:
    """
    도시 하나에 대한 체크인/체크아웃 날짜 계산 결과를 담는 자료구조임.

    dataclass를 쓰면 __init__을 직접 안 짜도 되고, city/checkin/checkout
    세 값을 하나로 묶어서 다루기 편해짐 (딕셔너리로 하면 오타로 인한
    키 이름 실수가 생길 수 있는데, dataclass는 속성 이름이 고정돼 있어서
    그런 실수를 줄여줌).
    """
    city: str
    checkin: date
    checkout: date

    def to_dict(self) -> dict:
        """
        hotel_search.search_hotel_candidates()에 바로 넘길 수 있는
        문자열 포맷("YYYY-MM-DD")의 딕셔너리로 변환하는 메서드임.

        date 객체를 그대로 넘기지 않고 문자열로 바꾸는 이유는, LiteAPI
        요청이나 MCP tool 응답은 결국 JSON으로 직렬화돼야 하는데,
        파이썬 date 객체는 JSON으로 바로 변환이 안 되기 때문임.
        .isoformat()을 쓰면 date(2026,8,1) -> "2026-08-01"로 자동 변환됨.
        """
        return {
            "city": self.city,
            "checkin": self.checkin.isoformat(),   # 예: "2026-08-01"
            "checkout": self.checkout.isoformat(),
        }


def split_city_dates(trip_start_date: date, city_nights: List[dict]) -> List[CityStay]:
    """
    여행 시작일과 "도시별 숙박 일수" 리스트를 받아서, 각 도시의 실제
    체크인/체크아웃 날짜를 순서대로 계산해서 반환하는 메인 함수임.

    ------------------------------------------------------------
    파라미터 설명
    ------------------------------------------------------------
    trip_start_date:
        여행 전체가 시작되는 날짜임. 이 날짜가 첫 번째 도시의 체크인
        날짜로 그대로 쓰임.
    city_nights:
        방문 순서대로 나열한 리스트임. 각 원소는
        {"city": "오사카", "nights": 2} 형태이고, nights는 반드시
        1 이상이어야 함(0박은 존재할 수 없는 조건이라 에러 처리함).

    ------------------------------------------------------------
    반환값
    ------------------------------------------------------------
    CityStay 객체들의 리스트가 반환됨. 리스트 순서는 입력받은
    city_nights의 방문 순서와 동일하게 유지됨.

    ------------------------------------------------------------
    핵심 로직: "이전 도시의 체크아웃 = 다음 도시의 체크인"
    ------------------------------------------------------------
    여행자가 도시를 이동할 때, 보통 오전에 체크아웃하고 그날 안에
    다음 도시로 이동해서 체크인하는 흐름이 일반적임. 그래서 이
    함수는 도시 A의 체크아웃 날짜를 그대로 도시 B의 체크인 날짜로
    이어붙이는 방식으로 계산함.

    예시로 동작을 따라가보면:
        trip_start_date = 2026-08-01
        city_nights = [{"오사카", 2}, {"교토", 1}]

        1) 오사카 처리
           체크인 = 2026-08-01 (여행 시작일 그대로)
           체크아웃 = 체크인 + 2박 = 2026-08-03

        2) 교토 처리
           체크인 = 오사카 체크아웃 = 2026-08-03
           체크아웃 = 체크인 + 1박 = 2026-08-04

    ------------------------------------------------------------
    예외 상황
    ------------------------------------------------------------
    CityDateSplitError가 발생하는 경우:
        - city_nights가 빈 리스트로 들어온 경우 (방문할 도시가 없음)
        - 어떤 도시의 nights 값이 1 미만인 경우 (0박은 불가능한 조건임)
    """
    # 입력값이 비어있으면 계산할 것 자체가 없으므로 바로 에러 처리함
    if not city_nights:
        raise CityDateSplitError("방문할 도시 목록이 비어있습니다.")

    # 계산을 시작하기 전에, 모든 도시의 nights 값이 유효한지 미리 검증함.
    # (미리 다 검증해두지 않고 계산 도중에 발견하면, 이미 일부 계산된
    #  결과가 어중간하게 남아있는 상태가 될 수 있어서 헷갈리기 때문에
    #  루프를 두 번 도는 한이 있어도 먼저 전체를 검증하는 방식을 택함)
    for entry in city_nights:
        if entry.get("nights", 0) < 1:
            raise CityDateSplitError(
                f"'{entry.get('city')}'의 숙박 일수(nights)는 1 이상이어야 합니다."
            )

    stays: List[CityStay] = []
    # current_checkin은 "지금 처리 중인 도시의 체크인 날짜"를 계속 갱신해가는
    # 변수임. 첫 도시는 여행 시작일이 체크인이고, 그 다음부터는 직전 도시의
    # 체크아웃 날짜가 다음 도시의 체크인 날짜가 됨.
    current_checkin = trip_start_date

    for entry in city_nights:
        nights = entry["nights"]

        # 체크아웃 날짜 = 체크인 날짜 + 숙박 일수
        # 예: 8/1 체크인, 2박이면 -> 8/1, 8/2 이틀 밤을 묵고 8/3에 체크아웃하는 것임
        # timedelta(days=nights)를 더하면 날짜 계산(월이 바뀌는 경우 등)이
        # 파이썬 datetime 모듈에 의해 자동으로 정확하게 처리됨
        current_checkout = current_checkin + timedelta(days=nights)

        stays.append(CityStay(
            city=entry["city"],
            checkin=current_checkin,
            checkout=current_checkout,
        ))

        # 다음 반복(다음 도시)을 위해, 이번 도시의 체크아웃을 다음 도시의
        # 체크인으로 넘겨줌. 이게 바로 "날짜 경계를 잇는" 핵심 로직임.
        current_checkin = current_checkout

    return stays


def to_search_params(stays: List[CityStay]) -> List[dict]:
    """
    CityStay 객체 리스트를, hotel_search.py에 바로 넘길 수 있는
    딕셔너리 리스트로 한 번에 변환해주는 헬퍼 함수임.

    split_city_dates()의 결과를 곧바로 이 함수에 넣으면, 각 도시별로
    hotel_search.search_hotel_candidates()를 호출할 때 필요한
    city/checkin/checkout 파라미터가 바로 준비됨.
    """
    return [stay.to_dict() for stay in stays]