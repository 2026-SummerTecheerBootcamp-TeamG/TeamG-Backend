from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.utils import extend_schema
from .serializers import SignupSerializer, LoginSerializer, ProfileSerializer


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