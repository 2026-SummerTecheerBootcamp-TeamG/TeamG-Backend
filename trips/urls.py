from django.urls import path
from . import views

urlpatterns = [
    # GET /api/v1/trips/    내 여행 계획 목록
    path("", views.trip_list, name="trip-list"),

    # GET /api/v1/trips/plans/{plan_id}/    플랜 상세
    path("plans/<int:plan_id>/", views.plan_detail, name="plan-detail"),

    # POST /api/v1/trips/plans/{plan_id}/confirm/   플랜 확정
    path("plans/<int:plan_id>/confirm/", views.plan_confirm, name="plan-confirm"),

    # POST /api/v1/trips/plans/{plan_id}/edits/     대화형 수정 접수
    path("plans/<int:plan_id>/edits/", views.plan_edit, name="plan-edit"),

    
]
