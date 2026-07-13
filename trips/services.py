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
    Flight, Hotel, ItineraryDay, ItineraryItem,
)


def create_request_and_plan(user, fields, raw_parsed=None):
    """
    확정된 파싱 결과로 요청/목적지/빈 플랜을 만듦
    
    반환: (trip_request, plan) 튜플
    """

    dates = fields.get("dates") or {}
    origin = fields.get("origin") or {}
    pax = fields.get("pax") or {}

    # 도시별 nights가 파싱에 없을 때를 대비한 전체 박 수
    start = date.fromisoformat(dates["start"])
    end = date.fromisoformat(dates["end"])
    total_nights = (end - start).days

    with transaction.atomic():
        trip_request = TripRequest.objects.create(
            user=user,
            departure=origin.get("city") or "서울",
            origin_iata=origin.get("iata"),
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
                nights=d.get("night") or total_nights,
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
                name=hotel_id,
                price_krw=sel_hotel.get("krw") or 0,
                price_original=sel_hotel.get("krw") or 0,
                currency="KRW",
                utility=sel_hotel.get("utility"),
                utility_reasons=raw.get("reasons"),
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