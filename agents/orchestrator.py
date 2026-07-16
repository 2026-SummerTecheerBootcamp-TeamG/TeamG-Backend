"""
오케스트레이터 코어 - Claude 툴 루프

전체 그림
    사용자 요청(자연어)
        ↓
    Claude에게 요청 + 통합 툴 목록 6개를 줌
        ↓
    Claude가 판단: "지금 어떤 툴이 팔요한가
    - 툴이 필요함 → 허브로 호출 → 결과를 Claude에게
    - 전달하고 다시 판단시킴 (루프 계속)
    - 필요 없음 → 최종 답변 작성 (루프 종료)
    
    역할 분담:
    - Claude    : 판단 (어떤 툴을, 어떤 순서로, 어떤 인자로)
    - ToolHub   : 실행 (올바른 MCP 서버로 라우팅해 실제 호출)
    - trace     : 중계 (이 모든 과정을 실시간 방송)
    - 이 파일    : 그 셋을 잇는 루프 
"""

import anthropic
import asyncio
import json

from agents.claude_client import DEFAULT_MODEL  # 팀 공용 모델명 재사용
from agents.mcp_client import ToolHub
from agents import trace

# ask_claude를 안 쓰는 이유: ask_claude는 질문->답변 문자열 전용이라 tools 파라미터를 지원하지 않음
# 툴 루프에는 원본 클라이언트가 필요함
_client = anthropic.Anthropic()

# 무한루프 방지
MAX_TURNS = 8

# system 프롬프트: Claude의 업무 지시서
# 역할/규칙을 고정함
SYSTEM_PROMPT = """당신은 여행 계획 서비스의 오케스트레이터입니다.
사용자의 여행 요청을 처리하기 위해 제공된 도구(항공 검색, 숙소 검색 등)를
스스로 판단하여 호출하세요.

규칙:
1. 도구가 반환한 데이터에 있는 정보만 사용하세요. 없는 정보를 지어내지 마세요.
2. 숙소 검색 전에는 반드시 객실 배분 도구를 먼저 호출하세요.
3. 서로 결과를 쓰지 않는 독립적인 도구들은 반드시 "같은 턴에 한꺼번에" 호출하세요.
   예: 항공 검색과 객실 배분은 서로 독립이므로 첫 턴에 함께 호출합니다.
   (같은 턴의 도구들은 동시에 실행되어 전체 시간이 크게 줄어듭니다.
    단, 한 도구의 출력이 다른 도구의 입력이면 턴을 나누세요 — 규칙 2처럼)
4. 도구가 오류나 0건을 반환하면, 조건을 바꿔 재시도하거나 그 사실을 솔직히 알리세요.
5. 최종 답변은 한국어로, 항공/숙소 후보를 가격과 함께 정리해서 작성하세요."""


def _extract_final_text(response):
    """
    응답의 content 블록들에서 텍스트만 이어붙임
    b.type == "text"로 골라내는 이유: content에는 text 외에 tool_use,
    (확장 사고 모델이면) thinking 블록도 섞여 올 수 있음
    """

    return "\n".join(b.text for b in response.content if b.type == "text")


def _collect_candidates(tool_name, result_text, collected):
    """
    검색 툴의 응답에서 후보 리스트를 코드가 직접 수집
    """

    try:
        data = json.loads(result_text)
    except (ValueError, TypeError):
        return
    
    if tool_name == "flight_search_candidates":
        # 항공 후보는 이미 예산 엔진 입력 형식과 동일
        collected["flight_options"] = data.get("candidates", [])

    elif tool_name == "accommodation_score_candidates":
        # 숙소 후보는 키가 다름
        collected["hotel_options"] = [
            {
                "label": c.get("hotel_id"),
                "krw": c.get("krw"),
                "utility": c.get("utility"),
                "raw": c,
            }
            for c in data.get("scored_candidates", [])
        ]

    elif tool_name == "booking_confirm":
        # 예약 확정 결과 — 성공 응답만 수집 (DB 저장은 태스크가 담당)
        if isinstance(data, dict) and "error" not in data:
            collected["booking"] = data

    elif tool_name == "flight_issue_ticket":
        # mock 항공 발권 결과 — PNR이 있으면 성공으로 수집
        if isinstance(data, dict) and data.get("pnr"):
            collected["flight_ticket"] = data

    elif tool_name == "post_bookings":
        # LiteAPI "공식" MCP 서버의 예약 확정 툴 — 응답 구조가 우리 툴과 달라
        # 여기서 우리 형식({"booking_id", "confirmation"})으로 맞춰 수집한다
        inner = data.get("data") if isinstance(data, dict) else None
        if isinstance(inner, dict) and inner.get("bookingId"):
            collected["booking"] = {
                "booking_id": inner.get("bookingId"),
                "confirmation": (inner.get("hotelConfirmationCode")
                                 or inner.get("supplierBookingId")),
                "status": inner.get("status"),
                "raw": inner,
            }


async def run_agent_loop(run_id, user_message, collected=None, finish_trace=True):
    
    """
    자연어 요청 -> (Claude 판단 <-> 툴 실행)* -> 최종 답변 문자열
    
    async인 이유: ToolHub(MCP 통신)가 비동기라서
    """

    trace.publish(run_id, "user", "user", "요청 접수", user_message[:200])

    hub = ToolHub(run_id)
    await hub.connect()

    try:
        # 대화기록
        # 루프가 돌 때마다 Claude의 응답과 툴 실행 결과가 번갈아 쌓임
        messages = [{"role": "user", "content": user_message}]

        for turn in range(1, MAX_TURNS + 1):
            trace.publish(run_id, "llm", "claude", f"판단 요청 (턴 {turn}/{MAX_TURNS})")

            response = _client.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=hub.claude_tools,
                messages=messages,
            )
            # Claude의 응답을 대화기록에 그대로 추가
            messages.append({"role": "assistant", "content": response.content})

            # 이번 응답에서 툴 호출 요청 블록만 골라냄
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if not tool_uses:
                # 툴 요청이 없음 = Claude가 이제 답할 수 있다고 판단한 것
                final_text = _extract_final_text(response)
                if finish_trace:
                    trace.done(run_id, f"총 {turn}턴에 완료")
                return final_text
            
            # Claude가 요청한 툴들을 실행하고, 결과를 tool_result 형식으로 수집
            # 한 턴에 tool_use가 여러 개 온 것 자체가 "서로 결과에 의존하지
            # 않으니 동시에 호출해도 된다"는 Claude의 판단이라 asyncio.gather로
            # 병렬 실행한다 (한 툴의 출력을 다른 툴 입력으로 써야 하면 Claude가
            # 턴을 나눠 순차 호출함 - 시스템 프롬프트 규칙 2번 참고)
            results = await asyncio.gather(
                *(hub.call(block.name, block.input) for block in tool_uses)
            )

            tool_results = []
            for block, result_text in zip(tool_uses, results):
                # 검색 툴 응답이면 후보를 원본 그대로 수집
                if collected is not None:
                    _collect_candidates(block.name, result_text, collected)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })

            # 툴 결과는 "user" 역할로 전달하는 것이 Claude API의 규약
            messages.append({"role": "user", "content": tool_results})

        # for문이 break/return 없이 다 돌면 여기 도달 = 턴 초과
        trace.publish(run_id, "rule", "orchestrator", "최대 턴 초과로 중단")
        return f"[중단] 최대 턴 수({MAX_TURNS}회)를 초과했습니다."
    
    finally:
        await hub.close()
        