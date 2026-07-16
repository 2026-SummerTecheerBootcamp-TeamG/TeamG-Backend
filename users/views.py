from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.utils import extend_schema
from .serializers import (
    SignupSerializer, LoginSerializer, ProfileSerializer, ProfileUpdateSerializer,
)


class SignupView(APIView):
    @extend_schema(request=SignupSerializer, responses=SignupSerializer, tags=["auth"])
    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {"user_id": user.id, "message": "가입 성공"},
            status=status.HTTP_201_CREATED,
        )

class LoginView(APIView):
    @extend_schema(request=LoginSerializer, responses=LoginSerializer, tags=["auth"])
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = serializer.save()
        return Response(result, status=status.HTTP_200_OK)

class ProfileView(APIView):
    permission_classes = [IsAuthenticated]   # 로그인(토큰)한 사용자만 접근 가능

    @extend_schema(responses=ProfileSerializer, tags=["users"])
    def get(self, request):
        serializer = ProfileSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(request=ProfileUpdateSerializer, responses=ProfileSerializer,
                   tags=["users"])
    def patch(self, request):
        """
        프로필 수정 — 닉네임/이메일/국적/기본 출발지 (보낸 필드만 갱신)

        partial=True: PATCH의 의미 그대로 "일부만" 보내도 된다.
        응답은 조회와 같은 ProfileSerializer — 프론트가 갱신값을 바로 반영.
        """
        serializer = ProfileUpdateSerializer(
            request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(ProfileSerializer(request.user).data,
                        status=status.HTTP_200_OK)