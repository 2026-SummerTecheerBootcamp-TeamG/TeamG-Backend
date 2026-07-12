"""
Celery 앱 초기화 파일

1. 생산자(Django): 주문서를 써서 큐에 넣음
2. 브로커(RabbitMQ): 주문서가 쌓이는 대기열
3. 워커: 대기열에서 주문서를 꺼내 실제로 실행

실행 결과는 Redis에 저장되고, Django는 나중에 작업 id로 결과를 조회

이 파일의 역할: Celery 앱 객체를 만들어 Django 설정과 연결
워커를 켤 때 이 파일이 시작점이 됨
"""

import os

# celery 패키지에서 Celery 클래스 가져옴
from celery import Celery


# Django 설정 위치 알려주기
# 워커는 manage.py를 거치지 않기 때문에 Django 설정 위치를 환경변수로 알려줘야 함
# os.environ: 환경변수를 담은 딕셔너리 비슷한 객체
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Celery 앱 생성
# "config"는 이 앱의 이름표
app = Celery("config")


# Django settings.py에서 Celery 설정 읽어오기
# "django.conf:settings": django.conf 모듈 안의 settings객체
app.config_from_object("django.conf:settings", namespace="CELERY")

# 태스크 자동 발견
# INSTALLED_APPS에 등록된 각 앱 폴더에서 tasks.py 파일을 찾아 그 안의 @shared_task 함수들을 자동 등록
app.autodiscover_tasks()

# 왕복 검증용 ping 태스크
# @app.task: 데코레이터
# 바로 아래 함수를 "Celery 태스크"로 등록
@app.task(name="config.ping")
def ping(message="pong"):
    return {"reply": message, "worker": "alive"}