"""
액티비티 시드 데이터 주입: python manage.py seed_activities

이미 있으면 건너뛰므로(멱등) 여러 번 실행해도 안전하다.
"프라이빗 다도 체험"은 재고 1개 — 동시성 데모("마지막 1석 레이스") 전용 상품.
"""

from django.core.management.base import BaseCommand

from provider.models import Activity

SEED = [
    # (도시, 이름, 카테고리, 1인 가격, 재고, 설명)
    ("오사카", "유니버설 스튜디오 재팬 1일권", "티켓", 98000, 50, "익스프레스 미포함 기본 입장권"),
    ("오사카", "도톤보리 타코야키 쿠킹클래스", "음식", 45000, 12, "현지 셰프와 2시간, 재료 포함"),
    ("오사카", "오사카성 + 우메다 스카이빌딩 투어", "투어", 62000, 20, "가이드 동행 반일 투어"),
    ("오사카", "프라이빗 다도 체험 (마지막 1석)", "체험", 88000, 1, "전통 다실 1:1 세션 — 단 1자리"),
    ("도쿄", "팀랩 플래닛 도쿄 입장권", "티켓", 42000, 40, "물 위를 걷는 미디어아트"),
    ("도쿄", "츠키지 시장 미식 워킹투어", "음식", 55000, 15, "아침 시장 + 스시 브런치"),
    ("도쿄", "시부야 고카트 시티런", "체험", 75000, 8, "코스튬 대여 포함, 국제면허 필요"),
    ("후쿠오카", "야타이(포장마차) 투어", "음식", 38000, 18, "현지인 가이드와 3곳 순회"),
    ("후쿠오카", "다자이후 텐만구 반일 투어", "투어", 41000, 25, "왕복 교통 포함"),
]


class Command(BaseCommand):
    help = "액티비티 공급자 시드 데이터 주입 (멱등)"

    def handle(self, *args, **options):
        created = 0
        for city, name, category, price, stock, desc in SEED:
            _, was_created = Activity.objects.get_or_create(
                city=city, name=name,
                defaults={"category": category, "price_krw": price,
                          "stock": stock, "description": desc},
            )
            created += int(was_created)
        self.stdout.write(self.style.SUCCESS(
            f"시드 완료: 신규 {created}건 / 전체 {Activity.objects.count()}건"))
