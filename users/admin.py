from django.contrib import admin
from .models import User

# User모델 admin 페이지에 등록
@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "nickname", "nationality", "is_staff", "created_at")
    search_fields = ("email", "nickname")