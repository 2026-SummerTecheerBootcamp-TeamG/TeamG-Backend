"""
agents 앱의 Celery 태스크 모음

중요한 이유: config/celery.py의 app.autodiscover_tasks()는 INSTALLED_APPS 각 앱에서 정확히 "tasks.py"라는 파일을 찾음
그래서 이 파일을 만들기만 하면 워커가 여기 태스크들을 자동 등록함
"""

import time
import asyncio

# shared_task: config/celery.py의 app 객체를 직접 import하지 않고도 태스크를 등록하는 데코레이터
from celery import shared_task

from agents import trace
from agents.orchestrator import run_agent_loop


@shared_task(name="agents.trace_demo")
def trace_demo(run_id):
    """
    trace 왕복 검증용 가짜 파이프라인
    실제 API는 안 부르고 time.sleep으로 일하는 척만 하며 각 단계에서 trace 이벤트를 발행
    """

    trace.publish(run_id, "agent", "orchestrator", "데모 파이프라인 시작")

    trace.publish(run_id, "api", "google", "장소 검색(모의)", "0.5초 걸리는 척")
    time.sleep(0.5)
    trace.publish(run_id, "data", "google", "후보 3건 수신(모의)")

    trace.publish(run_id, "llm", "claude", "추천 문구 생성(모의)", "1초 걸리는 척")
    time.sleep(1)

    trace.publish(run_id, "db", "postgres", "플랜 저장(모의)")
    trace.done(run_id, "데모 파이프라인 종료")

    return {"run_id": run_id, "events": 6}


@shared_task(name="agents.run_orchestrator")
def run_orchestrator(run_id, user_message):
    """
    오케스트레이터를 워커에서 실행하는 태스크
    
    왜 필요한가
        run_agent_loop는 20초 이상 걸릴 수 있는 작업
        웹 요청 처리 중에 직접 부르면 사용자가 빈 화면을 20초 보게 되므로 API는 이 태스크를 큐에 넣고 runId만 즉시 돌려줌
        진행 상황은 trace 방송으로, 최종 결과는 이 태스크의 반환값으로 조회

    반환값은 결과 백엔드(Redis DB 0)에 JSON으로 저장
    """

    answer = asyncio.run(run_agent_loop(run_id, user_message))
    return {"run_id": run_id, "answer": answer}