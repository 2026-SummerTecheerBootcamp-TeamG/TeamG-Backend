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
        # Plan 본체: 배분 스냅샷 + 내러티브 + 상태 전환
        plan.allocation = allocation
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
            for item in day.get("items") or []:
                ItineraryItem.objects.create(
                    day=day_row,
                    visit_order=item.get("visit_order"),
                    place_name=item.get("place_name") or "?",
                    latitude=item.get("lat"),
                    longitude=item.get("lng"),
                    place_detail=item.get("place_detail"),
                    travel_min_to_next=item.get("travel_min_to_next"),
                    travel_mode=item.get("travel_mode"),
                )
            
    return plan


def load_day_plan(plan):
    """
    저장된 플랜의 일정을 파이프라인 day_plan과 같은 dict 구조로 되살림
    (편집기 입력 + 재조립 원본 두 용도)
    """
    
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
                    "travel_min_to_next": item.travel_min_to_next,
                    "travel_mode": item.travel_mode,
                }
                for item in day.items.all()
            ],
        }
        for day in plan.days.all()
    ]


def create_edited_version(old_plan, edited, edit_request):
    """
    편집 결과로 새 버전을 만듦
    
    핵심 원칙: LLM은 이름만 골랐고, 데이터는 전부 원본 행에서 가져옴
    - 원본에 없는 이름 -> 제외
    - 이름 순서가 원본과 같은 날 -> 행을 통째로 복사
    - 순서/구성이 바뀐 날 -> 이동시간은 null
    """

    original = load_day_plan(old_plan)

    # 이름 -> 원본 item dict 조회표
    item_by_name = {}
    for d in original:
        for item in d["items"]:
            item_by_name[item["place_name"]] = item
    original_names = {d["day"]: [i["place_name"] for i in d["items"]] for d in original}
    city_by_day = {d["day"]: d.get("city") for d in original}

    dropped = []

    with transaction.atomic():
        # 새 plan 버전
        new_plan = Plan.objects.create(
            request=old_plan.request,
            status=Plan.Status.DRAFT,
            allocation=old_plan.allocation,     # 국소수정 = 예산 불변이므로 복사
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
                    dropped.append(name)

            # 이름 순서가 원본과 완전히 같으면 = 변경 없는 날 -> 이동시간 보존
            unchanged = valid_names == original_names.get(day_number, [])

            day_row = ItineraryDay.objects.create(
                plan=new_plan, day_number=day_number,
                city_name=city_by_day.get(day_number),
                date=start_date + timedelta(days=day_number - 1),
            )
            for order, name in enumerate(valid_names, start=1):
                src = item_by_name[name]
                ItineraryItem.objects.create(
                    day=day_row, visit_order=order,
                    place_name=src["place_name"],
                    latitude=src["lat"], longitude=src["lng"],
                    place_detail=src["place_detail"],
                    # 변경된 날은 동선이 달라져 기존 이동시간이 무의미
                    travel_min_to_next=src["travel_min_to_next"] if unchanged else None,
                    travel_mode=src["travel_mode"] if unchanged else None,
                )

    return new_plan, dropped


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
        for day in src_plan.days.all():
            day_row = ItineraryDay.objects.create(
                plan=new_plan, day_number=day.day_number,
                city_name=day.city_name, date=day.date,
            )
            for item in day.items.all():
                ItineraryItem.objects.create(
                    day=day_row, visit_order=item.visit_order,
                    place_name=item.place_name,
                    latitude=item.latitude, longitude=item.longitude,
                    place_detail=item.place_detail,
                    arrival_time=item.arrival_time,
                    duration_min=item.duration_min, est_cost=item.est_cost,
                    travel_min_to_next=item.travel_min_to_next,
                    travel_mode=item.travel_mode,
                )

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


def save_booking(plan, guest_first, guest_last, guest_email, booking_data):
    """
    예약 시도 결과를 기록한다 (성공/실패 모두 — 실패도 이력이다).

    booking_data: 오케스트레이터가 booking_confirm 툴 응답에서 수집한 dict
                  (None이면 Claude가 예약 확정까지 도달하지 못한 것)
    """
    data = booking_data or {}
    confirmed = bool(data.get("booking_id"))
    return Booking.objects.create(
        plan=plan,
        status=Booking.Status.CONFIRMED if confirmed else Booking.Status.FAILED,
        booking_id=data.get("booking_id"),
        confirmation=data.get("confirmation"),
        guest_name=f"{guest_first} {guest_last}".strip(),
        guest_email=guest_email,
        detail=data or None,
    )


def save_budget_edited_version(old_plan, new_plan, allocation, explanation):
    """
    예산영향 수정의 새 버전 완성: 배분/숙소는 새것, 항공/일정은 원본 유지.

    왜 항공은 복사인가: 예산영향 수정(예: "숙소 업그레이드")에서 항공은
    이미 확정된 선택이므로 고정 — 재배분 엔진에도 그 1개만 옵션으로 넣었다.
    왜 일정은 복사인가: 숙소가 바뀌어도 방문지 동선은 그대로 (일정 변경은 국소수정 관할).
    """
    selection = allocation.get("selection") or {}

    with transaction.atomic():
        new_plan.allocation = allocation
        # 배분 설명은 allocation과 함께 result로 반환되고, 일정 설명(narrative)은 불변
        new_plan.narrative = old_plan.narrative
        new_plan.status = Plan.Status.DRAFT
        new_plan.save()

        # 항공: 원본 선택 그대로 복사 (고정)
        old_flight = getattr(old_plan, "flight", None)
        if old_flight:
            Flight.objects.create(
                plan=new_plan, airline=old_flight.airline,
                price_krw=old_flight.price_krw, price_original=old_flight.price_original,
                currency=old_flight.currency, utility=old_flight.utility,
                utility_reasons=old_flight.utility_reasons, slices=old_flight.slices,
            )

        # 숙소: 재배분이 고른 새 선택
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

        # 일정: 원본 행 통째 복사 (이동시간 포함 — 내용 동일하므로 전부 유효)
        for day in old_plan.days.all():
            day_row = ItineraryDay.objects.create(
                plan=new_plan, day_number=day.day_number,
                city_name=day.city_name, date=day.date,
            )
            for item in day.items.all():
                ItineraryItem.objects.create(
                    day=day_row, visit_order=item.visit_order,
                    place_name=item.place_name,
                    latitude=item.latitude, longitude=item.longitude,
                    place_detail=item.place_detail,
                    arrival_time=item.arrival_time,
                    duration_min=item.duration_min, est_cost=item.est_cost,
                    travel_min_to_next=item.travel_min_to_next,
                    travel_mode=item.travel_mode,
                )

    return new_plan
