"""
=====================================================================
실제 Claude API 연동 검증 스크립트 (미니 오케스트레이터)
=====================================================================

[이 파일이 하는 일]
    verify_mcp_connection.py는 우리가 직접 정해준 파라미터로 tool을
    호출해봤음 (Claude 없이). 이 스크립트는 한 단계 더 나아가서,
    실제 Claude API에 자연어 요청을 던지고, Claude가 스스로 어떤
    tool을 어떤 순서로 어떤 파라미터로 호출할지 "판단"하게 만듦.

    즉, 진짜 오케스트레이터(다른 팀원이 만들고 있는 것)가 하게 될
    역할의 아주 작은 버전을 우리가 미리 만들어서, 숙소 에이전트의
    MCP tool들이 실제로 Claude 판단 하에서도 잘 동작하는지 미리
    확인해보는 용도임.

[왜 필요한가]
    지금까지 확인한 것들의 한계:
    - 파이썬 함수 직접 호출: 로직은 맞는지 확인되지만 MCP 프로토콜은 검증 안 됨
    - verify_mcp_connection.py: MCP 프로토콜 통신은 확인되지만, "Claude가
      상황에 맞게 tool을 올바르게 판단해서 부르는지"는 확인 안 됨
    이 스크립트가 바로 그 마지막 빈틈을 채워줌.

[실행 방법]
    환경변수 ANTHROPIC_API_KEY, LITEAPI_KEY가 .env에 설정돼 있어야 함
    (python-dotenv로 .env를 자동으로 읽어옴)

    python agents/accommodation/test_with_real_claude.py
=====================================================================
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dotenv import load_dotenv
load_dotenv()  # .env 파일을 읽어서 환경변수로 등록함

from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


MCP_SERVER_PATH = os.path.join(os.path.dirname(__file__), "mcp_server.py")
MODEL = "claude-sonnet-5"  # agents/claude_client.py의 DEFAULT_MODEL과 통일
MAX_TURNS = 6  # 무한루프 방지 안전장치 - Claude가 tool을 계속 부르기만 하면 최대 6번까지만 허용


def mcp_tool_to_claude_tool(mcp_tool) -> dict:
    """
    MCP tool 정의(mcp_tool.name, .description, .inputSchema)를
    Claude API가 요구하는 tool 스키마 형태로 변환하는 함수임.

    Claude API의 tools 파라미터는 {"name", "description", "input_schema"}
    형태를 요구하는데, MCP의 필드 이름(inputSchema)과 살짝 다르기 때문에
    이 변환이 필요함.
    """
    return {
        "name": mcp_tool.name,
        "description": mcp_tool.description or "",
        "input_schema": mcp_tool.inputSchema,
    }


def extract_tool_result_text(result) -> str:
    """MCP tool 호출 결과(content 리스트)에서 텍스트만 뽑아 이어붙이는 함수임"""
    texts = [block.text for block in result.content if hasattr(block, "text")]
    return "\n".join(texts)


async def run_accommodation_agent(user_message: str) -> str:
    """
    사용자의 자연어 요청 하나를 받아서, Claude가 숙소 에이전트 MCP tool들을
    스스로 판단해 호출하며 최종 답변을 만들어내는 전체 과정을 실행하는 함수임.
    """
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        raise RuntimeError(
            "환경변수 ANTHROPIC_API_KEY가 설정되지 않았습니다. "
            ".env 파일에 실제 사용 중인 키 이름과 일치하는지 확인해주세요."
        )

    client = Anthropic(api_key=anthropic_api_key)

    # 중요: mcp_server.py는 별도 프로세스로 실행되는데, MCP 라이브러리는
    # 보안을 위해 부모 프로세스의 환경변수를 자동으로 상속시키지 않음.
    # 그래서 LITEAPI_KEY, GOOGLE_MAPS_API_KEY 같은 값들을 명시적으로
    # env 파라미터에 넣어서 넘겨줘야, mcp_server.py 안에서
    # os.environ.get("LITEAPI_KEY")가 정상적으로 값을 찾을 수 있음.
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[MCP_SERVER_PATH],
        env=dict(os.environ),  # 현재 프로세스(.env 로드 완료된 상태)의 환경변수를 통째로 전달
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1) MCP 서버에서 tool 목록을 받아서 Claude가 이해하는 형태로 변환함
            tools_result = await session.list_tools()
            claude_tools = [mcp_tool_to_claude_tool(t) for t in tools_result.tools]
            print(f"[준비] Claude에게 알려줄 tool {len(claude_tools)}개 로드됨\n")

            messages = [{"role": "user", "content": user_message}]

            # 2) Claude가 "이제 tool 호출이 필요없다"고 판단할 때까지 반복하는 루프임
            for turn in range(1, MAX_TURNS + 1):
                print(f"--- Turn {turn} ---")
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=1024,
                    tools=claude_tools,
                    messages=messages,
                )
                messages.append({"role": "assistant", "content": response.content})

                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

                if not tool_use_blocks:
                    # tool 호출이 하나도 없으면, Claude가 이제 최종 답변을 낸 것임
                    final_text = "\n".join(
                        b.text for b in response.content if b.type == "text"
                    )
                    print(f"[Claude 최종 응답]\n{final_text}\n")
                    return final_text

                # 3) Claude가 요청한 tool 호출들을 실제로 MCP 서버에 실행시킴
                tool_results = []
                for block in tool_use_blocks:
                    print(f"  Claude가 호출 요청: {block.name}({block.input})")
                    result = await session.call_tool(block.name, block.input)
                    result_text = extract_tool_result_text(result)
                    print(f"  -> 결과: {result_text[:200]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })

                messages.append({"role": "user", "content": tool_results})
                print()

            return "[중단] 최대 턴 수(6회)를 초과해서 최종 답변을 만들지 못함"


async def main():
    print("=" * 60)
    print("실제 Claude API + 숙소 에이전트 MCP 연동 테스트")
    print("=" * 60 + "\n")

    # 실제 시나리오 예시 - 필요하면 이 문장을 자유롭게 바꿔서 다른 케이스도 테스트 가능함
    user_message = (
        "성인 2명이서 2026년 8월 1일부터 8월 3일까지 오사카 여행 가려고 해. "
        "쇼핑 위주로 다닐 건데 괜찮은 호텔 3개만 추천해줘."
    )
    print(f"[사용자 요청] {user_message}\n")

    result = await run_accommodation_agent(user_message)

    print("=" * 60)
    print("테스트 종료")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())