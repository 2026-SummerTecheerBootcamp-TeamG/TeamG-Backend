from django.urls import path
from . import views

urlpatterns = [
    # POST /api/v1/payments/prepare/   주문 준비 (서버가 금액 결정)
    path("prepare/", views.payment_prepare, name="payment-prepare"),

    # POST /api/v1/payments/confirm/   결제 승인 + 예약 에이전트 자동 접수
    path("confirm/", views.payment_confirm, name="payment-confirm"),

    # GET  /api/v1/payments/checkout/{plan_id}/   테스트 결제 페이지 (DEBUG 전용)
    path("checkout/<int:plan_id>/", views.payment_checkout, name="payment-checkout"),
]
