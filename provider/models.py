"""
provider/models.py - 자체 제작 "액티비티 공급자"의 재고 원장

[이 앱의 정체 — 멘토 피드백 반영]
    항공(Google)·숙소(LiteAPI)는 외부 공급자의 API를 쓰지만, 여기는
    우리가 "공급자 그 자체"가 되어 본다: 재고를 잠그고(hold), 이중 판매를
    막고(멱등), 마지막 1석 경쟁에서 한 명만 성공시키는(동시성) 속사정을
    직접 구현한다. 밖(MCP)에서 보면 다른 공급자와 똑같이 생겼다.

[재고 계산 방식 — 감소가 아니라 계산]
    stock 숫자를 깎지 않는다. "가용 재고 = stock - (살아있는 hold + 확정 예약)"을
    매번 계산한다 — 만료된 hold는 자동으로 계산에서 빠지므로(게으른 만료)
    재고를 되돌리는 코드가 필요 없다. 상태를 바꾸는 대신 사실을 기록하는 설계.
"""

from django.db import models


class Activity(models.Model):
    """액티비티 상품 (시드 데이터로 채움 — seed_activities 커맨드)."""

    city = models.CharField(max_length=50, db_index=True)      # 검색 키 (한글 도시명)
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=30)                 # 체험/티켓/투어/음식 등
    price_krw = models.PositiveIntegerField()                  # 1인당 가격
    stock = models.PositiveIntegerField()                      # 총 재고 (불변 — 가용은 계산)
    description = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.city} {self.name} ({self.price_krw:,}원, 재고 {self.stock})"


class ActivityHold(models.Model):
    """
    재고 임시 점유 (2단계 예약의 1단계).
    TTL이 지나면 자동 무효 — DB에서 지우지 않고 expires_at 비교로 판정한다.
    """

    activity = models.ForeignKey(Activity, on_delete=models.CASCADE,
                                 related_name="holds")
    hold_id = models.CharField(max_length=40, unique=True, db_index=True)
    qty = models.PositiveIntegerField()
    expires_at = models.DateTimeField()        # 이 시각 이후엔 없는 셈 (게으른 만료)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"hold {self.hold_id} x{self.qty} (~{self.expires_at:%H:%M:%S})"


class ActivityReservation(models.Model):
    """
    확정 예약 (2단계의 2단계).
    hold와 OneToOne = "한 hold는 딱 한 번만 예약된다"를 DB가 강제
    — reserve 멱등성의 구조적 토대 (같은 hold_id 재요청 = 기존 예약 반환).
    """

    class Status(models.TextChoices):
        CONFIRMED = "confirmed", "확정"
        CANCELED = "canceled", "취소"

    hold = models.OneToOneField(ActivityHold, on_delete=models.CASCADE,
                                related_name="reservation")
    confirmation = models.CharField(max_length=20, unique=True, db_index=True)
    traveler_name = models.CharField(max_length=100)
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.CONFIRMED)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.confirmation} ({self.status})"
