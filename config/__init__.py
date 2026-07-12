"""
config 패키지 초기화 파일
"""

# .celery: 같은 폴더의 celery.py 모듈이라는 뜻
# app을 celery_app이라는 별명으로 가져옴
from .celery import app as celery_app

# __all__: 이 패키지에서 공식적으로 내놓는 이름 목록
# ("celery_app",)은 원소 1개짜리 튜플
__all__ = ("celery_app",)