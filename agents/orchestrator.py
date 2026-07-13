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
스스로 판단하여 순서대로 호출하세요.

규칙:
1. 도구가 반환한 데이터에 있는 정보만 사용하세요. 없는 정보를 지어내지 마세요.
2. 숙소 검색 전에는 반드시 객실 배분 도구를 먼저 호출하세요.
3. 도구가 오류나 0건을 반환하면, 조건을 바꿔 재시도하거나 그 사실을 솔직히 알리세요.
4. 최종 답변은 한국어로, 항공/숙소 후보를 가격과 함께 정리해서 작성하세요."""


def _extract_final_text(response):
    """
    응답의 content 블록들에서 텍스트만 이어붙임
    b.type == "text"로 골라내는 이유: content에는 text 외에 tool_use,
    (확장 사고 모델이면) thinking 블록도 섞여 올 수 있음
    """

    return "\n".join(b.text for b in response.content if b.type == "text")


async def run_agent_loop(run_id, user_message):
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
                trace.done(run_id, f"총 {turn}턴에 완료")
                return final_text
            
            # Claude가 요청한 툴들을 실행하고, 결과를 tool_result 형식으로 수집
            tool_results = []
            for block in tool_uses:
                # block.name = 툴 이름
                # block.id = 이 호출의 고유 번호
                result_text = await hub.call(block.name, block.input)
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
        