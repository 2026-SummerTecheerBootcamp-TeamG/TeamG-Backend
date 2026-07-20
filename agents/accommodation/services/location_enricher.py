"""
=====================================================================
후보 품질 (p1) - 위치 정보 강화
=====================================================================

주기능: 위치 정보 강화
상세기능: 호텔 좌표 -> 일정 동선과의 거리 파악
설명: "쇼핑 거리 도보 5분"같은 근거 제공

[이 파일이 하는 일]
    호텔의 좌표(위도/경도)와, 일정에 포함된 관심 장소(POI - Point of
    Interest, 예: 쇼핑거리, 관광명소)의 좌표를 비교해서 "이 호텔은 OO까지
    도보 몇 분" 같은 문장을 자동으로 만들어줌.

[거리 계산 방식: Haversine 공식]
    지구는 평평하지 않고 둥글기 때문에, 위도/경도 두 점 사이의 실제 거리는
    단순 피타고라스 계산(직선거리 공식)으로는 부정확함. Haversine 공식은
    지구를 구(sphere)로 가정하고 두 좌표 사이의 "구면 거리"를 계산하는
    표준적인 방법임. 위경도 차이가 작은 도시 내 거리 계산엔 충분히
    정확한 것으로 알려져 있음.

[도보 시간 환산]
    평균 도보 속도를 시속 4.8km(분당 80m)로 가정해서 분 단위로 환산함.
    (여행/관광 업계에서 흔히 쓰이는 표준적인 도보 속도 가정치임)
=====================================================================
"""

from dataclasses import dataclass
from math import radians, sin, cos, sqrt, atan2
from typing import List, Optional


EARTH_RADIUS_KM = 6371.0088  # 지구 평균 반지름 (km 단위). Haversine 공식 계산에 필요함
WALKING_SPEED_KMH = 4.8      # 평균 도보 속도 (시속 km 기준). 분당 약 80m에 해당함


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    두 좌표(위도/경도) 사이의 거리를 km 단위로 계산하는 함수임 (Haversine 공식 사용).

    위경도는 보통 '도(degree)' 단위로 주어지는데, sin/cos 같은 삼각함수는
    '라디안(radian)' 단위를 입력으로 받아야 정확하게 계산됨. 그래서 계산
    시작 전에 radians() 함수로 단위를 변환해줌.

    ------------------------------------------------------------
    계산 원리 (참고용)
    ------------------------------------------------------------
    지구를 완전한 구라고 가정했을 때, 두 점 사이의 "구면 위 최단 거리"를
    구하는 공식임. 위도 차이(delta_lat)와 경도 차이(delta_lon)를 이용해서
    두 점 사이 각도를 구하고, 그 각도에 지구 반지름을 곱하면 실제 거리가
    나오는 방식임. 정확히 이해하지 않아도 사용하는 데는 문제 없음 -
    "위경도 두 개를 넣으면 km 단위 거리가 나온다" 정도만 알면 충분함.
    """
    lat1_rad, lon1_rad = radians(lat1), radians(lon1)
    lat2_rad, lon2_rad = radians(lat2), radians(lon2)

    delta_lat = lat2_rad - lat1_rad
    delta_lon = lon2_rad - lon1_rad

    # Haversine 공식의 핵심 중간값 계산 부분임
    a = sin(delta_lat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(delta_lon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))

    return EARTH_RADIUS_KM * c


def estimate_walking_minutes(distance_km: float, walking_speed_kmh: float = WALKING_SPEED_KMH) -> int:
    """
    거리(km)를 도보 소요 시간(분 단위, 올림 처리)으로 환산하는 함수임.
    예: 0.4km는 시속 4.8km 기준으로 계산하면 약 5분이 됨.

    ------------------------------------------------------------
    부동소수점 오차 보정이 필요한 이유
    ------------------------------------------------------------
    컴퓨터에서 소수 계산을 하다 보면, 수학적으로는 정확히 5.0이어야 할
    값이 5.0000000001처럼 아주 미세하게 큰 값으로 계산되는 경우가 있음
    (부동소수점 표현 방식의 한계 때문에 생기는 현상임). 이 상태에서
    그냥 올림 처리를 하면, 5분이어야 할 결과가 6분으로 잘못 계산되는
    문제가 발생할 수 있음. 그래서 계산된 값이 정수에 아주 가까우면
    (1e-6 이내 오차) 그 정수로 그대로 취급하도록 보정 로직을 추가함.
    """
    if distance_km <= 0:
        return 0

    hours = distance_km / walking_speed_kmh
    minutes = hours * 60

    # 부동소수점 오차 보정: 계산된 값이 정수에 아주 가까우면 그 정수로 취급함
    rounded = round(minutes)
    if abs(minutes - rounded) < 1e-6:
        return max(rounded, 1)

    # 올림 처리를 하는 이유: 4.2분처럼 소수점이 남으면 "5분"이라고 안내하는 게
    # 신호대기, 횡단보도 등을 감안한 실제 체감 시간과 더 가깝기 때문임
    return int(minutes) + 1


@dataclass
class LocationInsight:
    """
    호텔 하나에 대한 위치 강화 결과를 담는 자료구조임.

    nearest_poi_name, distance_km, walking_minutes는 계산이 불가능한
    경우(좌표 없음 등) None으로 채워질 수 있음. reason 필드에는 그 이유가
    사람이 읽을 수 있는 문장으로 항상 채워짐 (성공/실패와 무관하게 항상
    값이 존재함).
    """
    hotel_id: str
    nearest_poi_name: Optional[str]
    distance_km: Optional[float]
    walking_minutes: Optional[int]
    reason: str  # 예: "쇼핑거리 도보 5분" 또는 "호텔 좌표 정보 없음"

    def to_dict(self) -> dict:
        """
        MCP tool 응답이나 JSON 직렬화가 필요한 곳에 바로 쓸 수 있는
        딕셔너리 형태로 변환하는 메서드임. distance_km는 소수점 2자리로
        반올림해서, 불필요하게 긴 소수점 숫자가 사용자에게 그대로
        노출되지 않도록 처리함.
        """
        return {
            "hotel_id": self.hotel_id,
            "nearest_poi_name": self.nearest_poi_name,
            "distance_km": round(self.distance_km, 2) if self.distance_km is not None else None,
            "walking_minutes": self.walking_minutes,
            "reason": self.reason,
        }


def enrich_hotel_with_location(
    hotel_id: str,
    hotel_lat: Optional[float],
    hotel_lon: Optional[float],
    pois: List[dict],
) -> LocationInsight:
    """
    호텔 좌표 1개와 관심 장소(POI) 리스트를 비교해서, 가장 가까운 POI까지의
    거리/도보시간/근거문장을 계산하는 함수임.

    ------------------------------------------------------------
    파라미터 설명
    ------------------------------------------------------------
    hotel_id: 호텔 ID
    hotel_lat, hotel_lon: 호텔 좌표. 둘 중 하나라도 None이면 계산이 불가능함
    pois: [{"name": "신사이바시 쇼핑거리", "latitude": .., "longitude": ..}, ...]
          일정 에이전트가 만든 동선 상의 장소들(쇼핑거리, 관광명소 등)임.
          빈 리스트로 들어오면 계산이 불가능함.

    ------------------------------------------------------------
    설계 원칙: 에러를 던지지 않고 항상 결과를 반환함
    ------------------------------------------------------------
    좌표나 POI 정보가 없어서 계산을 못 하는 상황이 생기더라도, 이 함수는
    예외(Exception)를 던지지 않고 "계산 불가 이유가 담긴 결과"를 그대로
    반환함. 그 이유는, 위치 정보는 "있으면 더 좋은" 부가 정보 성격이라서,
    이 정보 하나가 없다고 해서 전체 숙소 검색 파이프라인이 멈춰버리면
    안 되기 때문임 (다른 정보-가격, 만족도 등-는 이미 정상적으로
    확보된 상태일 수 있으므로, 위치 정보만 비어있는 채로 결과를
    보여주는 게 더 나은 사용자 경험임).
    """
    if hotel_lat is None or hotel_lon is None:
        return LocationInsight(
            hotel_id=hotel_id, nearest_poi_name=None, distance_km=None,
            walking_minutes=None, reason="호텔 좌표 정보 없음",
        )

    if not pois:
        return LocationInsight(
            hotel_id=hotel_id, nearest_poi_name=None, distance_km=None,
            walking_minutes=None, reason="비교할 일정 동선 정보 없음",
        )

    # 여러 POI 중 가장 가까운 곳 하나를 찾는 루프임.
    # nearest_distance가 None인 초기 상태에서 시작해서, POI를 하나씩 순회하며
    # "지금까지 찾은 것보다 더 가까운지"를 계속 비교해나가는 방식임
    # (전형적인 "최솟값 찾기" 패턴임)
    nearest_name = None
    nearest_distance = None
    for poi in pois:
        distance = haversine_km(hotel_lat, hotel_lon, poi["latitude"], poi["longitude"])
        if nearest_distance is None or distance < nearest_distance:
            nearest_distance = distance
            nearest_name = poi["name"]

    minutes = estimate_walking_minutes(nearest_distance)
    reason = f"{nearest_name} 도보 {minutes}분"

    return LocationInsight(
        hotel_id=hotel_id,
        nearest_poi_name=nearest_name,
        distance_km=nearest_distance,
        walking_minutes=minutes,
        reason=reason,
    )


def enrich_candidates_with_location(
    hotels: List[dict],
    pois: List[dict],
) -> List[LocationInsight]:
    """
    여러 호텔 후보를 한 번에 위치 강화 처리하는 함수임.

    ------------------------------------------------------------
    파라미터 설명
    ------------------------------------------------------------
    hotels: [{"hotel_id":.., "latitude":.., "longitude":..}, ...] 형태의 리스트임.
            candidate_scorer.merge_candidates_with_static_info()와 비슷한 방식으로,
            hotel_search 결과와 liteapi_client의 좌표 정보를 미리 병합해서
            넘겨주면 됨.
    pois: enrich_hotel_with_location() 함수와 동일한 형태의 관심 장소 리스트임.

    ------------------------------------------------------------
    반환값
    ------------------------------------------------------------
    LocationInsight 객체들의 리스트가 반환되며, hotels 리스트에 넣어준
    순서가 그대로 유지됨 (리스트 컴프리헨션을 사용하기 때문에 순서가
    보장됨).
    """
    return [
        enrich_hotel_with_location(
            hotel_id=h["hotel_id"],
            hotel_lat=h.get("latitude"),
            hotel_lon=h.get("longitude"),
            pois=pois,
        )
        for h in hotels
    ]