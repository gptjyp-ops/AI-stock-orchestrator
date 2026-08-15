# AI 주식 모멘텀 오케스트레이터

한국시간 평일 **08:00 · 14:00 · 16:30**에 보유·관심종목, 강세 모멘텀 업종별 상위 3종목,
긍정 신규공시, 눌림목·거래량 신호를 계산해 GitHub Pages에 게시합니다.

## 설치 순서

1. 이 폴더를 새 GitHub 저장소에 업로드합니다.
2. 저장소 `Settings → Pages → Build and deployment`에서 `Deploy from a branch`를 고릅니다.
3. Branch는 `main`, 폴더는 `/docs`로 설정합니다.
4. [Open DART](https://opendart.fss.or.kr/)에서 API 키를 발급받습니다.
5. 저장소 `Settings → Secrets and variables → Actions`에 `DART_API_KEY`라는 이름으로 저장합니다.
6. `Actions → Stock momentum report → Run workflow`를 한 번 실행합니다.

이후 `https://깃허브아이디.github.io/저장소이름/`에서 대시보드를 확인할 수 있습니다.

## 종목 변경

`config.json`의 `portfolio`, `watchlist`, `sectors`, `ticker_names`만 수정하면 됩니다.
한국 코스피 종목코드는 `.KS`, 코스닥 종목코드는 `.KQ`를 붙입니다.

## 신호 기준

- 눌림목: 종가가 20일 이동평균선 위 3.5% 이내이며 60일선 위
- 거래량 신호: 당일 거래량이 20일 평균의 1.5배 이상이며 당일 상승
- 업종 모멘텀: 구성 종목의 최근 5거래일 평균 수익률

무료 공개 시세는 지연·누락될 수 있습니다. 결과는 투자 권유가 아닌 학습·관찰용입니다.
