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
        # .get(key, default)는 키가 "없을" 때만 default를 쓴다. origin이
        # {"city": None, "iata": None}처럼 값이 채워진 채로 비어있으면 그대로
        # None이 나와 "None(None)에서"라는 문장이 만들어지므로 or로 방어한다.
        + f"{origin.get('city') or '서울'}({origin.get('iata') or 'ICN'})에서 "
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


@shared_task(name="agents.run_local_edit")
def run_local_edit(run_id, plan_id, edit_request):
    """
    국소 수정 태스크: 기존 플랜 로드 -> LLM 편집(이름만) -> 새 버전 재조립 저장
    LLM 호출이 수 초 걸리므로 생성 파이프라인과 동일하게 비동기 패턴
    """

    from trips.models import Plan
    from trips.services import load_day_plan, create_edited_version
    from agents.plan_editor import edit_day_plan

    old_plan = Plan.objects.get(id=plan_id)
    day_plan = load_day_plan(old_plan)

    edited = edit_day_plan(run_id, day_plan, edit_request)

    new_plan, dropped = create_edited_version(old_plan, edited, edit_request)

    from trips.services import load_day_plan
    from agents.itinerary_narrate import narrate_day_plan

    trace.publish(run_id, "llm", "claude", "수정본 설명문 재생성")
    new_day_plan = load_day_plan(new_plan)
    cities = ", ".join(d.city_name for d in new_plan.request.destinations.all())
    new_plan.narrative = narrate_day_plan(
        cities, new_plan.request.themes or [], new_day_plan
    )
    new_plan.save()
    trace.publish(run_id, "db", "postgres", "새 버전 저장 (draft)",
                  f"plan {plan_id} -> {new_plan.id}"
                  + (f" · 제외된 미확인 장소 {len(dropped)}건" if dropped else ""))
    trace.done(run_id, "국소 수정 완료")

    return {
        "run_id": run_id,
        "old_plan_id": plan_id,
        "new_plan_id": new_plan.id,
        "summary": edited.get("summary", ""),
        "dropped_names": dropped,
    }


@shared_task(name="agents.run_replan")
def run_replan(run_id, old_plan_id, new_plan_id, edit_request):
    """
    재계획: 조건이 바뀌는 수정 -> 재파싱 -> 요청 갱신 -> 전체 파이프라인 재실행
    
    핵심 재사용 두 가지:
    - 재파싱: PoC 슬롯 게이트의 병합 방식
    - 실행: run_full_pipeline을 .delay 없이 그냥 함수로 호출 - 이미 워커 안이므로 또 큐에 넣을 필요 없이 이 자리에서 이어서 실행하면 됨
    """

    from trips.models import Plan
    from trips.services import update_request_fields
    from agents.parser import parse_intent

    old_plan = Plan.objects.get(id=old_plan_id)
    tr = old_plan.request   # 갱신 대상 요청

    # 1. 저장된 요청을 문장으로 복원 + 수정 요청 병합
    dest_txt = ", ".join(d.city_name for d in tr.destinations.all())
    base = (
        f"{tr.departure} 출발, {dest_txt} 여행. "
        f"{tr.start_date}부터 {tr.end_date}까지, "
        f"성인 {tr.adult}명 어린이 {tr.kid}명, 예산 {tr.total_budget}원, "
        f"테마: {', '.join(tr.themes or []) or '없음'}"
    )
    merged = f"{base}. 수정 요청: {edit_request}"
    trace.publish(run_id, "llm", "parser", "재계획 재파싱", merged[:120])

    profile = {
        "origin_iata": tr.origin_iata or "ICN",
        "nationality": getattr(tr.user, "nationality", "KR") or "KR",
    }
    parsed = parse_intent(merged, profile)
    fields = parsed.get("fields") or {}

    # 2. 날짜 필수 + 과거 날짜 검증
    dates = fields.get("dates") or {}
    if not dates.get("start") or not dates.get("end"):
        trace.done(run_id, "재계획 중단: 날짜 확인 불가")
        return {
            "run_id": run_id,
            "error": "수정 요청을 반영하면 날짜를 확정할 수 없습니다. "
                     "날짜를 포함해 다시 요청해 주세요.",
        }
    # 과거 날짜면 검색 API가 400으로 전멸하므로 여기서 중단 (실사고:
    # 파서가 "9월 5일"의 연도를 과거로 찍음 → 항공/숙소 모두 빈손)
    if dates["start"] < date.today().isoformat():
        trace.done(run_id, f"재계획 중단: 과거 날짜 ({dates['start']})")
        return {
            "run_id": run_id,
            "error": f"출발일({dates['start']})이 과거로 해석됐습니다. "
                     "연도를 포함해 다시 요청해 주세요. (예: 2026년 9월 5일부터)",
        }
    
    # 3. 요청 갱신 -> 파이프라인 재실행
    update_request_fields(tr, fields, parsed)
    trace.publish(run_id, "db", "postgres", "요청 조건 갱신", f"request {tr.id}")

    # .delay가 아니라 직접 호출 - 반환값도 그대로 이 태스크의 결과가 됨
    return run_full_pipeline(
        run_id, fields, profile["nationality"], new_plan_id
    )


@shared_task(name="agents.run_booking")
def run_booking(run_id, plan_id, first_name, last_name, email):
    """
    숙소 예약 태스크 (샌드박스) — A방식의 확장판.

    Claude에게 예약 임무와 booking 툴 3종(요금 재조회/가예약/확정)을 주면,
    스스로 순서를 밟아 예약을 완수한다. 저장된 요금이 만료된 경우의 재조회,
    offer 만료 시의 재시도 같은 상황 대응도 Claude의 판단에 맡긴다.
    결제 수단은 booking_confirm 툴 내부에 격리 — LLM은 카드번호를 만질 수 없다.
    """
    from trips.models import Plan
    from trips.services import save_booking

    plan = Plan.objects.get(id=plan_id)
    hotel = getattr(plan, "hotel", None)
    if hotel is None:
        trace.done(run_id, "예약 중단: 선택된 숙소 없음")
        return {"run_id": run_id,
                "error": "이 플랜에는 선택된 숙소가 없어 예약할 수 없습니다."}

    tr = plan.request
    mission = (
        f"다음 호텔을 예약하세요 (샌드박스 — 실제 결제 없음).\n"
        f"- 호텔 ID: {hotel.liteapi_hotel_id}\n"
        f"- 체크인: {tr.start_date} / 체크아웃: {tr.end_date}\n"
        f"- 성인: {tr.adult}명\n"
        f"- 게스트: {first_name} {last_name} ({email})\n"
        f"절차: ① 요금 조회 도구로 최신 요금 확인 (저장된 요금은 만료됨) "
        f"② 가장 저렴한 offer로 가예약(prebook) ③ 예약 확정(book/confirm). "
        f"제공된 도구 중 예약 계열(booking_* 또는 liteapi 공식 도구)을 사용하고, "
        f"offer 만료 오류가 나면 ①부터 다시 시도하세요. "
        f"결제 정보를 지어내지 마세요 — 도구가 요구하지 않으면 넘기지 않습니다. "
        f"최종 답변에는 예약 번호와 확정 요금을 요약하세요."
    )

    collected = {}
    summary = asyncio.run(
        run_agent_loop(run_id, mission, collected=collected, finish_trace=False)
    )

    booking_row = save_booking(plan, first_name, last_name, email,
                               collected.get("booking"))
    trace.publish(run_id, "db", "postgres",
                  f"예약 기록 저장 ({booking_row.status})",
                  f"booking_id={booking_row.booking_id or '-'}")
    trace.done(run_id, "예약 절차 완료")

    return {
        "run_id": run_id,
        "booking_status": booking_row.status,
        "booking_id": booking_row.booking_id,
        "confirmation": booking_row.confirmation,
        "summary": summary,
    }


@shared_task(name="agents.run_budget_edit")
def run_budget_edit(run_id, old_plan_id, new_plan_id, edit_request):
    """
    예산영향 수정: 숙소만 재검색 → 기존 항공 고정 → 재배분 → 새 버전.

    수정 라우터 3갈래의 마지막 조각 (국소수정/재계획은 기구현).
    예: "숙소를 더 좋은 걸로 바꿔줘" — 항공/일정은 그대로, 숙소와 예산 배분만 다시.
    """
    from trips.models import Plan
    from trips.services import save_budget_edited_version

    old_plan = Plan.objects.get(id=old_plan_id)
    new_plan = Plan.objects.get(id=new_plan_id)
    tr = old_plan.request
    dest = tr.destinations.first()

    # ── 1. 숙소 재검색 (A방식 — 수정 요청을 선호 조건으로 전달) ─────────
    mission = (
        f"숙소를 다시 검색합니다. 사용자의 수정 요청: \"{edit_request}\"\n"
        f"- 도시: {dest.city_en or dest.city_name} / 국가코드: {dest.country_code or 'JP'}\n"
        f"- 체크인: {tr.start_date} / 체크아웃: {tr.end_date} / 성인: {tr.adult}명\n"
        f"절차: 객실 배분 → 숙소 검색 → 후보 평가(점수)까지 수행하세요. "
        f"항공/예약 관련 도구는 사용하지 마세요. "
        f"수정 요청의 선호(등급/위치/분위기 등)를 평가에 반영하세요. "
        f"최종 답변은 후보 요약만 간단히."
    )
    collected = {}
    asyncio.run(run_agent_loop(run_id, mission,
                               collected=collected, finish_trace=False))
    hotel_options = collected.get("hotel_options", [])
    trace.publish(run_id, "data", "orchestrator", "숙소 재검색 완료",
                  f"후보 {len(hotel_options)}건")

    if not hotel_options:
        trace.done(run_id, "예산영향 수정 중단: 숙소 후보 없음")
        return {"run_id": run_id,
                "error": "조건에 맞는 숙소 후보를 찾지 못했습니다. 조건을 바꿔 다시 시도해 주세요."}

    # ── 2. 재배분: 기존 선택 항공을 "고정 옵션 1개"로 투입 ──────────────
    # 옵션이 1개면 그리디 엔진은 항공을 못 바꾸므로 자연스럽게 고정된다
    old_flight = getattr(old_plan, "flight", None)
    flight_options = []
    if old_flight:
        flight_options = [{
            "label": old_flight.airline,
            "krw": old_flight.price_krw,
            "utility": float(old_flight.utility) if old_flight.utility is not None else None,
            "raw": old_flight.slices,
        }]

    days = (tr.end_date - tr.start_date).days + 1
    travelers = tr.adult + tr.kid
    allocation = allocate_budget(
        total_budget=tr.total_budget,
        flight_options=flight_options,
        hotel_options=hotel_options,
        days=days,
        travelers=travelers,
    )
    trace.publish(run_id, "rule", "budget", "재배분 완료",
                  str(allocation.get("status", "")))

    # ── 3. 재배분 설명 (LLM) ─────────────────────────────────────────────
    trace.publish(run_id, "llm", "claude", "재배분 설명 생성")
    explanation = explain_allocation(
        {
            "목적지": dest.city_name,
            "수정요청": edit_request,
            "총예산_KRW": tr.total_budget,
        },
        allocation,
        language_for_nationality(getattr(tr.user, "nationality", None)),
    )

    # ── 4. 새 버전 저장 (배분/숙소 새것, 항공/일정 원본 유지) ───────────
    save_budget_edited_version(old_plan, new_plan, allocation, explanation)
    trace.publish(run_id, "db", "postgres", "새 버전 저장 (draft)",
                  f"plan {old_plan_id} -> {new_plan.id}")
    trace.done(run_id, "예산영향 수정 완료")

    return {
        "run_id": run_id,
        "old_plan_id": old_plan_id,
        "new_plan_id": new_plan.id,
        "allocation": allocation,
        "explanation": explanation,
    }
