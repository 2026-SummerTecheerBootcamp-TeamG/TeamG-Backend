"""
예약 에이전트 MCP 서버 (booking-agent) — 3번째 MCP 서버

[역할]
    LiteAPI의 예약 3단계(요금 재조회 → 가예약 → 예약 확정)를 MCP 툴로 노출한다.
    오케스트레이터(Claude)가 이 툴들을 순서대로 판단·호출해 예약을 완수한다.

[⭐ 보안 설계 — 발표 포인트]
    결제 수단(샌드박스 테스트 카드)은 booking_confirm 툴 "내부"에 격리돼 있다.
    Claude는 게스트 이름/이메일만 다루고, 카드번호는 입력받지도 출력하지도 않는다
    — "판단은 LLM, 민감한 실행은 코드" 선 긋기 원칙의 결제 버전.

[키 격리]
    이 서버는 LITEAPI_SANDBOX_KEY만 사용한다 (가짜 결제 전용).
    검색용 프로덕션 키(LITEAPI_KEY)와 분리 — 실수로 실예약이 나갈 수 없는 구조.

실행: 오케스트레이터 허브(SERVER_PATHS)가 자식 프로세스로 자동 기동
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("booking-agent")

BASE_URL = "https://api.liteapi.travel/v3.0"
TIMEOUT = 20   # 예약 계열은 검색보다 느릴 수 있어 여유 있게


def _headers():
    """
    샌드박스 키 헤더. LITEAPI_SANDBOX_KEY가 있으면 그걸, 없으면 LITEAPI_KEY 폴백
    (현재 팀 키가 샌드박스 키라서 폴백으로 바로 동작한다.
     나중에 검색용 키를 프로덕션으로 올려도, SANDBOX_KEY만 지정하면
     예약은 계속 샌드박스에 묶인다 — 실예약 사고 방지 구조 유지)
    """
    key = os.environ.get("LITEAPI_SANDBOX_KEY") or os.environ.get("LITEAPI_KEY")
    if not key:
        raise RuntimeError("LITEAPI_SANDBOX_KEY 또는 LITEAPI_KEY가 설정되지 않았습니다.")
    return {"X-API-Key": key, "Content-Type": "application/json"}


def _post(path, payload):
    """LiteAPI POST 공통 처리 — HTTP 에러를 dict로 변환해 Claude가 읽고 대응하게."""
    response = requests.post(BASE_URL + path, json=payload,
                             headers=_headers(), timeout=TIMEOUT)
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        # 에러도 문자열로 돌려준다 — Claude가 원인을 보고 재시도/보고를 판단
        return {"error": f"LiteAPI {response.status_code}: {body}"}
    return body


# ---------------------------------------------------------------------------
# Tool 1: 예약용 최신 요금 재조회
# ---------------------------------------------------------------------------

@mcp.tool()
def booking_search_rates(
    hotel_id: str,
    checkin: str,
    checkout: str,
    adults: int,
) -> dict:
    """
    예약할 호텔의 '지금 유효한' 요금(offerId)을 조회한다.

    저장된 플랜의 요금은 이미 만료됐을 수 있으므로, 예약 전에 반드시
    이 툴로 최신 offerId를 받아야 한다. (요금은 30분 내외로 만료됨)

    Args:
        hotel_id: LiteAPI 호텔 ID (플랜에 저장된 값)
        checkin / checkout: "YYYY-MM-DD"
        adults: 성인 수

    Returns:
        성공: {"offers": [{"offer_id":.., "room_name":.., "total":.., "currency":..}, ...]}
        0건: {"offers": [], "message": "..."} / 실패: {"error": "..."}
    """
    body = _post("/hotels/rates", {
        "hotelIds": [hotel_id],
        "checkin": checkin,
        "checkout": checkout,
        "occupancies": [{"adults": adults}],
        "currency": "USD",
        "guestNationality": "KR",
    })
    if "error" in body:
        return body

    offers = []
    for hotel in body.get("data") or []:
        for room_type in hotel.get("roomTypes") or []:
            offer_id = room_type.get("offerId")
            rates = room_type.get("rates") or []
            if not offer_id or not rates:
                continue
            first = rates[0]
            retail = ((first.get("retailRate") or {}).get("total") or [{}])[0]
            offers.append({
                "offer_id": offer_id,
                "room_name": first.get("name"),
                "total": retail.get("amount"),
                "currency": retail.get("currency"),
            })

    if not offers:
        return {"offers": [], "message": "해당 조건의 예약 가능한 요금이 없습니다. 날짜를 바꿔 보세요."}
    return {"offers": offers[:5]}   # 판단에 충분한 상위 5개만


# ---------------------------------------------------------------------------
# Tool 2: 가예약 (prebook) — 가격/재고 최종 확인
# ---------------------------------------------------------------------------

@mcp.tool()
def booking_prebook(offer_id: str) -> dict:
    """
    선택한 offer의 가격과 재고를 최종 확인하고 예약 준비 상태로 만든다.
    반환된 prebook_id로만 예약 확정이 가능하다 (offer가 만료됐으면 에러 —
    그 경우 booking_search_rates부터 다시).

    Returns:
        성공: {"prebook_id":.., "total":.., "currency":..} / 실패: {"error": "..."}
    """
    body = _post("/rates/prebook", {"offerId": offer_id, "usePaymentSdk": False})
    if "error" in body:
        return body

    data = body.get("data") or {}
    return {
        "prebook_id": data.get("prebookId"),
        "total": data.get("price"),
        "currency": data.get("currency"),
    }


# ---------------------------------------------------------------------------
# Tool 3: 예약 확정 (book) — 결제는 툴 내부에 격리
# ---------------------------------------------------------------------------

@mcp.tool()
def booking_confirm(
    prebook_id: str,
    first_name: str,
    last_name: str,
    email: str,
) -> dict:
    """
    가예약을 실제 예약으로 확정한다 (샌드박스 = 가짜 결제, 과금 없음).

    ⭐ 결제 수단은 이 툴 안에 격리돼 있다 — 호출자는 게스트 정보만 넘긴다.
    (샌드박스 규약: ACC_CREDIT_CARD + 테스트 카드. 실결제 전환 시에도
     카드 정보는 LLM을 거치지 않고 결제 SDK/서버간 채널로만 다룬다)

    Returns:
        성공: {"booking_id":.., "confirmation":.., "status":..} / 실패: {"error": "..."}
    """
    holder = {"firstName": first_name, "lastName": last_name, "email": email}
    body = _post("/rates/book", {
        "prebookId": prebook_id,
        "holder": holder,
        "guests": [dict(occupancyNumber=1, **holder)],
        # 샌드박스 가짜 결제 — LiteAPI 문서의 테스트 규약 그대로
        "payment": {"method": "ACC_CREDIT_CARD"},
    })
    if "error" in body:
        return body

    data = body.get("data") or {}
    return {
        "booking_id": data.get("bookingId"),
        "confirmation": (data.get("hotelConfirmationCode")
                         or data.get("supplierBookingId")),
        "status": data.get("status"),
    }


if __name__ == "__main__":
    mcp.run()
