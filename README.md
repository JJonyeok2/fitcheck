# fitcheck

IT 직무 채용공고를 넣으면 **지원할 수 있는 공고인지, 얼마나 맞는지, 무엇이 부족한지**를 판정하고, 지원 결정과 이후 진행 상황까지 관리하는 에이전트입니다.

> 진행 상태: Spec 완료, 판정 규칙 구현 및 테스트 완료. 에이전트 그래프, API, 화면은 구현 예정입니다.

## 무엇을 하나

- **IT 직무만 분석**: 담당업무 기준으로 직무를 분류하고, 비IT 공고는 입력 단계에서 거절합니다.
- **자격 필터**: 마감, 경력, 학력, 졸업, 병역, 자격증, 어학, 근무지, 고용형태를 조건별로 통과 / 보류 / 탈락으로 판정합니다.
- **요건 매칭**: 공고 요건을 내 프로젝트와 비교하고, 충족한 요건마다 근거 프로젝트를 붙입니다. 근거 없는 "충족"은 인정하지 않습니다.
- **라벨**: 합격권 / 안정권 / 불합격권 / 지원 불가. 합격 확률이 아니라 **공고 요건과의 적합도**입니다.
- **선호 조건**: 연봉, 사수, 회사 단계는 라벨과 섞지 않고 따로 보여줍니다.
- **이미지 공고**: 본문이 이미지인 공고도 비전 모델로 읽습니다.
- **지원 관리**: 플랫폼 간 중복 공고 묶기, 같은 회사 지원 이력 표시, 역량이 바뀌면 재판정.

## 설계 원칙

**LLM은 읽고 해석하는 일만, 판정은 규칙 코드가 합니다.**
공고 문장 구조화와 이미지 읽기, 요건과 프로젝트의 의미 비교는 LLM이 하고, 지원 가능 여부와 충족률, 라벨은 코드가 정합니다. 같은 공고에는 항상 같은 판정이 나와야 하고, 판정 이유를 설명할 수 있어야 하기 때문입니다.

자세한 흐름과 규칙은 [docs/SPEC.md](docs/SPEC.md), 화면 목업은 [docs/fit_mockup.html](docs/fit_mockup.html)에 있습니다.

## 구조

```
fitcheck/
  schemas.py      데이터 구조, LangGraph 상태
  filters.py      IT 직무 관문, 자격 필터, 학력 순위, 라벨, 정렬
  scoring.py      요건 충족률
  preferences.py  선호 조건
  tracking.py     중복 공고, 기지원 회사, 재판정 대상
  budget.py       파싱 캐시, LLM 호출 상한
  config.py       내 조건과 역량 불러오기
tests/            판정 규칙 테스트
eval/             정답셋, 시간 측정 템플릿
docs/             Spec, 화면 목업
```

## 실행

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

내 정보로 쓰려면 예시 파일을 복사해서 고칩니다. `competencies.local.yaml`은 Git에 올라가지 않습니다.

```bash
cp competencies.example.yaml competencies.local.yaml
```

## 기술 스택

Python, Pydantic, LangGraph (예정), FastAPI (예정), Playwright, Next.js (예정)
