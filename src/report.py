from __future__ import annotations

import html
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
MARKET_INDEXES = {"KOSPI": "^KS11", "KOSDAQ": "^KQ11", "NASDAQ": "^IXIC"}
POSITIVE_WORDS = ("수주", "공급계약", "유상증자 결정", "무상증자", "특허", "승인", "허가", "배당", "자사주", "영업이익")
NEGATIVE_WORDS = ("횡령", "배임", "상장폐지", "불성실", "감사의견", "파산", "회생절차")


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def fetch_prices(tickers: list[str]) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    for ticker in sorted(set(tickers)):
        try:
            frame = yf.download(ticker, period="6mo", interval="1d", auto_adjust=True, progress=False, timeout=20)
            if isinstance(frame.columns, pd.MultiIndex):
                frame.columns = frame.columns.get_level_values(0)
            if not frame.empty:
                result[ticker] = frame.dropna(subset=["Close"])
        except Exception as exc:
            print(f"price warning {ticker}: {exc}", file=sys.stderr)
    return result


def metrics(frame: pd.DataFrame) -> dict:
    close = frame["Close"].astype(float)
    volume = frame["Volume"].astype(float)
    latest = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) > 1 else latest
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean())
    avg_vol20 = float(volume.tail(20).mean()) or 1
    vol_ratio = float(volume.iloc[-1]) / avg_vol20
    ret1 = (latest / prev - 1) * 100 if prev else 0
    ret5 = (latest / float(close.iloc[-6]) - 1) * 100 if len(close) > 5 else ret1
    pullback = ma20 <= latest <= ma20 * 1.035 and latest >= ma60 and ret1 > -2
    volume_signal = vol_ratio >= 1.5 and ret1 > 0
    return {"price": latest, "ret1": ret1, "ret5": ret5, "ma20": ma20, "vol_ratio": vol_ratio,
            "pullback": pullback, "volume_signal": volume_signal}


def dart_corp_map(api_key: str) -> dict[str, str]:
    response = requests.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": api_key}, timeout=30)
    response.raise_for_status()
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        root = ElementTree.fromstring(archive.read("CORPCODE.xml"))
    return {item.findtext("stock_code", "").strip(): item.findtext("corp_code", "").strip()
            for item in root.findall("list") if item.findtext("stock_code", "").strip()}


def fetch_positive_disclosures(config: dict, now: datetime) -> list[dict]:
    api_key = os.getenv("DART_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        corp_map = dart_corp_map(api_key)
        start = (now - timedelta(days=3)).strftime("%Y%m%d")
        end = now.strftime("%Y%m%d")
        entries = config["portfolio"] + config["watchlist"]
        output = []
        for item in entries:
            code = item["ticker"].split(".")[0]
            corp_code = corp_map.get(code)
            if not corp_code:
                continue
            payload = requests.get("https://opendart.fss.or.kr/api/list.json", params={
                "crtfc_key": api_key, "corp_code": corp_code, "bgn_de": start, "end_de": end, "page_count": 100
            }, timeout=20).json()
            for row in payload.get("list", []):
                title = row.get("report_nm", "")
                if any(word in title for word in POSITIVE_WORDS) and not any(word in title for word in NEGATIVE_WORDS):
                    output.append({"name": item["name"], "title": title, "date": row.get("rcept_dt", ""),
                                   "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row.get('rcept_no', '')}"})
            time.sleep(0.08)
        return output
    except Exception as exc:
        print(f"dart warning: {exc}", file=sys.stderr)
        return []


def make_report(config: dict, prices: dict[str, pd.DataFrame], now: datetime) -> tuple[str, str, dict[str, str]]:
    stats = {ticker: metrics(frame) for ticker, frame in prices.items() if len(frame) >= 2}
    names = config["ticker_names"]
    rows = []
    for group, label in ((config["portfolio"], "보유"), (config["watchlist"], "관심")):
        for item in group:
            stat = stats.get(item["ticker"])
            if stat:
                rows.append((label, item["name"], stat))

    sectors = []
    for sector, tickers in config["sectors"].items():
        available = [(ticker, stats[ticker]) for ticker in tickers if ticker in stats]
        if available:
            score = sum(s["ret5"] for _, s in available) / len(available)
            top3 = sorted(available, key=lambda pair: pair[1]["ret5"], reverse=True)[:3]
            sectors.append((sector, score, top3))
    sectors.sort(key=lambda item: item[1], reverse=True)
    disclosures = fetch_positive_disclosures(config, now)

    def pct(value: float) -> str:
        return f"{value:+.2f}%"

    md = [f"# AI 주식 모멘텀 보고서", "", f"> 생성: {now:%Y-%m-%d %H:%M} KST · 자동 계산 참고자료", "",
          "## 보유·관심종목", "", "|구분|종목|현재가|1일|5일|거래량/20일|신호|", "|---|---|---:|---:|---:|---:|---|"]
    html_rows = []
    for label, name, stat in rows:
        signals = " · ".join(x for x, yes in (("눌림목", stat["pullback"]), ("거래량↑", stat["volume_signal"])) if yes) or "관찰"
        md.append(f"|{label}|{name}|{stat['price']:,.0f}|{pct(stat['ret1'])}|{pct(stat['ret5'])}|{stat['vol_ratio']:.2f}배|{signals}|")
        tone = "up" if stat["ret1"] >= 0 else "down"
        html_rows.append(f"<tr><td>{label}</td><td>{html.escape(name)}</td><td>{stat['price']:,.0f}</td><td class='{tone}'>{pct(stat['ret1'])}</td><td>{pct(stat['ret5'])}</td><td>{stat['vol_ratio']:.2f}배</td><td>{signals}</td></tr>")

    md += ["", "## 강세 모멘텀 업종", ""]
    sector_cards = []
    for sector, score, top3 in sectors[:5]:
        picks = ", ".join(f"{names.get(t, t)} {pct(s['ret5'])}" for t, s in top3)
        md += [f"### {sector} · 5일 평균 {pct(score)}", f"- 상위 3종목: {picks}", ""]
        sector_cards.append(f"<a class='card linkcard' href='sectors.html'><h3>{html.escape(sector)}</h3><b>{pct(score)}</b><p>{html.escape(picks)}</p><span>세부 종목 보기 →</span></a>")

    md += ["## 긍정 신규공시", ""]
    disclosure_html = []
    if disclosures:
        for row in disclosures:
            md.append(f"- [{row['name']} · {row['title']}]({row['url']}) ({row['date']})")
            disclosure_html.append(f"<li><a href='{row['url']}'>{html.escape(row['name'])} · {html.escape(row['title'])}</a></li>")
    else:
        msg = "조건에 맞는 신규공시가 없거나 DART_API_KEY가 아직 연결되지 않았습니다."
        md.append(f"- {msg}")
        disclosure_html.append(f"<li>{msg}</li>")

    md += ["", "## 신호 해석", "", "- **눌림목:** 종가가 20일선 위 3.5% 이내이며 60일선 위에 있는 경우",
           "- **거래량↑:** 당일 거래량이 20일 평균의 1.5배 이상이고 주가가 상승한 경우",
           "- 매수 추천이 아닌 학습·관찰용 정량 신호입니다.", ""]
    top_sector = sectors[0][0] if sectors else "데이터 대기"
    top_score = sectors[0][1] if sectors else 0
    signal_count = sum(1 for _, _, stat in rows if stat["pullback"] or stat["volume_signal"])
    market_cards = []
    for market_name, ticker in MARKET_INDEXES.items():
        stat = stats.get(ticker)
        if stat:
            tone = "up" if stat["ret1"] >= 0 else "down"
            market_cards.append(f"<article class='metric'><small>{market_name}</small><b>{stat['price']:,.2f}</b><span class='{tone}'>{pct(stat['ret1'])}</span></article>")
        else:
            market_cards.append(f"<article class='metric'><small>{market_name}</small><b>데이터 대기</b></article>")
    stylesheet = """:root{--bg:#060b14;--panel:#0d1624;--line:#1d2a3d;--text:#edf4ff;--muted:#8191a8;--red:#ff5573;--blue:#4c9fff;--mint:#38d9b3}*{box-sizing:border-box}body{font-family:Inter,Pretendard,system-ui,sans-serif;margin:0;background:radial-gradient(circle at 75% -10%,#132c52 0,transparent 34%),var(--bg);color:var(--text)}.wrap{max-width:1180px;margin:auto;padding:28px 22px 60px}.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:15px}.brand{display:flex;align-items:center;gap:12px}.logo{display:grid;place-items:center;width:42px;height:42px;border-radius:13px;background:linear-gradient(135deg,var(--blue),#765cff);font-weight:900}.brand h1{font-size:19px;margin:0}.muted{color:var(--muted)}.head-actions{display:flex;align-items:center;gap:8px}.live{font-size:12px;color:var(--mint);background:#0c2b27;border:1px solid #175247;padding:8px 11px;border-radius:99px}.refresh{border:1px solid #2f6fb8;background:#102846;color:#dcecff;border-radius:10px;padding:8px 11px;font:700 12px inherit;cursor:pointer}.refresh:hover{background:#17375f;border-color:var(--blue)}.refresh:active{transform:translateY(1px)}.nav{display:flex;gap:7px;overflow:auto;margin:0 0 28px;padding-bottom:5px}.nav a{white-space:nowrap;text-decoration:none;color:#9eafc5;background:#0b1523;border:1px solid var(--line);padding:9px 12px;border-radius:10px;font-size:12px}.nav a:hover{color:white;border-color:var(--blue)}.hero{padding:27px;border:1px solid var(--line);border-radius:20px;background:linear-gradient(145deg,rgba(17,29,47,.96),rgba(10,18,30,.96));margin-bottom:26px}.eyebrow{color:var(--blue);font-size:12px;font-weight:800;letter-spacing:.12em}.hero h2{font-size:30px;margin:9px 0}.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:22px}.metric{display:block;text-decoration:none;color:var(--text);background:#0a1321;border:1px solid var(--line);border-radius:14px;padding:15px}.metric:hover,.linkcard:hover{border-color:var(--blue);transform:translateY(-2px)}.metric small{display:block;color:var(--muted);margin-bottom:7px}.metric b{font-size:20px}.section-title{display:flex;justify-content:space-between;align-items:end;margin:28px 2px 12px}.section-title h2{font-size:17px;margin:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.card,table,.panel{background:linear-gradient(145deg,#101d2f,#0b1422);border:1px solid var(--line);border-radius:16px}.card,.panel{padding:18px}.linkcard{display:block;text-decoration:none;color:var(--text);transition:.2s}.linkcard span{font-size:11px;color:var(--blue)}.card h3{font-size:14px;margin:0 0 15px}.card b,.up{color:var(--red)}.card p{font-size:12px;color:#9cacbf;line-height:1.6}.down{color:var(--blue)}.table{border-radius:16px;overflow:hidden;border:1px solid var(--line)}table{width:100%;border-collapse:collapse;background:#0d1726}th{font-size:11px;color:var(--muted);background:#101d2f}th,td{padding:13px;text-align:right;border-bottom:1px solid var(--line)}td{font-size:13px}tr:last-child td{border:0}th:nth-child(-n+2),td:nth-child(-n+2),th:last-child,td:last-child{text-align:left}a{color:#75baff}.panel{margin-bottom:12px}.panel li{margin:8px 0;line-height:1.5}.pagehead{margin:18px 0 24px}.pagehead h2{font-size:28px;margin:5px 0}footer{text-align:center;color:#66768d;font-size:11px;margin-top:32px}@media(max-width:700px){.head-actions{align-items:flex-end;flex-direction:column}.refresh{padding:8px 10px}.summary{grid-template-columns:1fr}.table{overflow:auto}table{min-width:760px}.hero h2,.pagehead h2{font-size:24px}.top{align-items:flex-start}.live{font-size:10px}}"""
    nav = "<nav class='nav'><a href='index.html'>대시보드</a><a href='stocks.html'>종목분석</a><a href='sectors.html'>업종분석</a><a href='disclosures.html'>긍정공시</a><a href='signals.html'>매매신호</a></nav>"
    header = "<header class='top'><div class='brand'><div class='logo'>P</div><div><h1>Pulse Market</h1><small class='muted'>AI STOCK ORCHESTRATOR</small></div></div><div class='head-actions'><span class='live'>● 자동 분석 정상</span><button class='refresh' type='button' onclick=\"location.replace(location.pathname+'?refresh='+Date.now())\" aria-label='최신 데이터 새로고침'>↻ 새로고침</button></div></header>"

    def shell(title: str, body: str) -> str:
        return f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>{html.escape(title)} · Pulse Market</title><style>{stylesheet}</style></head><body><main class='wrap'>{header}{nav}{body}<footer>{now:%Y-%m-%d %H:%M} KST · 자동 계산 결과이며 투자 권유가 아닙니다.</footer></main></body></html>"

    dashboard = f"<section class='hero'><div class='eyebrow'>TODAY'S MARKET PULSE</div><h2>가격·거래량·공시를 한 화면에서.</h2><p class='muted'>{now:%Y-%m-%d %H:%M} KST 기준 자동 브리핑</p><div class='summary'>{''.join(market_cards)}</div><div class='summary'><a class='metric' href='stocks.html'><small>추적 종목</small><b>{len(rows)}개 →</b></a><a class='metric' href='sectors.html'><small>1위 모멘텀</small><b>{html.escape(top_sector)} {pct(top_score)} →</b></a><a class='metric' href='signals.html'><small>포착 신호</small><b>{signal_count}개 →</b></a></div></section><div class='section-title'><h2>보유 · 관심종목</h2><a href='stocks.html'>전체 보기 →</a></div><div class='table'><table><thead><tr><th>구분</th><th>종목</th><th>현재가</th><th>1일</th><th>5일</th><th>거래량</th><th>신호</th></tr></thead><tbody>{''.join(html_rows)}</tbody></table></div><div class='section-title'><h2>모멘텀 레이더</h2><a href='sectors.html'>전체 업종 →</a></div><section class='grid'>{''.join(sector_cards)}</section>"
    stock_body = f"<div class='pagehead'><div class='eyebrow'>STOCKS</div><h2>종목별 상세 분석</h2><p class='muted'>현재가, 단기 수익률, 거래량 배수와 포착 신호를 비교합니다.</p></div><div class='table'><table><thead><tr><th>구분</th><th>종목</th><th>현재가</th><th>1일</th><th>5일</th><th>거래량/20일</th><th>판정</th></tr></thead><tbody>{''.join(html_rows)}</tbody></table></div>"
    all_sector_cards = []
    for rank, (sector, score, top3) in enumerate(sectors, 1):
        picks = "".join(f"<li><b>{html.escape(names.get(t, t))}</b> · 5일 {pct(s['ret5'])} · 거래량 {s['vol_ratio']:.2f}배</li>" for t, s in top3)
        all_sector_cards.append(f"<article class='panel'><span class='muted'>RANK {rank}</span><h3>{html.escape(sector)} <span class='up'>{pct(score)}</span></h3><ol>{picks}</ol></article>")
    sector_body = f"<div class='pagehead'><div class='eyebrow'>SECTOR MOMENTUM</div><h2>업종별 모멘텀 순위</h2><p class='muted'>구성 종목의 최근 5거래일 평균과 업종별 상위 3종목입니다.</p></div>{''.join(all_sector_cards)}"
    disclosure_body = f"<div class='pagehead'><div class='eyebrow'>DART DISCLOSURES</div><h2>긍정 신규공시</h2><p class='muted'>수주·공급계약·특허·승인 등 긍정 키워드만 선별합니다.</p></div><section class='panel'><ul>{''.join(disclosure_html)}</ul></section>"
    signal_rows = [row for row, (_, _, stat) in zip(html_rows, rows) if stat["pullback"] or stat["volume_signal"]]
    signal_content = "".join(signal_rows) or "<tr><td colspan='7'>현재 조건에 맞는 신호가 없습니다.</td></tr>"
    signal_body = f"<div class='pagehead'><div class='eyebrow'>TRADING SIGNALS</div><h2>눌림목 · 거래량 신호</h2><p class='muted'>정량 조건을 통과한 종목만 모아 봅니다.</p></div><div class='table'><table><thead><tr><th>구분</th><th>종목</th><th>현재가</th><th>1일</th><th>5일</th><th>거래량</th><th>신호</th></tr></thead><tbody>{signal_content}</tbody></table></div><section class='panel'><h3>판정 기준</h3><p>눌림목: 20일선 위 3.5% 이내이며 60일선 위</p><p>거래량↑: 당일 거래량이 20일 평균의 1.5배 이상이며 당일 상승</p></section>"
    pages = {"stocks.html": shell("종목분석", stock_body), "sectors.html": shell("업종분석", sector_body), "disclosures.html": shell("긍정공시", disclosure_body), "signals.html": shell("매매신호", signal_body)}
    return "\n".join(md), shell("대시보드", dashboard), pages


def main() -> None:
    config = load_config()
    tickers = [item["ticker"] for key in ("portfolio", "watchlist") for item in config[key]]
    tickers += [ticker for values in config["sectors"].values() for ticker in values]
    tickers += list(MARKET_INDEXES.values())
    now = datetime.now(KST)
    prices = fetch_prices(tickers)
    markdown, page, detail_pages = make_report(config, prices, now)
    docs = ROOT / "docs"
    reports = ROOT / "reports"
    docs.mkdir(exist_ok=True)
    reports.mkdir(exist_ok=True)
    (docs / "index.html").write_text(page, encoding="utf-8")
    for filename, content in detail_pages.items():
        (docs / filename).write_text(content, encoding="utf-8")
    (reports / f"{now:%Y-%m-%d_%H%M}.md").write_text(markdown, encoding="utf-8")
    (ROOT / "LATEST.md").write_text(markdown, encoding="utf-8")
    print(f"generated report with {len(prices)} price series")


if __name__ == "__main__":
    main()
