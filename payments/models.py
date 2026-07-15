"""
payments/models.py - 결제 기록 (토스페이먼츠 샌드박스)

[설계 원칙]
    - 결제 금액의 진실은 이 테이블의 amount 하나뿐 (prepare 때 서버가 계산해 저장,
      confirm 때 클라이언트가 보낸 값과 "대조만" 한다 — 위변조 방어의 핵심)
    - order_id UNIQUE = 멱등성의 뿌리 (같은 주문은 세상에 하나)
    - PROTECT: 결제 기록이 달린 플랜/유저는 강제 삭제 불가 (돈이 얽힌 데이터 보호)
"""

import uuid

from django.conf import settings
from django.db import models

from trips.models import Plan


class Payment(models.Model):
    class Status(models.TextChoices):
        READY = "READY", "결제 준비"        # prepare 직후 (결제창 열리기 전)
        DONE = "DONE", "결제 완료"          # 토스 승인 성공
        ABORTED = "ABORTED", "승인 실패"    # 토스 승인 거절/오류
        CANCELED = "CANCELED", "취소"       # (후속: 취소 기능용 자리)

    plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,       # 결제된 플랜은 삭제 불가
        related_name="payments",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="payments",
    )

    order_id = models.CharField(max_length=64, unique=True, db_index=True)
    order_name = models.CharField(max_length=100)       # 결제창에 표시될 이름
    amount = models.PositiveIntegerField()              # 서버가 정한 최종 금액 (KRW)
    status = models.CharField(max_length=20, choices=Status.choices,
                              default=Status.READY)

    payment_key = models.CharField(max_length=200, blank=True, db_index=True)
    method = models.CharField(max_length=30, blank=True)        # 카드/토스페이 등
    approved_at = models.DateTimeField(null=True, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)   # 토스 응답 원본 스냅샷

    # 결제 성공 시 자동 접수된 예약 실행의 run_id — confirm 멱등 응답에 재사용
    booking_run_id = models.CharField(max_length=32, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["user", "status"])]

    @staticmethod
    def generate_order_id(plan_id):
        # 예: "plan16-a3f2c1d4e5b60718" — 사람이 봐도 어느 플랜 결제인지 보이게
        return f"plan{plan_id}-{uuid.uuid4().hex[:16]}"

    def __str__(self):
        return f"{self.order_id} {self.amount:,}원 ({self.status})"
