"""
액티비티 공급자 MCP 서버 (activity-provider) — 4번째 MCP 서버

[다른 서버들과의 결정적 차이]
    flight/accommodation/booking 서버는 외부 API의 어댑터였지만,
    이 서버의 재고 원장은 "우리 DB"다 — 우리가 공급자 그 자체.
    그래서 이 자식 프로세스는 Django를 직접 초기화한다 (아래 setup).
    밖(MCP)에서 보면 다른 공급자와 구별되지 않는다 — 그게 표준의 힘.

[툴 5종 = 멘토 제안 스펙 그대로]
    search_activities → hold(TTL 점유) → reserve(멱등 확정) → get/cancel
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 이 프로세스는 단독 실행되므로 Django ORM을 쓰려면 직접 초기화해야 한다
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP

from provider import services
from provider.services import ProviderError

mcp = FastMCP("activity-provider")

# sync_to_async: Django ORM(동기 전용)을 비동기 세계에서 안전하게 부르는 공식 다리.
# MCP 서버는 asyncio 위에서 도는데 ORM을 직접 부르면 Django 안전장치가
# "You cannot call this from an async context"로 거부한다 — 별도 스레드로
# 우회시키는 게 정석 (Django ORM을 쓰는 MCP 서버의 필수 패턴).


@mcp.tool()
async def search_activities(city: str, category: str | None = None) -> dict:
    """
    도시의 액티비티 상품을 검색한다 (가용 재고 포함).

    Args:
        city: 도시명 한글 (예: "오사카")
        category: 선택 필터 (체험/티켓/투어/음식)

    Returns:
        {"activities": [{"activity_id", "name", "category", "price_krw",
                         "available", "description"}, ...]}
    """
    results = await sync_to_async(services.search_activities)(city, category)
    if not results:
        return {"activities": [], "message": f"'{city}'의 액티비티가 없습니다."}
    return {"activities": results}


@mcp.tool()
async def hold_activity(activity_id: int, qty: int) -> dict:
    """
    액티비티 재고를 임시 점유한다 (10분 유효). 예약의 1단계 —
    reserve_activity 전에 반드시 이 툴로 hold_id를 받아야 한다.

    Returns:
        성공: {"hold_id", "activity", "qty", "total_krw", "expires_at"}
        실패: {"error": "재고 부족 등 사유"}
    """
    try:
        return await sync_to_async(services.hold_activity)(activity_id, qty)
    except ProviderError as e:
        return {"error": str(e)}


@mcp.tool()
async def reserve_activity(hold_id: str, traveler_name: str) -> dict:
    """
    hold를 예약으로 확정한다 (2단계). 같은 hold_id로 재호출해도
    새 예약이 생기지 않고 같은 확정번호가 반환된다 (멱등).

    Returns:
        성공: {"confirmation", "activity", "qty", "total_krw", "already_reserved"}
        실패: {"error": "만료 등 사유 — 만료면 hold부터 다시"}
    """
    try:
        return await sync_to_async(services.reserve_activity)(hold_id, traveler_name)
    except ProviderError as e:
        return {"error": str(e)}


@mcp.tool()
async def get_activity_reservation(confirmation: str) -> dict:
    """예약번호로 예약 상태를 조회한다."""
    try:
        return await sync_to_async(services.get_reservation)(confirmation)
    except ProviderError as e:
        return {"error": str(e)}


@mcp.tool()
async def cancel_activity_reservation(confirmation: str) -> dict:
    """예약을 취소한다 (취소 수량은 재고로 자동 복귀)."""
    try:
        return await sync_to_async(services.cancel_reservation)(confirmation)
    except ProviderError as e:
        return {"error": str(e)}


if __name__ == "__main__":
    mcp.run()
