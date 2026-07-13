"""
오케스트레이션 레이어 최종 검증 데모 — API가 하게 될 일을 그대로 재현한다.

    이 스크립트(=미래의 Django API 역할)          워커(별도 프로세스)
    ─────────────────────────────────            ──────────────────
    1. run_id 발급 + trace 구독
    2. 태스크 접수 (.delay)          ──큐──▶     3. Claude 툴 루프 실행
    4. trace 방송 실시간 수신        ◀─Redis──      (MCP 서버 2대는 워커의
       (사용자가 보는 진행 화면)                     자식 프로세스로 뜬다)
    5. 결과 조회 (.get)             ◀─Redis──   6. 최종 답변 반환

[실행 전 준비]
    1. docker compose up -d
    2. 워커 ★재시작★ (tasks.py가 바뀌었으므로):
       celery -A config worker --pool=solo -l info
       ([tasks]에 agents.run_orchestrator가 보여야 함)

[실행 — 리포 루트에서]
    python -m agents.pipeline_demo
"""

import json
import sys
import uuid

from config.celery import app as celery_app
from agents.tasks import run_orchestrator
from agents.trace import EMOJI, open_subscription

sys.stdout.reconfigure(encoding="utf-8")

USER_MESSAGE = (
    "성인 2명이 2026년 8월 1일부터 8월 3일까지 서울(ICN)에서 오사카(KIX)로 "
    "여행을 가려고 해. 왕복 항공권 후보와 쇼핑하기 좋은 호텔 3개를 추천해줘."
)

# 1) runId 발급 — 본 구현에서 API가 202 응답에 담아줄 그 값
run_id = uuid.uuid4().hex[:12]
print(f"run_id = {run_id}")

# 2) 방송 청취를 먼저 시작 (태스크보다 먼저! — 첫 이벤트를 놓치지 않도록)
pubsub = open_subscription(run_id)

# 3) 접수 — .delay는 즉시 반환된다. 이 시점 이후는 전부 워커의 일.
async_result = run_orchestrator.delay(run_id, USER_MESSAGE)
print(f"접수 완료 (task_id = {async_result.id}) — 워커가 실행 중...\n")

# 4) trace 실시간 수신 — 사용자가 보게 될 진행 화면의 원형
print("--- 진행 상황 (워커 발신, 실시간) ---")
for message in pubsub.listen():
    event = json.loads(message["data"])
    detail = f" — {event['detail']}" if event["detail"] else ""
    print(f"  {EMOJI.get(event['kind'], '·')} [{event['actor']}] {event['action']}{detail}")
    if event["kind"] == "done":
        break
pubsub.close()

# 5) 결과 조회 — done 방송을 봤으니 결과는 이미 저장돼 있다 (금방 나옴)
result = async_result.get(timeout=30)

print("\n--- 최종 답변 (결과 백엔드에서 조회) ---")
print(result["answer"])
print(f"\nrunId 비동기 파이프라인 완성! (run_id={result['run_id']})")