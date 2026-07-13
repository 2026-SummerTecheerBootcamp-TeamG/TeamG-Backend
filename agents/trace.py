"""
trace 발행 모듈 - 에이전트 활동을 Redis로 중계함

필요한 이유: 실제 작업이 Celery 워커(별도 프로세스)에서 돌기 때문에
워커 안의 일을 밖(Django API)에서 보려면 프로세스 사이를 건너는 통로가 필요
그 통로가 Redis

이벤트 하나를 발행할 때 Redis에 두 가지를 동시에 함
1. PUBLISH trace:{run_id} -> 실시간 방송
2. RPUSH trace: {run_id}:events -> 녹화
실시간성과 접속 전 이벤트

run_id: 실행 1회를 구분하는 문자열
"""

import json
import os
import sys
import time

import redis

# Redis 연결
# DB 번호 용도 정리: 0 = Celery 결과, 1 = Django 캐시, 2 = trace
TRACE_REDIS_URL = os.environ.get("TRACE_REDIS_URL", "redis://localhost:6379/2")

# 모듈을 처음 import할 때 딱 한 번 연결 객체를 만들어 계속 재사용
_redis = redis.Redis.from_url(TRACE_REDIS_URL, decode_responses=True)

# 이벤트 종류 + 콘솔 표시용 이모지
EMOJI = {
    "user": "👤", "agent": "🤖", "api": "🌐", "llm": "🧠",
    "data": "📦", "db": "💾", "rule": "⚖️", "done": "🏁",
}

EVENTS_TTL = 60 * 60    # 녹화본 보관 시간(초)


def _channel(run_id):
    """방송 채널 이름"""
    return f"trace:{run_id}"


def _events_key(run_id):
    """녹화 리스트의 키 이름"""
    return f"trace:{run_id}:events"


def _console(line):
    """
    워커/데모 콘솔에도 한 줄 출력
    Windows 한국어 콘솔(cp949)은 이모지를 못 그려서 UnicodeEncodeError로 죽을 수 있음
    try/except로 감싸서 실패하면 ?로 바꿔 출력
    """

    try:
        print(line)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        # encode(errors="replace"): 표현 못 하는 글자를 ?로 대체
        print(line.encode(enc, errors="replace").decode(enc))


def publish(run_id, kind, actor, action, detail=""):
    """
    이벤트 1건 발행
    호출하는 쪽 사용법: trace.publish(run_id, "api", "google", "장소 검색", "오사카 카페")
    
    detail=""는 기본값 인자
    """

    event = {
        "t": round(time.time(), 3),     # 현재 시각
        "kind": kind,                   # 이벤트 종류
        "actor": actor,                 # 행위 주체 (오케스트레이터, google, claude...)
        "action": action,               # 무엇을 했나
        "detail": detail,               # 부가 정보
    }

    # ensure_ascii=False: 한글을 \uXXXX로 깨뜨리지 않고 그대로 저장
    payload = json.dumps(event, ensure_ascii=False)

    # pipeline: 명령 3개를 한 묶음으로 보내기
    pipe = _redis.pipeline()
    pipe.rpush(_events_key(run_id), payload)        # 녹화: 리스트 끝에 추가
    pipe.expire(_events_key(run_id), EVENTS_TTL)    # 녹화본 기한 갱신
    pipe.publish(_channel(run_id), payload)         # 방송: 구독자에게 송출
    pipe.execute()                                  # 묶음 실행

    # .get(키, 기본값): 키가 없어도 에러 대신 기본값
    emoji = EMOJI.get(kind, "·")
    # 조건부 덧붙이기: detail이 빈 문자열이면 뒤를 안 붙임
    _console(f"{emoji} [{actor}] {action}" + (f" - {detail}" if detail else ""))
    return event


def done(run_id, detail=""):
    """
    파이프라인 종료 신호
    구독자는 kind=done을 보고 수신을 멈춤
    """

    return publish(run_id, "done", "system", "완료", detail)


def get_events(run_id):
    """
    녹화본 전체 조회
    lrange(키, 0, -1): 리스트 처음부터 끝까지 전부
    """

    return [json.loads(raw) for raw in _redis.lrange(_events_key(run_id), 0, -1)]


def open_subscription(run_id):
    """
    실시간 방송 구독 시작
    ignore_subscribe_messages=True: 구독 시작됐다는 안내 메시지는 걸러내고 이벤트만 받겠다는 옵션
    반환된 pubsub 객체의 .listen()을 돌리면 이벤트가 올 때마다 하나씩 나옴
    """

    pubsub = _redis.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(_channel(run_id))
    return pubsub

