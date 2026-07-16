'''
trips/models.py - 여행 도메인 모델 요청 계열

기준: ERD
이 파일의 클래스 하나가 DB 테이블 하나

id(PK) 컬럼은 Django가 자동 생성함 (bigint 자동 증가)
FK 하나를 적으면 DB에는 "컬럼명_id" 형태로 만들어짐

'''

from django.conf import settings    # AUTH_USER_MODEL("users.User") 문자열을 가져오기 위함
from django.db import models


class TripRequest(models.Model):
    """여행 요청
    
    파이프라인 전체의 입력값 원천
    """

    # 소유자 FK
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,   # 부모(User)가 삭제되면 같이 삭제
        related_name="trip_requests",
    )
    departure = models.CharField(max_length=100)    # 출발 도시 표시명 ("서울")
    origin_iata = models.CharField(max_length=3, null=True, blank=True) # 출발 공항 "ICN"
    start_date = models.DateField()     # 여행 시작일
    end_date = models.DateField()       # 여행 종료일
    total_budget = models.PositiveIntegerField()    # 총예산 (원화)
    adult = models.PositiveIntegerField()           # 성인 수
    kid = models.PositiveIntegerField(default=0)    # 아동 수 (기본값 0)
    themes = models.JSONField(null=True, blank=True)    # 테마 배열
    raw_input = models.JSONField(null=True, blank=True) # 원문+파싱 스냅샷
    created_at = models.DateTimeField(auto_now_add=True)    # 생성 시각
    updated_at = models.DateTimeField(auto_now=True)        # 수정 시각

    def __str__(self):
        # 관리자 페이지나 셀에서 이 객체가 어떻게 표시될지 정하는 함수
        return f"요청#{self.id} {self.departure}-> (예산 {self.total_budget:,}원)"
    

class TripDestination(models.Model):
    """요청 목적지
    
    멀티시티가 켜져도 스키마 변경이 없도록 처음부터 1:N 테이블로 설계
    """

    request = models.ForeignKey(
        TripRequest,                    # 같은 파일 안의 모델은 클래스를 직접 지정
        on_delete=models.CASCADE,       # 요청이 삭제되면 목적지들도 삭제
        related_name="destinations",    # request.destinations.all()
    )

    seq_order = models.PositiveIntegerField()                               # 방문 순서
    city_name = models.CharField(max_length=100)                            # 한국어 도시명
    city_en = models.CharField(max_length=100, null=True, blank=True)       # 영어명
    country_code = models.CharField(max_length=2, null=True, blank=True)    # ISO 국가코드
    iata_code = models.CharField(max_length=3, null=True, blank=True)       # 대표 공항
    nights = models.PositiveBigIntegerField()                               # 이 도시 숙박 일수
    created_at = models.DateTimeField(auto_now_add=True)                    # 생성 시각
    updated_at = models.DateTimeField(auto_now=True)                    # 수정 시각

    class Meta:
        # 모델 자체에 대한 설정을 담는 자리 (컬럼이 아닌 테이블 수준 규칙)
        # 여기서는 한 요청 안에서 방문 순서 번호가 중복되면 안 된다를 강제
        constraints = [
            models.UniqueConstraint(
                fields=["request", "seq_order"],
                name="uniq_destination_order_per_request",
            ),
        ]
        ordering = ["seq_order"]    # .all()로 꺼낼 때 항상 방문 순서대로 정렬돼 나옴

    def __str__(self):
        return f"{self.city_name} ({self.nights}박)"
    

class ChatMessage(models.Model):
    """대화 메시지"""

    # 발화자 구분 (ERD으 ENUM)
    # Django에는 ENUM 타입이 없고, CharField + choices로 같은 효과를 냄
    class Role(models.TextChoices):
        USER = "user", "사용자"
        AI = "ai", "AI"

    # 백필(backfill) 구조
    # 시간 순서상 대화가 요청보다 먼저 존재함
    # 따라서 session_id 필수
    # 요청이 없는 동안 메시지들을 묶는 키
    session_id = models.CharField(max_length=36, db_index=True)
    request = models.ForeignKey(
        TripRequest,
        on_delete=models.CASCADE,
        related_name="messages",
        null=True, blank=True,      # FK의 널 허용
    )
    role = models.CharField(max_length=10, choices=Role.choices)        # "user" 또는 "ai"만 허용
    content = models.TextField()                                        # 길이 무제한 문자열
    param_diff = models.JSONField(null=True, blank=True)                # 이 메시지로 바뀐 필드 추적
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]   # 대화는 항상 시간순으로

    def __str__(self):
        return f"[{self.role}] {self.content[:30]}"     # 내용은 앞 30자만 미리보기


class Plan(models.Model):
    """여행 계획 (버전)
    
    과거 버전이 보존되므로 롤백/비교가 가능하고, 수정이 잘못돼도 원본이 안 깨짐
    """

    # 상태 3단계
    class Status(models.TextChoices):
        PROCESSING = "processing", "생성 중"    # 파이프라인이 도는 동안
        DRAFT = "draft", "임시"                 # 결과 나옴, 아직 미확정
        CONFIRMED = "confirmed", "확정"         # 사용자가 확정

    request = models.ForeignKey(
        TripRequest,
        on_delete=models.CASCADE,
        related_name="plans",
    )

    # 파이프라인 시작 순간에 만들어지므로 첫 상태는 언제나 processing
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.PROCESSING,
    )
    allocation = models.JSONField(null=True, blank=True)        # 예산 배분 스냅샷
    narrative = models.TextField(null=True, blank=True)         # Claude가 쓴 일정 설명문
    edit_request = models.TextField(null=True, blank=True)      # 이 버전을 만든 수정 요청 원문
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]       # 버전 목록은 생성순 = 버전순

    def __str__(self):
        return f"플랜#{self.id} ({self.status}) - 요청#{self.request_id}"
        # FK를 만들면 Django가 "_id"가 붙은 숫자 필드를 덤으로 줌
    

class Flight(models.Model):
    """선택 항공
    
    후보 목록은 Redis에 살고(30분 만료), 여기엔 선택된 1건만
    """

    plan = models.OneToOneField(
        Plan,
        on_delete=models.CASCADE,
        related_name="flight",
    )
    airline = models.CharField(max_length=100)      # 항공사명
    price_krw = models.PositiveIntegerField()       # 원화 가격
    price_original = models.DecimalField(max_digits=10, decimal_places=2)   # 원통화 가격
    currency = models.CharField(max_length=3)                               # 통화 코드
    utility = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)    # 만족도 점수
    utility_reasons = models.JSONField(null=True, blank=True)   # 점수 근거 배열
    slices = models.JSONField(null=True, blank=True)            # 구간 상세
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.airline} {self.price_krw:,}원 (플랜#{self.plan_id})"
    

class Hotel(models.Model):
    """선택 숙소"""

    plan = models.OneToOneField(
        Plan,
        on_delete=models.CASCADE,
        related_name="hotel",
    )
    liteapi_hotel_id = models.CharField(max_length=100)     # LiteAPI 호텔 ID
    name = models.CharField(max_length=200)                 # 호텔명
    stars = models.PositiveIntegerField(null=True, blank=True)  # d성급
    price_krw = models.PositiveIntegerField()                   # 총 숙박비 원화
    price_original = models.DecimalField(max_digits=10, decimal_places=2)   # 원통화 금액
    currency = models.CharField(max_length=3)                               # 통화 코드
    utility = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    utility_reasons = models.JSONField(null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)     # 위도
    longitude = models.FloatField(null=True, blank=True)    # 경도
    detail = models.JSONField(null=True, blank=True)        # 주소 등 부가 정보
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} {self.price_krw:,}원 (플랜#{self.plan_id})"
    

class ItineraryDay(models.Model):
    """일자별 일정
    
    날짜/도시는 일(day) 레벨의 사실이므로 여기 두고, 항목마다 저장하지 않음
    """

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="days",
    )
    day_number = models.PositiveIntegerField()  # n일차 국소 수정의 대상 지정
    city_name = models.CharField(max_length=100, null=True, blank=True) # 이 날의 도시
    date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # 한 플랜 안에 2일차가 두 개 사고 방지
            models.UniqueConstraint(
                fields=["plan", "day_number"],
                name="uniq_day_number_per_plan",
            ),
        ]
        ordering = ["day_number"]

    def __str__(self):
        return f"{self.day_number}일차 (플랜#{self.plan_id})"
    

class ItineraryItem(models.Model):
    """방문 일정 항목
    
    장소 정보와 이동 정보가 임베드
    저장된 플랜을 다시 열 때 외부 API 호출 없이 화면 그릴 수 있음
    """

    day = models.ForeignKey(
        ItineraryDay,
        on_delete=models.CASCADE,
        related_name="items",
    )
    visit_order = models.PositiveIntegerField()             # 그 날의 방문 순서 = 동선
    place_name = models.CharField(max_length=200)           # 장소명
    latitude = models.FloatField(null=True, blank=True)     # 위도
    longitude = models.FloatField(null=True, blank=True)    # 경도
    place_detail = models.JSONField(null=True, blank=True)  # 구글ID/평점/카테고리 등
    arrival_time = models.TimeField(null=True, blank=True)  # 도착 예정 시간
    duration_min = models.PositiveIntegerField(null=True, blank=True)   # 체류 시간(분)
    est_cost = models.PositiveIntegerField(null=True, blank=True)       # 예상 비용(원)
    travel_min_to_next = models.PositiveIntegerField(null=True, blank=True) # 다음 경로 이동 시간(분)
    travel_mode = models.CharField(max_length=20, null=True, blank=True)    # 이동 수단
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # 방문 순서 번호 중복 사고 방지
            models.UniqueConstraint(
                fields=["day", "visit_order"],
                name="uniq_visit_order_per_day",
            ),
        ]
        ordering = ["visit_order"]

    def __str__(self):
        return f"{self.visit_order}. {self.place_name}"


class Booking(models.Model):
    """숙소 예약 기록 (샌드박스)

    ERD 다이어트 때 P2로 미뤄뒀던 예약 테이블의 실체화 —
    "재추가는 싼 마이그레이션"으로 분류해 둔 바로 그 케이스.
    FK(1:N)인 이유: 예약 실패 후 재시도 이력도 남기기 위해.
    """

    class Status(models.TextChoices):
        CONFIRMED = "confirmed", "확정"
        FAILED = "failed", "실패"

    class Kind(models.TextChoices):
        HOTEL = "hotel", "숙소"
        FLIGHT = "flight", "항공"

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="bookings",
    )
    # 무엇의 예약인지 — 항공 mock 발권이 생기며 한 테이블에 두 종류가 공존
    # (default=hotel: 기존 행들은 전부 숙소 예약이므로 마이그레이션이 안전)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.HOTEL)
    status = models.CharField(max_length=12, choices=Status.choices)
    booking_id = models.CharField(max_length=100, null=True, blank=True)     # LiteAPI 예약 번호
    confirmation = models.CharField(max_length=100, null=True, blank=True)   # 호텔 확인 코드
    guest_name = models.CharField(max_length=100)
    guest_email = models.EmailField()
    detail = models.JSONField(null=True, blank=True)    # 응답 스냅샷 (요금/상태 등)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"예약 {self.booking_id or '(실패)'} - 플랜#{self.plan_id}"
    