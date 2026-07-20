"""
항공 에이전트 - 항공권 후보를 검색해서 예산 에이전트가 쓸 형식으로 반환한다.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()  # .env에서 SERPAPI_KEY 읽기

SERPAPI_URL = "https://serpapi.com/search.json"


def build_route(origin: dict, destinations: list[dict]) -> list[dict]:
    """
    출발지/목적지로 여정(구간 목록)을 만든다. (왕복)
    """
    dest = destinations[0]
    return [
        {"from": origin["iata"], "to": dest["iata"]},   # 가는 편
        {"from": dest["iata"], "to": origin["iata"]},   # 오는 편
    ]


def score_utility(is_direct: bool, departure_hour: int, arrival_hour: int) -> float:
    # 항공권 후보의 만족도 점수. (높을수록 좋음)
    score = 0.0
    if is_direct:
        score += 20                        # 직항
    if 6 <= departure_hour <= 18:
        score += 10                        # 낮 출발
    if arrival_hour <= 18:
        score += 16                        # 이른 도착
    return score


def make_candidate(airline: str, krw: int, is_direct: bool,
                   departure_hour: int, arrival_hour: int,
                   departure_time: str | None = None, arrival_time: str | None = None,
                   duration_min: int | None = None, stops: int = 0,
                   departure_token: str | None = None) -> dict:
    # 항공권 정보를 예산 에이전트 사용할 형식으로 변환
    return {
        "label": airline,
        "krw": krw,
        "utility": score_utility(is_direct, departure_hour, arrival_hour),
        "raw": {
            "is_direct": is_direct,
            # "2026-08-09 12:30" 형식 (SerpApi 원본 그대로) - 가는 편 기준
            "departure_time": departure_time,
            "arrival_time": arrival_time,
            "duration_min": duration_min,
            "stops": stops,
            # 이 후보의 오는 편을 조회할 때 필요한 토큰 (SerpApi google_flights)
            "departure_token": departure_token,
        },
    }


def search_flights(departure_id: str, arrival_id: str,
                   outbound_date: str, return_date: str,
                   adults: int = 1, departure_token: str | None = None,
                   timeout: int = 60) -> list[dict]:
    """
    SerpApi(Google Flights)로 왕복 항공권을 검색한다.

    departure_token: 가는 편 후보에 딸려오는 토큰. 넘기면 그 가는 편에 대응하는
    "오는 편" 후보가 flights[] 자리에 대신 반환된다 (SerpApi 왕복 조회 규칙).
    """
    params = {
        "engine": "google_flights",
        "departure_id": departure_id,
        "arrival_id": arrival_id,
        "outbound_date": outbound_date,
        "return_date": return_date,
        "currency": "KRW",       # 응답을 원화로 받음
        "hl": "ko",
        "adults": adults,
        "api_key": os.environ.get("SERPAPI_KEY"),
    }
    if departure_token:
        params["departure_token"] = departure_token

    response = requests.get(SERPAPI_URL, params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    if data.get("error"):
        raise RuntimeError(f"SerpApi 오류: {data['error']}")

    # best_flights + other_flights 합치기
    return (data.get("best_flights") or []) + (data.get("other_flights") or [])


def parse_flight(raw_flight: dict) -> dict:
    """
    SerpApi 항공편 원본을 우리 후보 형식으로 변환한다.
    """
    flights = raw_flight["flights"]        # 구간 목록 (경유 있으면 여러 개)

    # 직항 여부: 구간이 1개면 직항
    is_direct = len(flights) == 1

    # 출발 시각: 첫 구간의 출발 시간 "2026-08-09 12:30" → 12
    departure_time = flights[0]["departure_airport"]["time"]
    departure_hour = int(departure_time.split(" ")[1].split(":")[0])

    # 도착 시각: 마지막 구간의 도착 시간 → 시(hour)
    arrival_time = flights[-1]["arrival_airport"]["time"]
    arrival_hour = int(arrival_time.split(" ")[1].split(":")[0])

    return make_candidate(
        airline=flights[0]["airline"],
        krw=raw_flight["price"],
        is_direct=is_direct,
        departure_hour=departure_hour,
        arrival_hour=arrival_hour,
        departure_time=departure_time,
        arrival_time=arrival_time,
        duration_min=raw_flight.get("total_duration"),
        stops=len(flights) - 1,
        departure_token=raw_flight.get("departure_token"),
    )


def get_return_leg_times(departure_token: str, departure_id: str, arrival_id: str,
                         outbound_date: str, return_date: str,
                         adults: int = 1, timeout: int = 12) -> dict | None:
    """
    선택된 가는 편(departure_token)에 대응하는 실제 오는 편 시각을 조회한다.

    SerpApi에 departure_token을 넣어 재조회하면 flights[]가 오는 편(귀국편)으로
    바뀐다 (실측 확인됨). 여러 후보 중 최저가 1건을 대표로 사용한다.
    실패/빈 결과는 None — 호출부가 기본 일정 윈도우로 조용히 폴백한다.

    timeout을 기본 검색(60초)보다 훨씬 짧게 잡는 이유: 이건 있으면 좋은 부가
    정보라 실패해도 파이프라인은 계속 진행된다. 느린 응답을 60초까지 기다리면
    그 시간이 고스란히 전체 파이프라인 지연에 더해져 프론트 폴링 타임아웃
    (120초)을 넘기게 만든 원인이었다.
    """
    results = search_flights(departure_id, arrival_id, outbound_date, return_date,
                             adults, departure_token=departure_token, timeout=timeout)
    if not results:
        return None
    cheapest = min(results, key=lambda f: f.get("price", float("inf")))
    flights = cheapest.get("flights")
    if not flights:
        return None
    return {
        "return_departure_time": flights[0]["departure_airport"]["time"],
        "return_arrival_time": flights[-1]["arrival_airport"]["time"],
    }


def get_flight_candidates(departure_id: str, arrival_id: str,
                          outbound_date: str, return_date: str,
                          adults: int = 1, top_n: int = 5) -> list[dict]:
    """
    항공권을 검색하고, 가격순으로 상위 N개 후보를 반환한다.
    검색 결과가 0건이면 빈 리스트를 반환한다.
    """
    raw_results = search_flights(departure_id, arrival_id,
                                 outbound_date, return_date, adults)

    if not raw_results:
        return []   # 항공 0건 → 예산 에이전트가 no_flights 처리

    candidates = [
        parse_flight(f) for f in raw_results
        if f.get("price")
    ]
    if not candidates:
        return []   # 전부 가격 없는 편이었던 경우도 0건으로 정상 처리
    candidates.sort(key=lambda c: c["krw"])
    return candidates[:top_n]


# 테스트용
if __name__ == "__main__":
    # 여정 테스트
    origin = {"city": "서울", "iata": "ICN"}
    destinations = [{"iata": "TYO"}]
    print(build_route(origin, destinations))

    # 점수 테스트
    print(score_utility(is_direct=True, departure_hour=10, arrival_hour=14))
    print(score_utility(is_direct=False, departure_hour=3, arrival_hour=23))

    # 후보 변환 테스트
    print(make_candidate("이스타항공", 442378, True, 10, 14))

    # 전체 파이프라인 테스트: 검색 → 변환 → 정렬 → 5개
    print("\n--- 최종 후보 (가격순 5개) ---")
    top5 = get_flight_candidates(
        departure_id="ICN",
        arrival_id="FUK",
        outbound_date="2026-08-09",
        return_date="2026-08-12",
        adults=2,
    )
    for c in top5:
        print(c)