"""
동시성 증명 데모 — "마지막 1석 레이스"

재고 1개짜리 상품에 hold 요청 두 개를 "동시에" 발사한다.
select_for_update 잠금이 제대로면: 정확히 1개 성공 + 1개 재고부족.
잠금이 없다면: 둘 다 성공(이중 판매 사고)이 날 수 있다 — 그걸 막았다는 증명.

실행 (리포 루트에서):
    python provider/race_demo.py
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

sys.stdout.reconfigure(encoding="utf-8")

from django.db import connection

from provider.models import Activity
from provider.services import ProviderError, hold_activity

# 데모 전용 상품 (시드의 재고 1개짜리)
TARGET_NAME = "프라이빗 다도 체험 (마지막 1석)"

activity = Activity.objects.filter(name=TARGET_NAME).first()
if activity is None:
    print("시드가 없습니다. 먼저: python manage.py seed_activities")
    sys.exit(1)

print(f"🎯 대상: {activity.name} (총 재고 {activity.stock}개)")
print("두 요청이 동시에 마지막 1석을 노립니다...\n")

results = {}
barrier = threading.Barrier(2)   # 두 스레드를 같은 순간에 출발시키는 출발선


def racer(tag):
    barrier.wait()               # 둘 다 도착할 때까지 대기 → 동시에 발사
    try:
        r = hold_activity(activity.id, qty=1)
        results[tag] = f"✅ 성공 — hold_id={r['hold_id']}"
    except ProviderError as e:
        results[tag] = f"🚫 거절 — {e}"
    finally:
        connection.close()       # 스레드별 DB 연결 정리


threads = [threading.Thread(target=racer, args=(f"요청{i+1}",)) for i in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()

for tag in sorted(results):
    print(f"{tag}: {results[tag]}")

wins = sum(1 for v in results.values() if v.startswith("✅"))
print(f"\n판정: 성공 {wins}건 / 거절 {2 - wins}건 → "
      + ("🏆 동시성 방어 정상 (정확히 1건만 성공)" if wins == 1
         else "⚠️ 비정상 — 잠금 로직 점검 필요"))
