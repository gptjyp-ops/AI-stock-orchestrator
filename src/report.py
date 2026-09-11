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


def disclosure_type(title: str) -> str:
    """Turn long DART report titles into a short, scannable category."""
    categories = (
        (("수주", "공급계약"), "수주·계약"),
        (("특허",), "특허"),
        (("승인", "허가"), "승인·허가"),
        (("무상증자", "유상증자"), "자금·증자"),
        (("배당",), "배당"),
        (("자사주",), "자사주"),
        (("영업이익",), "실적"),
    )
    for words, label in categories:
        if any(word in title for word in words):
            return label
    return "기타 긍정"


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
    return {
        "price": latest, "ret1": ret1, "ret5": ret5, "ma20": ma20, "ma60": ma60,
        "vol_ratio": vol_ratio, "pullback": pullback, "volume_signal": volume_signal
    }


def dart_corp_map(api_key: str) -> dict[str, str]:
    response = requests.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": api_key}, timeout=30)
    response.raise_for_status()
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        root = ElementTree.fromstring(archive.read("CORPCODE.xml"))
    return {
        item.findtext("stock_code", "").strip(): item.findtext("corp_code", "").strip()
        for item in root.findall("list") if item.findtext("stock_code", "").strip()
    }


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
            payload = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={"crtfc_key": api_key, "corp_code": corp_code, "bgn_de": start, "end_de": end, "page_count": 100},
                timeout=20,
            ).json()
            for row in payload.get("list", []):
                title = row.get("report_nm", "")
                if any(word in title for word in POSITIVE_WORDS) and not any(word in title for word in NEGATIVE_WORDS):
                    output.append({
                        "name": item["name"], "title": title, "date": row.get("rcept_dt", ""),
                        "type": disclosure_type(title),
                        "momentum": item.get("reason", "관심종목 관련"),
                        "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row.get('rcept_no', '')}"
                    })
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
            ranked = sorted(available, key=lambda pair: pair[1]["ret5"], reverse=True)
            sectors.append((sector, score, ranked))
    sectors.sort(key=lambda item: item[1], reverse=True)

    disclosures = fetch_positive_disclosures(config, now)

    def pct(value: float) -> str:
        return f"{value:+.2f}%"

    def row_html(label: str, name: str, stat: dict) -> str:
        signals = " · ".join(x for x, yes in (("눌림목", stat["pullback"]), ("거래량↑", stat["volume_signal"])) if yes) or "관찰"
        tone = "up" if stat["ret1"] >= 0 else "down"
        return (
            f"<tr><td>{label}</td><td>{html.escape(name)}</td><td>{stat['price']:,.0f}</td>"
            f"<td class='{tone}'>{pct(stat['ret1'])}</td><td>{pct(stat['ret5'])}</td>"
            f"<td>{stat['vol_ratio']:.2f}배</td><td>{signals}</td></tr>"
        )

    html_rows = [row_html(label, name, stat) for label, name, stat in rows]
    watch_rows = [row_html(label, name, stat) for label, name, stat in rows if label == "관심"]
    portfolio_rows = [row_html(label, name, stat) for label, name, stat in rows if label == "보유"]

    md = [
        "# AI 주식 모멘텀 보고서", "",
        f"> 생성: {now:%Y-%m-%d %H:%M} KST · 당일 자동 갱신본", "",
        "## 보유·관심종목", "",
        "|구분|종목|현재가|1일|5일|거래량/20일|신호|",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for label, name, stat in rows:
        signals = " · ".join(x for x, yes in (("눌림목", stat["pullback"]), ("거래량↑", stat["volume_signal"])) if yes) or "관찰"
        md.append(f"|{label}|{name}|{stat['price']:,.0f}|{pct(stat['ret1'])}|{pct(stat['ret5'])}|{stat['vol_ratio']:.2f}배|{signals}|")

    md += ["", "## 강세 모멘텀 업종", ""]
    sector_cards = []
    strong_sectors = sectors[:3]
    weak_sectors = sorted(sectors, key=lambda item: item[1])[:3]
    for sector, score, ranked in strong_sectors:
        top3 = ranked[:3]
        picks = ", ".join(f"{names.get(t, t)} {pct(s['ret5'])}" for t, s in top3)
        md += [f"### {sector} · 5일 평균 {pct(score)}", f"- 상위 3종목: {picks}", ""]
        sector_cards.append(
            f"<a class='card linkcard' href='sectors.html'><h3>{html.escape(sector)}</h3>"
            f"<b>{pct(score)}</b><p>{html.escape(picks)}</p><span>업종 상세 →</span></a>"
        )
    weak_sector_cards = []
    for sector, score, ranked in weak_sectors:
        bottom3 = sorted(ranked, key=lambda pair: pair[1]["ret5"])[:3]
        picks = ", ".join(f"{names.get(t, t)} {pct(s['ret5'])}" for t, s in bottom3)
        weak_sector_cards.append(
            f"<a class='card linkcard' href='sectors.html'><h3>{html.escape(sector)}</h3>"
            f"<b class='down'>{pct(score)}</b><p>{html.escape(picks)}</p><span>약세 상세 →</span></a>"
        )

    md += ["## 긍정 신규공시", ""]
    disclosure_html = []
    if disclosures:
        for row in disclosures:
            md.append(
                f"- [{row['name']} · {row['title']}]({row['url']})"
                f" · {row['type']} · {row['momentum']} ({row['date']})"
            )
            disclosure_html.append(
                "<tr>"
                f"<td>{html.escape(row['name'])}</td>"
                f"<td><span class='tag'>{html.escape(row['type'])}</span></td>"
                f"<td><a href='{row['url']}'>{html.escape(row['title'])}</a></td>"
                f"<td>{html.escape(row['momentum'])}</td>"
                f"<td>{html.escape(row['date'])}</td>"
                "</tr>"
            )
    else:
        msg = "조건에 맞는 신규공시가 없거나 DART_API_KEY가 아직 연결되지 않았습니다."
        md.append(f"- {msg}")
        disclosure_html.append(f"<tr><td colspan='5'>{msg}</td></tr>")

    md += ["", "## 약세 모멘텀 업종", ""]
    for sector, score, ranked in weak_sectors:
        bottom3 = sorted(ranked, key=lambda pair: pair[1]["ret5"])[:3]
        picks = ", ".join(f"{names.get(t, t)} {pct(s['ret5'])}" for t, s in bottom3)
        md += [f"### {sector} · 5일 평균 {pct(score)}", f"- 약세 3종목: {picks}", ""]

    md += [
        "", "## 신호 해석", "",
        "- **눌림목:** 종가가 20일선 위 3.5% 이내이며 60일선 위에 있는 경우",
        "- **거래량↑:** 당일 거래량이 20일 평균의 1.5배 이상이고 주가가 상승한 경우",
        "- 매수 추천이 아닌 학습·관찰용 정량 신호입니다.", ""
    ]

    signal_count = sum(1 for _, _, stat in rows if stat["pullback"] or stat["volume_signal"])
    market_cards = []
    market_rows = []
    for market_name, ticker in MARKET_INDEXES.items():
        stat = stats.get(ticker)
        if stat:
            tone = "up" if stat["ret1"] >= 0 else "down"
            market_cards.append(
                f"<a class='metric' href='market.html'><small>{market_name}</small>"
                f"<b>{stat['price']:,.2f}</b><span class='{tone}'>{pct(stat['ret1'])}</span></a>"
            )
            market_rows.append(
                f"<tr><td>{market_name}</td><td>{stat['price']:,.2f}</td>"
                f"<td class='{tone}'>{pct(stat['ret1'])}</td><td>{pct(stat['ret5'])}</td></tr>"
            )
        else:
            market_cards.append(f"<a class='metric' href='market.html'><small>{market_name}</small><b>데이터 대기</b></a>")
            market_rows.append(f"<tr><td>{market_name}</td><td colspan='3'>데이터 대기</td></tr>")

    stylesheet = ":root{--bg:#060b14;--panel:#0d1624;--line:#1d2a3d;--text:#edf4ff;--muted:#8191a8;--red:#ff5573;--blue:#4c9fff;--mint:#38d9b3}*{box-sizing:border-box}body{font-family:Inter,Pretendard,system-ui,sans-serif;margin:0;background:radial-gradient(circle at 75% -10%,#132c52 0,transparent 34%),var(--bg);color:var(--text)}.wrap{max-width:1180px;margin:auto;padding:28px 22px 60px}.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:15px}.brand{display:flex;align-items:center;gap:12px}.logo{display:grid;place-items:center;width:42px;height:42px;border-radius:13px;background:linear-gradient(135deg,var(--blue),#765cff);font-weight:900}.brand h1{font-size:19px;margin:0}.muted{color:var(--muted)}.head-actions{display:flex;align-items:center;gap:8px}.live{font-size:12px;color:var(--mint);background:#0c2b27;border:1px solid #175247;padding:8px 11px;border-radius:99px}.refresh{border:1px solid #2f6fb8;background:#102846;color:#dcecff;border-radius:10px;padding:8px 11px;font:700 12px inherit;cursor:pointer}.refresh:hover{background:#17375f;border-color:var(--blue)}.nav{display:flex;gap:7px;overflow:auto;margin:0 0 28px;padding-bottom:5px}.nav a{white-space:nowrap;text-decoration:none;color:#9eafc5;background:#0b1523;border:1px solid var(--line);padding:9px 12px;border-radius:10px;font-size:12px}.nav a:hover{color:white;border-color:var(--blue)}.hero{padding:27px;border:1px solid var(--line);border-radius:20px;background:linear-gradient(145deg,rgba(17,29,47,.96),rgba(10,18,30,.96));margin-bottom:26px}.eyebrow{color:var(--blue);font-size:12px;font-weight:800;letter-spacing:.12em}.hero h2{font-size:30px;margin:9px 0}.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:22px}.metric{display:block;text-decoration:none;color:var(--text);background:#0a1321;border:1px solid var(--line);border-radius:14px;padding:15px}.metric:hover,.linkcard:hover{border-color:var(--blue);transform:translateY(-2px)}.metric small{display:block;color:var(--muted);margin-bottom:7px}.metric b{font-size:20px}.section-title{display:flex;justify-content:space-between;align-items:end;margin:28px 2px 12px}.section-title h2{font-size:17px;margin:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.card,table,.panel{background:linear-gradient(145deg,#101d2f,#0b1422);border:1px solid var(--line);border-radius:16px}.card,.panel{padding:18px}.linkcard{display:block;text-decoration:none;color:var(--text);transition:.2s}.linkcard span{font-size:11px;color:var(--blue)}.card h3{font-size:14px;margin:0 0 10px}.card b,.up{color:var(--red)}.card b.down,.down{color:var(--blue)}.card p{font-size:12px;color:#9cacbf;line-height:1.6}.tag{display:inline-block;padding:4px 8px;border-radius:99px;background:#102846;color:#78b7ff;font-size:11px;font-weight:800}.table{border-radius:16px;overflow:hidden;border:1px solid var(--line);margin-bottom:14px}table{width:100%;border-collapse:collapse;background:#0d1726}th{font-size:11px;color:var(--muted);background:#101d2f}th,td{padding:13px;text-align:right;border-bottom:1px solid var(--line)}td{font-size:13px}tr:last-child td{border:0}th:nth-child(-n+2),td:nth-child(-n+2),th:last-child,td:last-child{text-align:left}a{color:#75baff}.panel{margin-bottom:12px}.panel li{margin:8px 0;line-height:1.5}.pagehead{margin:18px 0 24px}.pagehead h2{font-size:28px;margin:5px 0}.hubnote{padding:13px 15px;border:1px solid #28486e;background:#0c1b2e;border-radius:12px;color:#aac5e8;font-size:13px;line-height:1.6}footer{text-align:center;color:#66768d;font-size:11px;margin-top:32px}@media(max-width:700px){.head-actions{align-items:flex-end;flex-direction:column}.summary{grid-template-columns:1fr}.table{overflow:auto}table{min-width:760px}.hero h2,.pagehead h2{font-size:24px}.top{align-items:flex-start}.live{font-size:10px}}"

    nav = (
        "<nav class='nav'>"
        "<a href='index.html'>홈</a>"
        "<a href='briefing.html'>장전 브리핑</a>"
        "<a href='market.html'>시장지수</a>"
        "<a href='watchlist.html'>관심종목</a>"
        "<a href='stocks.html'>전체 종목</a>"
        "<a href='sectors.html'>업종 모멘텀</a>"
        "<a href='disclosures.html'>긍정공시</a>"
        "<a href='signals.html'>매매신호</a>"
        "</nav>"
    )
    header = (
        "<header class='top'><div class='brand'><div class='logo'>P</div><div><h1>Pulse Market</h1>"
        "<small class='muted'>AI STOCK ORCHESTRATOR</small></div></div><div class='head-actions'>"
        "<span class='live'>● 자동 분석 정상</span><button class='refresh' type='button' "
        "onclick=\"location.replace(location.pathname+'?refresh='+Date.now())\" aria-label='최신 데이터 새로고침'>↻ 새로고침</button>"
        "</div></header>"
    )

    def shell(title: str, body: str) -> str:
        return (
            f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>{html.escape(title)} · Pulse Market</title><style>{stylesheet}</style></head><body><main class='wrap'>"
            f"{header}{nav}{body}<footer>{now:%Y-%m-%d %H:%M} KST · 자동 계산 결과이며 투자 권유가 아닙니다.</footer>"
            "</main></body></html>"
        )

    quick_cards = (
        "<section class='grid'>"
        "<a class='card linkcard' href='briefing.html'><h3>🌅 장전 브리핑</h3><p>매일 아침 시장 방향, 업종 우선순위, 핵심 전략을 한 곳에서 확인.</p><span>오늘 장전판 →</span></a>"
        "<a class='card linkcard' href='market.html'><h3>🌐 시장지수</h3><p>KOSPI·KOSDAQ·NASDAQ 흐름만 빠르게 분리 확인.</p><span>지수 보기 →</span></a>"
        "<a class='card linkcard' href='watchlist.html'><h3>👀 관심종목</h3><p>추적 종목의 가격·1일·5일 수익률·거래량 신호.</p><span>관심종목 →</span></a>"
        "<a class='card linkcard' href='sectors.html'><h3>⚡ 업종 모멘텀</h3><p>반도체·방산·2차전지·로봇 등 업종 순위와 상위 3종목.</p><span>업종 보기 →</span></a>"
        "<a class='card linkcard' href='disclosures.html'><h3>📢 긍정공시</h3><p>수주·공급계약·승인·특허 등 신규 긍정 공시만 선별.</p><span>공시 보기 →</span></a>"
        "<a class='card linkcard' href='signals.html'><h3>🎯 매매신호</h3><p>눌림목과 거래량 증가 조건을 통과한 종목만 따로 확인.</p><span>신호 보기 →</span></a>"
        "</section>"
    )

    dashboard = (
        f"<section class='hero'><div class='eyebrow'>TODAY'S MARKET PULSE</div><h2>장전은 하나로, 장중 데이터는 세분화.</h2>"
        f"<p class='muted'>{now:%Y-%m-%d %H:%M} KST 기준 자동 갱신</p>"
        "<div class='hubnote'>장전 전략은 <b>장전 브리핑</b> 한 페이지에 모으고, 장중에는 시장·관심종목·업종·공시·신호를 각각 분리해서 봅니다.</div>"
        f"<div class='summary'>{''.join(market_cards)}</div></section>"
        "<div class='section-title'><h2>바로가기</h2></div>"
        f"{quick_cards}"
        "<div class='section-title'><h2>관심·보유 종목 요약</h2><a href='stocks.html'>전체 보기 →</a></div>"
        f"<div class='table'><table><thead><tr><th>구분</th><th>종목</th><th>현재가</th><th>1일</th><th>5일</th><th>거래량</th><th>신호</th></tr></thead><tbody>{''.join(html_rows)}</tbody></table></div>"
        "<div class='section-title'><h2>모멘텀 레이더</h2><a href='sectors.html'>전체 업종 →</a></div>"
        f"<section class='grid'>{''.join(sector_cards)}</section>"
        "<div class='section-title'><h2>약세 업종 3위</h2><a href='sectors.html'>약세 상세 →</a></div>"
        f"<section class='grid'>{''.join(weak_sector_cards)}</section>"
    )

    table_head = "<thead><tr><th>구분</th><th>종목</th><th>현재가</th><th>1일</th><th>5일</th><th>거래량/20일</th><th>판정</th></tr></thead>"
    stock_body = (
        "<div class='pagehead'><div class='eyebrow'>ALL TRACKED STOCKS</div><h2>전체 종목 분석</h2>"
        "<p class='muted'>보유·관심 종목을 한 화면에서 비교합니다.</p></div>"
        f"<div class='table'><table>{table_head}<tbody>{''.join(html_rows) or '<tr><td colspan=\"7\">등록된 종목이 없습니다.</td></tr>'}</tbody></table></div>"
    )
    watchlist_body = (
        "<div class='pagehead'><div class='eyebrow'>WATCHLIST</div><h2>관심종목</h2>"
        "<p class='muted'>관심종목만 분리해 가격·수익률·거래량·신호를 확인합니다.</p></div>"
        f"<div class='table'><table>{table_head}<tbody>{''.join(watch_rows) or '<tr><td colspan=\"7\">관심종목이 없습니다.</td></tr>'}</tbody></table></div>"
    )
    portfolio_body = (
        "<div class='pagehead'><div class='eyebrow'>PORTFOLIO</div><h2>보유종목</h2>"
        "<p class='muted'>config.json의 portfolio에 등록된 종목만 표시합니다.</p></div>"
        f"<div class='table'><table>{table_head}<tbody>{''.join(portfolio_rows) or '<tr><td colspan=\"7\">현재 portfolio에 등록된 종목이 없습니다.</td></tr>'}</tbody></table></div>"
    )
    market_body = (
        "<div class='pagehead'><div class='eyebrow'>MARKET INDEX</div><h2>시장지수</h2>"
        "<p class='muted'>국내외 핵심 지수의 1일·5일 흐름을 분리해서 봅니다.</p></div>"
        f"<div class='table'><table><thead><tr><th>지수</th><th>현재</th><th>1일</th><th>5일</th></tr></thead><tbody>{''.join(market_rows)}</tbody></table></div>"
    )

    def sector_panels(items, weak=False):
        cards = []
        for rank, (sector, score, ranked) in enumerate(items, 1):
            picks_data = sorted(ranked, key=lambda pair: pair[1]["ret5"])[:3] if weak else ranked[:3]
            picks = "".join(
                f"<li><b>{html.escape(names.get(t, t))}</b> · 5일 {pct(s['ret5'])} · 거래량 {s['vol_ratio']:.2f}배</li>"
                for t, s in picks_data
            )
            tone = "up" if score >= 0 else "down"
            cards.append(
                f"<article class='panel'><span class='muted'>RANK {rank}</span><h3>{html.escape(sector)} "
                f"<span class='{tone}'>{pct(score)}</span></h3><ol>{picks}</ol></article>"
            )
        return "".join(cards)

    all_sector_cards = []
    for rank, (sector, score, ranked) in enumerate(sectors, 1):
        top3 = ranked[:3]
        picks = "".join(
            f"<li><b>{html.escape(names.get(t, t))}</b> · 5일 {pct(s['ret5'])} · 거래량 {s['vol_ratio']:.2f}배</li>"
            for t, s in top3
        )
        tone = "up" if score >= 0 else "down"
        all_sector_cards.append(
            f"<article class='panel'><span class='muted'>RANK {rank}</span><h3>{html.escape(sector)} "
            f"<span class='{tone}'>{pct(score)}</span></h3><ol>{picks}</ol></article>"
        )
    sector_body = (
        "<div class='pagehead'><div class='eyebrow'>SECTOR MOMENTUM</div><h2>업종별 모멘텀 순위</h2>"
        "<p class='muted'>최근 5거래일 기준 강세 3개와 약세 3개를 먼저 분리해 봅니다.</p></div>"
        "<div class='section-title'><h2>강세 업종 TOP 3</h2></div>"
        f"{sector_panels(strong_sectors)}"
        "<div class='section-title'><h2>약세 업종 TOP 3</h2></div>"
        f"{sector_panels(weak_sectors, weak=True)}"
        "<div class='section-title'><h2>전체 업종 순위</h2></div>"
        f"{''.join(all_sector_cards)}"
    )
    disclosure_body = (
        "<div class='pagehead'><div class='eyebrow'>DART DISCLOSURES</div><h2>긍정 신규공시</h2>"
        "<p class='muted'>회사명 옆에서 공시 종류와 연결 모멘텀까지 함께 확인합니다.</p></div>"
        "<div class='table'><table><thead><tr><th>회사</th><th>종류</th><th>공시</th><th>모멘텀</th><th>날짜</th></tr></thead>"
        f"<tbody>{''.join(disclosure_html)}</tbody></table></div>"
    )
    signal_rows = [row for row, (_, _, stat) in zip(html_rows, rows) if stat["pullback"] or stat["volume_signal"]]
    signal_content = "".join(signal_rows) or "<tr><td colspan='7'>현재 조건에 맞는 신호가 없습니다.</td></tr>"
    signal_body = (
        "<div class='pagehead'><div class='eyebrow'>TRADING SIGNALS</div><h2>눌림목 · 거래량 신호</h2>"
        "<p class='muted'>정량 조건을 통과한 종목만 모아 봅니다.</p></div>"
        f"<div class='table'><table>{table_head}<tbody>{signal_content}</tbody></table></div>"
        "<section class='panel'><h3>판정 기준</h3><p>눌림목: 20일선 위 3.5% 이내이며 60일선 위</p>"
        "<p>거래량↑: 당일 거래량이 20일 평균의 1.5배 이상이며 당일 상승</p></section>"
    )

    pages = {
        "market.html": shell("시장지수", market_body),
        "portfolio.html": shell("보유종목", portfolio_body),
        "watchlist.html": shell("관심종목", watchlist_body),
        "stocks.html": shell("전체 종목", stock_body),
        "sectors.html": shell("업종 모멘텀", sector_body),
        "disclosures.html": shell("긍정공시", disclosure_body),
        "signals.html": shell("매매신호", signal_body),
    }
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

    # 하루에 보고서 파일 하나만 유지하고, 장중 실행은 같은 날짜 파일을 갱신한다.
    (reports / f"{now:%Y-%m-%d}.md").write_text(markdown, encoding="utf-8")
    (ROOT / "LATEST.md").write_text(markdown, encoding="utf-8")
    print(f"generated report with {len(prices)} price series")


if __name__ == "__main__":
    main()
