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


def make_report(config: dict, prices: dict[str, pd.DataFrame], now: datetime) -> tuple[str, str]:
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
        sector_cards.append(f"<article class='card'><h3>{html.escape(sector)}</h3><b>{pct(score)}</b><p>{html.escape(picks)}</p></article>")

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
    stylesheet = """body{font-family:system-ui,sans-serif;margin:0;background:#07111f;color:#e8eef7}.wrap{max-width:1050px;margin:auto;padding:24px}h1{margin-bottom:4px}.muted{color:#92a4bb}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.card,table,.panel{background:#101e31;border:1px solid #263a53;border-radius:14px}.card,.panel{padding:16px}.card b,.up{color:#ff647c}.down{color:#55a8ff}table{width:100%;border-collapse:collapse;overflow:hidden}th,td{padding:10px;text-align:right;border-bottom:1px solid #263a53}th:nth-child(-n+2),td:nth-child(-n+2),th:last-child,td:last-child{text-align:left}a{color:#75baff}@media(max-width:700px){.table{overflow:auto}table{min-width:720px}}"""
    page = f"""<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>AI 주식 모멘텀</title><style>{stylesheet}</style></head><body><main class='wrap'><h1>AI 주식 모멘텀 보고서</h1><p class='muted'>{now:%Y-%m-%d %H:%M} KST 자동 갱신 · 학습 및 관찰용</p><h2>보유·관심종목</h2><div class='table'><table><thead><tr><th>구분</th><th>종목</th><th>현재가</th><th>1일</th><th>5일</th><th>거래량</th><th>신호</th></tr></thead><tbody>{''.join(html_rows)}</tbody></table></div><h2>강세 모멘텀 업종</h2><section class='grid'>{''.join(sector_cards)}</section><h2>긍정 신규공시</h2><section class='panel'><ul>{''.join(disclosure_html)}</ul></section><section class='panel'><b>신호 기준</b><p>눌림목: 20일선 위 3.5% 이내 + 60일선 위 · 거래량↑: 20일 평균의 1.5배 이상 상승</p><p class='muted'>자동 계산 결과이며 투자 권유가 아닙니다.</p></section></main></body></html>"""
    return "\n".join(md), page


def main() -> None:
    config = load_config()
    tickers = [item["ticker"] for key in ("portfolio", "watchlist") for item in config[key]]
    tickers += [ticker for values in config["sectors"].values() for ticker in values]
    now = datetime.now(KST)
    prices = fetch_prices(tickers)
    markdown, page = make_report(config, prices, now)
    docs = ROOT / "docs"
    reports = ROOT / "reports"
    docs.mkdir(exist_ok=True)
    reports.mkdir(exist_ok=True)
    (docs / "index.html").write_text(page, encoding="utf-8")
    (reports / f"{now:%Y-%m-%d_%H%M}.md").write_text(markdown, encoding="utf-8")
    (ROOT / "LATEST.md").write_text(markdown, encoding="utf-8")
    print(f"generated report with {len(prices)} price series")


if __name__ == "__main__":
    main()

