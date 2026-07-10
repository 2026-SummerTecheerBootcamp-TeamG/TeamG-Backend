"""
=====================================================================
LiteAPI 공용 클라이언트
=====================================================================
여러 서비스(hotel_search.py, candidate_scorer.py 등)에서 공통으로 쓰는
LiteAPI HTTP 호출을 한 곳에 모아둔 파일.

[왜 서비스 파일마다 각자 만들지 않고 여기 하나로 모으나?]
    - API 키, 타임아웃, 에러 처리 같은 공통 로직을 중복해서 짤 필요 없게
    - 나중에 LiteAPI 엔드포인트 URL이 바뀌거나 인증 방식이 바뀌면
      이 파일 하나만 고치면 되게

[LiteAPI에서 우리가 쓰는 엔드포인트 2개]
    1) POST /hotels/rates
       -> "가격" 정보. 목록 검색 + 가격 조회를 한 번에 해줌 (hotel_search.py에서 사용)
    2) GET  /data/hotels
       -> "정적" 정보 (이름, 성급, 좌표, 편의시설 등). 가격은 안 들어있음
          (candidate_scorer.py, location_enricher.py에서 사용 예정)

    이 둘을 분리해서 부르는 이유: LiteAPI 자체가 "가격 정보"와
    "호텔 자체 정보(이름/성급/사진 등)"를 서로 다른 API로 나눠놨기 때문.
    /hotels/rates 응답에는 hotelId와 가격만 있고 starRating 같은
    정적 정보는 없어서, 성급이 필요하면 /data/hotels를 따로 불러야 함.
=====================================================================
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional
import requests


LITEAPI_BASE_URL = "https://api.liteapi.travel/v3.0"
DEFAULT_TIMEOUT_SECONDS = 8  # LiteAPI 권장: 라이브 요청 4~10초


class LiteAPIRequestError(Exception):
    """LiteAPI 호출 자체가 실패했을 때 (네트워크 오류, 인증 실패, 4xx/5xx 응답 등)"""


@dataclass
class HotelStaticInfo:
    """
    /data/hotels 응답에서 우리가 필요한 만큼만 추려낸 정적 정보.
    가격은 여기 없음 (가격은 hotel_search.py의 HotelCandidate 쪽 담당).
    """
    hotel_id: str
    name: Optional[str] = None
    star_rating: Optional[int] = None
    facilities: List[str] = field(default_factory=list)


class LiteAPIClient:
    """
    LiteAPI와 통신하는 얇은 HTTP 클라이언트.
    API 키는 Django settings나 환경변수에서 읽어와 생성 시점에 넣어주면 됨.

    사용 예:
        client = LiteAPIClient(api_key=settings.LITEAPI_API_KEY)
        rates = client.get_rates({...})
        static_info = client.get_hotel_static_info(["lp3803c", "lp1f982"])
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = LITEAPI_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout

    def _headers(self) -> dict:
        # 두 엔드포인트가 공통으로 쓰는 인증 헤더. 메서드로 빼서 중복 줄임.
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "X-API-Key": self.api_key,
        }

    def get_rates(self, payload: dict) -> dict:
        """
        POST /hotels/rates 호출 - 가격 조회용.
        payload 예시는 hotel_search.py의 search_hotel_candidates() 참고.
        """
        url = f"{self.base_url}/hotels/rates"
        try:
            resp = requests.post(
                url, json=payload, headers=self._headers(), timeout=self.timeout
            )
        except requests.RequestException as e:
            raise LiteAPIRequestError(f"LiteAPI 가격 조회 요청 실패: {e}") from e

        if resp.status_code != 200:
            raise LiteAPIRequestError(
                f"LiteAPI 가격 조회 응답 오류 (status={resp.status_code}): {resp.text[:500]}"
            )
        return resp.json()

    def get_hotel_static_info(self, hotel_ids: List[str]) -> Dict[str, HotelStaticInfo]:
        """
        GET /data/hotels 호출 - 호텔 정적 정보(이름/성급/편의시설) 조회용.

        중요한 설계 포인트:
            hotel_search.py에서 "가격 있는 후보"를 먼저 걸러낸 뒤,
            그 후보들의 hotel_id만 여기 넘겨서 정적 정보를 조회한다.
            (검색 대상 전체 호텔이 아니라 "최종 후보"만 조회 -> API 호출 최소화)

        Args:
            hotel_ids: 정적 정보를 조회할 호텔 ID 리스트

        Returns:
            {hotel_id: HotelStaticInfo} 형태의 딕셔너리.
            응답에 없는 hotel_id는 결과 딕셔너리에 아예 포함되지 않음
            (호출하는 쪽에서 .get(hotel_id)로 안전하게 조회하도록 설계)
        """
        if not hotel_ids:
            return {}

        url = f"{self.base_url}/data/hotels"
        params = {
            # LiteAPI는 hotelIds를 콤마로 구분된 문자열로 받음
            "hotelIds": ",".join(hotel_ids),
        }
        try:
            resp = requests.get(
                url, params=params, headers=self._headers(), timeout=self.timeout
            )
        except requests.RequestException as e:
            raise LiteAPIRequestError(f"LiteAPI 정적 정보 조회 요청 실패: {e}") from e

        if resp.status_code != 200:
            raise LiteAPIRequestError(
                f"LiteAPI 정적 정보 조회 응답 오류 (status={resp.status_code}): {resp.text[:500]}"
            )

        data = resp.json().get("data") or []
        result: Dict[str, HotelStaticInfo] = {}
        for entry in data:
            hotel_id = entry.get("id")
            if not hotel_id:
                continue
            result[hotel_id] = HotelStaticInfo(
                hotel_id=hotel_id,
                name=entry.get("name"),
                star_rating=entry.get("starRating"),
                facilities=entry.get("hotelFacilities") or [],
            )
        return result