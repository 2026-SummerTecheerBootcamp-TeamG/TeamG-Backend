# =============================================================
#  budget_explain_demo.py — 배분 설명 생성 데모 (실제 Claude 호출!)
#  실행: 리포 루트에서  python -m agents.budget_explain_demo
#  주의: ANTHROPIC_API_KEY가 .env에 있어야 하고, 호출 2번 = 소액 과금 발생.
# =============================================================

from agents.budget import allocate_budget
from agents.budget_explain import explain_allocation

# budget_demo와 같은 구도의 가짜 후보 (가격↑ = 만족도↑ 이도록)
FLIGHTS = [
    {"label": "이스타항공 직항", "krw": 442_000, "utility": 96.0, "raw": {}},
    {"label": "환승 1회 심야편", "krw": 380_000, "utility": 60.0, "raw": {}},
]
HOTELS = [
    {"label": "lyf Tenjin 3성",       "krw": 338_000, "utility": 80.0, "raw": {}},
    {"label": "Torifito Hakata 4성",  "krw": 440_000, "utility": 90.0, "raw": {}},
]

# 요청 요약 — explain_allocation이 기대하는 간단한 형태
SUMMARY = {"목적지": "후쿠오카", "기간": "3박 4일", "인원": 2,
           "테마": ["쇼핑"], "총예산_KRW": 2_000_000}

print("=" * 60)
print("케이스 1) 200만원 → fit: 업그레이드 스토리가 설명돼야 함")
alloc1 = allocate_budget(2_000_000, FLIGHTS, HOTELS, days=4, travelers=2)
print(explain_allocation(SUMMARY, alloc1))

print("=" * 60)
print("케이스 2) 60만원 → insufficient: 부족액 + 대안 제시가 나와야 함")
alloc2 = allocate_budget(600_000, FLIGHTS, HOTELS, days=4, travelers=2)
print(explain_allocation({**SUMMARY, "총예산_KRW": 600_000}, alloc2))
# {**딕셔너리, 키: 값} = 원본을 복사하면서 그 키 하나만 바꾼 새 딕셔너리 만들기

print("=" * 60)
print("케이스 3) 항공 0건 → LLM 호출 없이 안내문이 즉시 나와야 함")
alloc3 = allocate_budget(2_000_000, [], HOTELS, days=4, travelers=2)
print(explain_allocation(SUMMARY, alloc3))

print("=" * 60)
print("케이스 4) 미국 국적 사용자 → 같은 배분을 영어로 설명해야 함")
from agents.budget_explain import language_for_nationality
# 케이스 1의 배분 결과(alloc1)를 재사용 — 배분은 결정론이라 다시 계산할 필요 없음
print(explain_allocation(SUMMARY, alloc1, language=language_for_nationality("US")))

print("=" * 60)
print("✅ 4케이스 출력 완료 — 케이스 2의 '대안', 케이스 4의 '영어 응답'을 눈으로 확인!")