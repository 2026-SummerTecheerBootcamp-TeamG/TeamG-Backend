"""
Claude API 공용 클라이언트 모듈

TravelHelper의 4개 에이전트(항공/숙소/일정/예산)가 공통으로 사용하는
Claude API 호출 모듈입니다.

[사용법]
    from agents.claude_client import ask_claude

    answer = ask_claude(
        prompt="도쿄 3박 4일 일정 짜줘",          # 사용자 요청
        system="너는 여행 일정 전문 에이전트야.",  # 에이전트별 역할 정의
    )

[사전 준비]
    - backend/.env 파일에 ANTHROPIC_API_KEY가 있어야 합니다.
    - .env는 절대 커밋하지 않습니다. (.env.example 참고)
"""

import os

from dotenv import load_dotenv
import anthropic

# .env 파일의 내용을 환경변수로 로드한다.
# 이 줄이 실행되어야 아래에서 ANTHROPIC_API_KEY를 읽을 수 있다.
# (이미 시스템 환경변수에 값이 있으면 그 값을 우선 사용한다)
load_dotenv()

# Anthropic 클라이언트 인스턴스.
# 인자를 안 넘기면 환경변수 ANTHROPIC_API_KEY를 자동으로 찾아서 인증한다.
# 모듈 최상단에서 한 번만 생성하고, 모든 함수 호출이 이 인스턴스를 재사용한다.
# (호출할 때마다 새로 만들면 불필요한 오버헤드 발생)
_client = anthropic.Anthropic()

# 기본으로 사용할 Claude 모델.
# 모델 변경이 필요하면 이 상수만 바꾸거나, 호출 시 model 인자로 넘기면 된다.
DEFAULT_MODEL = "claude-sonnet-4-6"


def ask_claude(
    prompt: str,
    system: str = "",
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2048,
) -> str:
    """Claude에게 프롬프트를 보내고 텍스트 응답을 반환한다.

    각 에이전트는 system 프롬프트만 다르게 지정해서 이 함수를 재사용한다.
    예: 항공 에이전트는 "너는 항공권 검색 에이전트야..." 를 system으로 전달.

    Args:
        prompt: 사용자 요청 또는 에이전트가 처리할 입력 텍스트.
        system: 에이전트의 역할/규칙을 정의하는 시스템 프롬프트.
                빈 문자열이면 시스템 프롬프트 없이 호출한다.
        model: 사용할 Claude 모델명. 기본값은 DEFAULT_MODEL.
        max_tokens: 응답 최대 토큰 수. 응답이 이 길이에서 잘리므로
                    긴 답변(일정표 등)이 필요하면 값을 늘려서 호출할 것.

    Returns:
        Claude의 응답 텍스트 (str).

    Raises:
        anthropic.AuthenticationError: API 키가 없거나 잘못된 경우 (401)
        anthropic.RateLimitError: 요청 한도 초과 (429) — 잠시 후 재시도 필요
        anthropic.APIStatusError: 그 외 API 오류 (크레딧 부족 등)
    """
    # API 요청 파라미터 구성
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,  # 필수 파라미터 (없으면 400 에러)
        "messages": [
            # role은 "user"(사용자) / "assistant"(Claude) 두 종류.
            # 대화 이력을 이어가려면 이 리스트에 턴을 순서대로 쌓으면 된다.
            {"role": "user", "content": prompt}
        ],
    }

    # system 프롬프트는 messages 안이 아니라 별도 최상위 파라미터로 전달한다.
    # 빈 문자열을 넘기면 불필요하므로 값이 있을 때만 포함.
    if system:
        kwargs["system"] = system

    # Claude API 호출 (동기 방식 — 응답이 올 때까지 대기)
    response = _client.messages.create(**kwargs)

    # 응답 형식: response.content는 블록들의 리스트이며,
    # 일반 텍스트 응답은 첫 번째 블록의 .text에 들어 있다.
    # (나중에 tool use 등을 쓰면 블록이 여러 개일 수 있음)
    return response.content[0].text