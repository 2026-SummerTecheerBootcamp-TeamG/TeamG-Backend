from rest_framework import serializers
from django.contrib.auth import authenticate
from rest_framework_simplejwt.tokens import RefreshToken
from .models import User


class SignupSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ["email", "password", "nickname", "nationality", "default_departure"]

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("이미 가입된 이메일입니다.")
        return value

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)

class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        # 비밀번호 확인
        user = authenticate(email=data["email"], password=data["password"])
        if user is None:
            raise serializers.ValidationError("이메일 또는 비밀번호가 올바르지 않습니다.")
        data["user"] = user
        return data

    def create(self, validated_data):
        user = validated_data["user"]
        # refresh 토큰 생성 
        refresh = RefreshToken.for_user(user)
        return {
            "access_token": str(refresh.access_token),
            "refresh_token": str(refresh),
            "user_id": user.id,
        }

# 프로필 조회
class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["user_id", "nickname", "email", "nationality", "default_departure"]

    user_id = serializers.IntegerField(source="id")