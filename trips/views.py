"""
trips/views.py - 계획 조회/확정 API

이 파일의 성격
    agents/views.py가 만드는 쪽이라면 여기는 꺼내 보는 쪽
    전부 DB에 저장된 스냅샷만 읽어거 응답함
    저장해 둔 플랜은 언제라도 열 수 있음
"""

import uuid

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from trips.models import TripRequest, Plan

from django.core.cache import cache
from rest_framework import serializers
from drf_spectacular.utils import extend_schema

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
    

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def plan_detail(request, plan_id):
    """
    플랜 상세 - 배분/설명문/선택 항공 및 숙소/일자별 일정 전부
    
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

    return Response({
        "plan_id": plan.id,
        "request_id": plan.request_id,
        "status": plan.status,
        "allocation": plan.allocation,
        "narrative": plan.narrative,
        "flight": {
            "airline": flight.airline,
            "price_krw": flight.price_krw,
            "utility": flight.utility,
        } if flight else None,
        "hotel": {
            "liteapi_hotel_id": hotel.liteapi_hotel_id,
            "name": hotel.name,
            "price_krw": hotel.price_krw,
            "utility": hotel.utility,
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

    if routed["category"] != "국소수정":
        # 예산영향/재계획은 분류·안내까지
        return Response({
            "caategory": routed["category"],
            "reason": routed["reason"],
            "supported": False,
            "message": f"'{routed['category']}' 요청은 준비 중입니다. "
                       "일정 조정(순서/개수/여유도) 요청은 바로 처리할 수 있어요.",
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
