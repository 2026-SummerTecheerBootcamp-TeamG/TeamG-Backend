from django.urls import path
from .views import ProfileView

urlpatterns = [
    path("me/profile", ProfileView.as_view()),
]