"""
provider/services.py - 공급자의 핵심 로직: 재고 동시성 + 예약 멱등성

[이 파일이 점수가 남는 곳 (멘토 피드백의 핵심)]
    1. 동시성: 마지막 1석을 두 요청이 동시에 노리면 정확히 하나만 성공해야 한다
       → hold()가 Activity 행을 select_for_update로 잠근다.
         잠금을 쥔 요청이 재고를 계산·점유하는 동안 경쟁자는 줄을 서고,
         자기 차례가 왔을 땐 이미 재고가 소진돼 SoldOut을 받는다.
    2. 멱등성: 같은 hold_id로 reserve를 두 번 불러도 예약은 하나
       → OneToOne 제약(구조) + "이미 있으면 그걸 반환"(로직)의 이중 방어.
"""

import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from provider.models import Activity, ActivityHold, ActivityReservation

HOLD_TTL_MINUTES = 10   # hold 유효 시간 — 실제 공급자들의 임시 점유와 같은 개념


class ProviderError(Exception):
    """공급자 규칙 위반 (재고 부족, 만료, 없는 id 등) — 메시지가 곧 안내문."""
    pass


def _consumed_qty(activity, now):
    """
    현재 소진된 수량 = 살아있는 hold + 확정 예약이 점유한 수량.
    - 만료 전이며 아직 예약 안 된 hold: 점유 중
    - 확정(confirmed) 예약이 달린 hold: 영구 소진 (만료와 무관)
    - 취소된 예약의 hold: 소진 아님 (재고로 복귀)
    """
    holds = activity.holds.select_related("reservation").all()
    total = 0
    for hold in holds:
        reservation = getattr(hold, "reservation", None)
        if reservation is not None:
            if reservation.status == ActivityReservation.Status.CONFIRMED:
                total += hold.qty
        elif hold.expires_at > now:      # 예약 없는 hold는 만료 전까지만 점유
            total += hold.qty
    return total


def search_activities(city, category=None):
    """단순 조회 — 가용 재고를 계산해서 함께 반환."""
    now = timezone.now()
    queryset = Activity.objects.filter(city=city)
    if category:
        queryset = queryset.filter(category=category)

    results = []
    for activity in queryset:
        available = activity.stock - _consumed_qty(activity, now)
        results.append({
            "activity_id": activity.id,
            "name": activity.name,
            "category": activity.category,
            "price_krw": activity.price_krw,
            "available": max(0, available),
            "description": activity.description,
        })
    return results


def hold_activity(activity_id, qty):
    """
    재고 임시 점유 (TTL 10분). ⭐ 동시성 방어 지점.

    select_for_update: 이 Activity 행에 "쓰기 잠금"을 건다.
    같은 상품을 노리는 다른 트랜잭션은 이 블록이 끝날 때까지 대기 —
    즉 재고 확인과 hold 생성이 "한 덩어리"가 되어 끼어들 틈이 없다.
    """
    if qty < 1:
        raise ProviderError("수량은 1 이상이어야 합니다.")

    now = timezone.now()
    with transaction.atomic():
        try:
            activity = Activity.objects.select_for_update().get(id=activity_id)
        except Activity.DoesNotExist:
            raise ProviderError(f"존재하지 않는 액티비티입니다: {activity_id}")

        available = activity.stock - _consumed_qty(activity, now)
        if available < qty:
            raise ProviderError(
                f"재고 부족: '{activity.name}' 남은 수량 {max(0, available)}개, 요청 {qty}개"
            )

        hold = ActivityHold.objects.create(
            activity=activity,
            hold_id=f"hold_{uuid.uuid4().hex[:12]}",
            qty=qty,
            expires_at=now + timedelta(minutes=HOLD_TTL_MINUTES),
        )

    return {
        "hold_id": hold.hold_id,
        "activity": activity.name,
        "qty": qty,
        "total_krw": activity.price_krw * qty,
        "expires_at": hold.expires_at.isoformat(),
    }


def reserve_activity(hold_id, traveler_name):
    """
    예약 확정 (2단계의 2단계). ⭐ 멱등성 방어 지점.
    같은 hold_id로 다시 불러도 새 예약이 생기지 않고 기존 확정번호를 반환한다.
    """
    now = timezone.now()
    with transaction.atomic():
        try:
            # hold 행 잠금: 같은 hold에 대한 동시 reserve도 줄 세움
            hold = (ActivityHold.objects.select_for_update()
                    .select_related("activity").get(hold_id=hold_id))
        except ActivityHold.DoesNotExist:
            raise ProviderError(f"존재하지 않는 hold입니다: {hold_id}")

        # 멱등: 이미 예약된 hold면 그 예약을 그대로 반환 (이중 예약 원천 차단)
        existing = getattr(hold, "reservation", None)
        if existing is not None and existing.status == ActivityReservation.Status.CONFIRMED:
            return {
                "confirmation": existing.confirmation,
                "activity": hold.activity.name,
                "qty": hold.qty,
                "already_reserved": True,
            }

        if hold.expires_at <= now:
            raise ProviderError("hold가 만료되었습니다. 다시 hold부터 진행해 주세요.")

        reservation = ActivityReservation.objects.create(
            hold=hold,
            confirmation=f"ACT-{uuid.uuid4().hex[:8].upper()}",
            traveler_name=traveler_name,
        )

    return {
        "confirmation": reservation.confirmation,
        "activity": hold.activity.name,
        "qty": hold.qty,
        "total_krw": hold.activity.price_krw * hold.qty,
        "already_reserved": False,
    }


def get_reservation(confirmation):
    """예약 조회."""
    try:
        r = (ActivityReservation.objects
             .select_related("hold__activity").get(confirmation=confirmation))
    except ActivityReservation.DoesNotExist:
        raise ProviderError(f"존재하지 않는 예약번호입니다: {confirmation}")
    return {
        "confirmation": r.confirmation,
        "status": r.status,
        "activity": r.hold.activity.name,
        "qty": r.hold.qty,
        "traveler_name": r.traveler_name,
    }


def cancel_reservation(confirmation):
    """예약 취소 — 취소되면 그 수량은 가용 재고 계산에서 자동 복귀."""
    with transaction.atomic():
        try:
            r = ActivityReservation.objects.select_for_update().get(
                confirmation=confirmation)
        except ActivityReservation.DoesNotExist:
            raise ProviderError(f"존재하지 않는 예약번호입니다: {confirmation}")
        r.status = ActivityReservation.Status.CANCELED
        r.save()
    return {"confirmation": confirmation, "status": r.status}
