"""
agents 앱의 API 뷰 모음

현재 엔드포인트:
    POST /api/v1/parse/  ← 자연어 입력 → 구조화 JSON 변환

작동 흐름:
    1. 프론트(ChatInput)에서 사용자 메시지 POST로 전송
    2. parse_intent()로 자연어 파싱
    3. validate_slots()로 필수 슬롯 확인
    4. 슬롯 완전 → ok:True + 파싱 결과 반환 (파이프라인 실행)
       슬롯 누락 → ok:False + 재질문 메시지 반환 (파이프라인 대기)
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

    이 클래스가 없으면 Swagger에서 요청 바디 입력칸이 안 생겨.
    실제 유효성 검사는 views.py 안에서 직접 하고,
    여기선 Swagger UI에 입력 필드를 보여주는 역할만 함.
    """
    message = serializers.CharField(
        help_text="자연어 여행 요청 (예: 후쿠오카 3박4일 쇼핑, 80만)"
    )


@extend_schema(request=ParseRequestSerializer)  # Swagger에 요청 바디 스키마 연결
@api_view(["POST"])  # POST 요청만 허용, 다른 메서드(GET 등)는 405 반환
@permission_classes([IsAuthenticated])  # 로그인한 유저만 호출 가능, 미로그인 시 401 반환
def parse_request(request):
    """
    자연어 입력을 받아 구조화 JSON으로 변환하는 엔드포인트.

    프론트의 ChatInput 컴포넌트가 이 API를 호출해.
    파싱 결과에 누락 슬롯이 있으면 재질문 메시지를 돌려주고,
    다 있으면 파싱 결과를 돌려줘서 파이프라인이 이어서 실행됨.

    Request Body:
        {
            "message": "후쿠오카 3박4일 쇼핑, 80만"
        }

    Response 200 - 슬롯 완전 (파이프라인 실행 가능):
        {
            "ok": true,
            "parsed": {
                "destination": [...],
                "nights": 3,
                "budget_total": 800000,
                ...
            }
        }

    Response 200 - 슬롯 누락 (재질문 필요):
        {
            "ok": false,
            "reask_message": "여행 기간이 어떻게 되나요?",
            "missing": ["nights"],
            "parsed": { ... }
        }

    Response 400 - message 필드 없음:
        {"error": "message 필드가 필요합니다."}

    Response 422 - Claude 응답 파싱 실패:
        {"error": "Claude 응답 JSON 파싱 실패: ..."}

    Response 500 - 서버 에러:
        {"error": "파싱 중 오류가 발생했습니다.", "detail": "..."}
    """

    # ── Step 1. 요청에서 message 꺼내기 ──────────────────────────────────
    # request.data는 DRF가 JSON 바디를 파싱한 결과 딕셔너리
    # .get("message", "")로 꺼내서 없으면 빈 문자열 반환
    # .strip()으로 앞뒤 공백 제거 ("  " 같은 공백만 있는 입력도 차단)
    message = request.data.get("message", "").strip()

    # message가 없거나 공백만 있으면 더 진행할 필요 없이 400 반환
    # 빈 메시지로 Claude API 호출하면 비용 낭비 + 의미없는 결과 나옴
    if not message:
        return Response(
            {"error": "message 필드가 필요합니다."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Step 2. 로그인한 유저 프로필 기본값 가져오기 ─────────────────────
    # 사용자가 출발지를 안 말했을 때 프로필의 기본값으로 자동 채움
    # 예: "후쿠오카 3박4일 쇼핑" → 출발지 없음 → 프로필의 ICN으로 채움
    # getattr(obj, name, default): obj에 name 속성이 없으면 default 반환
    user = request.user
    user_profile = {
        # default_origin_iata: 유저가 설정한 기본 출발 공항 코드
        # 없으면 "ICN"(인천)을 기본값으로 사용
        "origin_iata": getattr(user, "default_origin_iata", "ICN"),
        # nationality: 유저 국적 코드 (항공권 검색 시 사용)
        # 없으면 "KR"(한국)을 기본값으로 사용
        "nationality": getattr(user, "nationality", "KR"),
    }

    # ── Step 3. 자연어 파싱 실행 ─────────────────────────────────────────
    # parse_intent()는 agents/parser/intent_parser.py에 정의된 함수
    # 내부적으로 Claude API를 호출해서 자연어 → JSON 변환
    try:
        parsed = parse_intent(message, user_profile)

    except ValueError as e:
        # Claude 응답이 JSON 형식이 아닐 때 발생
        # intent_parser.py의 _extract_json()에서 던지는 에러
        # SYSTEM_PROMPT를 수정하거나 재시도하면 해결되는 경우가 많음
        return Response(
            {"error": str(e)},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    except Exception as e:
        # Claude API 키 오류, 네트워크 문제, 크레딧 부족 등
        # 예상치 못한 모든 에러를 여기서 잡아서 500으로 반환
        # detail에 실제 에러 메시지를 담아서 디버깅하기 쉽게
        return Response(
            {"error": "파싱 중 오류가 발생했습니다.", "detail": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # ── Step 4. 필수 슬롯 검증 (파이프라인 게이트) ───────────────────────
    # Duffel(항공), LiteAPI(숙소) 같은 외부 API는 호출할 때마다 비용 발생
    # 필수 정보(목적지, 기간, 예산)가 없는 상태로 파이프라인 실행하면
    # API만 낭비되고 결과도 의미없음 → 먼저 게이트에서 차단
    # validate_slots()는 agents/parser/slot_validator.py에 정의된 함수
    validation = validate_slots(parsed)

    if not validation["ok"]:
        # 누락 슬롯 있음 → 재질문 메시지 반환, 파이프라인 실행 안 함
        # 프론트는 이 응답을 받으면 reask_message를 채팅창에 표시하고
        # 사용자 답변을 기다렸다가 다시 이 API를 호출함
        return Response({
            "ok": False,
            "reask_message": validation["reask_message"],  # 사용자에게 보낼 질문
            "missing": validation["missing"],              # 누락된 필드 목록
            "parsed": parsed,  # 지금까지 파싱된 것도 같이 줘서 프론트가 활용 가능
        })

    # ── Step 5. 모든 슬롯 완전 → 파싱 결과 반환 ─────────────────────────
    # 이 응답을 받은 프론트는 파이프라인(항공/숙소 검색)을 이어서 실행
    # parsed 안에는 목적지, 기간, 예산, 테마 등 모든 정보가 담겨있음
    return Response({"ok": True, "parsed": parsed})