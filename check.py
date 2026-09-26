import os
import time
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from curl_cffi import requests as cf_requests

SITE_NO = "0089"
THEATER_NAME = "CGV 센텀시티"
MOVIE_KEYWORD = "치이카와"

# 감시 날짜: 9/30 + 10/3
TARGET_DATES = [
    ("20260930", "2026-09-30"),
    ("20261003", "2026-10-03"),
]

CHECKS_PER_RUN = 5
CHECK_INTERVAL = 60

BASE_URL = "https://cgv.co.kr"
BOOKING_PAGE = f"{BASE_URL}/cnm/movieBook/cinema"
SCHEDULE_ENDPOINT = f"{BASE_URL}/api/v1/booking/searchMovScnInfo"

CO_CD = "A420"
RTCTL_SCOP_CD = "08"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_PATH = ".cgv_state.json"
KST = timezone(timedelta(hours=9))

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": BOOKING_PAGE,
}


def tg_send(text):
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
    r.raise_for_status()


def normalize(s):
    return (s or "").replace(" ", "").lower()


def fmt_time(raw):
    s = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return f"{s[-4:-2]}:{s[-2:]}" if len(s) >= 4 else (str(raw) if raw else "시간 미확인")


def session_key(row):
    # 사람이 보는 회차 기준만 사용.
    # 좌석 수 / 내부 ID 변화는 무시.
    return "|".join([
        str(row.get("movNm") or ""),
        str(row.get("scnsNm") or ""),
        str(row.get("scnsrtTm") or ""),
    ])


def make_date_state(rows):
    items = []

    for r in rows:
        if normalize(MOVIE_KEYWORD) in normalize(r.get("movNm") or ""):
            items.append({
                "key": session_key(r),
                "title": r.get("movNm") or "제목 미확인",
                "screen": r.get("scnsNm") or "상영관 미확인",
                "start": fmt_time(r.get("scnsrtTm")),
                "free": r.get("frSeatCnt"),
                "total": r.get("stcnt"),
            })

    items.sort(key=lambda x: (x["start"], x["screen"], x["title"]))
    return {"sessions": items}


def state_map(state):
    return {x["key"]: x for x in (state or {}).get("sessions", [])}


def diff_states(old, new):
    om, nm = state_map(old), state_map(new)
    added = [nm[k] for k in sorted(set(nm) - set(om))]
    removed = [om[k] for k in sorted(set(om) - set(nm))]
    return added, removed


def seat_txt(x):
    if x.get("free") is None and x.get("total") is None:
        return ""

    t = f" / 잔여 {x.get('free', '?')}"
    if x.get("total") is not None:
        t += f"/{x['total']}석"
    return t


def alert_text(date_label, state, added, removed, seq, initial=False):
    title = "현재 회차 발견" if initial else "회차 변동"

    lines = [
        f"🚨 CGV 치이카와 {title} ({seq}/4)",
        "",
        f"📍 {THEATER_NAME}",
        f"📅 {date_label}",
        f"🎬 현재 {len(state.get('sessions', []))}회차",
        "",
    ]

    if initial:
        for x in state.get("sessions", []):
            lines.append(f"• {x['start']} / {x['screen']}{seat_txt(x)}")

    else:
        if added:
            lines.append("✅ 추가")
            for x in added:
                lines.append(f"• {x['start']} / {x['screen']}{seat_txt(x)}")

        if removed:
            lines.append("❌ 삭제/변경 전")
            for x in removed:
                lines.append(f"• {x['start']} / {x['screen']}")

    lines += [
        "",
        "https://cgv.co.kr/cnm/movieBook/cinema",
    ]

    return "\n".join(lines)


def send_four(date_label, state, added=None, removed=None, initial=False):
    added, removed = added or [], removed or []

    for i in range(1, 5):
        tg_send(alert_text(date_label, state, added, removed, i, initial))
        print(f"[알림] {date_label} {i}/4", flush=True)

        if i < 4:
            time.sleep(60)


def load_state_local():
    p = Path(STATE_PATH)

    if not p.exists():
        print("[STATE] 저장된 상태 파일 없음", flush=True)
        return {}

    try:
        state = json.loads(p.read_text(encoding="utf-8"))
        print(
            f"[STATE] 기존 상태 로드 성공: 날짜 {len(state.get('dates', {}))}개",
            flush=True,
        )
        return state
    except Exception as e:
        print(f"[STATE] 읽기 실패: {e}", flush=True)
        return {}


def save_and_push_state(state):
    Path(STATE_PATH).write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ],
        check=True,
    )

    subprocess.run(["git", "add", STATE_PATH], check=True)

    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])

    if diff.returncode == 0:
        print("[STATE] 상태 변경 없음 - commit 생략", flush=True)
        return

    subprocess.run(
        ["git", "commit", "-m", "Update CGV watch state"],
        check=True,
    )
    subprocess.run(["git", "push"], check=True)

    print("[STATE] 상태 저장 및 push 성공", flush=True)


def fetch_schedule(target_date):
    session = cf_requests.Session(impersonate="chrome")

    params = {
        "coCd": CO_CD,
        "siteNo": SITE_NO,
        "scnYmd": target_date,
        "rtctlScopCd": RTCTL_SCOP_CD,
    }

    r = session.get(
        SCHEDULE_ENDPOINT,
        params=params,
        headers=HEADERS,
        timeout=20,
    )

    if r.status_code == 403:
        page = session.get(BOOKING_PAGE, timeout=20)

        print(
            f"[CGV] {target_date} API 403 → 예매페이지 HTTP {page.status_code}",
            flush=True,
        )

        if page.status_code == 200:
            r = session.get(
                SCHEDULE_ENDPOINT,
                params=params,
                headers=HEADERS,
                timeout=20,
            )

    if r.status_code != 200:
        raise RuntimeError(
            f"CGV {target_date} 조회 HTTP {r.status_code}"
        )

    payload = r.json()

    if payload.get("statusCode") != 0:
        raise RuntimeError(
            f"CGV {target_date} API statusCode={payload.get('statusCode')}"
        )

    return payload.get("data") or []


def main():
    state = load_state_local()

    # 이전 9/30 단일 날짜 포맷을 썼더라도 새 포맷으로 안전하게 전환
    if "dates" not in state:
        legacy_sessions = state.get("sessions")
        state = {"dates": {}}

        if legacy_sessions is not None:
            state["dates"]["20260930"] = {
                "sessions": legacy_sessions
            }

    for check_idx in range(CHECKS_PER_RUN):
        any_state_changed = False

        for target_date, date_label in TARGET_DATES:
            try:
                rows = fetch_schedule(target_date)
                new_date_state = make_date_state(rows)

                old_date_state = state["dates"].get(target_date)
                stamp = datetime.now(KST).strftime(
                    "%Y-%m-%d %H:%M:%S KST"
                )

                if old_date_state is None:
                    print(
                        f"[{stamp}] {date_label} 초기 / "
                        f"치이카와 {len(new_date_state['sessions'])}회차 / "
                        f"전체 {len(rows)}회차",
                        flush=True,
                    )

                    state["dates"][target_date] = new_date_state
                    save_and_push_state(state)
                    any_state_changed = True

                    if new_date_state["sessions"]:
                        send_four(
                            date_label,
                            new_date_state,
                            initial=True,
                        )

                else:
                    added, removed = diff_states(
                        old_date_state,
                        new_date_state,
                    )

                    if added or removed:
                        print(
                            f"[{stamp}] {date_label} 변동 "
                            f"+{len(added)} -{len(removed)}",
                            flush=True,
                        )

                        state["dates"][target_date] = new_date_state

                        # 중복 알림 방지를 위해 상태 저장 후 알림
                        save_and_push_state(state)
                        any_state_changed = True

                        send_four(
                            date_label,
                            new_date_state,
                            added,
                            removed,
                        )

                    else:
                        print(
                            f"[{stamp}] {date_label} 변동 없음 / "
                            f"치이카와 {len(new_date_state['sessions'])}회차 / "
                            f"전체 {len(rows)}회차",
                            flush=True,
                        )

            except Exception as e:
                print(
                    f"[조회 {check_idx+1}/{CHECKS_PER_RUN}] "
                    f"{date_label} 오류: {type(e).__name__}: {e}",
                    flush=True,
                )

        if check_idx < CHECKS_PER_RUN - 1:
            time.sleep(CHECK_INTERVAL)

    print(
        "[완료] 9/30 + 10/3 감시 완료. 다음 예약 실행에서 이어집니다.",
        flush=True,
    )


if __name__ == "__main__":
    main()
