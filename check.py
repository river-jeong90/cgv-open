import os, time, json, base64
from datetime import datetime, timezone, timedelta
from curl_cffi import requests as cf_requests

SITE_NO="0089"
TARGET_DATE="20260930"
MOVIE_KEYWORD="치이카와"
CHECKS_PER_RUN=5
CHECK_INTERVAL=60
BASE_URL="https://cgv.co.kr"
BOOKING_PAGE=f"{BASE_URL}/cnm/movieBook/cinema"
SCHEDULE_ENDPOINT=f"{BASE_URL}/api/v1/booking/searchMovScnInfo"
CO_CD="A420"
RTCTL_SCOP_CD="08"
BOT_TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID=os.environ["TELEGRAM_CHAT_ID"]
GITHUB_TOKEN=os.getenv("GITHUB_TOKEN","")
GITHUB_REPOSITORY=os.getenv("GITHUB_REPOSITORY","")
GITHUB_REF_NAME=os.getenv("GITHUB_REF_NAME","main")
STATE_PATH=".cgv_state.json"
KST=timezone(timedelta(hours=9))
HEADERS={"Accept":"application/json, text/plain, */*","Accept-Language":"ko-KR,ko;q=0.9","Referer":BOOKING_PAGE}

def tg_send(text):
    url=f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r=cf_requests.post(url,json={"chat_id":CHAT_ID,"text":text,"disable_web_page_preview":False},impersonate="chrome",timeout=20)
    r.raise_for_status()

def normalize(s): return (s or "").replace(" ","").lower()

def fmt_time(raw):
    s="".join(ch for ch in str(raw or "") if ch.isdigit())
    return f"{s[-4:-2]}:{s[-2:]}" if len(s)>=4 else (str(raw) if raw else "시간 미확인")

def session_key(row):
    return "|".join([str(row.get("movNm") or ""),str(row.get("scnsNo") or row.get("scnsNm") or ""),str(row.get("scnSseq") or ""),str(row.get("prodNo") or ""),str(row.get("scnsrtTm") or "")])

def make_state(rows):
    items=[]
    for r in rows:
        if normalize(MOVIE_KEYWORD) in normalize(r.get("movNm") or ""):
            items.append({
                "key":session_key(r),
                "title":r.get("movNm") or "제목 미확인",
                "screen":r.get("scnsNm") or "상영관 미확인",
                "start":fmt_time(r.get("scnsrtTm")),
                "free":r.get("frSeatCnt"),
                "total":r.get("stcnt"),
            })
    items.sort(key=lambda x:(x["start"],x["screen"],x["key"]))
    return {"sessions":items}

def state_map(s): return {x["key"]:x for x in (s or {}).get("sessions",[])}

def diff_states(old,new):
    om,nm=state_map(old),state_map(new)
    return [nm[k] for k in sorted(set(nm)-set(om))],[om[k] for k in sorted(set(om)-set(nm))]

def seat_txt(x):
    if x.get("free") is None and x.get("total") is None: return ""
    t=f" / 잔여 {x.get('free','?')}"
    if x.get("total") is not None: t+=f"/{x['total']}석"
    return t

def alert_text(state,added,removed,seq,initial=False):
    lines=[f"🚨 CGV 치이카와 {'현재 회차 발견' if initial else '회차 변동'} ({seq}/4)","",f"📅 2026-09-30",f"🎬 현재 {len(state.get('sessions',[]))}회차",""]
    if initial:
        for x in state.get("sessions",[]): lines.append(f"• {x['start']} / {x['screen']}{seat_txt(x)}")
    else:
        if added:
            lines.append("✅ 추가")
            for x in added: lines.append(f"• {x['start']} / {x['screen']}{seat_txt(x)}")
        if removed:
            lines.append("❌ 삭제/변경 전")
            for x in removed: lines.append(f"• {x['start']} / {x['screen']}")
    lines+=["","https://cgv.co.kr/cnm/movieBook/cinema"]
    return "\n".join(lines)

def send_four(state,added=None,removed=None,initial=False):
    added,removed=added or [],removed or []
    for i in range(1,5):
        tg_send(alert_text(state,added,removed,i,initial))
        if i<4: time.sleep(60)

def gh_headers():
    return {"Authorization":f"Bearer {GITHUB_TOKEN}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}

def load_state():
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY: return None,None
    url=f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{STATE_PATH}"
    r=cf_requests.get(url,headers=gh_headers(),params={"ref":GITHUB_REF_NAME},timeout=20)
    if r.status_code==404: return None,None
    if r.status_code!=200: return None,None
    p=r.json()
    return json.loads(base64.b64decode(p["content"]).decode()),p.get("sha")

def save_state(state,sha=None):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY: return sha
    url=f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{STATE_PATH}"
    body={"message":"Update CGV watch state","content":base64.b64encode(json.dumps(state,ensure_ascii=False,indent=2).encode()).decode(),"branch":GITHUB_REF_NAME}
    if sha: body["sha"]=sha
    r=cf_requests.put(url,headers=gh_headers(),json=body,timeout=20)
    if r.status_code not in (200,201): return sha
    return r.json().get("content",{}).get("sha",sha)

def fetch_schedule():
    s=cf_requests.Session(impersonate="chrome")
    params={"coCd":CO_CD,"siteNo":SITE_NO,"scnYmd":TARGET_DATE,"rtctlScopCd":RTCTL_SCOP_CD}
    r=s.get(SCHEDULE_ENDPOINT,params=params,headers=HEADERS,timeout=20)
    if r.status_code==403:
        p=s.get(BOOKING_PAGE,timeout=20)
        print(f"[CGV] API 403 → 예매페이지 HTTP {p.status_code}",flush=True)
        if p.status_code==200:
            r=s.get(SCHEDULE_ENDPOINT,params=params,headers=HEADERS,timeout=20)
    if r.status_code!=200: raise RuntimeError(f"CGV 조회 HTTP {r.status_code}")
    payload=r.json()
    if payload.get("statusCode")!=0: raise RuntimeError(f"CGV API statusCode={payload.get('statusCode')}")
    return payload.get("data") or []

def main():
    last_state,sha=load_state()
    for i in range(CHECKS_PER_RUN):
        try:
            rows=fetch_schedule()
            new_state=make_state(rows)
            stamp=datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
            if last_state is None:
                print(f"[{stamp}] 초기 / 치이카와 {len(new_state['sessions'])}회차 / 전체 {len(rows)}회차",flush=True)
                sha=save_state(new_state,sha)
                if new_state["sessions"]: send_four(new_state,initial=True)
                last_state=new_state
            else:
                added,removed=diff_states(last_state,new_state)
                if added or removed:
                    print(f"[{stamp}] 변동 +{len(added)} -{len(removed)}",flush=True)
                    sha=save_state(new_state,sha)
                    send_four(new_state,added,removed)
                    last_state=new_state
                else:
                    print(f"[{stamp}] 변동 없음 / 치이카와 {len(new_state['sessions'])}회차 / 전체 {len(rows)}회차",flush=True)
        except Exception as e:
            print(f"[조회 {i+1}/{CHECKS_PER_RUN}] 오류: {type(e).__name__}: {e}",flush=True)
        if i<CHECKS_PER_RUN-1: time.sleep(CHECK_INTERVAL)
    print("[완료] 이 runner 종료. 다음 5분 예약 실행에서 새 runner로 이어집니다.",flush=True)

if __name__=="__main__":
    main()
