"""
agents 앱의 Celery 태스크 모음

중요한 이유: config/celery.py의 app.autodiscover_tasks()는 INSTALLED_APPS 각 앱에서 정확히 "tasks.py"라는 파일을 찾음
그래서 이 파일을 만들기만 하면 워커가 여기 태스크들을 자동 등록함
"""

import re
import time
from datetime import date
import asyncio
# LLM 설명 생성과 Google 일정 구성을 동시에 돌리기 위한 스레드 풀
# (PoC에서 검증한 fan-out 패턴 — 45~58초를 23초로 줄였던 그 방식)
from concurrent.futures import ThreadPoolExecutor

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
        # 실측(2026-07-16): 최종 요약 작성에만 11초를 쓰고 있었는데 그 텍스트는
        # 화면에 표시되지 않는다 (후보는 코드가 _collect_candidates로 수집).
        # 그래서 한 문장으로 제한 — 파이프라인 전체에서 공짜로 ~10초 절약
        + "모든 검색이 끝나면 최종 답변은 '검색 완료' 한 문장만 쓰세요. "
        + "요약은 불필요합니다 — 후보 데이터는 시스템이 자동 수집합니다."
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
    
    # 3+4. 배분 설명 (LLM) ∥ 일정 + 내러티브
    # 설명 생성은 allocation만 있으면 되고 일정과는 서로 독립 →
    # 스레드로 띄워두고 그동안 일정을 만들면 설명 LLM 시간(~8초)이 통째로 숨는다
    request_summary = {
        "목적지": dest_names,
        "기간": f"{days}일",
        "인원": travelers,
        "테마": themes,
        "총예산_KRW": fields["budget"],
    }
    trace.publish(run_id, "llm", "claude", "배분 설명 생성 (일정 구성과 병렬)")
    with ThreadPoolExecutor(max_workers=1) as pool:
        explain_future = pool.submit(
            explain_allocation, request_summary, allocation,
            language_for_nationality(nationality),
        )

        trace.publish(run_id, "api", "google", "일정 장소 수집/동선 계산")
        plan_data = build_day_plan(
            fields["destinations"], themes, fields["dates"]["start"]
        )
        trace.publish(run_id, "llm", "claude", "일정 내러티브 생성")
        narrative = narrate_day_plan(
            plan_data["city"], themes, plan_data["day_plan"]
        )

        # .result() = 스레드가 끝날 때까지 기다렸다가 반환값 수령
        # (스레드 안에서 난 예외도 이 지점에서 다시 던져짐 — 조용히 삼켜지지 않음)
        explanation = explain_future.result()

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
    from agents.itinerary import collect_edit_candidates

    old_plan = Plan.objects.get(id=plan_id)
    day_plan = load_day_plan(old_plan)

    # "다른 음식점 추가해줘" 같은 요청에 대비해 신선한 후보를 미리 검색.
    # (편집기는 실존 목록의 이름만 고를 수 있어서, 후보 없이는 추가가 불가능했음
    #  — 오사카 실사용 피드백 반영. 검색 실패해도 편집 자체는 계속)
    exclude = {item["place_name"] for d in day_plan for item in d["items"]}
    extra_pool = []
    for dest in old_plan.request.destinations.all():
        try:
            extra_pool += collect_edit_candidates(
                dest.city_en or dest.city_name, dest.country_code,
                edit_request, exclude,
            )
        except Exception as e:
            trace.publish(run_id, "api", "google", "추가 후보 검색 실패(계속 진행)",
                          str(e)[:120])
    if extra_pool:
        trace.publish(run_id, "api", "google", "추가 후보 검색",
                      f"{len(extra_pool)}곳 (새 장소 추가 요청 대비)")

    edited = edit_day_plan(run_id, day_plan, edit_request,
                           extra_candidates=extra_pool)

    new_plan, dropped = create_edited_version(old_plan, edited, edit_request,
                                              extra_pool=extra_pool)

    from trips.services import load_day_plan
    from agents.itinerary_narrate import narrate_day_plan

    trace.publish(run_id, "llm", "claude", "수정본 설명문 재생성")
    new_day_plan = load_day_plan(new_plan)
    cities = ", ".join(d.city_name for d in new_plan.request.destinations.all())
    new_plan.narrative = narrate_day_plan(
        cities, new_plan.request.themes or [], new_day_plan
    )
    # AI의 편집 답변을 버전에 저장 — 대화 복원 때 원문 그대로 되살리기 위해
    new_plan.edit_summary = edited.get("summary", "")
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


@shared_task(name="agents.run_flight_ticketing")
def run_flight_ticketing(run_id, plan_id, lead_passenger, email):
    """
    항공 발권 태스크 (자체 mock 공급자) — 숙소 run_booking의 항공판.

    실제 항공 발권은 판매자 라이선스가 필요해 학생 팀이 할 수 없으므로,
    "판매자인 척" 하는 mock MCP 서버(flight-booking-agent)로 절차를 증명한다.
    Claude가 운임 재확인 → 좌석 점유 → 발권 확정을 자율 수행하고,
    만료 오류가 나면 스스로 처음부터 재시도한다 (숙소 예약과 동일 패턴).
    """
    from trips.models import Plan, Booking
    from trips.services import save_booking

    plan = Plan.objects.get(id=plan_id)
    flight = getattr(plan, "flight", None)
    if flight is None:
        trace.done(run_id, "발권 중단: 선택된 항공 없음")
        return {"run_id": run_id,
                "error": "이 플랜에는 선택된 항공이 없어 발권할 수 없습니다."}

    tr = plan.request
    passengers = tr.adult + tr.kid
    mission = (
        f"다음 항공편을 발권하세요 (자체 mock 공급자 — 실제 결제 없음).\n"
        f"- 항공사: {flight.airline}\n"
        f"- 총액(KRW): {flight.price_krw}\n"
        f"- 탑승 인원: {passengers}명\n"
        f"- 대표 탑승자: {lead_passenger}\n"
        f"절차: ① flight_fare_quote로 운임 재확인 ② flight_hold_seats로 좌석 점유 "
        f"③ flight_issue_ticket으로 발권 확정. flight_fare_quote/flight_hold_seats/"
        f"flight_issue_ticket 발권 계열 도구만 사용하세요 (검색·숙소 도구 금지). "
        f"만료 오류가 나면 ①부터 다시 시도하세요. "
        f"최종 답변에는 PNR(예약번호)과 총액을 요약하세요."
    )

    collected = {}
    summary = asyncio.run(
        run_agent_loop(run_id, mission, collected=collected, finish_trace=False)
    )

    ticket = collected.get("flight_ticket")
    # save_booking이 기대하는 키(booking_id/confirmation)로 매핑 — PNR이 그 역할
    data = ({"booking_id": ticket["pnr"], "confirmation": ticket["pnr"], **ticket}
            if ticket else None)
    row = save_booking(plan, lead_passenger, "", email, data,
                       kind=Booking.Kind.FLIGHT)
    trace.publish(run_id, "db", "postgres",
                  f"발권 기록 저장 ({row.status})",
                  f"PNR={row.booking_id or '-'}")
    trace.done(run_id, "발권 절차 완료")

    return {
        "run_id": run_id,
        "ticket_status": row.status,
        "pnr": row.booking_id,
        "summary": summary,
    }


# 위치 의도 감지: "X 근처", "가까운 곳", "주변" 등
_LOCATION_HINT = re.compile(r"(근처|가까|주변)")


def _resolve_location_anchor(old_plan, dest, edit_request):
    """
    수정 요청의 위치 의도를 좌표 앵커로 변환.

    실사고: "숙소를 마블마운틴 근처로" 요청이 재검색까지는 됐는데, 숙소 평가에
    좌표 개념이 없어서 그리디 엔진이 그냥 최저가를 골랐다. 이 앵커 좌표로
    후보마다 거리 가점을 주면 "가까운 곳" 조건이 점수에 실제로 반영된다.

    반환: ((lat, lng), 앵커 설명) 또는 (None, None)
    """
    from agents.google_client import search_places

    if not _LOCATION_HINT.search(edit_request):
        return None, None

    # ① "일정(장소들)과 가까운" → 이 계획 방문지들의 무게중심
    if "일정" in edit_request or "장소" in edit_request:
        coords = [(it.latitude, it.longitude)
                  for day in old_plan.days.all()
                  for it in day.items.all()
                  if it.latitude is not None and it.longitude is not None]
        if coords:
            lat = sum(c[0] for c in coords) / len(coords)
            lng = sum(c[1] for c in coords) / len(coords)
            return (lat, lng), "일정 장소들의 중심"

    # ② "마블마운틴 근처"처럼 특정 장소 언급 → 요청 문장 그대로 장소 검색
    #    (Places 텍스트 검색은 자연어에 강해서 문장에서 장소를 알아서 찾아냄)
    try:
        city = dest.city_en or dest.city_name
        for p in search_places(f"{city} {edit_request[:40]}", max_results=3):
            if p.get("lat") is not None:
                return (p["lat"], p["lng"]), p.get("name") or "요청 위치"
    except Exception:
        pass    # 앵커를 못 찾으면 가점 없이 진행 (기능 자체는 계속)
    return None, None


@shared_task(name="agents.run_budget_edit")
def run_budget_edit(run_id, old_plan_id, new_plan_id, edit_request):
    """
    예산영향 수정: 요청과 관련된 쪽(항공/숙소)만 재검색 → 나머지 고정 → 재배분 → 새 버전.

    예: "숙소를 일정과 가까운 곳으로" → 숙소 재검색, 항공 고정
        "아침에 출발하는 비행기로" → 항공 재검색, 숙소 고정
    어느 쪽을 재검색할지는 Claude가 수정 요청을 읽고 도구 선택으로 결정한다 (A방식).
    """
    from trips.models import Plan
    from trips.services import save_budget_edited_version
    from agents.parser.normalizer import normalize_budget

    old_plan = Plan.objects.get(id=old_plan_id)
    new_plan = Plan.objects.get(id=new_plan_id)
    tr = old_plan.request
    dest = tr.destinations.first()

    # "600만원으로 바꿔줘"처럼 총예산 자체를 지정한 경우, 그 금액으로 갱신.
    # (지정이 없으면 "숙소를 더 좋은 걸로 바꿔줘"처럼 기존 총예산 안에서 재배분)
    # "만"/"천"+"원"이 함께 있을 때만 시도 - normalize_budget은 이 표현이 없으면
    # 아무 숫자나 예산으로 오인식하는 fallback이 있어, 무관한 숫자("2개", "4성급" 등)를
    # 잘못 잡지 않도록 명확한 금액 표현이 있을 때만 호출한다.
    requested_budget = None
    if ("만" in edit_request or "천" in edit_request) and "원" in edit_request:
        requested_budget = normalize_budget(edit_request)
    if requested_budget and requested_budget != tr.total_budget:
        trace.publish(run_id, "rule", "budget", "총예산 변경",
                      f"{tr.total_budget}원 -> {requested_budget}원")
        tr.total_budget = requested_budget
        tr.save(update_fields=["total_budget"])

    # ── 1. 재검색 (A방식) — 요청과 관련된 쪽(항공/숙소)만 Claude가 골라 수행 ──
    # 실사고: "숙소를 가까운 곳으로 바꿔줘"가 아무것도 못 바꿨던 문제의 해법.
    # 항공 요청도 같은 경로로 처리된다 ("아침 비행기로 바꿔줘" 등).
    origin_iata = tr.origin_iata or "ICN"
    mission = (
        f"기존 여행 계획의 일부를 다시 검색합니다. 사용자의 수정 요청: \"{edit_request}\"\n"
        f"- 도시: {dest.city_en or dest.city_name} / 국가코드: {dest.country_code or 'JP'}\n"
        f"- 왕복 구간: {origin_iata} → {dest.iata_code or '?'} / 일정: {tr.start_date} ~ {tr.end_date}\n"
        f"- 성인: {tr.adult}명\n"
        f"규칙:\n"
        f"1) 요청이 '숙소' 변경이면: 객실 배분 → 숙소 검색 → 후보 평가만 수행\n"
        f"2) 요청이 '항공' 변경이면: 항공 검색과 평가만 수행\n"
        f"3) 요청과 관련 없는 쪽 도구와 예약/발권 도구는 사용하지 마세요\n"
        f"4) 요청의 선호(위치/시간대/등급 등)를 평가에 반영하세요\n"
        f"모든 검색이 끝나면 최종 답변은 '검색 완료' 한 문장만 쓰세요."
    )
    collected = {}
    asyncio.run(run_agent_loop(run_id, mission,
                               collected=collected, finish_trace=False))
    new_flights = collected.get("flight_options", [])
    new_hotels = collected.get("hotel_options", [])
    trace.publish(run_id, "data", "orchestrator", "재검색 완료",
                  f"항공 {len(new_flights)}건 / 숙소 {len(new_hotels)}건")

    # ── 1.5 위치 가점: "X 근처/일정과 가까운" 요청을 점수에 실제로 반영 ──
    # 가점 = 25점에서 km당 -5 (5km 밖은 0) — 결정론 보정이라 예산 엔진의
    # "같은 입력 = 같은 결과" 원칙이 유지된다
    anchor, anchor_name = (None, None)
    if new_hotels:
        from agents.google_client import haversine_km
        anchor, anchor_name = _resolve_location_anchor(old_plan, dest, edit_request)
        if anchor:
            for h in new_hotels:
                raw = h.get("raw") or {}
                if raw.get("latitude") is not None and raw.get("longitude") is not None:
                    dist = haversine_km(anchor, (raw["latitude"], raw["longitude"]))
                    h["utility"] = (h.get("utility") or 50.0) + max(0.0, 25.0 - 5.0 * dist)
                    raw["anchor_distance_km"] = round(dist, 1)   # 표시/검증용 기록
            trace.publish(run_id, "rule", "budget", "위치 가점 적용",
                          f"기준: {anchor_name} (0km +25점, km당 -5)")

    if not new_flights and not new_hotels:
        trace.done(run_id, "예산영향 수정 중단: 새 후보 없음")
        return {"run_id": run_id,
                "error": "요청과 관련된 새 후보를 찾지 못했습니다. 조건을 바꿔 다시 시도해 주세요."}

    # ── 2. 재배분: 재검색하지 않은 쪽은 기존 선택을 "고정 옵션 1개"로 투입 ──
    # 옵션이 1개면 그리디 엔진은 그쪽을 못 바꾸므로 자연스럽게 고정된다
    old_flight = getattr(old_plan, "flight", None)
    old_hotel = getattr(old_plan, "hotel", None)

    if new_flights:
        flight_options = new_flights
    elif old_flight:
        flight_options = [{
            "label": old_flight.airline,
            "krw": old_flight.price_krw,
            "utility": float(old_flight.utility) if old_flight.utility is not None else None,
            "utility_reasons": old_flight.utility_reasons,
            "raw": old_flight.slices,
        }]
    else:
        flight_options = []

    if new_hotels:
        hotel_options = new_hotels
    elif old_hotel:
        # 기존 숙소를 옵션 형태로 복원 — raw에 표시용 정보를 다시 담아
        # 저장 단계(save_budget_edited_version)가 한 갈래 로직으로 처리하게 한다
        hotel_options = [{
            "label": old_hotel.liteapi_hotel_id,
            "krw": old_hotel.price_krw,
            "utility": float(old_hotel.utility) if old_hotel.utility is not None else None,
            "raw": {
                **(old_hotel.detail or {}),
                "name": old_hotel.name, "star_rating": old_hotel.stars,
                "latitude": old_hotel.latitude, "longitude": old_hotel.longitude,
                "reasons": old_hotel.utility_reasons,
            },
        }]
    else:
        hotel_options = []

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

    # ── 4. 새 버전 저장 (재검색된 쪽 새것, 나머지 원본 유지) ────────────
    save_budget_edited_version(old_plan, new_plan, allocation, explanation)

    # 무엇을 바꿨는지 한 줄 요약 — 챗 응답과 대화 복원(edit_summary) 양쪽에 사용
    # 말투 원칙(피드백): 사용자 언어로 짧게. "가점/재배분/후보" 같은 시스템 용어 금지
    if new_flights and new_hotels:
        changed_txt = "항공편과 숙소를"
    elif new_flights:
        changed_txt = "항공편을"
    else:
        changed_txt = "숙소를"
    if anchor_name:
        summary_text = f"{anchor_name}에서 가까운 곳으로 {changed_txt} 다시 잡았습니다."
    else:
        summary_text = f"요청에 맞춰 {changed_txt} 다시 잡았습니다."
    new_plan.edit_summary = summary_text
    new_plan.save(update_fields=["edit_summary"])

    trace.publish(run_id, "db", "postgres", "새 버전 저장 (draft)",
                  f"plan {old_plan_id} -> {new_plan.id}")
    trace.done(run_id, "예산영향 수정 완료")

    return {
        "run_id": run_id,
        "old_plan_id": old_plan_id,
        "new_plan_id": new_plan.id,
        "allocation": allocation,
        "explanation": explanation,
        # FE 챗 말풍선에 표시되는 문구 (국소수정의 summary와 같은 계약)
        "summary": summary_text,
    }
