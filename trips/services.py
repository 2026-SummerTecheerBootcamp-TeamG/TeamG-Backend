"""
trips/services.py - 파이프라인 결과를 DB에 저장하는 서비스 계층

서비스 계층:
    여러 모델을 한 번에 다루는 업무 로직을 뷰/태스크에서 분리해 두는 파일
    저장 규칙이 바뀌어도 이 파일만 고치면 되고, 뷰와 태스크 양쪽에서 재사용
    
저장 시나리오:
    1. create_request_and_plan: 실행 접수 시점
    2. save_pipeline_result: 파이프라인 완료 시점
"""

from datetime import date, timedelta

# transaction.atomic: 블록 안의 DB 쓰기 여러 건을 전부 성공 아닌면 전부 취소로 묶음
from django.db import transaction

from trips.models import (
    TripRequest, TripDestination, Plan,
    Flight, Hotel, ItineraryDay, ItineraryItem, Booking,
)
from agents.itinerary import (
    DAY_START_MIN, DAY_END_MIN, estimate_duration_min, schedule_day_times,
    # _day_window: 항공 도착/출발 시각 -> 그 날 가용 시간대. 밑줄이 붙어 있지만
    # 최초 생성과 "같은 규칙"으로 재계산하는 것이 목적이라 일부러 같은 함수를 쓴다
    _day_window,
)


def create_request_and_plan(user, fields, raw_parsed=None):
    """
    확정된 파싱 결과로 요청/목적지/빈 플랜을 만듦
    
    반환: (trip_request, plan) 튜플
    """

    dates = fields.get("dates") or {}
    origin = fields.get("origin") or {}
    origin_iata = origin.get("iata")
    if isinstance(origin_iata, dict):
        origin_iata = origin_iata.get("iata")
    if not (isinstance(origin_iata, str) and len(origin_iata) == 3):
        origin_iata = None      # 모델이 null 허용

    origin_city = origin.get("city")
    if not origin_city and isinstance(origin.get("iata"), dict):
        origin_city = origin["iata"].get("city")    # 같은 사고에서 도시명 구조

    pax = fields.get("pax") or {}

    # 도시별 nights가 파싱에 없을 때를 대비한 전체 박 수
    start = date.fromisoformat(dates["start"])
    end = date.fromisoformat(dates["end"])
    total_nights = (end - start).days

    with transaction.atomic():
        trip_request = TripRequest.objects.create(
            user=user,
            departure=origin_city or "서울",
            origin_iata=origin_iata,
            start_date=dates["start"],  # DateField는 "YYYY-MM-DD" 문자열을 알아서 변환
            end_date=dates["end"],
            total_budget=fields["budget"],
            adult=pax.get("adult", 1),
            kid=pax.get("child", 0),
            themes=fields.get("themes") or [],
            raw_input=raw_parsed,       # 원문+파싱 스냅샷
        )
        
        for seq, d in enumerate(fields.get("destinations") or [], start=1):
            TripDestination.objects.create(
                request=trip_request,
                seq_order=seq,
                city_name=d.get("city") or "?",
                city_en=d.get("city_en"),
                country_code=d.get("country_code"),
                iata_code=d.get("iata"),
                nights=d.get("nights") or total_nights,
            )

        plan = Plan.objects.create(request=trip_request)    # status = processing

    return trip_request, plan


def _diet_candidates(flight_options, hotel_options, top_n=8):
    """
    검색 후보를 비교 UI용 스냅샷으로 다이어트 (Plan.candidates에 저장)

    왜 다이어트: 원본 후보(raw)에는 슬라이스 전체 등 부피 큰 데이터가 섞여 있는데
    비교·재선택에 필요한 필드는 소수다. 배분 엔진/저장 로직이 기대하는
    {"label","krw","utility","raw"} 형태는 유지 — 이 스냅샷을 그대로
    allocate_budget에 다시 넣어 "재검색 없는 교체 선택"이 가능하게 한다.
    정렬: 만족도(utility) 높은 순 = 추천순
    """

    def diet_flight(o):
        raw = o.get("raw") or {}
        return {
            "label": o.get("label"), "krw": o.get("krw"),
            "utility": o.get("utility"),
            "utility_reasons": o.get("utility_reasons"),
            # departure_token: 이 후보로 교체할 때 귀국편 시각을 SerpApi에서
            # 재조회하기 위한 열쇠 (귀국 시각 자체는 검색 단계에 없어서 저장 불가 —
            # 이 토큰이 빠지면 교체 후 귀국편 정보가 통째로 사라지는 버그가 됐었음)
            "raw": {k: raw.get(k) for k in
                    ("departure_time", "arrival_time", "duration_min",
                     "stops", "expires_at", "departure_token")},
        }

    def diet_hotel(o):
        raw = o.get("raw") or {}
        return {
            "label": o.get("label"), "krw": o.get("krw"),
            "utility": o.get("utility"),
            "raw": {k: raw.get(k) for k in
                    ("name", "star_rating", "latitude", "longitude",
                     "reasons", "address")},
        }

    by_utility = lambda o: o.get("utility") or 0
    priced_f = [o for o in flight_options if o.get("krw")]
    priced_h = [o for o in hotel_options if o.get("krw")]
    return {
        "flights": [diet_flight(o) for o in
                    sorted(priced_f, key=by_utility, reverse=True)[:top_n]],
        "hotels": [diet_hotel(o) for o in
                   sorted(priced_h, key=by_utility, reverse=True)[:top_n]],
    }


def save_pipeline_result(plan_id, result):
    """
    파이프라인 결과 dict를 DB에 채움
    저장이 끝나면 Plan.status가 draft가 됨
    """

    plan = Plan.objects.get(id=plan_id)
    allocation = result.get("allocation") or {}
    # selection은 배분 성공 때만 존재
    selection = allocation.get("selection") or {}

    with transaction.atomic():
        # Plan 본체: 배분 스냅샷 + 후보 스냅샷(비교 UI용) + 내러티브 + 상태 전환
        plan.allocation = allocation
        plan.candidates = _diet_candidates(
            result.get("flight_options") or [], result.get("hotel_options") or [])
        plan.narrative = result.get("narrative")
        plan.status = Plan.Status.DRAFT
        plan.save()

        # 선택 항공 - 배분에 항공이 포함됐을 때만
        sel_flight = selection.get("flight")
        if sel_flight:
            Flight.objects.create(
                plan=plan,
                airline=sel_flight.get("label") or "?",
                price_krw=sel_flight.get("krw") or 0,
                # SerpApi를 currency=KRW로 검색하므로 원통화도 KRW
                price_original=sel_flight.get("krw") or 0,
                currency="KRW",
                utility=sel_flight.get("utility"),
                slices=sel_flight.get("raw"),
            )

        # 선택 숙소
        sel_hotel = selection.get("hotel")
        if sel_hotel:
            raw = sel_hotel.get("raw") or {}
            hotel_id = str(sel_hotel.get("label") or "?")
            Hotel.objects.create(
                plan=plan,
                liteapi_hotel_id=hotel_id,
                name=raw.get("name") or hotel_id,
                stars=raw.get("star_rating"),
                price_krw=sel_hotel.get("krw") or 0,
                price_original=sel_hotel.get("krw") or 0,
                currency="KRW",
                utility=sel_hotel.get("utility"),
                utility_reasons=raw.get("reasons"),
                latitude=raw.get("latitude"),
                longitude=raw.get("longitude"),
                detail=raw,
            )

        # 일자별 일정
        start_date = plan.request.start_date
        for day in result.get("day_plan") or []:
            day_number = day.get("day") or 1
            day_row = ItineraryDay.objects.create(
                plan=plan,
                day_number=day_number,
                city_name=day.get("city"),
                date=start_date + timedelta(days=day_number - 1),
            )
            # bulk_create: 장소별 INSERT를 하루치 1번으로 묶음 (11박 여행이면 30회+ -> 11회)
            ItineraryItem.objects.bulk_create([
                ItineraryItem(
                    day=day_row,
                    visit_order=item.get("visit_order"),
                    place_name=item.get("place_name") or "?",
                    latitude=item.get("lat"),
                    longitude=item.get("lng"),
                    place_detail=item.get("place_detail"),
                    arrival_time=item.get("arrival_time"),
                    duration_min=item.get("duration_min"),
                    travel_min_to_next=item.get("travel_min_to_next"),
                    travel_mode=item.get("travel_mode"),
                )
                for item in day.get("items") or []
            ])
            
    return plan


def load_day_plan(plan):
    """
    저장된 플랜의 일정을 파이프라인 day_plan과 같은 dict 구조로 되살림
    (편집기 입력 + 재조립 원본 두 용도)
    """
    
    # prefetch_related("items"): 날짜별로 items 쿼리를 따로 날리던 N+1을
    # 2쿼리(days 1 + items 1)로 줄인다 — 결과 데이터는 완전히 동일
    return [
        {
            "day": day.day_number,
            "city": day.city_name,
            "items": [
                {
                    "place_name": item.place_name,
                    "lat": item.latitude,
                    "lng": item.longitude,
                    "place_detail": item.place_detail,
                    "arrival_time": item.arrival_time,
                    "duration_min": item.duration_min,
                    "travel_min_to_next": item.travel_min_to_next,
                    "travel_mode": item.travel_mode,
                }
                for item in day.items.all()     # prefetch 캐시 사용 (추가 쿼리 0)
            ],
        }
        for day in plan.days.prefetch_related("items")
    ]


def create_edited_version(old_plan, edited, edit_request, extra_pool=None):
    """
    편집 결과로 새 버전을 만듦

    핵심 원칙: LLM은 이름만 골랐고, 데이터는 전부 "실존 목록"에서 가져옴
    - 원본 일정 or 추가 후보 풀(extra_pool, 구글 실검색 결과)에 있는 이름만 통과
    - 둘 다에 없는 이름 -> 제외 (할루시네이션 차단은 그대로)
    - 이름 순서가 원본과 같은 날 -> 행을 통째로 복사 (시각/이동시간 포함)
    - 순서/구성이 바뀐 날 -> 이동시간은 null, 도착 시각은 그 날 가용 시간대에
      맞춰 재계산(_day_edit_window + schedule_day_times) - 새로 추가된 장소도
      이 과정에서 arrival_time을 얻음

    반환: (new_plan, dropped_unknown, dropped_no_time)
      dropped_unknown: 실존 목록에 없는 이름 (할루시네이션 차단으로 제외)
      dropped_no_time: 실존하지만 그 날 가용 시간을 넘어 잘린 장소
      — 사유가 다르면 사용자 안내도 달라야 해서 분리 ("목록에 없음"과
        "시간이 부족함"은 재시도 방향이 완전히 다름)

    extra_pool: [{"name","lat","lng","rating","user_ratings","address","kind"}, ...]
    """

    original = load_day_plan(old_plan)

    # 이름 -> 원본 item dict 조회표
    item_by_name = {}
    for d in original:
        for item in d["items"]:
            item_by_name[item["place_name"]] = item

    # 추가 후보도 item 형태로 변환해 조회표에 합류 (원본과 이름이 겹치면 원본 우선)
    for c in (extra_pool or []):
        name = c.get("name")
        if not name or name in item_by_name:
            continue
        kind = c.get("kind")
        item_by_name[name] = {
            "place_name": name,
            "lat": c.get("lat"), "lng": c.get("lng"),
            "place_detail": {
                "rating": c.get("rating"),
                "user_ratings": c.get("user_ratings"),
                "address": c.get("address"),
                # kind도 스냅샷에 보존 — 이 장소가 다음 수정 때 또 재스케줄돼도
                # 식사 앵커(12시/18시)를 잃지 않게 (최초 생성 _to_items와 같은 규칙)
                **({"kind": kind} if kind else {}),
            },
            # 새로 추가되는 장소는 원본에 없던 항목이라 체류시간을 여기서 추정해둠
            # (구성이 바뀐 날은 아래에서 어차피 시각을 통째로 재계산하므로 이 값이 실제로 쓰임)
            "duration_min": estimate_duration_min(c, kind),
            # 새 장소가 낀 날은 동선이 달라지므로 이동시간은 어차피 null 처리됨
            "travel_min_to_next": None, "travel_mode": None,
        }
    original_names = {d["day"]: [i["place_name"] for i in d["items"]] for d in original}
    city_by_day = {d["day"]: d.get("city") for d in original}

    dropped_unknown = []    # 실존 목록에 없는 이름 (할루시네이션 차단)
    dropped_no_time = []    # 실존하지만 그 날 가용 시간을 넘어 잘림

    with transaction.atomic():
        # 새 plan 버전
        new_plan = Plan.objects.create(
            request=old_plan.request,
            status=Plan.Status.DRAFT,
            allocation=old_plan.allocation,     # 국소수정 = 예산 불변이므로 복사
            candidates=old_plan.candidates,     # 후보 스냅샷도 계승 (비교 UI 유지)
            narrative=old_plan.narrative,       # 설명문 재생성은 후속
            edit_request=edit_request,
        )

        # 선택 항공/숙소도 새 버전에 복사
        old_flight = getattr(old_plan, "flight", None)
        if old_flight:
            Flight.objects.create(
                plan=new_plan, airline=old_flight.airline,
                price_krw=old_flight.price_krw, price_original=old_flight.price_original,
                currency=old_flight.currency, utility=old_flight.utility,
                utility_reasons=old_flight.utility_reasons, slices=old_flight.slices,
            )
        old_hotel = getattr(old_plan, "hotel", None)
        if old_hotel:
            Hotel.objects.create(
                plan=new_plan, liteapi_hotel_id=old_hotel.liteapi_hotel_id,
                name=old_hotel.name, stars=old_hotel.stars,
                price_krw=old_hotel.price_krw, price_original=old_hotel.price_original,
                currency=old_hotel.currency, utility=old_hotel.utility,
                utility_reasons=old_hotel.utility_reasons,
                latitude=old_hotel.latitude, longitude=old_hotel.longitude,
                detail=old_hotel.detail,
            )

        # 일정 재조전
        start_date = old_plan.request.start_date
        for day_edit in edited.get("days") or []:
            day_number = day_edit["day"]
            names = day_edit.get("place_names") or []

            # 원본에 실재하는 이름만 통과
            valid_names = []
            for name in names:
                if name in item_by_name:
                    valid_names.append(name)
                else:
                    dropped_unknown.append(name)

            # 이름 순서가 원본과 완전히 같으면 = 변경 없는 날 -> 이동시간/시각 보존
            unchanged = valid_names == original_names.get(day_number, [])

            day_row = ItineraryDay.objects.create(
                plan=new_plan, day_number=day_number,
                city_name=city_by_day.get(day_number),
                date=start_date + timedelta(days=day_number - 1),
            )

            if unchanged:
                # 구성이 그대로면 원본 시각/이동정보를 그대로 씀 (재계산 불필요)
                scheduled = [
                    dict(item_by_name[name], place_name=name)
                    for name in valid_names
                ]
            else:
                # 구성/순서가 바뀐 날(추가/삭제/재배열) -> 그 날 가용 시간대에 맞춰
                # 도착 시각을 통째로 다시 계산. 새로 추가된 장소도 여기서 처음으로
                # arrival_time을 얻음 (기존엔 이 경로에서 시간 필드가 통째로 비어버렸음)
                day_inputs = [
                    {
                        "place_name": name,
                        "lat": item_by_name[name]["lat"],
                        "lng": item_by_name[name]["lng"],
                        "place_detail": item_by_name[name]["place_detail"],
                        # kind: 점심/저녁 앵커(12시/18시) 식별용.
                        # 새 후보는 최상위에, 기존 항목은 place_detail 스냅샷에 있다
                        # (시간 기능 이후 생성분부터 — 그 전 구버전 플랜은 None이라
                        #  앵커 없이 순차 배치되는 기존 동작 유지)
                        "kind": (item_by_name[name].get("kind")
                                 or (item_by_name[name].get("place_detail") or {}).get("kind")),
                        "duration_min": item_by_name[name].get("duration_min"),
                        "travel_min_to_next": None,
                        "travel_mode": None,
                    }
                    for name in valid_names
                ]
                start_min, end_min = _day_edit_window(original, day_number)
                scheduled = schedule_day_times(day_inputs, start_min, end_min)
                # 가용 시간을 넘어 스케줄러가 잘라낸 장소 — "시간 부족" 사유로 분리 보고
                kept_names = {it["place_name"] for it in scheduled}
                dropped_no_time.extend(
                    name for name in valid_names if name not in kept_names)

            # bulk_create: 장소별 INSERT를 하루치 1번으로 묶음 (저장 내용은 동일)
            ItineraryItem.objects.bulk_create([
                ItineraryItem(
                    day=day_row, visit_order=order,
                    place_name=it["place_name"],
                    latitude=it["lat"],
                    longitude=it["lng"],
                    place_detail=it["place_detail"],
                    arrival_time=it.get("arrival_time"),
                    duration_min=it.get("duration_min"),
                    travel_min_to_next=it.get("travel_min_to_next"),
                    travel_mode=it.get("travel_mode"),
                )
                for order, it in enumerate(scheduled, start=1)
            ])

    return new_plan, dropped_unknown, dropped_no_time


def _day_edit_window(original: list[dict], day_number: int) -> tuple[int, int]:
    """
    국소수정으로 재스케줄할 때 쓸 그 날의 가용 시간대(분, 자정 기준) 추정

    원본 일정에 입국/출국 공항 항목이 있으면 그 도착 시각을 그대로 시작/종료
    기준으로 씀 (최초 생성 때 이미 항공편 시각+버퍼가 반영된 값이라 재계산이
    불필요함 - agents.itinerary._day_window/_schedule_day 참고). 없으면 기본
    하루 시간대(09:00~21:00)를 씀.

    알려진 비대칭(허용): 출국 공항의 arrival_time은 max(일정 종료, 창 종료)라
    일정이 꽉 찼던 날엔 실제 창보다 살짝 늦을 수 있음 → 편집 창이 그만큼
    넓어져 원래라면 잘렸을 항목이 살아남을 수 있다. 사용자에게 유리한 방향의
    오차라 그대로 둔다 (좁히면 멀쩡한 항목이 갑자기 잘리는 쪽이 더 나쁨).
    """

    day = next((d for d in original if d["day"] == day_number), None)
    if not day or not day["items"]:
        return DAY_START_MIN, DAY_END_MIN

    def is_airport(item: dict) -> bool:
        return (item.get("place_detail") or {}).get("category") == "airport"

    items = day["items"]
    start_min = DAY_START_MIN
    if is_airport(items[0]) and items[0].get("arrival_time"):
        t = items[0]["arrival_time"]
        start_min = t.hour * 60 + t.minute

    end_min = DAY_END_MIN
    if is_airport(items[-1]) and items[-1].get("arrival_time"):
        t = items[-1]["arrival_time"]
        end_min = t.hour * 60 + t.minute

    return start_min, end_min


def copy_plan_version(src_plan, edit_request_note):
    """
    플랜 버전을 통째로 복사해 '새 최신 버전'을 만듦
    
    외 되돌리기가 아니라 복사인가
        v3에서 v1으로 되돌아가면 v2, v3 이력이 애매해짐
        v1을 v4로 복사하면 이력이 한 방향으로 계속 흐름
        무엇을 언제 되돌렸는지도 기록에 남음
    """
    
    with transaction.atomic():
        new_plan = Plan.objects.create(
            request=src_plan.request,
            status=Plan.Status.DRAFT,       # 복사본은 다시 확정 전 상태로
            allocation=src_plan.allocation,
            # 후보 스냅샷 계승 — 이게 빠지면 롤백한 버전에서 비교 버튼이 사라진다
            # (후보 비교 기능이 롤백 경로를 빠뜨렸던 누락, 시간 기능 리뷰에서 발견)
            candidates=src_plan.candidates,
            narrative=src_plan.narrative,
            edit_request=edit_request_note  # "v{n}으로 롤백" 기록
        )

        src_flight = getattr(src_plan, "flight", None)
        if src_flight:
            Flight.objects.create(
                plan=new_plan, airline=src_flight.airline,
                price_krw=src_flight.price_krw, price_original=src_flight.price_original,
                currency=src_flight.currency, utility=src_flight.utility,
                utility_reasons=src_flight.utility_reasons, slices=src_flight.slices,
            )
        src_hotel = getattr(src_plan, "hotel", None)
        if src_hotel:
            Hotel.objects.create(
                plan=new_plan, liteapi_hotel_id=src_hotel.liteapi_hotel_id,
                name=src_hotel.name, stars=src_hotel.stars,
                price_krw=src_hotel.price_krw, price_original=src_hotel.price_original,
                currency=src_hotel.currency, utility=src_hotel.utility,
                utility_reasons=src_hotel.utility_reasons,
                latitude=src_hotel.latitude, longitude=src_hotel.longitude,
                detail=src_hotel.detail,
            )

        # 일정은 행 단위 그대로 복사
        # (prefetch로 읽기 N+1 제거 + bulk_create로 쓰기 INSERT 묶음 — 내용 동일)
        for day in src_plan.days.prefetch_related("items"):
            day_row = ItineraryDay.objects.create(
                plan=new_plan, day_number=day.day_number,
                city_name=day.city_name, date=day.date,
            )
            ItineraryItem.objects.bulk_create([
                ItineraryItem(
                    day=day_row, visit_order=item.visit_order,
                    place_name=item.place_name,
                    latitude=item.latitude, longitude=item.longitude,
                    place_detail=item.place_detail,
                    arrival_time=item.arrival_time,
                    duration_min=item.duration_min, est_cost=item.est_cost,
                    travel_min_to_next=item.travel_min_to_next,
                    travel_mode=item.travel_mode,
                )
                for item in day.items.all()
            ])

        return new_plan
    

def update_request_fields(trip_request, fields, raw_parsed=None):
    """
    재계획: 기존 TripRequest를 새 조건으로 갱신함
    목적지는 갱신이 아니라 전부 지우고 다시 만듦
    """

    dates = fields.get("dates") or {}
    origin = fields.get("origin") or {}
    pax = fields.get("pax") or {}

    start = date.fromisoformat(dates["start"])
    end = date.fromisoformat(dates["end"])
    total_nights = (end - start).days

    # origin_iata 방어
    origin_iata = origin.get("iata")
    if isinstance(origin_iata, dict):
        origin_iata = origin_iata.get("iata")
    if not (isinstance(origin_iata, str) and len(origin_iata) == 3):
        origin_iata = None

    with transaction.atomic():
        trip_request.departure = origin.get("city") or trip_request.departure
        trip_request.origin_iata = origin_iata or trip_request.origin_iata
        trip_request.start_date = dates["start"]
        trip_request.end_date = dates["end"]
        trip_request.total_budget = fields["budget"]
        trip_request.adult = pax.get("adult", 1)
        trip_request.kid = pax.get("child", 0)
        trip_request.themes = fields.get("themes") or []
        trip_request.raw_input = raw_parsed
        trip_request.save()

        trip_request.destinations.all().delete()    # 기존 목적지 제거 후
        for seq, d in enumerate(fields.get("destinations") or [], start=1):
            TripDestination.objects.create(
                request=trip_request, seq_order=seq,
                city_name=d.get("city") or "?",
                city_en=d.get("city_en"),
                country_code=d.get("country_code"),
                iata_code=d.get("iata"),
                nights=d.get("nights") or total_nights,
            )

    return trip_request


def save_booking(plan, guest_first, guest_last, guest_email, booking_data,
                 kind=Booking.Kind.HOTEL):
    """
    예약 시도 결과를 기록한다 (성공/실패 모두 — 실패도 이력이다).

    booking_data: 오케스트레이터가 booking_confirm(숙소) 또는
                  flight_issue_ticket(항공 mock) 툴 응답에서 수집한 dict
                  (None이면 Claude가 예약 확정까지 도달하지 못한 것)
    kind: 숙소/항공 구분 — 같은 테이블에 두 종류의 예약이 공존한다
    """
    data = booking_data or {}
    confirmed = bool(data.get("booking_id"))
    return Booking.objects.create(
        plan=plan,
        kind=kind,
        status=Booking.Status.CONFIRMED if confirmed else Booking.Status.FAILED,
        booking_id=data.get("booking_id"),
        confirmation=data.get("confirmation"),
        guest_name=f"{guest_first} {guest_last}".strip(),
        guest_email=guest_email,
        detail=data or None,
    )


def save_budget_edited_version(old_plan, new_plan, allocation, explanation):
    """
    예산영향 수정의 새 버전 완성: 항공/숙소는 재배분 선택(selection)에서, 일정은 원본 복사.

    항공도 selection에서 저장하는 이유: 이제 항공 재검색도 지원되므로
    (예: "아침 비행기로 바꿔줘") 어느 쪽이 바뀌었든 selection이 진실이다.
    재검색하지 않은 쪽은 태스크가 "기존 선택을 유일 옵션"으로 투입했으므로
    selection에 기존 값이 그대로 담겨 온다 — 저장 로직은 한 갈래로 통일.
    왜 일정은 복사인가: 숙소/항공이 바뀌어도 방문지 동선은 그대로 (일정 변경은 국소수정 관할).
    단, 항공편이 실제로 바뀌어 도착/귀국출발 '시각'이 달라졌으면 첫날/마지막날의
    도착 예정 시각만 새 항공 시각 기준으로 재계산한다 — 21시 도착 편으로 바꿨는데
    첫날이 10:30부터 시작하는 낡은 표시를 막기 위함 (시간 기능 리뷰 반영).

    반환: (new_plan, trimmed) — trimmed는 재계산 결과 새 항공 시간대에 들어가지
    못해 일정에서 빠진 장소 이름 목록 (호출자가 사용자 안내에 사용)
    """
    selection = allocation.get("selection") or {}

    # ── 항공 시각 변경 감지 (숙소만 바뀐 수정이면 시각이 같아 재계산 자체가 없음) ──
    new_raw = (selection.get("flight") or {}).get("raw") or {}
    old_slices = getattr(getattr(old_plan, "flight", None), "slices", None) or {}
    tr = old_plan.request

    new_arrival = new_raw.get("arrival_time")               # 가는 편 도착 "YYYY-MM-DD HH:MM"
    resched_first = bool(new_arrival) and new_arrival != old_slices.get("arrival_time")
    # 익일 도착 가드 (build_day_plan과 같은 원칙): 도착 날짜가 첫날이 아니면 반영 생략
    if resched_first and not new_arrival.startswith(str(tr.start_date)):
        resched_first = False

    new_return_dep = new_raw.get("return_departure_time")   # 귀국 편 출발 (없을 수 있음 —
    # 후보 비교 교체 경로의 스냅샷에는 귀국 시각이 없다. 그 경우 마지막 날은 기존
    # 시각대로 두는 것이 최선 (없는 정보로 기본 창을 강제하면 오히려 더 틀림)
    resched_last = bool(new_return_dep) and new_return_dep != old_slices.get("return_departure_time")
    if resched_last and not new_return_dep.startswith(str(tr.end_date)):
        resched_last = False

    trimmed = []    # 재계산으로 새 시간대에 못 들어가 빠진 장소들

    with transaction.atomic():
        new_plan.allocation = allocation
        # 후보 스냅샷 계승 — 새 버전에서도 계속 비교·재선택할 수 있게
        new_plan.candidates = old_plan.candidates
        # 배분 설명은 allocation과 함께 result로 반환되고, 일정 설명(narrative)은 불변
        new_plan.narrative = old_plan.narrative
        new_plan.status = Plan.Status.DRAFT
        new_plan.save()

        # 항공: 재배분이 고른 선택 (재검색 없었으면 = 기존 항공이 그대로 담김)
        sel_flight = selection.get("flight")
        if sel_flight:
            Flight.objects.create(
                plan=new_plan,
                airline=sel_flight.get("label") or "?",
                price_krw=sel_flight.get("krw") or 0,
                price_original=sel_flight.get("krw") or 0,
                currency="KRW",
                utility=sel_flight.get("utility"),
                utility_reasons=sel_flight.get("utility_reasons"),
                slices=sel_flight.get("raw"),
            )

        # 숙소: 재배분이 고른 선택
        sel_hotel = selection.get("hotel")
        if sel_hotel:
            raw = sel_hotel.get("raw") or {}
            hotel_id = str(sel_hotel.get("label") or "?")
            Hotel.objects.create(
                plan=new_plan,
                liteapi_hotel_id=hotel_id,
                name=raw.get("name") or hotel_id,
                stars=raw.get("star_rating"),
                price_krw=sel_hotel.get("krw") or 0,
                price_original=sel_hotel.get("krw") or 0,
                currency="KRW",
                utility=sel_hotel.get("utility"),
                utility_reasons=raw.get("reasons"),
                latitude=raw.get("latitude"),
                longitude=raw.get("longitude"),
                detail=raw,
            )

        # 일정: 기본은 원본 행 통째 복사 (이동시간·시각 포함 — 내용 동일하므로 전부 유효).
        # 항공 시각이 바뀐 첫날/마지막날만 도착 예정 시각을 재계산한다.
        # prefetch로 읽기 N+1 제거 + bulk_create로 쓰기 INSERT 묶음
        days = list(old_plan.days.prefetch_related("items"))
        first_no = days[0].day_number if days else None
        last_no = days[-1].day_number if days else None

        for day in days:
            day_row = ItineraryDay.objects.create(
                plan=new_plan, day_number=day.day_number,
                city_name=day.city_name, date=day.date,
            )

            is_first = day.day_number == first_no and resched_first
            is_last = day.day_number == last_no and resched_last
            if is_first or is_last:
                # 새 항공 시각으로 그 날 가용 시간대를 다시 만들고(최초 생성과 같은
                # _day_window 규칙), 같은 장소·같은 순서로 시각만 다시 흘린다
                start_min, end_min = _day_window(
                    new_arrival if is_first else None,
                    new_return_dep if is_last else None,
                )
                day_inputs = [
                    {
                        "place_name": item.place_name,
                        "lat": item.latitude, "lng": item.longitude,
                        "place_detail": item.place_detail,
                        # kind는 place_detail 스냅샷에서 복원 (식사 앵커 유지)
                        "kind": (item.place_detail or {}).get("kind"),
                        "duration_min": item.duration_min,
                        "travel_min_to_next": item.travel_min_to_next,
                        "travel_mode": item.travel_mode,
                    }
                    for item in day.items.all()
                ]
                scheduled = schedule_day_times(day_inputs, start_min, end_min)
                kept = {it["place_name"] for it in scheduled}
                trimmed.extend(
                    it["place_name"] for it in day_inputs if it["place_name"] not in kept)
                ItineraryItem.objects.bulk_create([
                    ItineraryItem(
                        day=day_row, visit_order=order,
                        place_name=it["place_name"],
                        latitude=it["lat"], longitude=it["lng"],
                        place_detail=it["place_detail"],
                        arrival_time=it.get("arrival_time"),
                        duration_min=it.get("duration_min"),
                        travel_min_to_next=it.get("travel_min_to_next"),
                        travel_mode=it.get("travel_mode"),
                    )
                    for order, it in enumerate(scheduled, start=1)
                ])
            else:
                ItineraryItem.objects.bulk_create([
                    ItineraryItem(
                        day=day_row, visit_order=item.visit_order,
                        place_name=item.place_name,
                        latitude=item.latitude, longitude=item.longitude,
                        place_detail=item.place_detail,
                        arrival_time=item.arrival_time,
                        duration_min=item.duration_min, est_cost=item.est_cost,
                        travel_min_to_next=item.travel_min_to_next,
                        travel_mode=item.travel_mode,
                    )
                    for item in day.items.all()
                ])

    return new_plan, trimmed
