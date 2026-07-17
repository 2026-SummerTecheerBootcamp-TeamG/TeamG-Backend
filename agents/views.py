"""
agents 앱의 API 뷰 모음

현재 엔드포인트:
    POST /api/v1/agents/parse/         ← 자연어 입력 → 구조화 JSON 변환
    POST /api/v1/agents/parse/answer/  ← 재질문 답변 병합 후 재파싱

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
from django.http import StreamingHttpResponse
from django.core.cache import cache
from trips.services import create_request_and_plan

import uuid
import json
import time

# AsyncResult: task_id로 Celery 결과 백엔드에서 상태/결과를 조회하는 클래스
from celery.result import AsyncResult

import anthropic

from agents.tasks import run_orchestrator, run_full_pipeline
from agents.trace import get_events, open_subscription
from agents.parser import parse_intent, validate_slots


def _parse_error_response(e):
    """
    파싱 실패 응답 조립 — 원인을 사용자가 알 수 있는 문구로.

    Anthropic 서버 혼잡(529 Overloaded)이나 요청 한도(429)는 우리 서버
    장애가 아니라 일시적 혼잡이므로, 503(Service Unavailable)과 함께
    "잠시 후 재시도" 안내를 준다 (실사고 2026-07-16: 529가 사용자에게
    "파싱 중 오류"로만 보였음). SDK 자동 재시도(max_retries=4)를 다
    소진하고도 실패했을 때만 여기 도달한다.
    """
    if isinstance(e, anthropic.APIStatusError) and e.status_code in (429, 529):
        return Response(
            {"error": "지금 AI 응답이 밀려 있어요. 10초쯤 뒤에 다시 시도해 주세요."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response(
        {"error": "파싱 중 오류가 발생했습니다.", "detail": str(e)},
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _user_profile(user) -> dict:
    """
    parse_intent()에 넘길 유저 프로필.

    User.default_departure는 {"city": "Seoul", "iata": "ICN"} 형태의
    JSONField라서, origin_iata에는 iata 문자열만 뽑아 넣어야 한다.
    (그대로 dict를 넣으면 parsed["origin"]["iata"]가 dict가 되어버려서
    이후 "{iata}" 같은 문자열 조립에서 깨진다)
    """
    departure = getattr(user, "default_departure", None) or {}
    return {
        "origin_iata": departure.get("iata") if isinstance(departure, dict) else None,
        "nationality": getattr(user, "nationality", "KR"),
    }


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


@extend_schema(request=ParseRequestSerializer)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def parse_request(request):
    """
    자연어 입력을 받아 API 명세서 형식의 구조화 JSON으로 변환하는 엔드포인트.

    Request Body:
        {
            "message": "도쿄 2박3일 미식, 30만",
            "session_id": "sess_1"
        }

    Response 200 - 슬롯 완전:
        {"parse_id": "p_xxx", "fields": {...}, ...}

    Response 200 - 슬롯 누락:
        {"parse_id": "p_xxx", "reask_message": "...", "missing_slots": [...]}

    Response 400: {"error": "message 필드가 필요합니다."}
    Response 422: {"error": "Claude 응답 JSON 파싱 실패: ..."}
    Response 500: {"error": "파싱 중 오류가 발생했습니다.", "detail": "..."}
    """

    # ── Step 1. 요청에서 message, session_id 꺼내기 ──────────────────────
    message = request.data.get("message", "").strip()
    session_id = request.data.get("session_id", "")

    if not message:
        return Response(
            {"error": "message 필드가 필요합니다."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Step 2. 유저 프로필 기본값 가져오기 ──────────────────────────────
    user = request.user
    user_profile = _user_profile(user)

    # ── Step 3. 자연어 파싱 실행 ─────────────────────────────────────────
    try:
        parsed = parse_intent(message, user_profile)
    except ValueError as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except Exception as e:
        return _parse_error_response(e)

    # ── Step 4. 캐시에 저장 (재질문 흐름 대비) ───────────────────────────
    # parse/answer/ API에서 session_id로 꺼내서 원문과 병합할 수 있게
    # parse_id를 키로 원문 메시지와 파싱 결과를 캐시에 저장
    cache.set(f"parse:{parsed['parse_id']}", {
        "original_message": message,
        "parsed": parsed,
    }, timeout=60 * 30)  # 30분 후 자동 삭제

    # ── Step 5. 필수 슬롯 검증 (파이프라인 게이트) ───────────────────────
    # destinations, budget 중 하나라도 없으면 재질문
    validation = validate_slots(parsed)

    if not validation["ok"]:
        # 누락 슬롯 있음 → 재질문 반환, 파이프라인 실행 안 함
        return Response({
            "parse_id":      parsed.get("parse_id"),
            "reask_message": validation["reask_message"],
            "missing_slots": validation["missing"],
            "fields":        parsed.get("fields"),
        })

    # ── Step 6. 모든 슬롯 완전 → 파싱 결과 반환 ─────────────────────────
    return Response(parsed)


class ParseAnswerSerializer(serializers.Serializer):
    """
    Swagger 문서용 재질문 답변 요청 바디 스키마.

    parse/ API에서 missing_slots가 있을 때
    사용자가 답변을 보내면 이 API를 호출함.
    """
    session_id = serializers.CharField(
        help_text="parse/ API 호출 시 받은 parse_id (예: p_bf9f7542)"
    )
    answer = serializers.CharField(
        help_text="재질문에 대한 사용자 답변 (예: 성인 2명)"
    )


@extend_schema(request=ParseAnswerSerializer)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def parse_answer(request):
    """
    재질문 답변을 받아서 기존 파싱 결과와 병합 후 재파싱하는 엔드포인트.

    흐름:
        1. parse/ 호출 → missing_slots: ["dates"] → reask_message 반환
        2. 사용자가 "3월 10일 출발" 이라고 답변
        3. parse/answer/ 호출 → 원문 + 답변 병합 → 재파싱
        4. 최종 파싱 결과 반환

    Request Body:
        {
            "session_id": "p_bf9f7542",
            "answer": "성인 2명"
        }

    Response 200: {"parse_id": "p_new", "fields": {...}, ...}
    Response 400: {"error": "session_id와 answer 필드가 필요합니다."}
    Response 404: {"error": "세션을 찾을 수 없습니다. 처음부터 다시 입력해 주세요."}
    """

    # ── Step 1. 요청에서 session_id, answer 꺼내기 ───────────────────────
    session_id = request.data.get("session_id", "").strip()
    answer = request.data.get("answer", "").strip()

    if not session_id or not answer:
        return Response(
            {"error": "session_id와 answer 필드가 필요합니다."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Step 2. 캐시에서 기존 파싱 결과 꺼내기 ───────────────────────────
    # parse/ 호출 시 parse_id를 키로 저장해둔 것을 꺼냄
    # 30분 지나면 자동 삭제되어 None 반환
    cached = cache.get(f"parse:{session_id}")
    if not cached:
        return Response(
            {"error": "세션을 찾을 수 없습니다. 처음부터 다시 입력해 주세요."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # ── Step 3. 원문 + 답변 병합 후 재파싱 ──────────────────────────────
    # 원문: "도쿄 2박3일 미식, 30만"
    # 답변: "성인 2명"
    # 병합: "도쿄 2박3일 미식, 30만 성인 2명"
    original_message = cached.get("original_message", "")
    merged_message = f"{original_message} {answer}"

    user = request.user
    user_profile = _user_profile(user)

    try:
        parsed = parse_intent(merged_message, user_profile)
    except ValueError as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except Exception as e:
        return _parse_error_response(e)

    # ── Step 4. 재파싱 결과 캐시 업데이트 ───────────────────────────────
    # 아직 missing_slots가 있으면 또 재질문할 수 있으니까 캐시 업데이트
    cache.set(f"parse:{parsed['parse_id']}", {
        "original_message": merged_message,
        "parsed": parsed,
    }, timeout=60 * 30)

    # ── Step 5. 슬롯 검증 후 반환 ────────────────────────────────────────
    validation = validate_slots(parsed)

    if not validation["ok"]:
        return Response({
            "parse_id":      parsed.get("parse_id"),
            "reask_message": validation["reask_message"],
            "missing_slots": validation["missing"],
            "fields":        parsed.get("fields"),
        })

    return Response(parsed)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def parse_detail(request, parse_id):
    """
    파싱 결과 조회 엔드포인트.

    프론트가 확인 카드를 보여주기 위해 호출함.
    parse/ 호출 후 받은 parse_id로 캐시에서 파싱 결과를 꺼내서 반환.

    Response 200:
        {
            "parse_id": "p_xxx",
            "fields": { ... },
            "assumed_fields": [...],
            "missing_slots": [...],
            "filled_from_profile": [...],
            "warnings": [...]
        }

    Response 404: {"error": "파싱 결과를 찾을 수 없습니다."}
    """

    # ── Step 1. 캐시에서 파싱 결과 꺼내기 ───────────────────────────────
    # parse/ 호출 시 parse_id를 키로 저장해둔 것을 꺼냄
    cached = cache.get(f"parse:{parse_id}")
    if not cached:
        return Response(
            {"error": "파싱 결과를 찾을 수 없습니다. 만료되었거나 잘못된 ID입니다."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # ── Step 2. 파싱 결과 반환 ───────────────────────────────────────────
    return Response(cached.get("parsed"))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def parse_confirm(request, parse_id):
    """
    사용자 확정 엔드포인트.

    프론트에서 확인 카드의 "확정" 버튼을 누르면 이 API를 호출함.
    파이프라인(항공/숙소 검색) 실행 신호를 반환.
    잘못 이해한 상태로 파이프라인이 실행되는 것을 방지하는 게이트 역할.

    Response 200:
        {
            "parse_id": "p_xxx",
            "status": "confirmed",
            "fields": { ... }   ← 파이프라인에 넘길 파싱 결과
        }

    Response 404: {"error": "파싱 결과를 찾을 수 없습니다."}
    """

    # ── Step 1. 캐시에서 파싱 결과 꺼내기 ───────────────────────────────
    cached = cache.get(f"parse:{parse_id}")
    if not cached:
        return Response(
            {"error": "파싱 결과를 찾을 수 없습니다. 만료되었거나 잘못된 ID입니다."},
            status=status.HTTP_404_NOT_FOUND,
        )

    parsed = cached.get("parsed")

    # ── Step 2. 확정 상태로 캐시 업데이트 ───────────────────────────────
    # 확정된 파싱 결과임을 표시해서 파이프라인이 신뢰하고 사용할 수 있게
    cached["confirmed"] = True
    cache.set(f"parse:{parse_id}", cached, timeout=60 * 30)

    # ── Step 3. 확정 결과 반환 ───────────────────────────────────────────
    # 이 응답을 받은 프론트는 파이프라인(항공/숙소 검색)을 실행
    return Response({
        "parse_id": parse_id,
        "status":   "confirmed",
        "fields":   parsed.get("fields"),
    })


class ParseCorrectSerializer(serializers.Serializer):
    """
    Swagger 문서용 정정 요청 바디 스키마.

    사용자가 확인 카드에서 특정 필드를 정정할 때 사용.
    field: 수정할 필드명 (예: "budget", "destinations", "pax")
    value: 수정할 값 (예: 500000, "성인 2명")
    """
    field = serializers.CharField(
        help_text="수정할 필드명 (예: budget, destinations, pax, dates)"
    )
    value = serializers.CharField(
        help_text="수정할 값을 자연어로 입력 (예: 50만원, 성인 2명, 3박4일)"
    )


@extend_schema(request=ParseCorrectSerializer)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def parse_correct(request, parse_id):
    """
    사용자 정정 엔드포인트.

    프론트에서 확인 카드의 특정 필드를 수정하면 이 API를 호출함.
    수정된 값을 자연어로 받아서 재파싱 후 슬롯 재검증.

    Request Body:
        {
            "field": "budget",
            "value": "50만원"
        }

    Response 200 - 정정 후 슬롯 완전:
        {
            "parse_id": "p_new",
            "status": "corrected",
            "fields": { ... }
        }

    Response 200 - 정정 후 슬롯 누락:
        {
            "parse_id": "p_new",
            "status": "corrected",
            "reask_message": "...",
            "missing_slots": [...]
        }

    Response 400: {"error": "field와 value 필드가 필요합니다."}
    Response 404: {"error": "파싱 결과를 찾을 수 없습니다."}
    """

    # ── Step 1. 요청에서 field, value 꺼내기 ─────────────────────────────
    field = request.data.get("field", "").strip()
    value = request.data.get("value", "").strip()

    if not field or not value:
        return Response(
            {"error": "field와 value 필드가 필요합니다."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Step 2. 캐시에서 기존 파싱 결과 꺼내기 ───────────────────────────
    cached = cache.get(f"parse:{parse_id}")
    if not cached:
        return Response(
            {"error": "파싱 결과를 찾을 수 없습니다. 만료되었거나 잘못된 ID입니다."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # ── Step 3. 원문 + 정정 내용 병합 후 재파싱 ─────────────────────────
    # 원문: "후쿠오카 3박4일 쇼핑, 80만"
    # 정정: field="budget", value="50만원"
    # 병합: "후쿠오카 3박4일 쇼핑, 80만 예산은 50만원으로 수정"
    original_message = cached.get("original_message", "")
    merged_message = f"{original_message} {field}은(는) {value}으로 수정"

    user = request.user
    user_profile = _user_profile(user)

    try:
        parsed = parse_intent(merged_message, user_profile)
    except ValueError as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except Exception as e:
        return _parse_error_response(e)

    # ── Step 4. 정정된 결과 캐시 저장 ───────────────────────────────────
    cache.set(f"parse:{parsed['parse_id']}", {
        "original_message": merged_message,
        "parsed": parsed,
    }, timeout=60 * 30)

    # ── Step 5. 슬롯 검증 후 반환 ────────────────────────────────────────
    validation = validate_slots(parsed)

    if not validation["ok"]:
        return Response({
            "parse_id":      parsed.get("parse_id"),
            "status":        "corrected",
            "reask_message": validation["reask_message"],
            "missing_slots": validation["missing"],
            "fields":        parsed.get("fields"),
        })

    return Response({
        "parse_id": parsed.get("parse_id"),
        "status":   "corrected",
        "fields":   parsed.get("fields"),
    })


class RunCreateSerializer(serializers.Serializer):
    """Swagger 문서용 실행 접수 요청 바디 스키마"""
    parse_id = serializers.CharField(
        required=False, allow_blank=True,
        help_text="확정(confirm)된 파싱 ID -> 풀 파이프라인 실행 (권장)"
    )
    message = serializers.CharField(
        required=False, allow_blank=True,
        help_text="자연어 요청 -> 검색 단계만 실행 (데모/디버깅용)"
    )



@extend_schema(request=RunCreateSerializer)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def run_create(request):
    """
    오케스트레이터 실행 접수 엔드포인트
    
    pipeline_demo가 하던 일을 HTTP로 옮긴 것:
    run_id 발급 -> 태스크를 큐에 접수 -> 즉시 202 반환
    실제 실행은 Celery 워커에서 진행되고, 프론트는 응답의 run_id로 GET /runs/{run_id}/를 폴링함
    
    Response 202:
        {"run_id": "a1b2c3...", "task_id": "...", "status": "accepted"}
    Response 400: {"error": "message 필드가 필요합니다."}
    """
    
    # 1. parse_id 우선, 없으면 message 꺼내기
    parse_id = request.data.get("parse_id", "").strip()
    message = request.data.get("message", "").strip()

    if not parse_id and not message:
        return Response(
            {"error": "parse_id 또는 message 중 하나가 필요합니다."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    run_id = uuid.uuid4().hex[:12]

    # 2. 실행 경로 선택
    if parse_id:
        # 확정된 파싱 결과 -> 풀 파이프라인
        cached = cache.get(f"parse:{parse_id}")
        if not cached:
            return Response(
                {"error": "파싱 결과를 찾을 수 없습니다. 만료되었거나 잘못된 ID입니다."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not cached.get("confirmed"):
            # 확인 카드에서 확정을 안 누른 요청은 실행 금지
            return Response(
                {"error": "확정되지 않은 파싱입니다. confirm을 먼저 호출하세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        fields = cached["parsed"]["fields"]
        dates = fields.get("dates") or {}
        if not dates.get("start") or not dates.get("end"):
            return Response(
                {"error": "여행 날짜가 없습니다. 날짜를 포함해 다시 요청해 주세요. (예: 8월 1일부터 3일까지)"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # 과거 날짜 차단 — 파서가 연도를 과거로 찍으면 항공 검색이 400으로
        # 전멸한다 (실사고). ISO 형식(YYYY-MM-DD)은 문자열 비교 = 날짜 비교.
        from datetime import date as _date
        if dates["start"] < _date.today().isoformat():
            return Response(
                {"error": f"출발일({dates['start']})이 과거입니다. 미래 날짜로 다시 요청해 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # raw_input에 원문 메시지도 함께 보존 — 파싱 캐시(30분)가 증발해도
        # 마이페이지에서 계획을 다시 열 때 대화 첫 줄(사용자 요청)을 복원할 수 있게
        trip_request, plan = create_request_and_plan(
            request.user, fields, {
                "original_message": cached.get("original_message", ""),
                "parsed": cached.get("parsed"),
            }
        )
        nationality = getattr(request.user, "nationality", None)
        async_result = run_full_pipeline.delay(run_id, fields, nationality, plan.id)
    else:
        # 자연어 직접 입력
        async_result = run_orchestrator.delay(run_id, message)

    # 3. run_id -> task_id 매핑을 캐시에 저장
    # trace는 run_id 기준, Celery 결과는 task_id 기준으로 저장됨
    cache.set(f"run:{run_id}", {
        "task_id": async_result.id,
        "plan_id": plan.id if parse_id else None,
    }, timeout=60 * 60)

    # 4. 202 Accepted 반환
    return Response(
        {"run_id": run_id, "task_id": async_result.id, 
         "plan_id": plan.id if parse_id else None, "status": "accepted"},
        status=status.HTTP_202_ACCEPTED,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def run_detail(request, run_id):
    """
    실행 상태 폴링 엔드포인트
    
    프론트가 1~2초 간격으로 호출해서
    - events(trace 녹화본)로 진행 화면을 갱신하고
    - status가 completed가 되면 answer를 표시함
    
    Response 200:
        {
            "run_id": "...",
            "status": "running" | "completed" | "failed",
            "events": [{"t":.., "kind":.., "actor":.., "action":.., "detail":..}, ...],
            "answer": "최종 답변" (completed일 때만, 그 외 null)
        }
    Response 404: {"error": "run_id를 찾을 수 없습니다..."}
    """

    # 1. run_id -> task_id 매핑 조회
    mapping = cache.get(f"run:{run_id}")
    if not mapping:
        return Response(
            {"error": "run_id를 찾을 수 없습니다. 만료되었거나 잘못된 ID입니다."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # 2. 진행 이벤트(trace 녹화본) 조회
    events = get_events(run_id)

    # 3. Celery 결과 백엔드에서 상태 판정
    async_result = AsyncResult(mapping["task_id"])

    if async_result.state == "SUCCESS":
        run_status = "completed"
        result_payload = async_result.result
    elif async_result.state == "FAILURE":
        run_status = "failed"
        result_payload = None
    else:
        run_status = "running"
        result_payload = None

    return Response({
        "run_id": run_id,
        "status": run_status,
        "events": events,
        "result": result_payload,
    })


def _sse_format(event):
    """
    이벤트 1건을 SSE 전송 형식으로 포장
    SSE는 단순한 텍스트 규약:
        id: <이벤트 식별자>
        data: <내용 한 줄>
        (빈 줄 = 이벤트 끝)
    id에 trace 타임스탬프(t)를 쓰는 게 이어받기의 핵심
    브라우저가 재접속할 때 마지막으로 받은 id를 자동으로 보내주기 때문
    """

    return f"id: {event['t']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def _sse_generator(run_id, last_t):
    """
    SSE 본체
    순서가 유실/중복 방지의 핵심
        - 방송 구독을 먼저 킴
        - 녹화본 재생
        - 실시간 방송으로 전환
    재생과 방송이 겹치는 구간은 타임스탬프 비고로 걸러냄
    """
    yielded_t = last_t      # 마지막으로 보낸 이벤트의 t
    pubsub = open_subscription(run_id)  # 방송 먼저
    try:
        # 녹화 재생 - 이어받기 지점 이후 것만
        for event in get_events(run_id):
            if event["t"] <= yielded_t:
                continue
            yielded_t = event["t"]
            yield _sse_format(event)
            if event["kind"] == "done":
                return      # 이미 끝난 실행
        
        # 실시간 방송 (최대 5분 안전장치)
        deadline = time.time() + 300
        while time.time() < deadline:
            # get_message(timeout=1.0): 1초까지 기다렸다가 없으면 None
            # listen()과 달리 주기적으로 깨어나므로 keep-alive와 시간제한이 가능
            message = pubsub.get_message(timeout=1.0)
            if message is None:
                # ":"로 시작하는 줄 = SSE 주석
                # 브라우저는 무시하지만 중간 장비들이 연결이 살아있다고 인식하게 해줌
                yield ": keep-alive\n\n"
                continue
            event = json.loads(message["data"])
            if event["t"] <= yielded_t:     # 재생 구간과 겹친 방송 제거
                continue
            yielded_t = event["t"]
            yield _sse_format(event)
            if event["kind"] == "done":
                return
    finally:
        pubsub.close()      # 어떤 경로로 끝나든 구독 정리


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def run_stream(request, run_id):
    """
    SSE 스트림: 폴링 대신 서버가 이벤트를 실시간으로 밀어줌
    프론트는 이걸 구독해서 진행 화면을 갱신하고, kind=done에서 결과 조회로 전환
    
    재접속 이어받기: 브라우저/클라이언트가 Last-Event-ID 헤더로 마지막 수신 id(=t)를 보내면 그 이후 이벤트부터 재생됨
    """

    mapping = cache.get(f"run:{run_id}")
    if not mapping:
        return Response({"error": "run_id를 찾을 수 없습니다."},
                        status=status.HTTP_404_NOT_FOUND)
    
    # 재접속이면 Last-Event-ID가 옴
    last_t = float(request.headers.get("Last-Event-ID", 0) or 0)

    response = StreamingHttpResponse(
        _sse_generator(run_id, last_t),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"  # 중간 캐시가 스트림에 붙잡지 않게
    response["X-Accel-Buffering"] = "no"    # Nginx가 버퍼링하지 않게
    return response

