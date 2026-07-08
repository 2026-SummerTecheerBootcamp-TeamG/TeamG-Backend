from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from drf_spectacular.utils import extend_schema
from .serializers import SignupSerializer


class SignupView(APIView):
    @extend_schema(request=SignupSerializer, responses=SignupSerializer)
    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {"user_id": user.id, "message": "가입 성공"},
            status=status.HTTP_201_CREATED,
        )