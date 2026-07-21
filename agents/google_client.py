"""
google_client.py - Google 지도 계열 API 3종 클라이언트

1) geocode          : 지명 -> 좌표 (Geocoding API)
2) search_places    : 테마 장소/맛집 검색 (Places API (New))
3) get_travel_time  : 두 지점 이동 시간 (Routes API v2)
+) haversine_km     : 직선거리 (API 없음)
"""

import json
import logging
import os
from math import atan2, cos, radians, sin, sqrt

import requests
from dotenv import load_dotenv

load_dotenv()   # 리포 루트 .env의 GOOGLE_MAPS_API_KEY를 환경변수로

logger = logging.getLogger(__name__)

# 모듈 공용 세션 — 매 호출마다 새 TCP+TLS 연결을 맺는 대신 연결을 재사용한다.
# 일정 생성 한 번에 Routes/Places 호출이 20회를 넘는데(장기 여행은 30회+),
# 호출당 TLS 핸드셰이크(~100ms대)가 전부 절약된다. 요청 내용/응답은 완전히 동일.
# 날짜 단위 병렬 호출(스레드 최대 6)이 있어 풀 크기를 여유 있게 잡는다
# (풀이 작으면 "connection pool is full" 경고와 함께 연결을 버렸다 다시 만듦)
_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=10, pool_maxsize=10))


def _require_key() -> str:
    """키가 없을 경우 401보다 원인 추적이 쉬움"""
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_MAPS_API_KEY가 없습니다. .env를 확인하세요")
    return key


def geocode(address: str, country_code: str | None = None) -> dict | None:
    """지명 -> 좌표, 실패하면 None
    
    규칙
    1. address는 반드시 '영문 지명'으로 넘길 것 (파서의 city_en)
    2. country_code(ISO 2자리, 파서의 country_code)를 주면 components 파라미터로 국가를 제한함
    """
    
    params = {"address": address, "key": _require_key(), "language": "ko"}
    if country_code:
        # (오타 수정: componenets -> components. 오타 동안 국가 제한이 조용히 무시되고 있었음
        #  = '코펜하겐 NY' 사고 방지 장치가 실제로는 꺼져 있던 상태)
        params["components"] = f"country:{country_code.upper()}"

    response = _session.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params=params, timeout=15,
    )
    data = response.json()

    if data.get("status") != "OK":
        logger.warning("Geocoding 실패 '%s': %s", address, data.get("status"))
        return None
    
    top = data["results"][0]
    loc = top["geometry"]["location"]
    logger.info("Geocoding '%s' -> (%.2f, %.2f)", address, loc["lat"], loc["lng"])
    return {"lat": loc["lat"], "lng": loc["lng"], "formatted": top["formatted_address"]}


def search_places(
    text_query: str,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_m: int = 20000,
    max_results: int = 8,
) -> list[dict]:
    """장소 텍스트 검색 (Places API New)
    
    locationBias: 좌표를 주면 그 주변으로 검색을 몰아줌
    """
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _require_key(),
        # FieldMask(필수 헤더)
        "X-Goog-FieldMask": (
            "places.displayName,places.formattedAddress,places.location,"
            "places.rating,places.userRatingCount,places.types"
        ),
    }
    body = {"textQuery": text_query, "languageCode": "ko", "maxResultCount": max_results}
    if latitude is not None and longitude is not None:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": float(min(radius_m, 50000)),
            }
        }
    response = _session.post(
        "https://places.googleapis.com/v1/places:searchText",
        headers=headers, data=json.dumps(body), timeout=20,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Places 검색 실패: HTTP {response.status_code}\n{response.text[:500]}")
    
    places = response.json().get("places", []) or []
    logger.info("Places '%s' -> %d곳%s", text_query, len(places),
                " (좌표제한 ON)" if latitude is not None else "")
    
    return [
        {
            "name": (p.get("displayName", {}) or {}).get("text"),
            "address": p.get("formattedAddress"),
            "lat": (p.get("location", {}) or {}).get("latitude"),
            "lng": (p.get("location", {}) or {}).get("longitude"),
            "rating": p.get("rating"),
            "user_ratings": p.get("userRatingCount"),
            "types": p.get("types", []),
        }
        for p in places
    ]


def _routes_once(origin, destination, mode: str, departure_time_iso: str | None) -> dict | None:
    """Routes API v2를 한 번 호출, 실패/경로 없음이면 None
    """
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _require_key(),
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }
    travel_mode = {"transit": "TRANSIT", "driving": "DRIVE", "walking": "WALK"}[mode]
    body = {
        "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
        "destination": {"location": {"latLng": {"latitude": destination[0], "longitude": destination[1]}}},
        "travelMode": travel_mode
    }
    # 대중교통은 시간표 기반이라 출발 시각이 있어야 함
    if travel_mode == "TRANSIT" and departure_time_iso:
        body["departureTime"] = departure_time_iso

    response = _session.post(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        headers=headers, data=json.dumps(body), timeout=15,
    )
    if response.status_code != 200:
        return None
    routes = response.json().get("routes") or []
    if not routes:
        return None     # 경로 없음 - 대중교통 데이터 없는 구간이면 정상
    
    top = routes[0]
    # duration은 '1234s'처럼 초 단위 문자열로 옴 -> s 떼고 정수로 변환
    seconds = int(str(top.get("duration", "0s")).rstrip("s") or 0)
    return {
        "mode": mode,                                   # 실제 계산에 쓰인 이동수단
        "duration_min": max(1, round(seconds / 60)),    # ERD의 travel_min_to_next와 매핑
        "distance_km": round(top.get("distanceMeters", 0) / 1000, 1),
    }


def get_travel_time(origin, destination, mode: str = "transit",
                    departure_time_iso: str | None = None) -> dict | None:
    """두 좌표 사이 이동 시간. 폴백 체인: 요청 mode -> 자동차 -> 도보
    
    폴백이 필요한 이유: 일부 도시는 Google에 대중교통 경로 데이터가 없음
    그 경우 자동차가 도보보다 현실적이라 위 순서
    """
    
    result = _routes_once(origin, destination, mode, departure_time_iso)
    if not result and mode != "driving":
        result = _routes_once(origin, destination, "driving", None)
    if not result and mode != "walking":
        result = _routes_once(origin, destination, "walking", None)
        
    if result:
        logger.info("Routes: %s . %d분 . %.1fkm", result["mode"],
                    result["duration_min"], result["distance_km"])
    else:
        logger.warning("Routes: 모든 이동수단 경로 없음")
    return result


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """두 좌표의 직선거리
    API 호출 없이 동선 정렬(NN)에 쓰는 계산
    """

    R = 6371        # 지구 반지름 (km)
    lat1, lng1 = radians(a[0]), radians(a[1])
    lat2, lng2 = radians(b[0]), radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return 2 * R * atan2(sqrt(h), sqrt(1 - h))