"""
payments/toss.py - 토스페이먼츠 API 클라이언트

[인증 방식]
    Basic 인증: base64(시크릿키 + ":") — 콜론 뒤가 빈 값인 게 토스 규약 (함정 주의).
    시크릿키는 백엔드 전용 — 프론트/응답에 절대 노출 금지.

[테스트 키]
    test_sk_ 로 시작하는 키는 샌드박스 전용 — 실제 청구가 발생하지 않는다.
"""

import base64
import os

import requests

TOSS_API_BASE = os.environ.get("TOSS_API_BASE", "https://api.tosspayments.com")
TIMEOUT = 15


class TossError(Exception):
    """토스 API가 승인 거절/오류를 돌려줬을 때 (메시지 = 토스의 설명)."""
    pass


def _auth_header():
    secret = os.environ.get("TOSS_SECRET_KEY")
    if not secret:
        # 결제 기능이 키 없이 조용히 도는 것보다 즉시 죽는 게 안전
        raise RuntimeError("환경변수 TOSS_SECRET_KEY가 설정되지 않았습니다.")
    token = base64.b64encode(f"{secret}:".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def confirm_payment(payment_key, order_id, amount):
    """
    토스 결제 승인 — 결제창에서 사용자가 결제를 마친 뒤,
    "진짜로 승인해도 된다"는 최종 확정을 서버가 보내는 단계.
    성공: 토스 응답 dict / 실패: TossError (토스의 거절 사유 포함)
    """
    response = requests.post(
        f"{TOSS_API_BASE}/v1/payments/confirm",
        json={"paymentKey": payment_key, "orderId": order_id, "amount": amount},
        headers=_auth_header(),
        timeout=TIMEOUT,
    )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        # 토스 에러 형식: {"code": "...", "message": "사람이 읽는 사유"}
        raise TossError(body.get("message") or f"토스 승인 실패 (HTTP {response.status_code})")
    return body


def cancel_payment(payment_key, cancel_reason):
    """결제 취소 — 후속 기능(환불/예약 실패 대응)용 자리. MVP에서는 미사용."""
    response = requests.post(
        f"{TOSS_API_BASE}/v1/payments/{payment_key}/cancel",
        json={"cancelReason": cancel_reason},
        headers=_auth_header(),
        timeout=TIMEOUT,
    )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        raise TossError(body.get("message") or f"토스 취소 실패 (HTTP {response.status_code})")
    return body
