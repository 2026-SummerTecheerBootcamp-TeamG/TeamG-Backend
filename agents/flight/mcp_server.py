"""
항공 에이전트 MCP 서버

flight.py의 함수들을 MCP tool로 감싸서 Claude에 노출한다.
로직은 새로 짜지 않고, 기존 함수를 그대로 호출한다.

실행: python agents/flight/mcp_server.py
환경변수: SERPAPI_KEY 필요
"""

import os
import sys

# 프로젝트 루트를 import 경로에 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from mcp.server.fastmcp import FastMCP
from agents.flight.flight import (
    build_route,
    search_flights,
    parse_flight,
    get_flight_candidates,
)

mcp = FastMCP("flight-agent")


@mcp.tool()
def flight_build_route(origin: dict, destinations: list[dict]) -> dict:
    """
    출발지와 목적지로 항공 여정(구간 목록)을 구성한다. (현재 왕복)

    Args:
        origin: 출발지 {"city": "서울", "iata": "ICN"}
        destinations: 목적지 리스트 [{"iata": "FUK"}]

    Returns:
        {"route": [{"from": "ICN", "to": "FUK"}, {"from": "FUK", "to": "ICN"}]}
    """
    try:
        return {"route": build_route(origin, destinations)}
    except Exception as e:
        return {"error": f"여정 구성 실패: {e}"}


@mcp.tool()
def flight_search_candidates(
    departure_id: str,
    arrival_id: str,
    outbound_date: str,
    return_date: str,
    adults: int = 1,
    top_n: int = 5,
) -> dict:
    """
    SerpApi(Google Flights)로 항공권을 검색하고, 가격순 상위 N개 후보를 반환한다.
    각 후보에는 만족도 점수(utility)가 함께 계산되어 있다.

    Args:
        departure_id: 출발 공항코드 (예: "ICN")
        arrival_id: 도착 공항코드 (예: "FUK")
        outbound_date: 가는 날 "YYYY-MM-DD"
        return_date: 오는 날 "YYYY-MM-DD"
        adults: 성인 인원 (기본 1)
        top_n: 반환할 후보 개수 (기본 5)

    Returns:
        성공: {"candidates": [{"label":.., "krw":.., "utility":.., "raw":..}, ...]}
        0건: {"candidates": [], "message": "항공 후보가 0건입니다."}
        실패: {"error": "에러 메시지"}
    """
    try:
        candidates = get_flight_candidates(
            departure_id=departure_id,
            arrival_id=arrival_id,
            outbound_date=outbound_date,
            return_date=return_date,
            adults=adults,
            top_n=top_n,
        )
    except Exception as e:
        return {"error": f"항공권 검색 실패: {e}"}

    if not candidates:
        return {
            "candidates": [],
            "message": "항공 후보가 0건입니다. 날짜를 바꾸거나 인근 공항을 시도해 보세요.",
        }

    return {"candidates": candidates}


if __name__ == "__main__":
    mcp.run()