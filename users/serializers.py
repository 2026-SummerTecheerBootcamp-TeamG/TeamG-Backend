"""
사용자 및 인증 관련 Serializer
"""

from django.contrib.auth import authenticate
from django.db import IntegrityError
from rest_framework import serializers

from rest_framework_simplejwt.tokens import RefreshToken

from .models import User


class SignupSerializer(serializers.ModelSerializer):
    """
    회원가입 Serializer

    Request:
    {
        "email": "user@example.com",
        "password": "securePassword123!",
        "nickname": "레이서킹",
        "nationality": "KR",
        "default_departure": {"city": "서울"}
    }
    """

    password = serializers.CharField(
        write_only=True,
        min_length=8,
        error_messages={"min_length": "비밀번호는 8자 이상이어야 합니다."},
        help_text="비밀번호 (최소 8자)",
    )
    nationality = serializers.CharField(
        max_length=2,
        required=False,
        allow_blank=True,
        error_messages={"max_length": "국가코드 2자리로 입력해 주세요. (예: KR)"},
        help_text="ISO 3166-1 alpha-2 국가코드 (예: KR)",
    )

    class Meta:
        model = User
        fields = ["email", "password", "nickname", "nationality", "default_departure"]

    def validate_email(self, value):
        """
        email 중복 검사
        """
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("이미 가입된 이메일입니다.")

        return value

    def validate_nationality(self, value):
        """
        nationality 형식 검사: ISO 3166-1 alpha-2 국가코드 (예: KR, JP, US)

        길이(2자)는 위 필드 선언에서 이미 검사하므로, 여기서는 알파벳 여부만
        확인하고 대문자로 정규화한다.
        """
        if not value:  # allow_blank=True 이므로 빈 값은 허용
            return value

        if not value.isalpha():
            raise serializers.ValidationError(
                "국가코드는 영문 2자리여야 합니다. (예: KR)"
            )

        return value.upper()

    def create(self, validated_data):
        """
        사용자 생성

        validate_email()에서 중복을 걸러내지만, 그 사이에 동일한 이메일이
        먼저 저장되면 DB unique 제약에 걸려 IntegrityError가 발생한다.
        그대로 두면 500이 나가므로 ValidationError로 변환한다.
        """
        try:
            return User.objects.create_user(**validated_data)
        except IntegrityError:
            raise serializers.ValidationError(
                {"email": ["이미 가입된 이메일입니다."]}
            )


class LoginSerializer(serializers.Serializer):
    """
    로그인 Serializer

    Request:
    {
        "email": "user@example.com",
        "password": "securePassword123!"
    }
    """

    email = serializers.EmailField(help_text="이메일 (로그인용)")
    password = serializers.CharField(write_only=True, help_text="비밀번호")

    def validate(self, attrs):
        """
        인증 처리

        계정이 없든 비밀번호가 틀리든 동일한 메시지를 반환한다.
        (어떤 이메일이 가입되어 있는지 노출하지 않기 위함)
        """
        user = authenticate(email=attrs["email"], password=attrs["password"])

        if user is None:
            raise serializers.ValidationError(
                {"detail": "이메일 또는 비밀번호가 올바르지 않습니다."}
            )

        attrs["user"] = user
        return attrs

    def create(self, validated_data):
        """
        토큰 발급
        """
        user = validated_data["user"]
        refresh = RefreshToken.for_user(user)

        return {
            "access_token": str(refresh.access_token),
            "refresh_token": str(refresh),
        }


class ProfileSerializer(serializers.ModelSerializer):
    """
    프로필 조회 Serializer

    GET /api/v1/users/me/profile 응답에 사용
    """

    user_id = serializers.IntegerField(source="id")

    class Meta:
        model = User
        fields = ["user_id", "nickname", "email", "nationality", "default_departure"]


class ProfileUpdateSerializer(serializers.ModelSerializer):
    """
    프로필 수정 Serializer — PATCH /api/v1/users/me/profile

    partial 업데이트 전제: 보낸 필드만 갱신된다 (안 보낸 필드는 그대로).
    """

    nationality = serializers.CharField(
        max_length=2,
        required=False,
        allow_blank=True,
        error_messages={"max_length": "국가코드 2자리로 입력해 주세요. (예: KR)"},
        help_text="ISO 3166-1 alpha-2 국가코드 (예: KR)",
    )

    class Meta:
        model = User
        fields = ["nickname", "email", "nationality", "default_departure"]
        extra_kwargs = {
            "nickname": {"required": False},
            "email": {"required": False},
            "default_departure": {"required": False},
        }

    def validate_email(self, value):
        """
        이메일 중복 검사 — 단, "내 현재 이메일 그대로"는 통과해야 하므로
        나 자신(self.instance)은 검사 대상에서 제외한다.
        """
        if User.objects.filter(email=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError("이미 사용 중인 이메일입니다.")
        return value

    def validate_nationality(self, value):
        """SignupSerializer와 동일 규칙: 영문 2자리, 대문자 정규화"""
        if not value:
            return value
        if not value.isalpha():
            raise serializers.ValidationError(
                "국가코드는 영문 2자리여야 합니다. (예: KR)"
            )
        return value.upper()

    def validate_default_departure(self, value):
        """
        기본 출발지는 {"city": "서울", "iata": "ICN"} 형태의 dict여야 한다.
        (파서가 프로필로 슬롯을 채울 때 dict를 기대 — 과거 문자열이 들어와
         통합 버그를 냈던 그 지점이라 형태를 여기서 강제한다)
        """
        if value is None:
            return value
        if not isinstance(value, dict) or not str(value.get("city") or "").strip():
            raise serializers.ValidationError(
                '기본 출발지는 {"city": "서울", "iata": "ICN"} 형태로 보내주세요.'
            )
        return value