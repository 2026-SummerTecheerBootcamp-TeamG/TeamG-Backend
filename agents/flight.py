"""
항공 에이전트 - 항공권 후보를 검색해서 예산 에이전트가 쓸 형식으로 반환한다.
"""


def build_route(origin: dict, destinations: list[dict]) -> list[dict]:
    """
    출발지/목적지로 여정(구간 목록)을 만든다. (왕복)
    """
    dest = destinations[0]
    return [
        {"from": origin["iata"], "to": dest["iata"]},   # 가는 편
        {"from": dest["iata"], "to": origin["iata"]},   # 오는 편
    ]


def score_utility(is_direct: bool, departure_hour: int, arrival_hour: int) -> float:
    # 항공권 후보의 만족도 점수. (높을수록 좋음)
    score = 0.0
    if is_direct:
        score += 20                        # 직항
    if 6 <= departure_hour <= 18:
        score += 10                        # 낮 출발
    if arrival_hour <= 18:
        score += 16                        # 이른 도착
    return score


def make_candidate(airline: str, krw: int, is_direct: bool,
                   departure_hour: int, arrival_hour: int) -> dict:
    # 항공권 정보를 예산 에이전트 사용할 형식으로 변환
    return {
        "label": airline,
        "krw": krw,
        "utility": score_utility(is_direct, departure_hour, arrival_hour),
        "raw": {"is_direct": is_direct},   # 원본 참고 데이터
    }


# 테스트용
if __name__ == "__main__":
    # 여정 테스트
    origin = {"city": "서울", "iata": "ICN"}
    destinations = [{"iata": "TYO"}]
    print(build_route(origin, destinations))

    # 점수 테스트
    print(score_utility(is_direct=True, departure_hour=10, arrival_hour=14))   # 46
    print(score_utility(is_direct=False, departure_hour=3, arrival_hour=23))   # 0

    # 후보 변환 테스트
    print(make_candidate("이스타항공", 442378, True, 10, 14))