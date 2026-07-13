from django.urls import path
from . import views

urlpatterns = [
    # POST /api/v1/agents/parse/
    # 자연어 입력 → 구조화 JSON 변환
    path("parse/", views.parse_request, name="parse-request"),

    # POST /api/v1/agents/parse/answer/
    # 재질문 답변 병합 후 재파싱
    path("parse/answer/", views.parse_answer, name="parse-answer"),

    # GET /api/v1/agents/parse/{parse_id}/
    # 파싱 결과 조회 (프론트 확인 카드용)
    path("parse/<str:parse_id>/", views.parse_detail, name="parse-detail"),

    # POST /api/v1/agents/parse/{parse_id}/confirm/
    # 사용자 확정 → 파이프라인 실행 신호
    path("parse/<str:parse_id>/confirm/", views.parse_confirm, name="parse-confirm"),

    # POST /api/v1/agents/parse/{parse_id}/correct/
    # 사용자 정정 → 특정 필드 수정 후 재검증
    path("parse/<str:parse_id>/correct/", views.parse_correct, name="parse-correct"),

    # POST /api/v1/agents/runs/
    # 오케스트레이터 실행 접수 (202 + run_id) - parse/confirm 다음 단계
    path("runs/", views.run_create, name="run-create"),

    # GET /api/v1/agents/runs/{run_id}/
    # 실행 상태/진행 이벤트/결과 폴링 조회
    path("runs/<str:run_id>/", views.run_detail, name="run-detail"),
]