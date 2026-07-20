"""
항공 발권 MCP 서버 (flight-booking-agent) — 5번째 MCP 서버, ⭐ 자체 mock 공급자

[왜 mock인가 — 멘토 피드백 반영]
    실제 항공 발권·정산은 항공사/여행사(아고다 등) 라이선스가 있어야 해서
    학생 팀이 실판매자가 될 수는 없다. 대신 "판매자인 척" 하는 공급자 서버를
    직접 만들어, 대화 → 계획 → 확정 → 발권까지 전체 시스템이 실제로 굴러갈 수
    있음을 증명한다. (액티비티 자체 공급자 서버와 같은 취지의 항공판)

[실제 발권 절차를 그대로 흉내낸 3단계]
    ① flight_fare_quote  : 운임 재확인  (실세계: 검색 시점 가격은 보장되지 않음)
    ② flight_hold_seats  : 좌석 임시 점유 (실세계: 발권 전 좌석 블록, 만료시간 있음)
    ③ flight_issue_ticket: 발권 확정 → PNR 반환

[멱등성 — 발표 포인트]
    PNR은 hold_id의 해시에서 "결정적으로" 만들어진다.
    같은 hold_id로 두 번 발권해도 항상 같은 PNR = 재시도해도 이중 발권이
    구조적으로 불가능 (액티비티 서버의 reserve 멱등성과 같은 원칙).

실행: 오케스트레이터 허브(SERVER_PATHS)가 자식 프로세스로 자동 기동.
외부 API·키 없음 — 순수 시뮬레이션이라 어떤 환경에서도 동작한다.
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("flight-booking-agent")

QUOTE_TTL_MIN = 10  # 견적 유효 시간 (분)
HOLD_TTL_MIN = 10   # 좌석 점유 유효 시간 (분)

# 이 프로세스(= 한 번의 파이프라인 실행) 동안의 견적/점유 장부.
# 실서비스라면 DB/Redis에 두겠지만, mock의 목적은 "절차와 규칙의 증명"이므로
# 메모리 dict로 충분하다. (PNR 멱등성은 해시 기반이라 장부와 무관하게 성립)
_quotes: dict[str, dict] = {}
_holds: dict[str, dict] = {}


def _now():
    return datetime.now(timezone.utc)


def _expires_iso(minutes: int) -> str:
    return (_now() + timedelta(minutes=minutes)).isoformat()


def _pnr_from(hold_id: str) -> str:
    """hold_id -> 6자리 PNR. 해시라서 같은 입력이면 언제나 같은 출력 = 멱등."""
    return hashlib.sha1(hold_id.encode()).hexdigest().upper()[:6]


@mcp.tool()
def flight_fare_quote(airline: str, total_krw: int) -> dict:
    """예약 직전 운임 재확인 (시뮬레이션).

    실세계에서는 검색 시점 가격이 발권 시점까지 보장되지 않아 재확인이 필수다.
    이 mock 공급자는 저장된 가격을 그대로 유효 처리해 견적(quote_id)을 발급한다.

    Args:
        airline: 항공사 표시명 (계획에 저장된 값)
        total_krw: 계획에 저장된 총액 (KRW)

    Returns:
        {"quote_id", "airline", "total_krw", "expires_at"}
    """
    quote_id = "FQ-" + uuid.uuid4().hex[:8].upper()
    _quotes[quote_id] = {
        "airline": airline, "total_krw": total_krw,
        "expires": _now() + timedelta(minutes=QUOTE_TTL_MIN),
    }
    return {"quote_id": quote_id, "airline": airline,
            "total_krw": total_krw, "expires_at": _expires_iso(QUOTE_TTL_MIN)}


@mcp.tool()
def flight_hold_seats(quote_id: str, passengers: int) -> dict:
    """좌석 임시 점유 (시뮬레이션) — 발권 전 좌석 블록 단계.

    flight_fare_quote로 받은 quote_id가 있어야 호출할 수 있다.

    Returns:
        성공: {"hold_id", "airline", "passengers", "expires_at"}
        실패: {"error": 원인} — 만료/무효 견적이면 ①부터 다시
    """
    q = _quotes.get(quote_id)
    if q is None:
        return {"error": "유효하지 않은 quote_id입니다. flight_fare_quote부터 다시 호출하세요."}
    if q["expires"] < _now():
        return {"error": "견적이 만료됐습니다. flight_fare_quote부터 다시 호출하세요."}
    if passengers < 1:
        return {"error": "탑승 인원은 1명 이상이어야 합니다."}

    hold_id = "FH-" + uuid.uuid4().hex[:10].upper()
    _holds[hold_id] = {
        "airline": q["airline"], "total_krw": q["total_krw"],
        "passengers": passengers,
        "expires": _now() + timedelta(minutes=HOLD_TTL_MIN),
        "issued": False,
    }
    return {"hold_id": hold_id, "airline": q["airline"],
            "passengers": passengers, "expires_at": _expires_iso(HOLD_TTL_MIN)}


@mcp.tool()
def flight_issue_ticket(hold_id: str, lead_passenger: str) -> dict:
    """발권 확정 → PNR(예약번호) 반환 (시뮬레이션, 멱등).

    같은 hold_id로 두 번 호출해도 같은 PNR이 나온다 — 이중 발권 방지.
    결제 수단은 이 툴 내부 개념으로 격리되어 LLM은 결제 정보를 다루지 않는다
    (숙소 booking_confirm과 동일한 "판단은 LLM, 민감한 실행은 코드" 원칙).

    Returns:
        성공: {"pnr", "status", "airline", "total_krw", "passengers",
               "lead_passenger", "already_issued"}
        실패: {"error": 원인}
    """
    h = _holds.get(hold_id)
    if h is None:
        return {"error": "유효하지 않은 hold_id입니다. flight_hold_seats부터 다시 호출하세요."}
    # 이미 발권된 hold는 만료돼도 같은 PNR을 돌려준다 (멱등 재조회)
    if h["expires"] < _now() and not h["issued"]:
        return {"error": "좌석 점유가 만료됐습니다. flight_fare_quote부터 다시 시작하세요."}

    already = h["issued"]
    h["issued"] = True
    return {
        "pnr": _pnr_from(hold_id), "status": "TICKETED",
        "airline": h["airline"], "total_krw": h["total_krw"],
        "passengers": h["passengers"], "lead_passenger": lead_passenger,
        "already_issued": already,   # True면 "재시도였음"을 정직하게 표시
    }


if __name__ == "__main__":
    mcp.run()   # stdio 모드 — 허브가 표준입출력 파이프로 통신
