"""
trips/views.py - 계획 조회/확정 API

이 파일의 성격
    agents/views.py가 만드는 쪽이라면 여기는 꺼내 보는 쪽
    전부 DB에 저장된 스냅샷만 읽어거 응답함
    저장해 둔 플랜은 언제라도 열 수 있음
"""

import uuid

from django.db.models import ProtectedError
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from trips.models import TripRequest, Plan
# payments.models만 가져온다 (payments.views가 아님) — payments.views는 이미
# trips.views의 _get_my_plan을 가져다 쓰고 있어서, 여기서 payments.views를
# 반대로 가져오면 순환 임포트가 생긴다. models끼리는 서로 참조 안 하니 안전하다.
from payments.models import Payment

from django.core.cache import cache
from rest_framework import serializers
from drf_spectacular.utils import extend_schema
from urllib.parse import quote

from agents.edit_router import route_edit_request
from agents.tasks import run_local_edit


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def trip_list(request):
    """
    내 여행 계획 목록
    
    Response 200:
        {"trips": [{"request_id:.., "departure":.., "destinations":[..],
         "start_date":.., "end_date":.., "total_budget":..,
         "plan_id":.., "status":.., "created_at":..}, ...]}
    """

    # filter(user=...): 반드시 내 것만
    # prefetch_related: 목적지/플랜을 미리 한꺼번에 가져옴
    requests_qs = (
        TripRequest.objects
        .filter(user=request.user)
        .order_by("-created_at")    # 최신 요청부터
        .prefetch_related("destinations", "plans")
    )

    trips = []
    for tr in requests_qs:
        # plans는 모델 Meta에서 created_at 순 정렬 -> .last() = 최신 버전
        latest_plan = tr.plans.last()
        trips.append({
            "request_id": tr.id,
            "departure": tr.departure,
            "destinations": [d.city_name for d in tr.destinations.all()],
            "start_date": tr.start_date,
            "end_date": tr.end_date,
            "total_budget": tr.total_budget,
            "plan_id": latest_plan.id if latest_plan else None,
            "status": latest_plan.status if latest_plan else None,
            "created_at": tr.created_at,
        })

    return Response({"trips": trips})


def _get_my_plan(request, plan_id):
    """
    plan_id로 내 플랜을 찾는 공용 헬퍼
    
    request__user: 밑줄 2개는 Django의 관계 건너 조건 문법
    plan.request.user가 나인 것만
    """
    try:
        return Plan.objects.select_related("request").get(
            id=plan_id, request__user=request.user
        )
    except Plan.DoesNotExist:
        return None
    

def _flight_booking_url(trip_request):
    """
    Google Flights 검색 URL 조립
    우리 항공 데이터의 출처가 Google Flights(SerpApi)라서, 같은 조건으로 검색 URL을 만들면 사용자가 데모에서 본 그 항공편을 실제 결제 화면에서 다시 만남
    q= 파라미터는 자연어 질의를 받아줌 (Flights from ICN to KIX on ...)
    """

    dest = trip_request.destinations.first()    # MVP: 첫 목적지 기준 왕복
    if not dest or not dest.iata_code or not trip_request.origin_iata:
        return None     # 공항 코드가 없으면 링크 생략 (없는 것보다 틀린 링크가 나쁨)
    
    query = (
        f"Flights from {trip_request.origin_iata} to {dest.iata_code} "
        f"on {trip_request.start_date} through {trip_request.end_date}"
    )
    return f"https://www.google.com/travel/flights?q={quote(query)}"


def _hotel_booking_url(hotel, trip_request):
    """
    Google 호텔 검색 URL 조립
    호텔 이름+도시로 검색하면 구글이 예약 가능한 사이트들을 모아 보여줌
    이름이 hotel_id 그대로인 경우(정적 정보 미조회)는 링크 품질이 없으므로 생략
    """

    # 이름이 ID처럼 생겼으면(lp로 시작하는 LiteAPI ID 패턴) 검색어로 무의미
    if not hotel.name or hotel.name == hotel.liteapi_hotel_id:
        return None
    
    dest = trip_request.destinations.first()
    city = (dest.city_en or dest.city_name) if dest else ""
    return f"https://www.google.com/travel/search?q={quote(f'{hotel.name} {city}')}"


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def plan_detail(request, plan_id):
    """
    플랜 상세 - 배분/설명문/선택 항공 및 숙소/일자별 일정 전부

    payment(결제 상세)/bookings(예약 이력)를 합치면 "결제→예약" 흐름을
    프론트가 이 API 하나로 재구성할 수 있다. payment는 결제 완료(DONE) 건이
    없으면 null, bookings는 시도 자체가 없었으면 빈 배열([]).

    Response 200: 아래 조립 참조 / Response 404: 없거나 남의 플랜
    """

    plan = _get_my_plan(request, plan_id)
    if plan is None:
        return Response(
            {"error": "플랜을 찾을 수 없습니다."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # OneToOne 역참조는 행이 없으면 예외를 던지는데, 그 예외가 AttributeError의 자식이라 getattr으로 받으면 깔끔하게 None 처리됨
    flight = getattr(plan, "flight", None)
    hotel = getattr(plan, "hotel", None)

    # 결제 완료(DONE) 건만 찾는다 — READY(결제창 열기 전)/ABORTED(승인 실패)는
    # "결제됐다"고 보여주면 안 되는 상태라 제외. 같은 플랜에 DONE이 두 개 이상
    # 생길 일은 없다 (payment_prepare가 이미 결제완료된 플랜은 막음, trips/views.py
    # 밖 payments/views.py:63-65 참고) — 그래도 혹시 몰라 .first()로 안전하게.
    payment = plan.payments.filter(status=Payment.Status.DONE).first()

    return Response({
        "plan_id": plan.id,
        "request_id": plan.request_id,
        "status": plan.status,
        "allocation": plan.allocation,
        "narrative": plan.narrative,
        # 결제 상세 — 프론트가 "결제 완료" 화면을 나중에 다시 열어도(새로고침 등)
        # 이 필드 하나로 얼마를/언제/무엇으로 냈는지 그릴 수 있게 하려고 추가.
        # 결제 전이거나 승인 실패면 payment가 None이라 전체가 null로 내려감.
        "payment": {
            "status": payment.status,
            "amount": payment.amount,
            "method": payment.method,
            "order_name": payment.order_name,
            "approved_at": payment.approved_at,
        } if payment else None,
        "flight": {
            "airline": flight.airline,
            "price_krw": flight.price_krw,
            "utility": flight.utility,
            "booking_url": _flight_booking_url(plan.request),
            # slices는 agents/flight/flight.py의 make_candidate()가 채운 raw 값
            "departure_time": (flight.slices or {}).get("departure_time"),
            "arrival_time": (flight.slices or {}).get("arrival_time"),
            "duration_min": (flight.slices or {}).get("duration_min"),
            "stops": (flight.slices or {}).get("stops"),
        } if flight else None,
        "hotel": {
            "liteapi_hotel_id": hotel.liteapi_hotel_id,
            "name": hotel.name,
            "price_krw": hotel.price_krw,
            "utility": hotel.utility,
            "booking_url": _hotel_booking_url(hotel, plan.request),
            "stars": hotel.stars,
            "latitude": hotel.latitude,
            "longitude": hotel.longitude,
        } if hotel else None,
        "days": [
            {
                "day_number": day.day_number,
                "city_name": day.city_name,
                "date": day.date,
                "items": [
                    {
                        "visit_order": item.visit_order,
                        "place_name": item.place_name,
                        "latitude": item.latitude,
                        "longitude": item.longitude,
                        "place_detail": item.place_detail,
                        "travel_min_to_next": item.travel_min_to_next,
                        "travel_mode": item.travel_mode,
                    }
                    for item in day.items.all()
                ],
            }
            for day in plan.days.all()  # Meta ordering으로 일차순 보장
        ],
        # 예약 이력 (샌드박스) — 성공/실패 재시도까지 시간순으로
        "bookings": [
            {
                "status": b.status,
                "booking_id": b.booking_id,
                "confirmation": b.confirmation,
                "guest_name": b.guest_name,
                "created_at": b.created_at,
            }
            for b in plan.bookings.all()
        ],
        "created_at": plan.created_at,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def plan_confirm(request, plan_id):
    """
    플랜 확정: draft -> confirmed
    기능명세 규칙 그대로 processing 상태에서는 확정 불가
    
    Response 200: {"plan_id":.., "status": "confirmed"}
    Response 400: draft가 아닌 상태 / Response 404: 없거나 남의 플랜
    """

    plan = _get_my_plan(request, plan_id)
    if plan is None:
        return Response(
            {"error": "플랜을 찾을 수 없습니다."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    if plan.status != Plan.Status.DRAFT:
        return Response(
            {"error": f"확정할 수 없는 상태입니다 (현재: {plan.status}). "
                      "결과 생성이 완료된(draft) 플랜만 확정할 수 있습니다."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    plan.status = Plan.Status.CONFIRMED
    plan.save()

    return Response({"plan_id": plan.id, "status": plan.status})


class PlanEditSerializer(serializers.Serializer):
    """Swagger 문서용 수정 요청 바디 스키마"""
    
    message = serializers.CharField(help_text="자연어 수정 요청 (예: 2일차는 좀 여유롭게 해줘)")


@extend_schema(request=PlanEditSerializer)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def plan_edit(request, plan_id):
    """
    대화형 수정 접수: 요청을 분류하고, 국소수정이면 비동기 실행
    
    Response 202 (국소수정): {"category", "reason", "run_id", "task_id"}
    -> GET /api/v1/agents/runs/{run_id}/ 폴링, result.new_plan_id로 새 버전 조회
    Response 200 (예산영향/재계획): {"category", "reason", "supported": false, "message"}
    Response 400/404
    """

    plan = _get_my_plan(request, plan_id)
    if plan is None:
        return Response({"error": "플랜을 찾을 수 없습니다."},
                        status=status.HTTP_404_NOT_FOUND)
        
    message = request.data.get("message", "").strip()
    if not message:
        return Response({"error": "message 필드가 필요합니다."},
                        status=status.HTTP_400_BAD_REQUEST)
    
    if plan.status == Plan.Status.PROCESSING:
        return Response({"error": "생성 중인 플랜은 수정할 수 없습니다."},
                        status=status.HTTP_400_BAD_REQUEST)
    
    run_id = uuid.uuid4().hex[:12]
    routed = route_edit_request(run_id, message)    # 분류

    if routed["category"] == "재계획":
        # 새 버전 자리(processing)를 먼저 만들어 목록에 "생성 중"으로 보이게
        from agents.tasks import run_replan
        new_plan = Plan.objects.create(request=plan.request, edit_request=message)
        async_result = run_replan.delay(run_id, plan.id, new_plan.id, message)
        cache.set(f"run:{run_id}",
                  {"task_id": async_result.id, "plan_id": new_plan.id},
                  timeout=60 * 60)
        return Response({
            "category": routed["category"],
            "reason": routed["reason"],
            "run_id": run_id,
            "task_id": async_result.id,
            "plan_id": new_plan.id,
            "status": "accepted",
        }, status=status.HTTP_202_ACCEPTED)

    if routed["category"] == "예산영향":
        # 숙소 재검색 + 재배분 (항공/일정 고정) — 라우터 3갈래의 마지막 실행
        # 이 편집은 "기존 항공을 고정한 채" 숙소만 다시 고르는 방식이라,
        # 원본 플랜에 애초에 선택된 항공이 없으면(예산 부족으로 무선택이었던 경우)
        # 고정할 대상이 없어 재배분이 무조건 no_flights로 끝난다. 그 전에 막는다.
        if getattr(plan, "flight", None) is None:
            return Response(
                {"error": "이 플랜은 선택된 항공이 없어 예산영향 수정을 진행할 수 없습니다. "
                          "예산을 늘리거나 조건을 바꿔 처음부터 다시 만들어 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from agents.tasks import run_budget_edit
        new_plan = Plan.objects.create(request=plan.request, edit_request=message)
        async_result = run_budget_edit.delay(run_id, plan.id, new_plan.id, message)
        cache.set(f"run:{run_id}",
                  {"task_id": async_result.id, "plan_id": new_plan.id},
                  timeout=60 * 60)
        return Response({
            "category": routed["category"],
            "reason": routed["reason"],
            "run_id": run_id,
            "task_id": async_result.id,
            "plan_id": new_plan.id,
            "status": "accepted",
        }, status=status.HTTP_202_ACCEPTED)

    if routed["category"] != "국소수정":
        # 알 수 없는 분류에 대한 안전망 (정상 흐름에서는 도달하지 않음)
        return Response({
            "category": routed["category"],
            "reason": routed["reason"],
            "supported": False,
            "message": f"'{routed['category']}' 분류를 처리할 수 없습니다.",
        })
    
    async_result = run_local_edit.delay(run_id, plan.id, message)
    cache.set(f"run:{run_id}", {"task_id": async_result.id, "plan_id": plan.id},
              timeout=60 * 60)
    
    return Response({
        "category": routed["category"],
        "reason": routed["reason"],
        "run_id": run_id,
        "task_id": async_result.id,
        "status": "accepted",
    }, status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def plan_rollback(request, plan_id):
    """
    롤백: 이 plan_id의 버전을 복사해 '새 최신 버전(draft)'으로 만듦
    LLM/외부 API 호출이 없어 즉시 응답
    
    Response 200: {"new_plan_id":.., "copied_from":.., "status": "draft"}
    Response 404: 없거나 남의 플랜
    """

    src_plan = _get_my_plan(request, plan_id)
    if src_plan is None:
        return Response({"error": "플랜을 찾을 수 없습니다."},
                        status=status.HTTP_404_NOT_FOUND)
        
    if src_plan.status == Plan.Status.PROCESSING:
        return Response({"error": "생성 중인 플랜은 롤백 대상이 될 수 없습니다."},
                        status=status.HTTP_400_BAD_REQUEST)
    
    from trips.services import copy_plan_version
    new_plan = copy_plan_version(
        src_plan, edit_request_note=f"플랜 #{src_plan.id} 버전으로 롤백"
    )

    return Response({
        "new_plan_id": new_plan.id,
        "copied_from": src_plan.id,
        "status": new_plan.status,
    })


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def trip_delete(request, request_id):
    """
    여행 요청 삭제 - 하위 목적지/플랜(전 버전)/일정이 FK CASCADE로 함께 삭제
    
    하드 삭제(복구 불가)
    Response 204: 삭제 완료
    Response 404: 없거나 남의 요청
    """

    try:
        trip_request = TripRequest.objects.get(id=request_id, user=request.user)
    except TripRequest.DoesNotExist:
        return Response({"error": "여행 요청을 찾을 수 없습니다."},
                        status=status.HTTP_404_NOT_FOUND)
    
    try:
        trip_request.delete()   # CASCADE: destinations, plans, flights, hotels, days, items
    except ProtectedError:
        # 결제 기록(PROTECT FK)이 달린 여행은 강제 삭제 불가 — 돈이 얽힌 데이터 보호
        return Response(
            {"error": "결제 이력이 있는 여행은 삭제할 수 없습니다."},
            status=status.HTTP_409_CONFLICT,
        )

    # 204 No Content = "성공했고 돌려줄 내용이 없음"
    return Response(status=status.HTTP_204_NO_CONTENT)


class PlanBookSerializer(serializers.Serializer):
    """Swagger 문서용 예약 요청 바디 스키마."""
    first_name = serializers.CharField(help_text="게스트 이름 (영문, 예: MINJAE)")
    last_name = serializers.CharField(help_text="게스트 성 (영문, 예: HEON)")
    email = serializers.CharField(help_text="예약 확인 메일 주소")


@extend_schema(request=PlanBookSerializer)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def plan_book(request, plan_id):
    """
    숙소 예약 접수 (샌드박스) — 에이전트가 MCP 예약 도구로 예약을 수행한다.

    확정(confirmed)된 플랜만 예약 가능 — 상태 수명주기의 마지막 단계:
    processing → draft → confirmed → (예약)

    Response 202: {"run_id", "task_id"} → GET /agents/runs/{run_id}/ 폴링,
                  result에 booking_status/booking_id/confirmation
    Response 400/404
    """
    plan = _get_my_plan(request, plan_id)
    if plan is None:
        return Response({"error": "플랜을 찾을 수 없습니다."},
                        status=status.HTTP_404_NOT_FOUND)

    if plan.status != Plan.Status.CONFIRMED:
        return Response(
            {"error": f"확정된 플랜만 예약할 수 있습니다 (현재: {plan.status}). "
                      "먼저 confirm을 호출하세요."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if getattr(plan, "hotel", None) is None:
        return Response({"error": "이 플랜에는 선택된 숙소가 없습니다."},
                        status=status.HTTP_400_BAD_REQUEST)

    # 결제 관문: 결제 완료(DONE) 없이는 예약 불가 — 결제 기능이 생긴 순간부터
    # 이 직접 예약 경로는 "무결제 우회"가 되므로 여기서 차단한다.
    # (결제 완료 시에는 confirm이 예약을 자동 접수하므로, 이 API는 사실상
    #  재시도/수동 예약용 보조 경로가 된다)
    from payments.models import Payment
    if not plan.payments.filter(status=Payment.Status.DONE).exists():
        return Response(
            {"error": "결제가 완료되지 않은 플랜입니다. 먼저 결제를 진행해 주세요."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    first_name = request.data.get("first_name", "").strip()
    last_name = request.data.get("last_name", "").strip()
    email = request.data.get("email", "").strip()
    if not first_name or not last_name or not email:
        return Response({"error": "first_name, last_name, email이 모두 필요합니다."},
                        status=status.HTTP_400_BAD_REQUEST)

    from agents.tasks import run_booking
    run_id = uuid.uuid4().hex[:12]
    async_result = run_booking.delay(run_id, plan.id, first_name, last_name, email)
    cache.set(f"run:{run_id}", {"task_id": async_result.id, "plan_id": plan.id},
              timeout=60 * 60)

    return Response({
        "run_id": run_id,
        "task_id": async_result.id,
        "status": "accepted",
    }, status=status.HTTP_202_ACCEPTED)
