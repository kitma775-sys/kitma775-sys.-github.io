# 同類 Bot 可行性研究

根據 [0xSurferX 2026-08-23 X Article](https://x.com/0xSurferX/status/2091564875294097907) 做嘅頂層對賬。頁面係靜態研究報告，**唔會落單**。

## 判決（一句）

軟件可以起；文中終身 **$66,670 PnL 屬實**；「每週 $12k」以 2026-08-24 官方數據計 **唔成立**（本週 $347）。熱路徑唔應該用 LLM。

完整論證、費用計算器、即時紙盤掃描：[index.html](index.html)

## 本地掃描

```bash
python3 research/scan_books.py --tag 15M --limit 12
```

只讀 Gamma + CLOB 公開 HTTP API。快照數字見 `research/findings.json`。

## 免責

唔係投資、法律或稅務建議。香港 IFEC 曾公開提醒預測市場可能涉及賭博條例。遵守你所在地法律同 Polymarket geoblock。
