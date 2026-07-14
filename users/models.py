from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin

class UserManager(BaseUserManager):
    def create_user(self, email, password, nickname, **extra):
        if not email:
            raise ValueError("이메일은 필수입니다.")
        user = self.model(email=self.normalize_email(email), nickname=nickname, **extra)
        user.set_password(password)   
        user.save(using=self._db)
        return user
        
    # 관리자 계정 생성
    def create_superuser(self, email, password, nickname="admin", **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self.create_user(email, password, nickname, **extra)

class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    nickname = models.CharField(max_length=30)
    nationality = models.CharField(max_length=2, blank=True, default="")  # 국가코드 2자리
    default_departure = models.JSONField(null=True, blank=True) 

    is_active = models.BooleanField(default=True)  
    is_staff = models.BooleanField(default=False)  
    created_at = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "email"     
    REQUIRED_FIELDS = ["nickname"]  # createsuperuser 시 추가로 물어볼 필드

    def __str__(self):
        return self.email