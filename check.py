import os
import time
from datetime import datetime, timezone, timedelta
from curl_cffi import requests as cf_requests

SITE_NO = "0089"
THEATER_NAME = "CGV 센텀시티"
TARGET_DATE = "20260930"
MOVIE_KEYWORD = "치이카와"
CHECK_INTERVAL = 60
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "20000"))

BASE_URL = "https://cgv.co.kr"
BOOKING_PAGE = f"{BASE_URL}/cnm/movieBook"
SCHEDULE_ENDPOINT = f"{BASE_URL}/api/v1/booking/searchMovScnInfo"
CO_CD = "A420"
RTCTL_SCOP_CD = "08"
COMMON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": BOOKING_PAGE,
}

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
KST = timezone(timedelta(hours=9))


def now_kst():
    return datetime.now(KST)


def tg_send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = cf_requests.post(url, json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False}, impersonate="chrome", timeout=20)
    if r.status_code == 429:
        try:
            retry_after = int(r.json().get("parameters", {}).get("retry_after", 60))
        except Exception:
            retry_after = 60
        print(f"[Telegram] 429, retry_after={retry_after}s")
        time.sleep(min(retry_after, 120))
        return tg_send(text)
    r.raise_for_status()


class CGVClient:
    def __init__(self):
        self.session = None

    def _new_session(self):
        # 정상 세션을 30분마다 강제로 버리지 않는다.
        # 새 세션 생성 시 예매 메인 페이지를 먼저 방문하지 않고
        # 시간표 API를 바로 호출한다.
        print("[CGV] 새 API 세션 생성")
        self.session = cf_requests.Session(impersonate="chrome")
        return self.session

    def _get_session(self):
        return self.session or self._new_session()

    def reset_session(self):
        try:
            if self.session is not None:
                self.session.close()
        except Exception:
            pass
        self.session = None

    def _fetch_once(self):
        s = self._get_session()
        r = s.get(
            SCHEDULE_ENDPOINT,
            params={
                "coCd": CO_CD,
                "siteNo": SITE_NO,
                "scnYmd": TARGET_DATE,
                "rtctlScopCd": RTCTL_SCOP_CD,
            },
            headers=COMMON_HEADERS,
            timeout=20,
        )
        if r.status_code == 403:
            raise RuntimeError("CGV 시간표 API HTTP 403")
        if r.status_code == 429:
            raise RuntimeError("CGV 시간표 API HTTP 429")
        if r.status_code != 200:
            raise RuntimeError(f"CGV 시간표 API HTTP {r.status_code}")
        payload = r.json()
        if payload.get("statusCode") != 0:
            raise RuntimeError(
                f"CGV API statusCode={payload.get('statusCode')} "
                f"message={payload.get('statusMessage')}"
            )
        return payload.get("data") or []

    def fetch(self):
        # 403/429가 나더라도 예전처럼 60→120→240→300초씩 길게 쉬지 않고
        # 새 세션으로 짧게 복구를 시도한다.
        delays = [0, 5, 10, 20, 30]
        last_error = None

        for attempt, delay in enumerate(delays, start=1):
            if delay:
                print(f"[CGV] 빠른 복구 재시도: {delay}초 대기")
                time.sleep(delay)

            if attempt > 1:
                self.reset_session()

            try:
                return self._fetch_once()
            except Exception as e:
                last_error = e
                print(f"[CGV] 조회 실패 {attempt}/{len(delays)}: {e}")

        raise last_error or RuntimeError("CGV 조회 실패")


def normalize(text):
    return (text or "").replace(" ", "").lower()


def find_matches(rows):
    keyword = normalize(MOVIE_KEYWORD)
    return [row for row in rows if keyword in normalize(row.get("movNm") or "")]


def fmt_time(raw):
    raw = str(raw or "")
    if len(raw) >= 4 and raw[-4:].isdigit():
        return f"{raw[-4:-2]}:{raw[-2:]}"
    return raw or "시간 확인 필요"


def build_alert(matches, seq):
    lines = [
        f"🚨 CGV 예매 오픈 감지 ({seq}/4)",
        "",
        f"📍 {THEATER_NAME}",
        "📅 2026-09-30",
        f"🎬 제목에 '{MOVIE_KEYWORD}' 포함",
        "",
    ]
    seen = set()
    for row in matches:
        key = (row.get("movNm"), row.get("scnsNm"), row.get("scnsrtTm"))
        if key in seen:
            continue
        seen.add(key)
        title = row.get("movNm") or "제목 미확인"
        screen = row.get("scnsNm") or "상영관 미확인"
        start = fmt_time(row.get("scnsrtTm"))
        free = row.get("frSeatCnt")
        total = row.get("stcnt")
        seat_txt = ""
        if free is not None or total is not None:
            seat_txt = f" / 잔여 {free if free is not None else '?'}"
            if total is not None:
                seat_txt += f"/{total}석"
        lines.append(f"• {title}")
        lines.append(f"  {screen} {start}{seat_txt}")
    lines += ["", "바로 예매 확인:", "https://cgv.co.kr/cnm/movieBook/cinema"]
    return "\n".join(lines)


def disable_this_workflow():
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        print("[GitHub] 자동 비활성화 생략")
        return
    url = f"https://api.github.com/repos/{repo}/actions/workflows/watch.yml/disable"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        r = cf_requests.put(url, headers=headers, timeout=20)
        print(f"[GitHub] workflow disable HTTP {r.status_code}")
    except Exception as e:
        print(f"[GitHub] workflow disable 실패: {e}")


def send_four_alerts(matches):
    for seq in range(1, 5):
        tg_send(build_alert(matches, seq))
        print(f"[알림] {seq}/4 전송 완료")
        if seq < 4:
            time.sleep(60)


def main():
    if os.getenv("TEST_ALERT", "false").lower() == "true":
        tg_send("✅ CGV 알림봇 테스트 성공\n\n📍 CGV 센텀시티\n📅 2026-09-30\n🎬 제목에 '치이카와' 포함 여부를 감시합니다.")
        print("테스트 알림 전송 완료")
        return

    client = CGVClient()
    start = time.monotonic()
    fail_count = 0
    print(f"[시작] {THEATER_NAME} / {TARGET_DATE} / keyword={MOVIE_KEYWORD} / {CHECK_INTERVAL}초 간격")

    while time.monotonic() - start < RUN_SECONDS:
        try:
            rows = client.fetch()
            matches = find_matches(rows)
            stamp = now_kst().strftime("%Y-%m-%d %H:%M:%S KST")
            if matches:
                print(f"[{stamp}] FOUND: {len(matches)}개")
                send_four_alerts(matches)
                disable_this_workflow()
                print("[완료] 알림 4회 전송 후 감시 종료")
                return
            print(f"[{stamp}] 아직 없음 / 전체 회차 {len(rows)}개")
            fail_count = 0
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            return
        except Exception as e:
            fail_count += 1
            print(f"[오류] 빠른 복구까지 실패: {type(e).__name__}: {e}")
            print("[재시도] 30초 후 다시 확인")
            time.sleep(30)

    print("[종료] 이번 Actions 실행 시간 종료. 다음 예약 실행이 이어서 감시합니다.")


if __name__ == "__main__":
    main()
