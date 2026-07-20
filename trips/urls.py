from django.urls import path
from . import views

urlpatterns = [
    # GET /api/v1/trips/    내 여행 계획 목록
    path("", views.trip_list, name="trip-list"),

    # GET /api/v1/trips/plans/{plan_id}/            플랜 상세
    path("plans/<int:plan_id>", views.plan_detail, name="plan-detail"),

    # POST /api/v1/trips/plans/{plan_id}/confirm/   플랜 확정
    path("plans/<int:plan_id>/confirm", views.plan_confirm, name="plan-confirm"),

    # POST /api/v1/trips/plans/{plan_id}/edits/     대화형 수정 접수
    path("plans/<int:plan_id>/edits", views.plan_edit, name="plan-edit"),

    # POST /api/v1/trips/plans/{plan_id}/rollback/  과거 버전을 새 버전으로 복사
    path("plans/<int:plan_id>/rollback", views.plan_rollback, name="plan-rollback"),

    # POST /api/v1/trips/plans/{plan_id}/select     후보 목록에서 항공/숙소 직접 선택(교체)
    path("plans/<int:plan_id>/select", views.plan_select_candidate, name="plan-select"),

    # POST /api/v1/trips/plans/{plan_id}/book/      숙소 예약 접수 (샌드박스, 에이전트 수행)
    path("plans/<int:plan_id>/book/", views.plan_book, name="plan-book"),

    # POST /api/v1/trips/plans/{plan_id}/ticket     항공 발권 접수 (자체 mock 공급자, 에이전트 수행)
    path("plans/<int:plan_id>/ticket", views.plan_ticket_flight, name="plan-ticket"),

    # PATCH /api/v1/trips/{request_id}/title        계획 이름(제목) 수정
    path("<int:request_id>/title", views.trip_update_title, name="trip-title"),

    # DELETE /api/v1/trips/{request_id}/            여행 요청 삭제
    path("<int:request_id>", views.trip_delete, name="trip-delete"),


]
