from django.urls import path
from . import views

urlpatterns = [
    # POST /api/v1/parse/
    # config/urls.py에서 "api/v1/parse/"로 include했으니까
    # 여기선 빈 문자열로 받으면 됨
    path("parse/", views.parse_request, name="parse-request"),
]
