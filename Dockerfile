# Django 앱 + Celery 워커 공용 이미지
# (코드와 패키지가 같으니 이미지는 하나, 실행 명령만 compose에서 다르게)

# 베이스: 파이썬 3.13 슬림판 (경량 리눅스 + 파이썬)
FROM python:3.13-slim

# 파이썬 출력 버퍼링 끔 - 로그가 docker logs에 즉시 보이게
ENV PYTHONUNBUFFERED=1

# 컨테이너 안 작업 폴더
WORKDIR /app

# requirements만 먼저 복사해서 설치 - 코드만 바뀌면 이 층은 캐시돼서 재빌드가 몇 초로 끝남
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 나머지 코드 전체 복사
COPY . .

# 기본 실행 명령: gunicorn (워커 컨테이너는 compose에서 command로 덮어씀)
# -k gevent: SSE 같은 오래 물고 있는 연결을 많이 감당하는 워커 방식
CMD ["gunicorn", "config.wsgi", "-k", "gevent", "-w", "2", "--bind", "0.0.0.0:8000"]
