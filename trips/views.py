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

from agents.budget import allocate_budget
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
        # plans는 모델 Meta에서 created_at 순 정렬 -> 마지막 원소 = 최신 버전.
        # (.last()를 쓰면 prefetch 캐시를 무시하고 행마다 쿼리를 새로 날린다 —
        #  N+1이었음. list()로 캐시를 그대로 써서 목록 전체가 쿼리 3번에 끝남)
        plans = list(tr.plans.all())
        latest_plan = plans[-1] if plans else None
        trips.append({
            "request_id": tr.id,
            "title": tr.title,      # 사용자가 붙인 이름 (빈 값이면 프론트가 목적지로 표시)
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


def _candidates_ui(plan, flight_row, hotel_row):
    """
    저장된 후보 스냅샷(Plan.candidates)을 비교 UI가 바로 쓸 평평한 형태로 변환

    selected: 현재 이 플랜이 선택 중인 후보인지 — 프론트가 "현재 선택" 배지를 붙일 근거.
    (항공은 항공사명+가격, 숙소는 LiteAPI id+가격으로 대조 — 저장 시점 값 그대로라 안전)
    """
    cands = plan.candidates or {}
    flights, hotels = [], []
    for i, o in enumerate(cands.get("flights") or []):
        raw = o.get("raw") or {}
        flights.append({
            "index": i,
            "airline": o.get("label"),
            "price_krw": o.get("krw"),
            "utility": o.get("utility"),
            "utility_reasons": o.get("utility_reasons"),
            "departure_time": raw.get("departure_time"),
            "arrival_time": raw.get("arrival_time"),
            "duration_min": raw.get("duration_min"),
            "stops": raw.get("stops"),
            "selected": bool(flight_row and flight_row.airline == o.get("label")
                             and flight_row.price_krw == (o.get("krw") or 0)),
        })
    for i, o in enumerate(cands.get("hotels") or []):
        raw = o.get("raw") or {}
        hotels.append({
            "index": i,
            "name": raw.get("name") or str(o.get("label")),
            "price_krw": o.get("krw"),
            "utility": o.get("utility"),
            "utility_reasons": raw.get("reasons"),
            "stars": raw.get("star_rating"),
            "address": raw.get("address"),
            # 상세 펼침의 미니 지도용 좌표 (스냅샷에 이미 저장돼 있음)
            "latitude": raw.get("latitude"),
            "longitude": raw.get("longitude"),
            "selected": bool(hotel_row and hotel_row.liteapi_hotel_id == str(o.get("label"))
                             and hotel_row.price_krw == (o.get("krw") or 0)),
        })
    return {"flights": flights, "hotels": hotels}


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

    # 결제 완료(DONE) 건만, 대상별로 찾는다 — 이제 한 플랜에 숙소/항공 결제가
    # 각각 존재할 수 있으므로 (READY/ABORTED는 "결제됐다"로 보여주면 안 되니 제외)
    payment = plan.payments.filter(
        status=Payment.Status.DONE, target=Payment.Target.HOTEL).first()
    flight_payment = plan.payments.filter(
        status=Payment.Status.DONE, target=Payment.Target.FLIGHT).first()

    def _payment_dict(p):
        """결제 상세 공통 조립 — 새로고침 후에도 결제 화면을 그릴 수 있게"""
        return {
            "status": p.status,
            "amount": p.amount,
            "method": p.method,
            "order_name": p.order_name,
            "approved_at": p.approved_at,
        } if p else None

    # 왕복 노선 표기 (ICN → FUK): 요청의 출발/첫 목적지 공항 코드
    first_dest = plan.request.destinations.first()
    route = (f"{plan.request.origin_iata} → {first_dest.iata_code}"
             if plan.request.origin_iata and first_dest and first_dest.iata_code
             else None)

    # ── 대화 복원 (스냅샷 재구성) ────────────────────────────────────────
    # 대화 원본은 프론트 메모리에만 있어서 새로고침/재방문 시 사라진다.
    # DB에 남아 있는 재료(원문 raw_input + 버전별 edit_request + allocation)로
    # 대화 흐름을 재구성해 내려준다 — 마이페이지에서 계획을 다시 열 때 사용.
    conversation = []
    raw = plan.request.raw_input or {}
    if raw.get("original_message"):
        conversation.append({"role": "user", "text": raw["original_message"]})
    versions = plan.request.plans.order_by("created_at")
    for idx, p in enumerate(versions, start=1):
        if p.edit_request:      # v2부터 존재 (그 버전을 만든 수정 요청)
            conversation.append({"role": "user", "text": p.edit_request})
        total = (p.allocation or {}).get("total_cost")
        # 봇 답변: 저장된 AI 요약(edit_summary)이 있으면 원문 그대로 —
        # "2일차에 ...을 옮겼습니다" 같은 실제 답변이 복원된다.
        # 없으면(edit_summary 도입 전 버전) 일반 문구로 재구성
        if idx == 1:
            bot_text = "계획서를 완성했습니다."
        else:
            bot_text = p.edit_summary or "요청을 반영해 계획서를 갱신했습니다."
        if total:
            bot_text += f" 총 {total:,}원."
        conversation.append({"role": "bot", "text": bot_text})
        if p.id == plan.id:     # 지금 보고 있는 버전까지만 (이후 버전 대화는 제외)
            break

    return Response({
        "plan_id": plan.id,
        "request_id": plan.request_id,
        "status": plan.status,
        # 프론트가 days.length로 여행 기간(박수)을 추정하다 생긴 버그(Bug#96) 대응.
        # days 배열은 귀국일이 빠진 "일정이 있는 날"만 담기 때문에 총 여행일수와 다름 —
        # 실제 기간은 항상 이 날짜로 계산해야 함
        "start_date": plan.request.start_date,
        "end_date": plan.request.end_date,
        "allocation": plan.allocation,
        "narrative": plan.narrative,
        "payment": _payment_dict(payment),                # 숙소 결제
        "flight_payment": _payment_dict(flight_payment),  # 항공 결제
        "conversation": conversation,                     # 복원된 대화 (위 재구성)
        "flight": {
            "airline": flight.airline,
            "price_krw": flight.price_krw,
            "utility": flight.utility,
            "booking_url": _flight_booking_url(plan.request),
            "route": route,     # "ICN → FUK" (왕복)
            # slices는 agents/flight/flight.py의 make_candidate()가 채운 raw 값
            "departure_time": (flight.slices or {}).get("departure_time"),
            "arrival_time": (flight.slices or {}).get("arrival_time"),
            "duration_min": (flight.slices or {}).get("duration_min"),
            "stops": (flight.slices or {}).get("stops"),
            # 오는 편(귀국편) 실제 시각 - 조회에 실패했으면 null (구버전 플랜도 null)
            "return_departure_time": (flight.slices or {}).get("return_departure_time"),
            "return_arrival_time": (flight.slices or {}).get("return_arrival_time"),
        } if flight else None,
        "hotel": {
            "liteapi_hotel_id": hotel.liteapi_hotel_id,
            "name": hotel.name,
            "price_krw": hotel.price_krw,
            "utility": hotel.utility,
            # 만족도 근거(성급/테마 가점 등) — 상세 정보 펼침에서 표시
            "utility_reasons": hotel.utility_reasons,
            "booking_url": _hotel_booking_url(hotel, plan.request),
            "stars": hotel.stars,
            "latitude": hotel.latitude,
            "longitude": hotel.longitude,
            # LiteAPI 응답 스냅샷에 주소가 있으면 노출 (없으면 null — FE가 숨김)
            "address": (hotel.detail or {}).get("address"),
        } if hotel else None,
        # 검색 당시 후보 목록 (비교·재선택 UI용, 가격은 검색 시점 기준)
        "candidates": _candidates_ui(plan, flight, hotel),
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
                        "arrival_time": item.arrival_time.strftime("%H:%M") if item.arrival_time else None,
                        "duration_min": item.duration_min,
                        "travel_min_to_next": item.travel_min_to_next,
                        "travel_mode": item.travel_mode,
                    }
                    for item in day.items.all()     # prefetch 캐시 사용 (추가 쿼리 0)
                ],
            }
            # prefetch_related("items"): 날짜마다 items 쿼리가 나가던 N+1 제거
            # (11일 여행이면 12쿼리 -> 2쿼리). Meta ordering으로 일차순은 그대로 보장
            for day in plan.days.prefetch_related("items")
        ],
        # 예약 이력 (샌드박스) — 성공/실패 재시도까지 시간순으로
        "bookings": [
            {
                "kind": b.kind,     # hotel(숙소) / flight(항공 mock 발권)
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


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def plan_select_candidate(request, plan_id):
    """
    저장된 후보 목록에서 항공/숙소를 직접 골라 교체 (멘토 피드백: 비교·선택 UI)

    핵심: 재검색 없이 검색 당시 후보 스냅샷으로 "결정론 재배분"만 수행 —
    외부 API·LLM 호출이 0회라 동기(즉시) 응답이 가능하다.
    고른 쪽은 그 후보로 고정, 반대쪽은 현재 선택 유지, 활동비 등급만
    남은 예산에 맞게 엔진(allocate_budget)이 다시 정한다.
    결과는 기존 수정 흐름과 동일하게 "새 버전"으로 저장 (원본 보존).

    Request:  {"kind": "flight" | "hotel", "index": 0}
    Response 200: {"new_plan_id": .., "summary": "숙소를 '...'(으)로 변경했습니다."}
    Response 400: 확정된 계획 / 잘못된 kind·index
    """
    from trips.services import save_budget_edited_version

    plan = _get_my_plan(request, plan_id)
    if plan is None:
        return Response({"error": "플랜을 찾을 수 없습니다."},
                        status=status.HTTP_404_NOT_FOUND)
    if plan.status == Plan.Status.CONFIRMED:
        return Response({"error": "확정된 계획은 후보를 변경할 수 없습니다."},
                        status=status.HTTP_400_BAD_REQUEST)

    kind = request.data.get("kind")
    if kind not in ("flight", "hotel"):
        return Response({"error": "kind는 flight 또는 hotel이어야 합니다."},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        index = int(request.data.get("index"))
    except (TypeError, ValueError):
        return Response({"error": "index가 올바르지 않습니다."},
                        status=status.HTTP_400_BAD_REQUEST)

    pool = (plan.candidates or {}).get(
        "flights" if kind == "flight" else "hotels") or []
    if not 0 <= index < len(pool):
        return Response({"error": "해당 후보를 찾을 수 없습니다."},
                        status=status.HTTP_400_BAD_REQUEST)
    chosen = pool[index]

    # 반대쪽은 현재 선택을 "유일 옵션"으로 넣는다 — 예산영향 수정과 같은 수법.
    # 엔진은 (고른 후보 + 기존 반대쪽) 조합을 기준으로 활동비 등급만 재조정한다.
    selection = (plan.allocation or {}).get("selection") or {}
    cur_flight, cur_hotel = selection.get("flight"), selection.get("hotel")
    flight_pool = [chosen] if kind == "flight" else ([cur_flight] if cur_flight else [])
    hotel_pool = [chosen] if kind == "hotel" else ([cur_hotel] if cur_hotel else [])

    tr = plan.request
    days = (tr.end_date - tr.start_date).days + 1
    allocation = allocate_budget(
        total_budget=tr.total_budget,
        flight_options=flight_pool,
        hotel_options=hotel_pool,
        days=days,
        travelers=tr.adult + tr.kid,
    )
    if allocation.get("status") in ("no_flights", "no_hotels"):
        # 방어: 저장 스냅샷이라 가격이 항상 있지만, 만약을 위해
        return Response({"error": "이 후보로는 배분할 수 없습니다."},
                        status=status.HTTP_400_BAD_REQUEST)

    kind_ko = "항공" if kind == "flight" else "숙소"
    label = ((chosen.get("raw") or {}).get("name")
             or chosen.get("label") or "?")
    summary = f"{kind_ko}을 '{label}'(으)로 변경했습니다."
    if allocation.get("status") == "insufficient":
        summary += " 다만 이 조합은 예산을 초과해요 — 다른 후보를 고르거나 예산을 조정해 보세요."

    new_plan = Plan.objects.create(
        request=tr,
        edit_request=f"[후보 선택] {kind_ko} 변경 → {label}",
    )
    # 선택/일정/후보 스냅샷을 새 버전으로 저장 (status는 안에서 DRAFT로 전환)
    save_budget_edited_version(plan, new_plan, allocation, None)
    new_plan.edit_summary = summary
    new_plan.save(update_fields=["edit_summary"])

    return Response({"new_plan_id": new_plan.id, "summary": summary})


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def trip_update_title(request, request_id):
    """
    계획 이름(제목) 수정 — 목록에서 연필 버튼으로 이름을 바꿀 때 사용

    Request:  {"title": "샌프란시스코 출장"}  (빈 문자열 = 이름 제거 → 목적지 표시로 복귀)
    Response 200: {"request_id": .., "title": ".."}
    Response 400: 60자 초과
    Response 404: 없거나 남의 요청
    """

    try:
        trip_request = TripRequest.objects.get(id=request_id, user=request.user)
    except TripRequest.DoesNotExist:
        return Response({"error": "여행 요청을 찾을 수 없습니다."},
                        status=status.HTTP_404_NOT_FOUND)

    # strip(): 앞뒤 공백만 있는 입력은 "이름 없음"과 동일하게 취급
    title = (request.data.get("title") or "").strip()
    if len(title) > 60:
        return Response({"error": "이름은 60자 이내로 입력해 주세요."},
                        status=status.HTTP_400_BAD_REQUEST)

    trip_request.title = title
    trip_request.save(update_fields=["title", "updated_at"])
    return Response({"request_id": trip_request.id, "title": trip_request.title})


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

    # 확정된 계획은 삭제 불가 — 예약/결제로 이어질 수 있는 상태라서
    # (미확정 draft만 정리 가능하게 한다는 정책. 결제 이력 보호는 아래 PROTECT가 이중 방어)
    latest_plan = trip_request.plans.order_by("created_at").last()
    if latest_plan and latest_plan.status == Plan.Status.CONFIRMED:
        return Response(
            {"error": "확정된 계획은 삭제할 수 없습니다."},
            status=status.HTTP_400_BAD_REQUEST,
        )

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
    # 숙소 예약이므로 "숙소" 결제 완료를 요구 (항공 결제만으로는 통과 불가)
    if not plan.payments.filter(status=Payment.Status.DONE,
                                target=Payment.Target.HOTEL).exists():
        return Response(
            {"error": "숙소 결제가 완료되지 않은 플랜입니다. 먼저 결제를 진행해 주세요."},
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


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def plan_ticket_flight(request, plan_id):
    """
    항공 발권 접수 (자체 mock 공급자) — 에이전트가 발권 MCP 도구로 수행한다.

    실제 항공 발권·정산은 판매자 라이선스가 필요해 구현할 수 없으므로,
    "판매자인 척" 하는 mock 서버로 절차(운임 재확인→좌석 점유→발권)를 증명한다.
    시뮬레이션이므로 결제 관문 없이 확정(confirmed) 플랜이면 접수한다.

    Body: {"lead_passenger": 대표 탑승자 이름 (없으면 닉네임/이메일 앞부분)}
    Response 202: {"run_id", "task_id"} → GET /agents/runs/{run_id}/ 폴링,
                  result에 ticket_status/pnr
    Response 400/404
    """
    plan = _get_my_plan(request, plan_id)
    if plan is None:
        return Response({"error": "플랜을 찾을 수 없습니다."},
                        status=status.HTTP_404_NOT_FOUND)

    if plan.status != Plan.Status.CONFIRMED:
        return Response(
            {"error": f"확정된 플랜만 발권할 수 있습니다 (현재: {plan.status}). "
                      "먼저 confirm을 호출하세요."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if getattr(plan, "flight", None) is None:
        return Response({"error": "이 플랜에는 선택된 항공이 없습니다."},
                        status=status.HTTP_400_BAD_REQUEST)

    # 대표 탑승자: 요청에 없으면 닉네임 → 이메일 앞부분 순서로 대신 사용
    lead = (request.data.get("lead_passenger") or "").strip()
    if not lead:
        lead = (getattr(request.user, "nickname", "") or
                request.user.email.split("@")[0])

    from agents.tasks import run_flight_ticketing
    run_id = uuid.uuid4().hex[:12]
    async_result = run_flight_ticketing.delay(
        run_id, plan.id, lead, request.user.email)
    cache.set(f"run:{run_id}", {"task_id": async_result.id, "plan_id": plan.id},
              timeout=60 * 60)

    return Response({
        "run_id": run_id,
        "task_id": async_result.id,
        "status": "accepted",
    }, status=status.HTTP_202_ACCEPTED)
