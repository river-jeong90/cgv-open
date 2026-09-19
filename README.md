# CGV 센텀시티 치이카와 9/30 예매 오픈 알림

- 극장: CGV 센텀시티 (`0089`)
- 날짜: `2026-09-30`
- 영화명: 제목에 `치이카와` 포함
- 확인 간격: 1분
- 알림: 최초 1회 + 1분 간격 3회 = 총 4회
- 알림 4회 완료 후 워크플로 자동 비활성화

## GitHub Secrets
Settings → Secrets and variables → Actions → New repository secret

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## 테스트
Actions → `CGV 센텀시티 치이카와 9월30일 감시` → Run workflow
→ `텔레그램 테스트 알림만 보내기` 체크 → Run workflow
