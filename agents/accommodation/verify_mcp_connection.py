"""
=====================================================================
MCP 프로토콜 연동 검증 스크립트
=====================================================================

[이 파일이 하는 일]
    지금까지 한 테스트들은 accommodation_allocate_rooms() 같은 tool
    함수를 파이썬에서 "직접" 호출해본 거였음. 이건 우리 로직이 맞는지는
    검증하지만, "MCP 프로토콜을 통해 진짜로 통신이 되는지"는 검증하지
    못함 (직접 호출은 MCP 계층을 건너뛰기 때문).

    이 스크립트는 진짜 MCP client가 mcp_server.py를 별도 프로세스로
    띄우고, 표준 MCP 프로토콜(stdio)로 tool 목록을 조회하고 실제로
    tool을 호출해보는, "Claude 없이 할 수 있는 가장 실전에 가까운 검증"임.

    이게 통과하면: Claude가 붙었을 때도 최소한 "tool을 찾고 호출하는"
    기본 통신 자체는 문제없다는 확신을 가질 수 있음. (Claude가 상황에
    맞게 "판단"하는 부분은 이 스크립트로는 검증 불가 - 그건 오케스트레이터가
    완성된 후, 실제 Claude API를 붙여야만 확인 가능함)

[실행 방법]
    python agents/accommodation/verify_mcp_connection.py
=====================================================================
"""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


MCP_SERVER_PATH = os.path.join(os.path.dirname(__file__), "mcp_server.py")


async def main():
    print("=" * 60)
    print("MCP 프로토콜 연동 검증 시작")
    print("=" * 60)

    # mcp_server.py를 별도 프로세스로 실행하고, stdio(표준입출력)로
    # 연결하는 부분임. 이게 바로 "진짜 MCP 통신"이 시작되는 지점임.
    # mcp_server.py 프로세스에도 현재 환경변수(.env 로드값 포함)를 명시적으로 전달함.
    # (MCP는 보안을 위해 자식 프로세스에 부모 환경변수를 자동 상속시키지 않음)
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[MCP_SERVER_PATH],
        env=dict(os.environ),
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # 1) 핸드셰이크: 클라이언트-서버 간 프로토콜 버전 등을 확인하는 절차
            await session.initialize()
            print("\n[1] 서버 연결 및 초기화 성공\n")

            # 2) tool 목록 조회: 서버가 자기가 가진 tool들을 JSON 스키마
            #    형태로 클라이언트에게 알려주는 단계임. 이게 바로 Claude가
            #    "이런 도구들을 쓸 수 있구나"라고 파악하는 것과 동일한 절차임.
            tools_result = await session.list_tools()
            print(f"[2] tool 목록 조회 성공 - 총 {len(tools_result.tools)}개")
            for t in tools_result.tools:
                print(f"    - {t.name}")
            print()

            # 3) 실제 tool 호출: accommodation_allocate_rooms을 진짜 MCP
            #    프로토콜을 통해 호출해봄. 파라미터가 JSON으로 직렬화되고,
            #    서버가 그걸 파싱해서 실행하고, 결과를 다시 JSON으로
            #    돌려주는 전체 왕복 과정이 여기서 검증됨.
            print("[3] accommodation_allocate_rooms 실제 호출 테스트")
            result = await session.call_tool(
                "accommodation_allocate_rooms",
                arguments={"adults": 2, "children_ages": [4, 7]},
            )
            # 결과는 TextContent 리스트로 옴 - JSON 문자열을 파싱해서 확인
            result_text = result.content[0].text
            parsed = json.loads(result_text)
            print(f"    응답: {parsed}")
            assert "occupancies" in parsed, "occupancies 키가 응답에 없음"
            print("    -> 통과 (MCP 프로토콜 왕복 정상)\n")

            # 4) 도시 기반 검색조건 tool도 같은 방식으로 확인
            print("[4] accommodation_split_city_dates 실제 호출 테스트")
            result2 = await session.call_tool(
                "accommodation_split_city_dates",
                arguments={
                    "trip_start_date": "2026-08-01",
                    "city_nights": [{"city": "오사카", "nights": 2}],
                },
            )
            parsed2 = json.loads(result2.content[0].text)
            print(f"    응답: {parsed2}")
            assert "stays" in parsed2, "stays 키가 응답에 없음"
            print("    -> 통과\n")

            print("=" * 60)
            print("모든 MCP 연동 검증 통과")
            print("=" * 60)
            print("\n주의: 이 검증은 'tool이 정상 등록/호출되는지'까지만 확인함.")
            print("실제 Claude가 상황에 맞게 tool을 올바르게 '판단'해서 부르는지는")
            print("오케스트레이터 완성 후 실제 Claude API로 별도 확인이 필요함.")


if __name__ == "__main__":
    asyncio.run(main())