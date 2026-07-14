"""
agents/parser/exceptions.py - 파서 전용 예외 클래스

[핫픽스 배경]
    intent_parser.py가 이 모듈을 import하는데 파일이 리포에 누락되어
    develop에서 서버가 기동되지 않던 문제의 복구 파일.
    (커밋 0331a50에서 import는 추가됐으나 파일이 함께 커밋되지 않음)

[설계 근거 — intent_parser.py의 실제 사용 방식에 맞춤]
    1. raise ClaudeResponseError("개발자용 메시지", user_message="사용자용 문구")
       형태로 호출되므로, user_message 키워드 인자를 받아 보관한다.
       (message = 로그/디버깅용 원본 에러, user_message = 챗봇 UI 표시용)
    2. ValueError를 상속해서 views.py의 기존 처리(except ValueError → 422)가
       이 예외들도 그대로 받아낸다 — intent_parser docstring의
       "→ views.py에서 422" 의도와 일치.
"""


class ParserError(ValueError):
    """파서 예외 공통 부모 — 개발자용 message + 사용자용 user_message 분리 보관."""

    def __init__(self, message, user_message=None):
        super().__init__(message)          # str(e)로 꺼내지는 개발자용 메시지
        self.user_message = user_message or message   # UI 표시용 (없으면 원본)


class ClaudeResponseError(ParserError):
    """Claude 응답이 JSON 형식이 아니어서 파싱에 실패했을 때 (재시도 소진 포함)."""
    pass


class NotTravelRelatedError(ParserError):
    """여행과 무관한 입력일 때 (예: "안녕", "오늘 날씨 어때?")."""
    pass
