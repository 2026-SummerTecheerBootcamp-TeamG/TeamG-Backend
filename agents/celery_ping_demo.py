"""
Celery 왕복 검증 데모.

[실행 전 준비]
    1. docker compose up -d       (RabbitMQ/Redis 컨테이너 켜기)
    2. 다른 터미널에서 worker 켜기:
       celery -A config worker --pool=solo -l info

[실행 방법 — 반드시 리포 루트에서]
    python -m agents.celery_ping_demo

[이 데모가 확인하는 것]
    이 스크립트(생산자) → RabbitMQ(브로커) → worker(실행)
    → Redis(결과 저장) → 이 스크립트(결과 조회)  한 바퀴 왕복.
"""

# config 패키지를 import하는 순간 config/__init__.py가 실행되어
# Celery 앱이 로드된다. ping은 거기 등록된 태스크 함수.
from config.celery import ping

print("1) 태스크를 브로커(RabbitMQ)에 넣는 중...")

# ping("안녕 Celery")  ← 이렇게 그냥 부르면 "내 프로세스에서 즉시 실행"이라
#                        Celery를 전혀 안 거친다. (그건 그냥 함수 호출)
# ping.delay(...)      ← "주문서만 큐에 넣고 즉시 반환". 실제 실행은 worker가 한다.
# 반환값은 결과 자체가 아니라 AsyncResult 객체 = "결과 교환권"(작업 id 포함).
result = ping.delay("안녕 Celery")

# .id = 작업 고유 번호. 본 구현에서 API가 클라이언트에게 돌려줄 runId의 정체.
print(f"2) 접수 완료. task_id = {result.id}")

# .get(timeout=10) = 결과가 나올 때까지 최대 10초 기다렸다가 값을 꺼낸다.
# 10초 안에 안 나오면 TimeoutError 발생 → worker가 안 켜져 있다는 신호.
# (주의: 웹 요청 처리 코드에서 .get()으로 기다리면 비동기의 의미가 없어짐.
#  본 구현은 폴링/SSE로 조회한다. 데모니까 편하게 기다리는 것)
value = result.get(timeout=10)

print(f"3) 결과 수신: {value}")
print("왕복 성공 — Celery 뼈대 완성!")