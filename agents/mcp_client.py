"""
MCP 멀티서버 클라이언트 허브(ToolHub)

하는 일: MCP 서버 2대(항공/숙소)를 자식 프로세스로 띄워 연결함
각 서버의 툴들을 Claude에게 줄 하나의 통합 목록으로 합침
툴 호출이 오면 그 툴을 가진 서버로 라우팅

async/await 문법 설명
MCP 라이브러리는 비동기(asyncio) 방식으로 만들어져 있음
- async def 함수: 기다림이 있는 함수
호출한다고 바로 실행되지 않고, await를 붙여야 실행되고 끝날 때까지 기다림
- await: 이 작업이 끝날 때까지 기다리라는 표시
- async with: with의 비동기 버전
"""

import os
import sys

# AsyncExitStack: 나중에 정리해야 할 자원을 쌓아두는 바구니
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agents import trace


# 서버 등록부
# 새 MCP 서버가 생기면 여기 한 줄 추가하면 됨
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SERVER_PATHS = {
    "flight": os.path.join(_REPO_ROOT, "agents", "flight", "mcp_server.py"),
    "accommodation": os.path.join(_REPO_ROOT, "agents", "accommodation", "mcp_server.py"),
}


def mcp_tool_to_claude_tool(mcp_tool):
    """
    MCP의 툴 정의를 Claude API의 tools 파라미터 형식으로 변환
    """
    return {
        "name": mcp_tool.name,
        "description": mcp_tool.description or "",
        "input_schema": mcp_tool.inputSchema,
    }


def extract_text(result):
    """
    MCP 툴 호출 결과에서 텍스트만 뽑아 이어붙임
    hasattr(객체, "속성이름"): 그 속성이 있는지 확인
    """

    texts = [block.text for block in result.content if hasattr(block, "text")]
    return "\n".join(texts)


class ToolHub:
    """
    사용법 (async 함수 안에서):
        hub = ToolHub(run_id)               # run_id 주면 모든 활동이 trace로 방송됨
        await hun.connect()                 # 서버 전부 기동 + 연결
        hub.claude_tools                    # Claude에게 줄 통합 툴 목록(6개)
        text = await hub.call(이름, 인자)    # 어느 서버 툴이든 이름만으로 호출
        await hub.close()                   # 서버 프로세스 정리
    """

    def __init__(self, run_id=None):
        self.run_id = run_id        # None이면 trace 발행 생략
        self._stack = None          # 자원 정리용 바구니
        self._session_by_tool = {}  # 툴 이름 -> 그 툴을 가진 서버 세션 라우팅 표
        self.claude_tools = []      # Claude 형식으로 변환된 통합 툴 목록

    def _trace(self, kind, actor, action, detail=""):
        """
        run_id가 있을 때만 trace 발행하는 내부 헬퍼
        """

        if self.run_id:
            trace.publish(self.run_id, kind, actor, action, detail)

    async def connect(self):
        """
        등록부의 모든 서버를 자식 프로세스로 띄우고 툴 목록을 수집함
        """

        self._stack = AsyncExitStack()

        for server_name, server_path in SERVER_PATHS.items():
            # 서버 실행 정보: 현재 파이썬으로 서버 실행
            # env=dict(os.environ): MCP는 보안상 부모의 환경변수를 자식에게 자동 상속하지 않음
            # APi 키가 든 환경변수를 명시적으로 전달함
            params = StdioServerParameters(
                command=sys.executable,
                args=[server_path],
                env=dict(os.environ),
            )

            # enver_async_context = "async with ...를 열고 바구니에 등록"과 같음
            # stdio_client가 자식 프로세스를 실제로 띄우는 지점
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()  # 핸드셰이크 (프로토콜 버전 확인)

            # 이 서버의 툴들을 조회해서 통합 목록과 라우팅 표에 등록
            tools_result = await session.list_tools()
            for t in tools_result.tools:
                self._session_by_tool[t.name] = session
                self._session_by_tool[t.name] = session
                self.claude_tools.append(mcp_tool_to_claude_tool(t))

            tool_names = [t.name for t in tools_result.tools]
            self._trace("agent", server_name, "MCP 서버 연결",
                        f"툴 {len(tool_names)}개: {', '.join(tool_names)}")
            
    async def call(self, tool_name, arguments):
        """
        툴 이름만으로 호출 - 어느 서버 소속인지는 라우팅 표가 앎
        반환은 항상 JSON 텍스트
        """

        session = self._session_by_tool.get(tool_name)
        if session is None:
            # Claude가 존재하지 않는 툴 이름을 지어내는 경우 방어
            # 에러로 죽이지 않고 문자열로 알려주면 Claude가 스스로 정정함
            return f'{{"error": "존재하지 않는 툴: {tool_name}"}}'
        
        self._trace("api", tool_name, "툴 호출", str(arguments)[:200])
        result = await session.call_tool(tool_name, arguments)
        text = extract_text(result)
        # 결과가 길 수 있으니 trace에는 앞 200자만
        self._trace("data", tool_name, "툴 응답", text[:200])
        return text
    
    async def close(self):
        """
        서버 프로세스들 정리
        """

        if self._stack:
            await self._stack.aclose()
