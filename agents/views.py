"""
agents 앱의 API 뷰 모음

현재 엔드포인트:
    POST /api/v1/agents/parse/  ← 자연어 입력 → 구조화 JSON 변환

작동 흐름:
    1. 프론트(ChatInput)에서 사용자 메시지 POST로 전송
    2. parse_intent()로 자연어 파싱
    3. validate_slots()로 필수 슬롯 확인
    4. 슬롯 완전 → 파싱 결과 반환 (파이프라인 실행)
       슬롯 누락 → 재질문 메시지 반환 (파이프라인 대기)
"""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status, serializers
from drf_spectacular.utils import extend_schema

from agents.parser import parse_intent, validate_slots


class ParseRequestSerializer(serializers.Serializer):
    """
    Swagger 문서용 요청 바디 스키마 정의.

    이 클래스가 없으면 Swagger에서 요청 바디 입력칸이 안 생김.
    실제 유효성 검사는 views.py 안에서 직접 하고,
    여기선 Swagger UI에 입력 필드를 보여주는 역할만 함.
    """
    message = serializers.CharField(
        help_text="자연어 여행 요청 (예: 도쿄 2박3일 미식, 30만)"
    )
    session_id = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="세션 ID (재질문 흐름에서 대화 맥락 유지용, 예: sess_1)"
    )


@extend_schema(request=ParseRequestSerializer)  # Swagger에 요청 바디 스키마 연결
@api_view(["POST"])          # POST 요청만 허용, 다른 메서드는 405 반환
@permission_classes([IsAuthenticated])  # 로그인한 유저만 호출 가능, 미로그인 시 401
def parse_request(request):
    """
    자연어 입력을 받아 API 명세서 형식의 구조화 JSON으로 변환하는 엔드포인트.

    프론트의 ChatInput 컴포넌트가 이 API를 호출함.
    파싱 결과에 누락 슬롯이 있으면 재질문 메시지를 돌려주고,
    다 있으면 파싱 결과를 돌려줘서 파이프라인이 이어서 실행됨.

    Request Body:
        {
            "message": "도쿄 2박3일 미식, 30만",
            "session_id": "sess_1"   ← 선택값, 재질문 흐름에서 사용
        }

    Response 200 - 슬롯 완전 (파이프라인 실행 가능):
        {
            "parse_id": "p_a3f2c1d4",
            "fields": {
                "origin": {"city": "서울", "iata": "ICN"},
                "destinations": [{"city": "도쿄", "nights": 2, ...}],
                "budget": 300000,
                "pax": {"adult": 1, "child": 0},
                "themes": ["미식"],
                "dates": {"start": "2026-03-10", "end": "2026-03-12"}
            },
            "assumed_fields": ["origin"],
            "missing_slots": [],
            "filled_from_profile": ["origin"],
            "warnings": []
        }

    Response 200 - 슬롯 누락 (재질문 필요):
        {
            "parse_id": "p_a3f2c1d4",
            "reask_message": "총 예산은 얼마 정도 생각하고 계세요?",
            "missing_slots": ["budget"],
            "fields": { ... }   ← 지금까지 파싱된 것도 같이 줌
        }

    Response 400 - message 필드 없음:
        {"error": "message 필드가 필요합니다."}

    Response 422 - Claude 응답 파싱 실패:
        {"error": "Claude 응답 JSON 파싱 실패: ..."}

    Response 500 - 서버 에러:
        {"error": "파싱 중 오류가 발생했습니다.", "detail": "..."}
    """

    # ── Step 1. 요청에서 message, session_id 꺼내기 ──────────────────────
    # request.data는 DRF가 JSON 바디를 파싱한 결과 딕셔너리
    # session_id는 선택값이라 없어도 됨
    message = request.data.get("message", "").strip()
    session_id = request.data.get("session_id", "")

    # message가 없거나 공백만 있으면 400 반환
    # 빈 메시지로 Claude API 호출하면 비용 낭비 + 의미없는 결과
    if not message:
        return Response(
            {"error": "message 필드가 필요합니다."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Step 2. 로그인한 유저 프로필 기본값 가져오기 ─────────────────────
    # 사용자가 출발지를 안 말했을 때 프로필의 기본값으로 자동 채움
    # getattr(obj, name, default): obj에 name 속성이 없으면 default 반환
    user = request.user
    user_profile = {
        # default_departure: 유저 모델의 기본 출발 공항 코드 필드명
        # users 앱에서 default_departure로 정의되어 있음
        "origin_iata": getattr(user, "default_departure", "ICN"),
        "nationality": getattr(user, "nationality", "KR"),
    }

    # ── Step 3. 자연어 파싱 실행 ─────────────────────────────────────────
    # parse_intent()는 agents/parser/intent_parser.py에 정의된 함수
    # 내부적으로 Claude API를 호출해서 자연어 → API 명세서 형식 JSON 변환
    try:
        parsed = parse_intent(message, user_profile)

    except ValueError as e:
        # Claude 응답이 JSON 형식이 아닐 때
        # SYSTEM_PROMPT 수정하거나 재시도하면 해결되는 경우가 많음
        return Response(
            {"error": str(e)},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    except Exception as e:
        # Claude API 키 오류, 네트워크 문제 등 예상 못한 에러
        return Response(
            {"error": "파싱 중 오류가 발생했습니다.", "detail": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # ── Step 4. 필수 슬롯 검증 (파이프라인 게이트) ───────────────────────
    # destinations, budget 중 하나라도 없으면 재질문
    # 외부 API(Duffel/LiteAPI) 호출 전에 필수 정보 확인해서 비용 낭비 방지
    validation = validate_slots(parsed)

    if not validation["ok"]:
        # 누락 슬롯 있음 → 재질문 반환, 파이프라인 실행 안 함
        # 프론트는 reask_message를 채팅창에 표시하고
        # 사용자 답변을 기다렸다가 parse/answer API를 호출함
        return Response({
            "parse_id":      parsed.get("parse_id"),
            "reask_message": validation["reask_message"],
            "missing_slots": validation["missing"],
            "fields":        parsed.get("fields"),
        })

    # ── Step 5. 모든 슬롯 완전 → API 명세서 형식으로 파싱 결과 반환 ──────
    # 이 응답을 받은 프론트는 파이프라인(항공/숙소 검색)을 이어서 실행
    return Response(parsed)