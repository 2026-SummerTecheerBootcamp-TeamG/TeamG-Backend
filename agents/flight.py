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
                   departure_hour: int, arrival_hour: int) -> dict:
    # 항공권 정보를 예산 에이전트 사용할 형식으로 변환
    return {
        "label": airline,
        "krw": krw,
        "utility": score_utility(is_direct, departure_hour, arrival_hour),
        "raw": {"is_direct": is_direct},   # 원본 참고 데이터
    }


def search_flights(departure_id: str, arrival_id: str,
                   outbound_date: str, return_date: str,
                   adults: int = 1) -> list[dict]:
    """
    SerpApi(Google Flights)로 왕복 항공권을 검색한다.

    Args:
        departure_id: 출발 공항코드 (예: "ICN")
        arrival_id: 도착 공항코드 (예: "FUK")
        outbound_date: 가는 날 "YYYY-MM-DD"
        return_date: 오는 날 "YYYY-MM-DD"
        adults: 성인 인원

    Returns:
        항공권 옵션 리스트 (SerpApi 원본 형식)
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

    response = requests.get(SERPAPI_URL, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()

    if data.get("error"):
        raise RuntimeError(f"SerpApi 오류: {data['error']}")

    # best_flights + other_flights 합치기
    return (data.get("best_flights") or []) + (data.get("other_flights") or [])


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

    # SerpApi 실제 검색 테스트
    print("\n--- SerpApi 검색 ---")
    results = search_flights(
        departure_id="ICN",
        arrival_id="FUK",
        outbound_date="2026-08-09",
        return_date="2026-08-12",
        adults=2,
    )
    print(f"검색된 항공편: {len(results)}개")
    if results:
        print("첫 번째 항공편 원본:")
        import json
        print(json.dumps(results[0], indent=2, ensure_ascii=False))