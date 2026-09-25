import os
import time
import json
import base64
from datetime import datetime, timezone, timedelta

from curl_cffi import requests as cf_requests

# =========================
# 감시 조건
# =========================
SITE_NO = "0089"
THEATER_NAME = "CGV 센텀시티"
TARGET_DATE = "20260930"
MOVIE_KEYWORD = "치이카와"

CHECK_INTERVAL = 60
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "20000"))

BASE_URL = "https://cgv.co.kr"
SCHEDULE_ENDPOINT = f"{BASE_URL}/api/v1/booking/searchMovScnInfo"

CO_CD = "A420"
RTCTL_SCOP_CD = "08"

COMMON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": "https://cgv.co.kr/cnm/movieBook",
}

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main")

STATE_PATH = ".cgv_state.json"

KST = timezone(timedelta(hours=9))


def now_kst():
    return datetime.now(KST)


def tg_send(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = cf_requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
        impersonate="chrome",
        timeout=20,
    )
    if r.status_code == 429:
        try:
            retry_after = int(r.json().get("parameters", {}).get("retry_after", 60))
        except Exception:
            retry_after = 60
        time.sleep(min(retry_after, 120))
        return tg_send(text)
    r.raise_for_status()


class CGVClient:
    def __init__(self):
        self.session = None

    def _new_session(self):
        print("[CGV] 새 API 세션 생성", flush=True)
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

    def fetch_once(self):
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

        if r.status_code in (403, 429):
            raise RuntimeError(f"CGV 시간표 API HTTP {r.status_code}")

        if r.status_code != 200:
            raise RuntimeError(f"CGV 시간표 API HTTP {r.status_code}")

        payload = r.json()

        if payload.get("statusCode") != 0:
            raise RuntimeError(
                f"CGV API statusCode={payload.get('statusCode')} "
                f"message={payload.get('statusMessage')}"
            )

        return payload.get("data") or []

    def fetch_with_fast_recovery(self):
        delays = [0, 5, 10, 20, 30]
        last_error = None

        for idx, delay in enumerate(delays):
            if delay:
                print(f"[CGV] 빠른 복구 재시도: {delay}초 대기", flush=True)
                time.sleep(delay)

            if idx > 0:
                self.reset_session()

            try:
                return self.fetch_once()
            except Exception as e:
                last_error = e
                print(
                    f"[CGV] 조회 실패 {idx + 1}/{len(delays)}: {e}",
                    flush=True,
                )

        raise last_error or RuntimeError("CGV 조회 실패")


def normalize(text):
    return (text or "").replace(" ", "").lower()


def find_matches(rows):
    keyword = normalize(MOVIE_KEYWORD)
    return [
        row for row in rows
        if keyword in normalize(row.get("movNm") or "")
    ]


def fmt_time(raw):
    raw = str(raw or "")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 4:
        hhmm = digits[-4:]
        return f"{hhmm[:2]}:{hhmm[2:]}"
    return raw or "시간 확인 필요"


def session_key(row):
    """
    좌석 수(frSeatCnt)는 키에 넣지 않습니다.
    좌석 수는 예매될 때마다 바뀌므로 '회차 변동' 알림 대상이 아닙니다.

    시간/상영관/영화명 등 구조적인 변화만 감지합니다.
    """
    return "|".join([
        str(row.get("movNm") or ""),
        str(row.get("scnsNm") or ""),
        str(row.get("scnsrtTm") or ""),
        str(row.get("scnedTm") or ""),
        str(row.get("scnSseq") or ""),
    ])


def compact_session(row):
    return {
        "key": session_key(row),
        "title": row.get("movNm") or "제목 미확인",
        "screen": row.get("scnsNm") or "상영관 미확인",
        "start": fmt_time(row.get("scnsrtTm")),
        "end": fmt_time(row.get("scnedTm")),
        "free": row.get("frSeatCnt"),
        "total": row.get("stcnt"),
    }


def current_state(matches):
    items = [compact_session(row) for row in matches]
    items.sort(key=lambda x: (x["start"], x["screen"], x["title"], x["key"]))
    return {"sessions": items}


def state_map(state):
    return {
        item["key"]: item
        for item in (state or {}).get("sessions", [])
    }


def diff_states(old_state, new_state):
    old_map = state_map(old_state)
    new_map = state_map(new_state)

    added_keys = sorted(set(new_map) - set(old_map))
    removed_keys = sorted(set(old_map) - set(new_map))

    return (
        [new_map[k] for k in added_keys],
        [old_map[k] for k in removed_keys],
    )


def seat_text(item):
    free = item.get("free")
    total = item.get("total")
    if free is None and total is None:
        return ""
    txt = f" / 잔여 {free if free is not None else '?'}"
    if total is not None:
        txt += f"/{total}석"
    return txt


def build_change_alert(new_state, added, removed, seq):
    lines = [
        f"🚨 CGV 치이카와 회차 변동 감지 ({seq}/4)",
        "",
        f"📍 {THEATER_NAME}",
        "📅 2026-09-30",
        f"🎬 현재 치이카와 회차: {len(new_state.get('sessions', []))}개",
        "",
    ]

    if added:
        lines.append("✅ 새로 추가된 회차")
        for item in added:
            lines.append(
                f"• {item['start']} / {item['screen']}{seat_text(item)}"
            )
        lines.append("")

    if removed:
        lines.append("❌ 사라진 회차")
        for item in removed:
            lines.append(
                f"• {item['start']} / {item['screen']}"
            )
        lines.append("")

    lines.append("현재 전체 치이카와 회차")
    for item in new_state.get("sessions", []):
        lines.append(
            f"• {item['start']} / {item['screen']}{seat_text(item)}"
        )

    lines += [
        "",
        "바로 예매 확인:",
        "https://cgv.co.kr/cnm/movieBook/cinema",
    ]
    return "\n".join(lines)


def build_initial_alert(state, seq):
    sessions = state.get("sessions", [])
    lines = [
        f"🚨 CGV 치이카와 현재 회차 발견 ({seq}/4)",
        "",
        f"📍 {THEATER_NAME}",
        "📅 2026-09-30",
        f"🎬 현재 치이카와 회차: {len(sessions)}개",
        "",
    ]

    for item in sessions:
        lines.append(
            f"• {item['start']} / {item['screen']}{seat_text(item)}"
        )

    lines += [
        "",
        "이후 회차 추가/삭제/시간·상영관 변경이 생기면 다시 알려드립니다.",
        "좌석 수 변화만으로는 알림하지 않습니다.",
        "",
        "https://cgv.co.kr/cnm/movieBook/cinema",
    ]
    return "\n".join(lines)


# ============================================================
# GitHub 저장소에 상태 파일 저장
# ============================================================

def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_persisted_state():
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        print("[STATE] GitHub 상태 저장 비활성 - 메모리 상태만 사용", flush=True)
        return None, None

    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{STATE_PATH}"

    r = cf_requests.get(
        url,
        headers=github_headers(),
        params={"ref": GITHUB_REF_NAME},
        timeout=20,
    )

    if r.status_code == 404:
        print("[STATE] 기존 상태 파일 없음", flush=True)
        return None, None

    if r.status_code != 200:
        print(f"[STATE] 상태 읽기 실패 HTTP {r.status_code}", flush=True)
        return None, None

    payload = r.json()
    sha = payload.get("sha")

    try:
        raw = base64.b64decode(payload["content"]).decode("utf-8")
        return json.loads(raw), sha
    except Exception as e:
        print(f"[STATE] 상태 파싱 실패: {e}", flush=True)
        return None, sha


def save_persisted_state(state, sha=None):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return sha

    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{STATE_PATH}"

    body = {
        "message": "Update CGV watch state",
        "content": base64.b64encode(
            json.dumps(
                state,
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
        ).decode("ascii"),
        "branch": GITHUB_REF_NAME,
    }

    if sha:
        body["sha"] = sha

    r = cf_requests.put(
        url,
        headers=github_headers(),
        json=body,
        timeout=20,
    )

    if r.status_code not in (200, 201):
        print(
            f"[STATE] 상태 저장 실패 HTTP {r.status_code}: {r.text[:300]}",
            flush=True,
        )
        return sha

    try:
        new_sha = r.json()["content"]["sha"]
    except Exception:
        new_sha = sha

    print("[STATE] 현재 회차 상태 저장 완료", flush=True)
    return new_sha


# ============================================================
# 반복 알림 큐
# ============================================================

pending_alerts = []


def queue_repeat_alerts(message_builder):
    """
    즉시 1회 발송하고, 이후 60/120/180초에 3회 추가 예약.
    감시 루프는 멈추지 않습니다.
    """
    tg_send(message_builder(1))
    print("[알림] 1/4 전송 완료", flush=True)

    now = time.monotonic()
    for seq, delay in [(2, 60), (3, 120), (4, 180)]:
        pending_alerts.append({
            "due": now + delay,
            "seq": seq,
            "builder": message_builder,
        })


def flush_due_alerts():
    now = time.monotonic()
    due = [x for x in pending_alerts if x["due"] <= now]

    for item in due:
        try:
            tg_send(item["builder"](item["seq"]))
            print(f"[알림] {item['seq']}/4 전송 완료", flush=True)
        except Exception as e:
            print(f"[알림] 반복 알림 실패: {e}", flush=True)
        finally:
            pending_alerts.remove(item)


def main():
    test_alert = os.getenv("TEST_ALERT", "false").lower() == "true"

    if test_alert:
        tg_send(
            "✅ CGV 변동 감시봇 테스트 성공\n\n"
            "CGV 센텀시티 / 2026-09-30 / 치이카와\n"
            "회차 추가·삭제·시간/상영관 변경을 감시합니다."
        )
        print("테스트 알림 전송 완료", flush=True)
        return

    client = CGVClient()
    start = time.monotonic()

    last_state, state_sha = load_persisted_state()
    state_initialized = last_state is not None

    print(
        f"[시작] {THEATER_NAME} / {TARGET_DATE} / "
        f"keyword={MOVIE_KEYWORD} / {CHECK_INTERVAL}초 간격",
        flush=True,
    )

    while time.monotonic() - start < RUN_SECONDS:
        cycle_start = time.monotonic()

        flush_due_alerts()

        try:
            rows = client.fetch_with_fast_recovery()
            matches = find_matches(rows)
            new_state = current_state(matches)

            stamp = now_kst().strftime("%Y-%m-%d %H:%M:%S KST")

            if not state_initialized:
                # 최초 기준 상태 설정.
                # 이미 치이카와 회차가 있다면 사용자 요청대로 첫 상태도 알림.
                print(
                    f"[{stamp}] 초기 상태 / 치이카와 {len(new_state['sessions'])}회차 / "
                    f"전체 회차 {len(rows)}개",
                    flush=True,
                )

                if new_state["sessions"]:
                    snapshot = json.loads(json.dumps(new_state, ensure_ascii=False))
                    queue_repeat_alerts(
                        lambda seq, s=snapshot: build_initial_alert(s, seq)
                    )

                state_sha = save_persisted_state(new_state, state_sha)
                last_state = new_state
                state_initialized = True

            else:
                added, removed = diff_states(last_state, new_state)

                if added or removed:
                    print(
                        f"[{stamp}] 변동 감지 / +{len(added)} -{len(removed)} / "
                        f"현재 {len(new_state['sessions'])}회차",
                        flush=True,
                    )

                    snapshot = json.loads(json.dumps(new_state, ensure_ascii=False))
                    added_copy = json.loads(json.dumps(added, ensure_ascii=False))
                    removed_copy = json.loads(json.dumps(removed, ensure_ascii=False))

                    queue_repeat_alerts(
                        lambda seq, s=snapshot, a=added_copy, r=removed_copy:
                            build_change_alert(s, a, r, seq)
                    )

                    state_sha = save_persisted_state(new_state, state_sha)
                    last_state = new_state

                else:
                    print(
                        f"[{stamp}] 변동 없음 / 치이카와 {len(new_state['sessions'])}회차 / "
                        f"전체 회차 {len(rows)}개",
                        flush=True,
                    )

            elapsed = time.monotonic() - cycle_start
            time.sleep(max(1, CHECK_INTERVAL - elapsed))

        except KeyboardInterrupt:
            return
        except Exception as e:
            stamp = now_kst().strftime("%Y-%m-%d %H:%M:%S KST")
            print(
                f"[{stamp}] [오류] {type(e).__name__}: {e}",
                flush=True,
            )
            time.sleep(30)

    # job 종료 직전 도착한 반복 알림이 있더라도 다음 scheduled run에서
    # 상태 자체는 저장되어 있으므로 중복 신규 감지는 발생하지 않습니다.
    print("[종료] 이번 Actions 실행 시간 종료", flush=True)


if __name__ == "__main__":
    main()
