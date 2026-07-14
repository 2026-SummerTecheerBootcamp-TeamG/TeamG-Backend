"""
=====================================================================
숙소 에이전트 MCP 서버
=====================================================================

[이 파일이 하는 일]
    기존에 만든 room_allocator.py / hotel_search.py / candidate_scorer.py를
    "MCP tool"이라는 형태로 감싸서 외부(Claude)에 노출한다.

    중요: 이 파일 안에서는 로직을 새로 짜지 않는다. 전부 기존 함수를
    그대로 호출하고, MCP가 요구하는 입출력 형태(JSON 직렬화 가능한 dict)로
    변환하는 역할만 한다. "판단"은 이 tool을 부르는 쪽(Claude)이 한다.

[MCP 서버 vs 지금까지 만든 서비스 파일들의 관계]
    services/*.py       = 실제 로직 (변경 없음, 그대로 재사용)
    mcp_server.py (이 파일) = 그 로직을 "Claude가 발견하고 호출할 수 있는
                              tool"로 등록하는 어댑터 계층

[실행 방법]
    python agents/accommodation/mcp_server.py
    (MCP는 기본적으로 stdio(표준입출력)로 통신함. 별도 포트를 여는 게
     아니라, 이 프로세스를 다른 프로세스가 실행시켜서 표준입출력으로
     대화하는 방식. 다음 단계에서 만들 "오케스트레이터"가 이 프로세스를
     실행시켜서 Claude API와 연결해줄 예정)

[환경변수]
    LITEAPI_KEY: LiteAPI API 키 (필수)
=====================================================================
"""

import os
import sys

# 이 파일 기준으로 상위 폴더(프로젝트 루트)를 import 경로에 추가
# (mcp_server.py를 단독으로 실행해도 agents.accommodation... import가 되도록)
# mcp_server.py는 agents/accommodation/ 바로 아래에 있으므로, 2단계만 올라가면
# 프로젝트 루트(manage.py가 있는 곳)에 도달함
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from mcp.server.fastmcp import FastMCP

from agents.accommodation.clients.liteapi_client import LiteAPIClient
from agents.accommodation.services.room_allocator import (
    allocate_rooms,
    to_liteapi_occupancies,
    RoomAllocationError,
)
from agents.accommodation.services.hotel_search import (
    search_hotel_candidates,
    NoHotelCandidatesError,
)
from agents.accommodation.services.candidate_scorer import (
    score_candidates,
    merge_candidates_with_static_info,
)
from agents.accommodation.services.city_date_splitter import (
    split_city_dates,
    to_search_params,
    CityDateSplitError,
)
from agents.accommodation.services.location_enricher import (
    enrich_candidates_with_location,
)
from datetime import date


# MCP 서버 인스턴스 생성. "accommodation-agent"라는 이름으로 등록됨
# (Claude 쪽에서 여러 MCP 서버를 붙일 수 있는데, 그중 이걸 구분하는 이름)
mcp = FastMCP("accommodation-agent")


def _get_liteapi_client() -> LiteAPIClient:
    """
    LiteAPIClient를 매번 새로 만드는 헬퍼.
    API 키를 환경변수에서 읽어옴 (.env에 LITEAPI_KEY로 설정돼 있어야 함).
    """
    api_key = os.environ.get("LITEAPI_KEY")
    if not api_key:
        raise RuntimeError("환경변수 LITEAPI_KEY가 설정되지 않았습니다.")
    return LiteAPIClient(api_key=api_key)


# ---------------------------------------------------------------------------
# Tool 1: 인원 -> 객실 배분
# ---------------------------------------------------------------------------

@mcp.tool()
def accommodation_allocate_rooms(adults: int, children_ages: list[int] | None = None) -> dict:
    """
    여행 인원(성인/아동)을 받아서 호텔 예약에 필요한 객실 배분안을 계산한다.
    방 하나당 성인 최대 2명, 아동 최대 2명 정책을 적용하며,
    아동은 반드시 성인이 있는 방에만 배정한다.

    호텔 검색을 시작하기 전에 항상 이 tool을 먼저 호출해서
    occupancies를 얻은 뒤, accommodation_search_hotels에 그 결과를 넘겨야 한다.

    Args:
        adults: 총 성인 수 (1 이상)
        children_ages: 아동 나이 리스트. 아동이 없으면 생략하거나 빈 리스트.

    Returns:
        성공 시: {"occupancies": [{"adults": 2, "children": [4, 7]}, ...]}
        실패 시: {"error": "에러 메시지"}
    """
    try:
        rooms = allocate_rooms(adults=adults, children_ages=children_ages or [])
        return {"occupancies": to_liteapi_occupancies(rooms)}
    except RoomAllocationError as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 2: 숙소 검색 (가격 조회)
# ---------------------------------------------------------------------------

@mcp.tool()
def accommodation_search_hotels(
    city_name: str,
    country_code: str,
    checkin: str,
    checkout: str,
    occupancies: list[dict],
    target_count: int = 20,
) -> dict:
    """
    LiteAPI로 실제 호텔 후보를 검색한다. 가격이 없는 호텔은 자동으로 제외되고,
    남은 후보는 가격 오름차순으로 정렬돼서 반환된다.

    이 tool을 부르기 전에 accommodation_allocate_rooms로 occupancies를 먼저 만들어야 한다.

    Args:
        city_name: 도시명 (예: "Fukuoka")
        country_code: 국가코드 (예: "JP")
        checkin: 체크인 날짜 "YYYY-MM-DD"
        checkout: 체크아웃 날짜 "YYYY-MM-DD"
        occupancies: accommodation_allocate_rooms의 결과 (occupancies 배열)
        target_count: 원하는 후보 개수 (기본 20)

    Returns:
        성공 시: {"candidates": [{"hotel_id": ..., "price_krw": ..., "currency": ...}, ...]}
        후보 0건: {"candidates": [], "message": "후보 부재 안내 메시지"}
        API 호출 실패: {"error": "에러 메시지"}
    """
    client = _get_liteapi_client()
    try:
        candidates = search_hotel_candidates(
            client=client,
            city_name=city_name,
            country_code=country_code,
            checkin=checkin,
            checkout=checkout,
            occupancies=occupancies,
            target_count=target_count,
        )
    except NoHotelCandidatesError as e:
        # 후보 0건은 "에러"가 아니라 "정상적으로 처리해야 할 결과"로 취급.
        # Claude가 이 메시지를 보고 사용자에게 안내하거나, 다른 조건으로
        # 재검색을 시도하는 등의 다음 판단을 할 수 있게 함.
        return {"candidates": [], "message": str(e)}
    except Exception as e:
        return {"error": f"호텔 검색 실패: {e}"}

    return {
        "candidates": [
            {
                "hotel_id": c.hotel_id,
                "price_krw": c.min_price_amount,
                "currency": c.currency,
            }
            for c in candidates
        ]
    }


# ---------------------------------------------------------------------------
# Tool 3: 후보 평가 (만족도 산정)
# ---------------------------------------------------------------------------

@mcp.tool()
def accommodation_score_candidates(
    candidates: list[dict],
    theme: str | None = None,
) -> dict:
    """
    호텔 후보들의 만족도 점수를 산정한다. 성급을 기준으로 기본 점수를 매기고,
    테마('관광' 또는 '쇼핑')가 주어지면 관련 편의시설이 있는 호텔에 가산점을 준다.

    candidates에 star_rating이 없으므로, 이 tool 내부에서 LiteAPI 정적 정보
    조회(성급/편의시설)를 자동으로 수행한 뒤 점수를 매긴다.

    accommodation_search_hotels의 결과를 그대로 이 tool의 candidates 인자로 넘기면 된다.

    Args:
        candidates: accommodation_search_hotels이 반환한 candidates 리스트
                    (각 원소: {"hotel_id": ..., "price_krw": ...})
        theme: 여행 테마. '관광' 또는 '쇼핑' 중 사용자 요청에 맞는 것.
               해당 없으면 생략.

    Returns:
        {"scored_candidates": [{"hotel_id":.., "krw":.., "utility":.., "reasons":[...]}, ...]}
        (utility 점수 내림차순 정렬됨)
    """
    if not candidates:
        return {"scored_candidates": []}

    client = _get_liteapi_client()
    hotel_ids = [c["hotel_id"] for c in candidates]
    static_info_by_id = client.get_hotel_static_info(hotel_ids)

    # hotel_search 결과(가격)와 clients 결과(성급/편의시설)를 hotel_id 기준으로 병합하기
    # 위해, merge_candidates_with_static_info가 기대하는 형태(min_price_amount 속성)로
    # 맞춰주는 작은 어댑터 객체를 만든다.
    class _CandidateAdapter:
        def __init__(self, hotel_id, price_krw):
            self.hotel_id = hotel_id
            self.min_price_amount = price_krw

    adapted = [_CandidateAdapter(c["hotel_id"], c["price_krw"]) for c in candidates]
    merged = merge_candidates_with_static_info(adapted, static_info_by_id)
    scored = score_candidates(merged, theme=theme)

    return {"scored_candidates": [s.to_dict() for s in scored]}


# ---------------------------------------------------------------------------
# Tool 4: 도시 기반 검색 조건 (여러 도시 방문 시 날짜 분할)
# ---------------------------------------------------------------------------

@mcp.tool()
def accommodation_split_city_dates(
    trip_start_date: str,
    city_nights: list[dict],
) -> dict:
    """
    여러 도시를 방문하는 여행에서, 여행 시작일과 도시별 숙박 일수를 받아
    각 도시의 실제 체크인/체크아웃 날짜를 계산한다.

    사용자가 "오사카 2박, 교토 1박"처럼 도시별 박 수로 말했을 때,
    실제 날짜가 필요한 accommodation_search_hotels를 도시마다 호출하기 전에
    이 tool을 먼저 호출해서 각 도시의 checkin/checkout을 얻어야 한다.
    (도시가 1개뿐이면 이 tool 없이 바로 accommodation_search_hotels를 써도 된다.)

    Args:
        trip_start_date: 여행 전체 시작일 "YYYY-MM-DD"
        city_nights: 방문 순서대로 [{"city": "오사카", "nights": 2}, ...]
                     nights는 1 이상

    Returns:
        성공 시: {"stays": [{"city":.., "checkin":.., "checkout":..}, ...]}
        실패 시: {"error": "에러 메시지"}
    """
    try:
        year, month, day = map(int, trip_start_date.split("-"))
        start = date(year, month, day)
        stays = split_city_dates(trip_start_date=start, city_nights=city_nights)
        return {"stays": to_search_params(stays)}
    except CityDateSplitError as e:
        return {"error": str(e)}
    except (ValueError, TypeError) as e:
        return {"error": f"trip_start_date 형식이 잘못됐습니다 (YYYY-MM-DD 필요): {e}"}


# ---------------------------------------------------------------------------
# Tool 5: 후보 품질 (위치 정보 강화 - 동선 거리)
# ---------------------------------------------------------------------------

@mcp.tool()
def accommodation_enrich_location(
    scored_candidates: list[dict],
    pois: list[dict],
) -> dict:
    """
    호텔 후보들에 "OO까지 도보 몇 분" 같은 위치 근거를 추가한다.

    accommodation_score_candidates 실행 후, 일정 에이전트가 만든 동선 상의
    관심 장소(POI) 목록이 있으면 이 tool을 호출해서 각 후보에 위치 근거를
    보강할 수 있다. 호텔 좌표 조회를 위해 LiteAPI 정적 정보를 내부적으로
    다시 조회한다.

    ⚠️ 중요: pois 파라미터는 반드시 일정 에이전트가 실제로 계산한 좌표만
    사용해야 한다. 정확한 좌표를 모른다면, 스스로 좌표를 추측/생성해서
    이 tool을 호출하지 말 것. 잘못된 좌표로 계산된 "도보 N분" 정보는
    사실이 아닌데 사실처럼 보이는 형태로 사용자에게 전달되어 위험하다.
    일정 에이전트의 좌표 데이터가 아직 없다면, 이 tool을 호출하지 않고
    accommodation_score_candidates의 결과만으로 답변하는 것이 안전하다.

    Args:
        scored_candidates: accommodation_score_candidates의 결과
                            (각 원소에 hotel_id가 있어야 함)
        pois: 일정 동선 상의 관심 장소들. 반드시 일정 에이전트가 제공한
              실제 좌표여야 함 (임의로 추측한 좌표 사용 금지)
              [{"name": "신사이바시 쇼핑거리", "latitude": 34.67, "longitude": 135.50}, ...]

    Returns:
        {"insights": [{"hotel_id":.., "nearest_poi_name":.., "distance_km":..,
                        "walking_minutes":.., "reason":..}, ...]}
    """
    if not scored_candidates:
        return {"insights": []}

    client = _get_liteapi_client()
    hotel_ids = [c["hotel_id"] for c in scored_candidates]
    static_info_by_id = client.get_hotel_static_info(hotel_ids)

    hotels_with_coords = [
        {
            "hotel_id": hid,
            "latitude": static_info_by_id[hid].latitude if hid in static_info_by_id else None,
            "longitude": static_info_by_id[hid].longitude if hid in static_info_by_id else None,
        }
        for hid in hotel_ids
    ]

    insights = enrich_candidates_with_location(hotels_with_coords, pois)
    return {"insights": [i.to_dict() for i in insights]}


if __name__ == "__main__":
    # stdio 방식으로 MCP 서버 실행 (표준입출력으로 통신)
    mcp.run()