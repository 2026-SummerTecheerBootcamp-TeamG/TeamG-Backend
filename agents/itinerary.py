"""
itinerary.py - 일정 에이전트

테마에 맞는 관광지/맛집을 찾고, 날짜별 방문 순서를 최적화함

장소 수집(Places) -> 인기도 랭킹 -> 동선 정렬(최근접 이웃) -> 날짜 분할 -> 실이동시간(Routes) 부착
"""

import logging
import math
# 날짜별 이동시간(Routes API) 호출을 병렬로 돌리기 위한 스레드 풀
from concurrent.futures import ThreadPoolExecutor

from agents.google_client import geocode, get_travel_time, haversine_km, search_places

logger = logging.getLogger(__name__)


# 장소가 아니라 '지역' 자체인 결과를 걸러내기 위한 타입 목록
# (코펜하겐 사고: "Copenhagen 관광 명소" 검색이 도시 '코펜하겐' 1건만 반환 → 일정에 도시가 들어감)
_NON_POI_TYPES = {
    "locality", "sublocality", "country", "continent",
    "administrative_area_level_1", "administrative_area_level_2",
}


def _collect_places(queries: list[str], seen: set, center, per_query: int = 20) -> list[dict]:
    """검색어 목록으로 장소를 모음
    seen 집합으로 중복 제거

    seen을 공유하는 게 포인트
    per_query=20: Places 요금은 '요청 횟수' 기준이라 8개나 20개나 같은 값 →
                  최대치로 받아야 장기 여행(11박 등)에서 장소가 바닥나지 않음
    """

    lat, lng = (center if center else (None, None))
    collected = []
    for q in queries:
        try:
            for p in search_places(q, latitude=lat, longitude=lng, max_results=per_query):
                name = p.get("name")
                # 이름 없음 / 이미 수집됨 / 좌표 없음 -> 제외
                if not name or name in seen:
                    continue
                if p.get("lat") is None or p.get("lng") is None:
                    continue
                # 도시/행정구역 자체가 검색 결과로 오면 제외 (방문지가 될 수 없음)
                if _NON_POI_TYPES & set(p.get("types") or []):
                    continue
                seen.add(name)
                collected.append(p)
        except Exception as e:
            # 검색어 하나가 실패해도 전체 일정 생성은 계속
            logger.warning("장소 검색 '%s' 실패: %s", q, e)
    return collected


def _pick_top(places: list[dict], how_many: int) -> list[dict]:
    """평점 * log10(리뷰 수)로 상위 N개 선별
    
    평점만 쓰면 리뷰 적은 무명 가게가 1등이 될 수 있음
    리뷰 수에 log를 씌워 곱하면 가중치 낮아짐
    """

    def score(p: dict) -> float:
        return (p.get("rating") or 0) * math.log10((p.get("user_ratings") or 0) + 10)
    
    return sorted(places, key=score, reverse=True)[:how_many]


def _order_by_nearest(places: list[dict]) -> list[dict]:
    """최근접 이웃(NN) 방식 동선 정렬 - "지금 위치에서 가장 가까운 곳을 다음으로"
    
    완벽한 최적해는 아니지만 계산이 즉시 끝나고 결과가 충분히 자연스러움
    """

    if not places:
        return []
    remaining = places[:]       # 복사본
    path = [remaining.pop(0)]   # 시작점 = 랭킹 1위 장소
    while remaining:
        last = (path[-1]["lat"], path[-1]["lng"])
        nxt = min(remaining, key=lambda p: haversine_km(last, (p["lat"], p["lng"])))
        remaining.remove(nxt)
        path.append(nxt)
    return path


def _split_into_days(ordered: list[dict], num_days: int) -> list[list[dict]]:
    """동선 정렬된 목록을 앞에서부터 하루 분량씩 자름
    
    번갈아 담지 않고 연속 구간으로 자름
    최근접 정렬을 거쳤으니 리스트에서 이웃한 장소들을 연속으로 자르면 자연스럽게 가까운 곳끼리 같은 날이 됨
    """

    if num_days <= 0:
        return []
    # divmod = 몫과 나머지를 한 번에: 17곳/11일 -> base 1, extra 6
    # -> 앞의 6일은 2곳, 뒤의 5일은 1곳 (모든 날이 최대한 균등, 빈 날 최소화)
    # (예전 방식은 round()로 하루치를 정한 뒤 나머지를 전부 마지막 날에 몰아서
    #  장소가 부족하면 중간이 텅 비고 마지막 날만 4곳이 되는 문제가 있었음)
    base, extra = divmod(len(ordered), num_days)
    days, idx = [], 0
    for i in range(num_days):
        size = base + (1 if i < extra else 0)   # 나머지를 앞쪽 날들에 1곳씩 배분
        days.append(ordered[idx: idx + size])
        idx += size
    return days


def _to_items(stops: list[dict], departure_time_iso: str | None) -> list[dict]:
    """하루의 장소들을 ERD ItineraryItem 형태로 변환

    ERD와 이름을 맞춰두면 나중에 DB 저장이 단순해짐
    """

    items = []
    for i, s in enumerate(stops):
        travel = None
        if i < len(stops) - 1:      # 마지막 장소가 아니면 다음 장소까지 경로 계산
            nxt = stops[i + 1]
            travel = get_travel_time(
                (s["lat"], s["lng"]), (nxt["lat"], nxt["lng"]),
                mode="transit", departure_time_iso=departure_time_iso,
            )       # 폴백 체인은 google_client가 처리
        place_detail = {       # ERD의 place_detail JSON - 재검색 없이 표시용 저장
            "rating": s.get("rating"),
            "user_ratings": s.get("user_ratings"),
            "address": s.get("address"),
        }
        if "airport" in (s.get("types") or []):
            place_detail["category"] = "airport"   # 프론트에서 공항 항목 구분용
        items.append({
            "visit_order": i + 1,
            "place_name": s["name"],
            "lat": s["lat"],
            "lng": s["lng"],
            "place_detail": place_detail,
            "travel_min_to_next": travel["duration_min"] if travel else None,
            "travel_mode": travel["mode"] if travel else None,
        })
    return items


def _airport_stop(city_en: str, center: tuple[float, float] | None) -> dict | None:
    """도시의 대표 공항 1곳을 장소 형태로 검색

    도심 기준 반경 20km(기본값)로는 공항이 잘리는 도시가 많아(간사이/인천 등)
    반경을 최대치(50km)로 넓혀서 검색함
    """

    lat, lng = center if center else (None, None)
    for q in (f"{city_en} international airport", f"{city_en} 국제공항"):
        try:
            results = search_places(q, latitude=lat, longitude=lng,
                                    radius_m=50000, max_results=3)
        except Exception as e:
            logger.warning("공항 검색 '%s' 실패: %s", q, e)
            continue
        for r in results:
            if r.get("lat") is not None and r.get("lng") is not None:
                return r
    return None


def _build_city_days(destination: dict, themes: list[str], plan_days: int,
                   day_offset: int, departure_time_iso: str | None,
                   include_arrival_airport: bool = False,
                   include_departure_airport: bool = False) -> list[dict]:
    """도시 하나의 일정을 만듦

    day_offset: 이 도시의 일정이 전체 여행의 며칠째부터 시작하는지
    include_arrival_airport: 여행 전체의 첫 날 맨 앞에 도착 공항을 넣을지
    include_departure_airport: 여행 전체의 마지막 날 맨 뒤에 출발(귀국) 공항을 넣을지
    """
    
    city = destination.get("city")
    city_en = destination.get("city_en") or city

    # 목적지 중심 좌표
    geo = geocode(city_en, country_code=destination.get("country_code"))
    center = (geo["lat"], geo["lng"]) if geo else None

    # 수집: 맛짐을 먼저 모음
    # 순서가 중요한 이유: 테마가 맛집이면 테마 검색어와 맛집 검색어가 같아짐
    # 관광 쪽이 먼저 수집하면 seen 중복 제거에 걸려 맛집 슬롯이 비어버림
    # 검색어는 한국어 + 영어 병행
    # 이유(코펜하겐 사고): 한국인 리뷰가 적은 도시에서는 한국어 검색어가 무너짐 —
    #   "Copenhagen 관광 명소" -> 도시 이름 1건, "Copenhagen 맛집" -> 한식당만 반환됨
    #   (후쿠오카/파리는 한국어 데이터가 많아 우연히 통과했던 것)
    # 영어 검색어가 현지 명소를 채우고, languageCode=ko라 표시 이름은 그대로 한국어로 옴
    seen: set = set()
    foods = _collect_places(
        [f"{city_en} 맛집", f"best restaurants in {city_en}"], seen, center)
    attraction_queries = [f"{city_en} {t}" for t in themes]
    attraction_queries += [
        f"{city_en} 관광 명소",
        f"top tourist attractions in {city_en}",
        f"famous landmarks in {city_en}",
    ]
    attractions = _collect_places(attraction_queries, seen, center)
    logger.info("%s 장소 수집: 관광/테마 %d곳 . 맛집 %d곳", city, len(attractions), len(foods))

    # 선별: 하루 = 관광 2곳 + 맛집 1곳 기준 -> 동선 정렬 -> 날짜 분할
    top_attractions = _pick_top(attractions, plan_days * 2)
    top_foods = _pick_top(foods, plan_days)
    attr_chunks = _split_into_days(_order_by_nearest(top_attractions), plan_days)

    # 날짜별 구성 1단계: 그 날의 관광지들 + 맛집 1곳 -> 동선 재정렬 (API 호출 없음, 즉시 끝남)
    day_stops = []
    for i in range(plan_days):
        stops = list(attr_chunks[i]) if i < len(attr_chunks) else []
        if i < len(top_foods):
            stops.append(top_foods[i])
        day_stops.append(_order_by_nearest(stops))  # 맛집이 끼어들었으니 그 날 동선을 재정렬

    # 공항 부착: 동선 정렬 이후에 붙여야 함 (NN 정렬에 섞이면 공항이 하루 중간에 낄 수 있음)
    if (include_arrival_airport or include_departure_airport) and day_stops:
        airport = _airport_stop(city_en, center)
        if airport:
            if include_arrival_airport:
                day_stops[0].insert(0, airport)
            if include_departure_airport:
                day_stops[-1].append(airport)
        else:
            logger.warning("%s 공항을 찾지 못해 공항 이동 일정을 생략합니다", city)

    # 날짜별 구성 2단계: 이동시간 부착 (_to_items 안의 Routes API 호출 = 일정 생성 최대 병목)
    # 11박이면 leg가 20개 이상 — 직렬로는 수십 초 걸리므로 날짜 단위로 병렬 호출
    # (날짜끼리는 서로 독립이라 안전. pool.map은 입력 순서대로 결과를 돌려줘서 날짜가 안 섞임)
    with ThreadPoolExecutor(max_workers=6) as pool:
        items_by_day = list(pool.map(
            lambda stops: _to_items(stops, departure_time_iso), day_stops
        ))

    days = []
    for i in range(plan_days):
        days.append({
            "day": day_offset + i + 1,  # 전체 여행 기준 통산 일차
            "city": city,               # ERD ItineraryDay.city_name - 멀티시티 구분용
            "items": items_by_day[i],
        })
    return days
    

def collect_edit_candidates(city_en: str, country_code: str | None,
                            edit_request: str, exclude_names: set,
                            per_query: int = 10) -> list[dict]:
    """
    국소수정용 '추가 허용 후보' 검색.

    배경: 편집기 LLM은 할루시네이션 방지를 위해 "실존 목록의 이름만" 고를 수
    있는데, 원래는 그 목록이 현재 일정뿐이라 "다른 음식점 추가해줘" 같은
    요청을 수행할 수 없었다 (오사카 실사용 피드백). 이 함수가 신선한 후보를
    검색해 목록을 넓혀준다 — 원칙(실존 목록에서만 선택)은 그대로.

    exclude_names: 현재 일정에 이미 있는 이름들 (중복 제안 방지)
    """
    geo = geocode(city_en, country_code=country_code)
    center = (geo["lat"], geo["lng"]) if geo else None

    # seen을 기존 일정 이름으로 시작 -> 이미 일정에 있는 곳은 후보에서 제외
    seen = set(exclude_names)

    # 수정 요청 문장 자체를 첫 검색어로 활용 — Places 텍스트 검색은 자연어에
    # 강해서 "특별한 저녁" 같은 뉘앙스를 반영한 결과를 준다.
    # 뒤의 고정 쿼리들은 안전망 (요청이 검색어로 부적합해도 후보가 비지 않게)
    queries = []
    req = (edit_request or "").strip()
    if req:
        queries.append(f"{city_en} {req[:40]}")
    queries += [f"{city_en} 맛집", f"best restaurants in {city_en}",
                f"{city_en} 관광 명소"]

    candidates = _collect_places(queries, seen, center, per_query=per_query)
    # 숙소(호텔)는 방문 장소가 아니므로 제외 — 실사고: 수정 요청 문장을 검색어로
    # 쓰다 보니 호텔이 후보로 들어와 편집기가 일정에 숙소를 "제안"하는 혼선 발생.
    # 숙소 변경은 예산영향 라우트(재검색+재배분)의 관할이다.
    candidates = [c for c in candidates
                  if "lodging" not in (c.get("types") or [])]
    # 프롬프트 비대 방지: 인기도 상위 12곳까지만
    return _pick_top(candidates, 12)


def build_day_plan(destinations: list[dict], themes: list[str] | None = None,
                   start_date: str | None = None) -> dict:
    """일정의 데이터 부분을 만듦
    
    Args:
        destinations: 파서 출력의 목적지 목록
        themes / start_date: 이전과 동일

    도시별 활동일수 = 그 도시의 nights (마지막 도시는 귀국일만큼 +1)
    """

    if not destinations or not destinations[0].get("city"):
        raise ValueError("목적지가 없습니다. 파서 출력을 확인하세요.")
    themes = themes or []

    plan_desc = " + ".join(f"{d.get('city')} {d.get('nights', '?')}박" for d in destinations)
    logger.info("일정 생성 시작: %s . 테마 %s", plan_desc, themes)

    departure_time_iso = f"{start_date}T01:00:00Z" if start_date else None

    day_plan = []
    day_offset = 0
    last_idx = len(destinations) - 1
    for idx, dest in enumerate(destinations):
        city_days = max(1, dest.get("nights") or 1)
        if idx == last_idx:
            city_days += 1  # 전체 여행의 마지막 날(귀국일)을 마지막 도시 일정에 포함
        day_plan.extend(
            _build_city_days(
                dest, themes, city_days, day_offset, departure_time_iso,
                include_arrival_airport=(idx == 0),
                include_departure_airport=(idx == last_idx),
            )
        )
        day_offset += city_days

    total = sum(len(d["items"]) for d in day_plan)
    logger.info("일정 완성: 총 %d일 . %d곳", day_offset, total)
    return {
        "city": ", ".join(d.get("city", "?") for d in destinations),
        "plan_days": day_offset,
        "day_plan": day_plan,
    }