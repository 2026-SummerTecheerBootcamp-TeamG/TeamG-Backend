"""
itinerary.py - 일정 에이전트

테마에 맞는 관광지/맛집을 찾고, 날짜별 방문 순서를 최적화함

장소 수집(Places) -> 인기도 랭킹 -> 동선 정렬(최근접 이웃) -> 날짜 분할 -> 실이동시간(Routes) 부착
"""

import logging
import math
from datetime import time as _time
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

# 하루 일정의 기본 시간대 (항공편 시각 정보가 없는 날/구간에 사용)
DAY_START_MIN = 9 * 60    # 09:00
DAY_END_MIN = 21 * 60      # 21:00
ARRIVAL_BUFFER_MIN = 60    # 입국심사+수하물+공항→시내 이동 여유
DEPARTURE_BUFFER_MIN = 120  # 출국 수속 여유 (공항 도착 목표 시각까지)
# 장소 종류별 체류 시간 기본값(분) - 실측 데이터가 없어 쓰는 보수적 추정치
# (예전 90/70분 기준으로는 관광 2곳+점심을 다 마쳐도 오후 3시밖에 안 돼서
#  저녁 앵커(18시대)까지 3시간 넘게 비는 날이 흔했음 - 실사용 피드백 반영)
VISIT_MIN = {"food": 80, "attraction": 110, "airport": 0}
# 점심/저녁 각각 "최소 이 시각 이후" 배치 - 관광이 일찍 끝나도 식사 시각까지는
# 자연스러운 대기 시간으로 채워짐 (앞 일정이 끝나자마자 바로 저녁을 먹는 부자연스러움 방지)
# 저녁을 18:30 -> 18:00으로 살짝 당겨서 위 체류시간 증가분과 함께 공백을 더 좁힘
MEAL_ANCHORS_MIN = [12 * 60, 18 * 60]   # 점심 12:00, 저녁 18:00
DINNER_MIN_DURATION = 90   # 저녁은 점심보다 여유 있게 - 기본 food 체류시간(70분)보다 길게

# Google Places의 구체 타입별 체류 시간(분) - VISIT_MIN의 food/attraction 2분류보다
# 세분화된 값이 있으면 우선 적용 (박물관/공원/카페가 전부 "관광 90분"으로 뭉개지던 문제)
# dict 순서 = 우선순위 (한 장소가 여러 타입을 가질 때 앞쪽 타입이 우선)
DURATION_BY_TYPE = {
    "amusement_park": 180, "water_park": 180, "movie_theater": 150, "zoo": 150,
    "museum": 120, "aquarium": 120, "national_park": 120, "night_club": 120,
    "amusement_center": 120, "shopping_mall": 90, "department_store": 90,
    "art_gallery": 90, "spa": 90, "bar": 90, "park": 60, "market": 60,
    "cafe": 45, "church": 45, "hindu_temple": 45, "mosque": 45, "synagogue": 45,
    "bakery": 30,
}
# 음식점 계열 타입 - 편집 시 추가되는 후보의 food/attraction 분류(kind)에 씀
FOOD_TYPES = {"restaurant", "cafe", "bakery", "bar", "food"}


def estimate_duration_min(place: dict, kind: str | None = None) -> int:
    """장소의 Google Places 타입을 보고 체류 시간을 추정. 세분류가 없으면 kind의
    food/attraction 기본값(VISIT_MIN)으로 폴백"""

    types = set(place.get("types") or [])
    for t, minutes in DURATION_BY_TYPE.items():
        if t in types:
            return minutes
    return VISIT_MIN.get(kind, 90)


def infer_kind(place: dict) -> str:
    """Google Places 타입으로 food/attraction 분류 - 편집 시 새로 추가되는
    후보(collect_edit_candidates)는 최초 생성 파이프라인처럼 미리 분류되어
    있지 않아 타입으로 역추정해야 함"""

    return "food" if set(place.get("types") or []) & FOOD_TYPES else "attraction"


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
            "kind": s.get("kind"),   # _schedule_day가 식사 항목(점심/저녁)을 식별하는 데 씀
            "duration_min": estimate_duration_min(s, s.get("kind")),   # 체류 시간 추정
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
                r["kind"] = "airport"
                return r
    return None


def _parse_clock_minutes(dt_str: str | None) -> int | None:
    """'2026-08-09 18:30' -> 1110 (자정 기준 분). 형식이 다르면 None."""

    if not dt_str:
        return None
    try:
        hh, mm = dt_str.strip().split(" ")[1].split(":")[:2]
        return int(hh) * 60 + int(mm)
    except Exception:
        return None


def _day_window(arrival_time_str: str | None = None,
                departure_time_str: str | None = None) -> tuple[int, int]:
    """그 날 방문지를 채울 수 있는 시간대(분, 자정 기준)를 정함

    항공편 도착시각이 있으면 그 이후(+버퍼)로 시작을 늦추고,
    출발시각이 있으면 그 이전(-버퍼)으로 끝을 당김
    """

    start, end = DAY_START_MIN, DAY_END_MIN
    arr = _parse_clock_minutes(arrival_time_str)
    if arr is not None:
        start = max(start, arr + ARRIVAL_BUFFER_MIN)
    dep = _parse_clock_minutes(departure_time_str)
    if dep is not None:
        end = min(end, dep - DEPARTURE_BUFFER_MIN)
    return start, end


def _clock_time(minutes: int) -> _time:
    """자정 기준 분 -> time 객체. 자정 넘김 방어(모듈로 24h)."""
    return _time(hour=(minutes // 60) % 24, minute=minutes % 60)


def _schedule_day(items: list[dict], start_min: int, end_min: int) -> list[dict]:
    """가용 시간대에 맞춰 항목을 채우고 각 항목의 arrival_time(도착 예정 시각)을 채움

    시간대를 넘기는 항목부터는 그 날 일정에서 뺀다. 공항 항목(맨 앞=도착공항 /
    맨 뒤=출발공항)은 시간 체크 없이 항상 포함한다 - 이동 거점이지 시간에 맞춰
    뺄 수 있는 방문 활동이 아니기 때문. (주의: 단순 break로는 안 됨 - 출발일은
    공항이 리스트 맨 뒤라, 앞쪽 일반 항목에서 break하면 공항까지 통째로 사라짐)
    """

    if not items:
        return []

    def is_airport(it: dict) -> bool:
        return (it.get("place_detail") or {}).get("category") == "airport"

    airport_front = is_airport(items[0])
    # 항목이 1개뿐이고 그게 공항이면 "앞" 쪽으로만 취급 (앞/뒤 중복 방지)
    airport_back = is_airport(items[-1]) and not (airport_front and len(items) == 1)
    regular = items[(1 if airport_front else 0):(len(items) - 1 if airport_back else len(items))]

    kept = []
    clock = start_min
    if airport_front:
        items[0]["arrival_time"] = _clock_time(clock)
        kept.append(items[0])
        clock += items[0].get("travel_min_to_next") or 0

    trimmed = False
    meal_idx = 0    # 몇 번째 식사 항목을 만났는지 (0=점심, 1=저녁)
    for item in regular:
        if item.get("kind") == "food" and meal_idx < len(MEAL_ANCHORS_MIN):
            # 관광이 일찍 끝났어도 식사 시각까지는 자연스럽게 대기 - 뒤가 안 당겨짐
            clock = max(clock, MEAL_ANCHORS_MIN[meal_idx])
            if meal_idx == 1:   # 저녁은 좀 더 여유 있게 먹는다고 가정
                item["duration_min"] = max(item.get("duration_min") or 0, DINNER_MIN_DURATION)
            meal_idx += 1
        duration = item.get("duration_min") or 0
        if clock + duration > end_min:
            trimmed = True
            break
        item["arrival_time"] = _clock_time(clock)
        kept.append(item)
        clock += duration + (item.get("travel_min_to_next") or 0)

    if trimmed and kept:
        # 뒤가 잘렸으니 마지막 남은 일반 항목의 '다음 장소까지 이동정보'는 무의미해짐
        # (공항 항목의 travel_min_to_next는 원래 없음 - 항상 마지막 항목이므로 무해)
        kept[-1]["travel_min_to_next"] = None
        kept[-1]["travel_mode"] = None

    if airport_back:
        # 남은 일정이 없거나 일찍 끝났어도 출발 공항 시각은 최소 end_min 기준으로 표시
        clock = max(clock, end_min)
        items[-1]["arrival_time"] = _clock_time(clock)
        kept.append(items[-1])

    for order, item in enumerate(kept, start=1):
        item["visit_order"] = order
    return kept


def schedule_day_times(items: list[dict], start_min: int = DAY_START_MIN,
                       end_min: int = DAY_END_MIN) -> list[dict]:
    """국소수정으로 재조립된 하루 일정에 도착 예정 시각을 (재)계산해 채움

    최초 생성과 동일한 규칙(_schedule_day)을 그대로 재사용 - 가용 시간대를
    넘는 항목은 반환값에서 빠짐 (호출자가 원래 목록과 대조해 dropped 처리)
    """
    return _schedule_day(items, start_min, end_min)


def _build_city_days(destination: dict, themes: list[str], plan_days: int,
                   day_offset: int, departure_time_iso: str | None,
                   include_arrival_airport: bool = False,
                   include_departure_airport: bool = False,
                   flight_arrival_time: str | None = None,
                   flight_departure_time: str | None = None) -> list[dict]:
    """도시 하나의 일정을 만듦

    day_offset: 이 도시의 일정이 전체 여행의 며칠째부터 시작하는지
    include_arrival_airport: 여행 전체의 첫 날 맨 앞에 도착 공항을 넣을지
    include_departure_airport: 여행 전체의 마지막 날 맨 뒤에 출발(귀국) 공항을 넣을지
    flight_arrival_time / flight_departure_time: 선택된 항공편의 실제
    도착/출발 시각("YYYY-MM-DD HH:MM"). 각각 여행 전체 첫날/마지막날에만 적용됨
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

    # kind 태깅 - 시각 스케줄링(_schedule_day)에서 체류 시간 기본값을 정할 때 씀
    for p in foods:
        p["kind"] = "food"
    for p in attractions:
        p["kind"] = "attraction"

    # 선별: 하루 = 관광 2곳 + 맛집 2곳(점심/저녁) 기준, 총 4개 -> 동선 정렬 -> 날짜 분할
    # (예전엔 관광 2 + 맛집 1뿐이라 항목들을 시간 순으로 그냥 이어붙이기만 해서
    #  총 체류시간이 4시간 안팍 -> 하루 가용시간(09~21시)의 절반도 못 채우고
    #  오후 1~4시쯔 일정이 끝나버리는 문제가 있었음. 지금은 개수 자체보다
    #  점심/저녁을 실제 식사 시간대에 고정 배치하는 쪽(_schedule_day의
    #  MEAL_ANCHORS_MIN)이 핵심 - 관광이 일찍 끝나도 저녁 전까지는 자연스러운
    #  대기 시간으로 채워져서 하루가 저녁까지 이어짐)
    top_attractions = _pick_top(attractions, plan_days * 2)
    top_foods = _pick_top(foods, plan_days * 2)
    attr_chunks = _split_into_days(_order_by_nearest(top_attractions), plan_days)

    # 날짜별 구성 1단계: 관광지 사이에 점심을 끼워 넣고 저녁은 맨 뒤에 붙임
    # (예전엔 관광+식사를 한 목록에 다 넣고 동선(NN)으로만 정렬해서 점심/저녁이
    #  아무 위치에나 낄 수 있었음 - 저녁이 오후 2시 자리에 꽂히는 식. 식사는 동선보다
    #  "언제 먹는지"가 더 중요해서 위치를 명시적으로 고정하고, _schedule_day가
    #  실제 시각을 맞춰줌)
    day_stops = []
    for i in range(plan_days):
        attrs = list(attr_chunks[i]) if i < len(attr_chunks) else []
        day_foods = top_foods[i * 2:(i + 1) * 2]
        lunch, dinner = day_foods[:1], day_foods[1:2]

        mid = (len(attrs) + 1) // 2   # 관광지를 반으로 나눠 점심 앞뒤로 배치
        stops = attrs[:mid] + lunch + attrs[mid:] + dinner
        day_stops.append(stops)

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

    # 시각 스케줄링: 모든 날에 도착 예정 시각(arrival_time)을 채우고,
    # 항공편 시각이 걸린 첫날/마지막날은 가용 시간에 맞춰 방문지를 자름
    for i in range(plan_days):
        is_first_global_day = include_arrival_airport and i == 0
        is_last_global_day = include_departure_airport and i == plan_days - 1
        start_min, end_min = _day_window(
            flight_arrival_time if is_first_global_day else None,
            flight_departure_time if is_last_global_day else None,
        )
        items_by_day[i] = _schedule_day(items_by_day[i], start_min, end_min)

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

    def _drop_lodging(places: list[dict]) -> list[dict]:
        # 숙소(호텔)는 방문 장소가 아니므로 제외 — 실사고: 수정 요청 문장을 검색어로
        # 쓰다 보니 호텔이 후보로 들어와 편집기가 일정에 숙소를 "제안"하는 혼선 발생.
        # 숙소 변경은 예산영향 라우트(재검색+재배분)의 관할이다.
        return [c for c in places if "lodging" not in (c.get("types") or [])]

    # 수정 요청 문장 자체를 첫 검색어로 활용 — Places 텍스트 검색은 자연어에
    # 강해서 "특별한 저녁" 같은 뉘앙스를 반영한 결과를 준다.
    # ⭐ 이 결과는 사용자 의도의 직접 반영이므로 상위 3곳을 "보호 슬롯"으로
    # 무조건 포함한다 (인기도 컷 면제). 실사고: "깃허브 본사 추가해줘" —
    # 검색은 정확히 찾았는데(리뷰 293) 관광 명소들의 인기도 점수에 밀려
    # top12에서 탈락 → 편집기가 "목록에 없다"며 거부. 순서는 Places의
    # 관련도 순 그대로 쓴다 (요청과 가장 닮은 결과가 앞에 옴).
    req = (edit_request or "").strip()
    req_results = []
    if req:
        req_results = _drop_lodging(
            _collect_places([f"{city_en} {req[:40]}"], seen, center, per_query=per_query)
        )
    protected, rest = req_results[:3], req_results[3:]

    # 고정 쿼리들은 안전망 (요청이 검색어로 부적합해도 후보가 비지 않게).
    # 요청 쿼리의 4위 이하(rest)도 여기 합류해 인기도 경쟁으로 살아남을 수 있다
    queries = [f"{city_en} 맛집", f"best restaurants in {city_en}",
               f"{city_en} 관광 명소"]
    generic = _drop_lodging(_collect_places(queries, seen, center, per_query=per_query)) + rest

    # 프롬프트 비대 방지: 보호 슬롯 + 인기도 상위로 총 12곳까지만
    result = protected + _pick_top(generic, 12 - len(protected))
    # 최초 생성 파이프라인은 food/attraction을 검색어 단계에서 미리 나눠 태깅하지만
    # 여긴 한 목록에서 섞어 고르므로 타입으로 역추정 - 나중에 체류시간 추정에 씀
    for p in result:
        p["kind"] = infer_kind(p)
    return result


def build_day_plan(destinations: list[dict], themes: list[str] | None = None,
                   start_date: str | None = None,
                   flight_arrival_time: str | None = None,
                   flight_departure_time: str | None = None) -> dict:
    """일정의 데이터 부분을 만듦

    Args:
        destinations: 파서 출력의 목적지 목록
        themes / start_date: 이전과 동일
        flight_arrival_time: 선택된 항공편의 가는 편 도착시각("YYYY-MM-DD HH:MM").
            여행 전체 첫날 일정을 이 시각 이후로 시작하도록 반영됨
        flight_departure_time: 선택된 항공편의 오는 편 출발시각(동일 형식).
            여행 전체 마지막날 일정을 이 시각 이전에 끝나도록 반영됨

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
                flight_arrival_time=flight_arrival_time if idx == 0 else None,
                flight_departure_time=flight_departure_time if idx == last_idx else None,
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