"""
payments/views.py - 결제 API (prepare / confirm) + 테스트 페이지

[흐름]
    prepare: 서버가 금액을 결정하고 주문(READY)을 만든다 → 프론트가 결제창을 연다
    confirm: 결제창 완료 후 최종 승인 — 멱등 + 금액 대조 + 토스 승인 +
             ⭐ 예약 에이전트(run_booking) 자동 접수 (결제가 예약의 관문)
"""

import os
import uuid

from django.conf import settings as django_settings
from django.core.cache import cache
from django.db import transaction
from django.http import Http404
from django.shortcuts import render
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from payments.models import Payment
from payments.toss import TossError, confirm_payment
from trips.views import _get_my_plan
from trips.models import Plan


class PrepareSerializer(serializers.Serializer):
    """Swagger 문서용 — 주문 준비 요청 바디."""
    plan_id = serializers.IntegerField(help_text="결제할 확정(confirmed) 플랜 ID")


@extend_schema(request=PrepareSerializer)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def payment_prepare(request):
    """
    주문 준비: 금액은 클라이언트가 아니라 "서버가" 플랜에서 계산한다.
    반환값으로 프론트가 토스 결제창을 연다.

    Response 200: {"order_id", "order_name", "amount", "client_key"}
    Response 400/404/409
    """
    plan_id = request.data.get("plan_id")
    plan = _get_my_plan(request, plan_id) if plan_id else None
    if plan is None:
        return Response({"error": "플랜을 찾을 수 없습니다."},
                        status=status.HTTP_404_NOT_FOUND)

    if plan.status != Plan.Status.CONFIRMED:
        return Response({"error": f"확정된 플랜만 결제할 수 있습니다 (현재: {plan.status})."},
                        status=status.HTTP_400_BAD_REQUEST)

    hotel = getattr(plan, "hotel", None)
    if hotel is None:
        return Response({"error": "이 플랜에는 결제할 숙소가 없습니다."},
                        status=status.HTTP_400_BAD_REQUEST)

    # 중복 결제 방지: 이미 완료된 결제가 있으면 새 주문을 만들지 않는다
    if plan.payments.filter(status=Payment.Status.DONE).exists():
        return Response({"error": "이미 결제가 완료된 플랜입니다."},
                        status=status.HTTP_409_CONFLICT)

    client_key = os.environ.get("TOSS_CLIENT_KEY")
    if not client_key:
        return Response({"error": "결제 설정(TOSS_CLIENT_KEY)이 없습니다."},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    dests = ", ".join(d.city_name for d in plan.request.destinations.all()) or "여행"
    payment = Payment.objects.create(
        plan=plan,
        user=request.user,
        order_id=Payment.generate_order_id(plan.id),
        order_name=f"{dests} 숙소 예약 ({hotel.name})"[:100],
        amount=hotel.price_krw,        # ← 금액의 진실은 서버 계산값 하나뿐
    )

    return Response({
        "order_id": payment.order_id,
        "order_name": payment.order_name,
        "amount": payment.amount,
        "client_key": client_key,      # 프론트 노출용 키 (시크릿키 아님!)
    })


class ConfirmSerializer(serializers.Serializer):
    """Swagger 문서용 — 결제 승인 요청 바디 (successUrl 쿼리 값 그대로)."""
    payment_key = serializers.CharField()
    order_id = serializers.CharField()
    amount = serializers.IntegerField()


@extend_schema(request=ConfirmSerializer)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def payment_confirm(request):
    """
    결제 승인 + 예약 에이전트 자동 접수.

    방어 3층: ① 멱등(재호출 무해) ② 금액 대조(위변조 차단) ③ 토스 승인 실패 기록
    Response 200: {"status": "DONE", "run_id", "task_id"}
    """
    payment_key = request.data.get("payment_key", "").strip()
    order_id = request.data.get("order_id", "").strip()
    amount = request.data.get("amount")
    if not payment_key or not order_id or amount is None:
        return Response({"error": "payment_key, order_id, amount가 모두 필요합니다."},
                        status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        try:
            # select_for_update: 같은 주문의 동시 confirm(더블클릭)을 줄 세움
            payment = (Payment.objects.select_for_update()
                       .select_related("plan", "user")
                       .get(order_id=order_id, user=request.user))
        except Payment.DoesNotExist:
            return Response({"error": "주문을 찾을 수 없습니다."},
                            status=status.HTTP_404_NOT_FOUND)

        # ① 멱등: 이미 완료된 주문이면 그때의 결과를 그대로 반환 (중복 예약 없음)
        if payment.status == Payment.Status.DONE:
            return Response({
                "status": payment.status,
                "run_id": payment.booking_run_id or None,
                "already_confirmed": True,
            })

        # ② 금액 대조 — 결제창을 거치며 금액이 조작됐다면 여기서 끊긴다
        if payment.amount != int(amount):
            return Response({"error": "결제 금액이 주문 금액과 일치하지 않습니다."},
                            status=status.HTTP_400_BAD_REQUEST)

        # ③ 토스 최종 승인
        try:
            result = confirm_payment(payment_key, order_id, payment.amount)
        except TossError as e:
            payment.status = Payment.Status.ABORTED
            payment.raw_response = {"error": str(e)}
            payment.save()
            return Response({"error": f"결제 승인 실패: {e}"},
                            status=status.HTTP_400_BAD_REQUEST)

        payment.status = Payment.Status.DONE
        payment.payment_key = result.get("paymentKey", payment_key)
        payment.method = result.get("method", "")
        if result.get("approvedAt"):
            payment.approved_at = parse_datetime(result["approvedAt"])
        payment.raw_response = result

        # ⭐ 결제 완료 → 예약 에이전트 자동 접수 (기존 run_booking 무수정 재사용)
        # 게스트 = 결제자 가정 (MVP — 실서비스는 여권명 별도 입력 필요)
        from agents.tasks import run_booking
        run_id = uuid.uuid4().hex[:12]
        user = payment.user
        async_result = run_booking.delay(
            run_id, payment.plan_id,
            user.nickname or "GUEST", "TRAVELER", user.email,
        )
        cache.set(f"run:{run_id}",
                  {"task_id": async_result.id, "plan_id": payment.plan_id},
                  timeout=60 * 60)

        payment.booking_run_id = run_id
        payment.save()

    return Response({
        "status": payment.status,
        "run_id": run_id,
        "task_id": async_result.id,
    })


def payment_checkout(request, plan_id):
    """
    테스트/데모용 결제 페이지 (DEBUG 전용 — 운영에서는 404).

    FE 없이 백엔드만으로 결제창→승인→예약까지 e2e를 돌릴 수 있게 하는
    최소 HTML. JWT는 ?token= 쿼리로 받아 JS가 API 호출에 사용한다.
    (데모 전용 편법 — 실서비스 페이지가 아님)
    """
    if not django_settings.DEBUG:
        raise Http404
    return render(request, "payments/checkout.html", {"plan_id": plan_id})
