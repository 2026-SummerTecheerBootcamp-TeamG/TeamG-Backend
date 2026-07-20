'''
budget.py - 예산 배분 에이전트 (한계효용 그리디 방식)
한계효용이란 - 재화를 1단위 추가로 소비했을 때 증가하는 효용의 크기

PoC의 budget_agent.py에서 검증된 로직을 그대로 이식함

동작 원리
1.  기본 배분: 가장 싼 조합(최저가 항공/숙소/활동)에서 시작.
    이 조합이 예산을 넘으면 "예산 부족"
2.  업그레이드: 남은 예산으로 "1,000원당 만족도가 가장 많이 오르는"
    업그레이드부터 순서대로 적용(한계효용 그리디).
3.  결정론적: 같은 입력이면 항상 같은 결과 출력. LLM, 루프 없음.

'''

import logging

# 로거
# print() 대신 logging을 쓰는 이유
# 1. 나중에 Django/Celery에 붙으면 그쪽의 로그 설정을 자동으로 따라감.
# 2. 운영에서 로그 끄기/레벨 조절 기능이 한 줄 설정으로 가능해짐.
# __name__은 파이썬이 자동으로 넣어주는 "이 파일의 모듈 경로" 문자열
logger = logging.getLogger(__name__)

# 확동/식비 등급표: 등급 이름, 1인 1일 금액, 만족도 점수
# 증가폭이 점점 줄어들게 설계(한계효용 그래프)
ACTIVITY_TIERS = [
    ("최소", 30_000, 50.0),
    ("표준", 60_000, 70.0),
    ("넉넉", 100_000, 85.0), 
]

# 예비비 제거 (2026-07-16 결정): 예전 5%는 부족액 계산에까지 끼어들어
# "예산은 500만인데 475만 기준으로 부족" 같은 직관에 어긋나는 표시를 만들었다.
# 파라미터 자체는 남겨둠 — 필요해지면 호출자가 reserve_ratio=0.05로 되살릴 수 있음
RESERVE_RATIO = 0.0
MAX_GREEDY_STEPS = 6    # 업그레이드 최대 횟수

# 후보에 만족도 점수가 빠져 있을 때 쓰는 중립값
DEFAULT_UTILITY = 50.0


# 함수 이름이 "_"로 시작하는 건 "이 파일 안에서만 쓰는 내부 부품이니 밖에서 import하지 마라"라는 표시
def _activity_cost(tier_idx: int, days: int, travelers: int) -> int:
    """활동비 총액 = 등급 단가 * 일수 * 인원. 함수로 뺀 이유: 4군데서 반복 사용."""
    return ACTIVITY_TIERS[tier_idx][1] * days * travelers


# 예산 배분 함수
def allocate_budget(
    total_budget: int,
    flight_options: list[dict],
    hotel_options: list[dict],
    days: int,
    travelers: int,
    reserve_ratio: float = RESERVE_RATIO,   # 예비비 기본 5%, 필요 시 호출자가 조절
) -> dict:
    """
    예산 배분 핵심 함수.
    
    flight_options / hotel_options 형식:
    [{"label": 표시명, "krw": 원화가격, "utility": 만족도점수,
    "raw": 원본데이터}, ...]
    
    반환 dict의 상태:
      "fit"             예산 안에 들어감
      "insufficient"    최저가 조합이 예산 초과 (부족액 명시)
      "no_flights"      항공 후보 0건
      "no_hotels"       숙소 후보 0건
    """

    # 1. 리스트 컴프리헨션: [o for o in flight_options if o.get("krw")]
    # = flight_options의 각 항목 중에서 조건을 만족하는 것만 모은 새 리스트
    # 2. o.get("krw") vs o["krw"]의 차이:
    # o["krw"]      -> 키가 없으면 프로그램이 에러로 죽음
    # o.get("krw")  -> 키가 없으면 None을 돌려줌
    fo = sorted([o for o in flight_options if o.get("krw")], key=lambda o: o["krw"])
    ho = sorted([o for o in hotel_options if o.get("krw")], key=lambda o: o["krw"])
    if not fo:
        return {"status": "no_flights",
                "message": "항공 후보가 0건입니다. 날짜를 바꾸거나 인근 공항을 시도해 보세요."}
    if not ho:
        return {"status": "no_hotels",
                "message": "숙소 후보가 0건입니다. 날짜 또는 도시명을 확인해 주세요."}
        
    spendable = round(total_budget * (1 - reserve_ratio))   # 실제로 쓸 수 있는 돈
    reserve = total_budget - spendable                      # 예비비


    # 1. 기본 배분
    sel = {"flight": fo[0], "hotel": ho[0], "tier": 0}  # 현재 선택 상태

    def current_total() -> int:
        return (sel["flight"]["krw"] + sel["hotel"]["krw"]
                + _activity_cost(sel["tier"], days, travelers))
    
    def current_utility() -> float:
        return (sel["flight"].get("utility", DEFAULT_UTILITY)
                + sel["hotel"].get("utility", DEFAULT_UTILITY)
                + ACTIVITY_TIERS[sel["tier"]][2])
    
    base = {
        "flight": fo[0]["label"], "hotel": ho[0]["label"],
        "activity_tier": ACTIVITY_TIERS[0][0],
        "total": current_total(), "utility": round(current_utility(), 1),
    }
    logger.info("기본 배분(최저가 조합): 합계 %s원, 만족도 %s", f"{base['total']:,}", base["utility"])

    # 최저가 조합이 예산 초과 -> 예산 부족으로 처리, 부족액 명시 후 반환
    if base["total"] > spendable:
        logger.info("예산 부족: 최저 조합 %s원 > 사용가능 %s원", f"{base['total']:,}", f"{spendable:,}")
        return {
            "status": "insufficient", "total_budget": total_budget,
            "reserve": reserve, "spendable": spendable, "base": base,
            # selection에 최저가 조합을 담아 반환 (예전엔 None이었음)
            # 이유: None이면 DB에 Flight/Hotel 스냅샷이 저장되지 않아서
            # 화면에 "가격(breakdown)은 있는데 항공/숙소 정보는 없는" 반쪽 표시가 됐다.
            # 부족 판정의 기준이 된 조합을 보여줘야 사용자가 "무엇이 얼마나 부족한지" 알 수 있음
            "upgrades": [], "selection": {
                "flight": fo[0], "hotel": ho[0],
                "activity_tier": ACTIVITY_TIERS[0][0],
                "activity_krw": _activity_cost(0, days, travelers),
                "offer_expires_at": (fo[0].get("raw") or {}).get("expires_at"),
            },
            "breakdown": {"flight_krw": fo[0]["krw"], "hotel_krw": ho[0]["krw"],
                          "activity_krw": _activity_cost(0, days, travelers)},
            "total_cost": base["total"],
            "shortfall": base["total"] - spendable,     # 부족액
        }
    
    # 2. 한계효용 그리디 업그레이드
    def possible_moves() -> list[dict]:
        """지금 상태에서 갈 수 있는 '업그레이드 한 걸음'을 전부 나열."""
        remaining = spendable - current_total()     # 남은 예산
        moves = []
        # 항공: 지금보다 비싸지만 남은 예산 안에서 만족도가 높은 후보로 교체
        for o in fo:
            dc = o["krw"] - sel["flight"]["krw"]        # 추가 비용
            du = o.get("utility", DEFAULT_UTILITY) - sel["flight"].get("utility", DEFAULT_UTILITY)
            if dc > 0 and du > 0 and dc <= remaining:
                moves.append({"category": "항공", "apply": ("flight", o), "to": o["label"],
                              "cost_delta": dc, "utility_delta": du})
                
        # 숙소: 항공과 동일
        for o in ho:
            dc = o["krw"] - sel["hotel"]["krw"]
            du = o.get("utility", DEFAULT_UTILITY) - sel["hotel"].get("utility", DEFAULT_UTILITY)
            if dc > 0 and du > 0 and dc <= remaining:
                moves.append({"category": "숙소", "apply": ("hotel", o), "to": o["label"],
                              "cost_delta": dc, "utility_delta": du})
                
        # 활동: 바로 위 등급으로 한 단계씩만
        nxt = sel["tier"] + 1
        if nxt < len(ACTIVITY_TIERS):
            dc = _activity_cost(nxt, days, travelers) - _activity_cost(sel["tier"], days, travelers)
            du = ACTIVITY_TIERS[nxt][2] - ACTIVITY_TIERS[sel["tier"]][2]
            if dc <= remaining:
                moves.append({"category": "활동", "apply": ("tier", nxt),
                              "to": f"{ACTIVITY_TIERS[nxt][0]} 등급",
                              "cost_delta": dc, "utility_delta": du})
        return moves
    
    upgrades = []
    for _ in range(MAX_GREEDY_STEPS):   # 상한이 있는 반복, 무한 루프 방지
        moves = possible_moves()
        if not moves:
            break
        # (만족도/비용)이 최고인 것을 선택. 동률이면 저렴한 쪽 우선
        best = max(moves, key=lambda m: (m["utility_delta"] / m["cost_delta"], - m["cost_delta"]))
        key, value = best["apply"]
        sel[key] = value
        ratio = best["utility_delta"] / best["cost_delta"] * 1000       # 1000원당 만족도
        upgrades.append({"category": best["category"], "to": best["to"],
                         "cost_delta": best["cost_delta"],
                         "utility_delta": round(best["utility_delta"], 1),
                         "ratio_per_1000won": round(ratio, 2)})
        logger.info("업그레이드: %s -> %s (+%s원, 만족도 +%.1f)",
                    best["category"], best["to"], f"{best['cost_delta']:,}", best["utility_delta"])
        
    total_cost = current_total()
    return {
        "status": "fit", "total_budget": total_budget,
        "reserve": reserve, "spendable": spendable,
        "base": base, "upgrades": upgrades,
        "selection": {
            "flight": sel["flight"], "hotel": sel["hotel"],
            "activity_tier": ACTIVITY_TIERS[sel["tier"]][0],
            "activity_krw": _activity_cost(sel["tier"], days, travelers),
            # Duffel offer 만료 시각 - 재조회 판단의 근거
            "offer_expires_at": (sel["flight"].get("raw") or {}).get("expires_at"),
        },
        "breakdown": {
            "flight_krw": sel["flight"]["krw"], "hotel_krw": sel["hotel"]["krw"],
            "activity_krw": _activity_cost(sel["tier"], days, travelers),
        },
        "total_cost": total_cost,
        "surplus": spendable - total_cost,      # 남은 여유
        "utility_total": round(current_utility(), 1),
    }