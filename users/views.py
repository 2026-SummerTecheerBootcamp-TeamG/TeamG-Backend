from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from drf_spectacular.utils import extend_schema
from .serializers import SignupSerializer, LoginSerializer


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