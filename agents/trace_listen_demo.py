"""
trace 실시간 구독 + 녹화 재생 검증 데모.

[실행 전 준비]
    1. docker compose up -d
    2. 워커 "재시작" — tasks.py는 워커가 켜질 때 등록되므로,
       이미 켜져 있던 워커는 새 태스크를 모른다. Ctrl+C 후 다시:
       celery -A config worker --pool=solo -l info
       ([tasks]에 agents.trace_demo가 보여야 함)

[실행 — 리포 루트에서]
    python -m agents.trace_listen_demo

[검증하는 것]
    워커(다른 프로세스)가 발행한 이벤트가
    ① 실시간 방송으로 이 스크립트에 도착하고
    ② 끝난 뒤 녹화본으로도 동일하게 재생되는가
"""

import json
import sys
import uuid   # 겹치지 않는 무작위 id를 만들어주는 표준 라이브러리

from agents.tasks import trace_demo
from agents.trace import EMOJI, get_events, open_subscription

# Windows 콘솔(cp949)에서 이모지 출력이 죽지 않도록 UTF-8로 전환 (PoC 방식)
sys.stdout.reconfigure(encoding="utf-8")

# uuid4() = 무작위 UUID 생성 → .hex = 하이픈 없는 32자리 문자열 → [:12] = 앞 12자만
# (데모니까 짧게. 본 구현의 runId도 이런 식으로 만들면 된다)
run_id = uuid.uuid4().hex[:12]
print(f"run_id = {run_id}")

# 1) 구독을 '먼저' 시작한다 — 방송 시작 전에 라디오를 켜야 첫 이벤트를 안 놓침.
#    (순서를 바꿔서 태스크부터 보내면 초반 이벤트는 방송으론 못 듣는다.
#     그래도 녹화본에는 남는다 — 이중 기록의 존재 이유)
pubsub = open_subscription(run_id)

# 2) 워커에게 데모 태스크 주문 (.delay = 큐에 넣고 즉시 반환, 1단계와 동일)
trace_demo.delay(run_id)

# 3) 실시간 수신. .listen()은 "이벤트가 올 때까지 기다렸다가 하나씩 내놓는"
#    블로킹 제너레이터 — for문이 무한히 돌며 도착할 때마다 한 바퀴씩 돈다.
#    ※ 몇 초가 지나도 아무것도 안 나오면 워커가 안 켜진 것 (Ctrl+C로 탈출)
print("— 실시간 방송 수신 중 —")
for message in pubsub.listen():
    # message는 {"type": ..., "channel": ..., "data": JSON문자열} 형태의 dict
    event = json.loads(message["data"])
    detail = f" — {event['detail']}" if event["detail"] else ""
    print(f"  {EMOJI.get(event['kind'], '·')} [{event['actor']}] {event['action']}{detail}")
    if event["kind"] == "done":   # 종료 신호를 받으면 수신 중단
        break
pubsub.close()   # 구독 정리 (연결 자원 반납)

# 4) 녹화 재생 — 폴링 API가 쓸 방식 그대로
events = get_events(run_id)
print(f"— 녹화본 재생: {len(events)}건 (늦게 접속한 사람은 이걸 본다) —")
for event in events:
    print(f"  {EMOJI.get(event['kind'], '·')} [{event['actor']}] {event['action']}")

print("실시간 방송 + 녹화 이중 기록 검증 완료!")