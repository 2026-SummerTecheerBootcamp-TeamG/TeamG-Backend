"""
agents 앱의 Celery 태스크 모음

중요한 이유: config/celery.py의 app.autodiscover_tasks()는 INSTALLED_APPS 각 앱에서 정확히 "tasks.py"라는 파일을 찾음
그래서 이 파일을 만들기만 하면 워커가 여기 태스크들을 자동 등록함
"""

import time
from datetime import date
import asyncio

# shared_task: config/celery.py의 app 객체를 직접 import하지 않고도 태스크를 등록하는 데코레이터
from celery import shared_task

from agents import trace
from agents.budget import allocate_budget
from agents.budget_explain import explain_allocation, language_for_nationality
from agents.itinerary import build_day_plan
from agents.itinerary_narrate import narrate_day_plan
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
    오케스트레이터(검색 단계만)를 워커에서 실행하는 태스크
    
    자연어 message 직접 입력용
    확정된 파싱 결과로 전체 흐름을 돌리는 건 아래가 담당
    """

    answer = asyncio.run(run_agent_loop(run_id, user_message))
    return {"run_id": run_id, "answer": answer}


@shared_task(name="agents.run_full_pipeline")
def run_full_pipeline(run_id, fields, nationality=None, plan_id=None):
    """
    확정된 파싱 결과로 전체 파이프라인을 실행하는 태스크
    
    fields = 파서 출력의 fields 그대로
    단계: 검색(Claude+MCP, 후보 수집) -> 예산 배분(결정론) -> 배분 설명(LLM) -> 일정+내러티브 -> 결과 dict 반환
    """

    # 1. 검색 단계
    # fields를 자연어 한 문장으로 조립해 Claude에게 줌
    dest_names = ", ".join(
        f"{d.get('city')}({d.get('iata')})" for d in fields["destinations"]
    )
    origin = fields.get("origin") or {}
    pax = fields.get("pax") or {}
    adults = pax.get("adult", 1)
    children = pax.get("child", 0)
    themes = fields.get("themes") or []

    search_message = (
        f"성인 {adults}명"
        + (f", 어린이 {children}명" if children else "")
        + f"이 {fields['dates']['start']}부터 {fields['dates']['end']}까지 "
        + f"{origin.get('city', '서울')}({origin.get('iata', 'ICN')})에서 "
        + f"{dest_names}(으)로 여행합니다. 테마: {', '.join(themes) or '없음'}. "
        + "왕복 항공권 후보와 호텔 후보를 검색하고 점수 평가까지 해주세요. "
        + "최종 답변은 검색 결과 요약만 간단히 작성하세요."
    )

    collected = {}  # 검색 후보가 담기는 곳
    search_summary = asyncio.run(
        run_agent_loop(run_id, search_message,
                       collected=collected, finish_trace=False)
    )
    flight_options = collected.get("flight_options", [])
    hotel_options = collected.get("hotel_options", [])
    trace.publish(run_id, "data", "orchestrator", "후보 수집 완료",
                  f"항공 {len(flight_options)}건 / 숙소 {len(hotel_options)}건")
    
    # 2. 예산 배분 (결정론)
    start = date.fromisoformat(fields["dates"]["start"])
    end = date.fromisoformat(fields["dates"]["end"])
    days = (end - start).days + 1
    travelers = adults + children

    allocation = allocate_budget(
        total_budget=fields["budget"],
        flight_options=flight_options,
        hotel_options=hotel_options,
        days=days,
        travelers=travelers,
    )
    trace.publish(run_id, "rule", "budget", "예산 배분 완료",
                  str(allocation.get("status", "")))
    
    # 3. 배분 설명 (LLM)
    request_summary = {
        "목적지": dest_names,
        "기간": f"{days}일",
        "인원": travelers,
        "테마": themes,
        "총예산_KRW": fields["budget"],
    }
    trace.publish(run_id, "llm", "claude", "배분 설명 생성")
    explanation = explain_allocation(
        request_summary, allocation, language_for_nationality(nationality)
    )

    # 4. 일정 + 내러티브
    trace.publish(run_id, "api", "google", "일정 장소 수집/동선 계산")
    plan_data = build_day_plan(
        fields["destinations"], themes, fields["dates"]["start"]
    )
    trace.publish(run_id, "llm", "claude", "일정 내러티브 생성")
    narrative = narrate_day_plan(
        plan_data["city"], themes, plan_data["day_plan"]
    )

    result = {
        "run_id": run_id,
        "search_summary": search_summary,
        "flight_options": flight_options,
        "hotel_options": hotel_options,
        "allocation": allocation,
        "explanation": explanation,
        "day_plan": plan_data["day_plan"],
        "narrative": narrative,
    }

    # 5. DB 저장
    if plan_id:
        # Django 모델은 함수 "안"에서 import 하는 게 의도적
        # 이 모듈은 Django 밖에서도 import 되는데, 모델을 파일 맨 위에서 import하면 그 순간 Django 초기화가 필요해져서 데모가 죽음
        # 워커에서는 Celery의 Django 연동이 초기화해 줌
        from trips.services import save_pipeline_result
        save_pipeline_result(plan_id, result)
        trace.publish(run_id, "db", "postgres", "플랜 저장 완료 (draft)",
                      f"plan_id={plan_id}")

    trace.done(run_id, "풀 파이프라인 완료")
    return result