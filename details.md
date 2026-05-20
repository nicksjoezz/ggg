# pump.fun Token Analysis — Filter Optimization Report

**Generated:** 2026-05-16 12:04:30  
**Run duration:** 30 minutes  
**Tokens seen:** 378  
**Tokens fully monitored (300s):** 263  
**Unmonitored (create-event data only):** 115  
**Winners (>=100% peak gain):** 68 (25.9% of monitored)  
**Non-performers:** 195  

---

## Filter Optimization Recommendations

### **[OK]** Market Cap Range (`min_market_cap_usd` / `max_market_cap_usd`)

Initial mcap is similar for winners (28.6 SOL) and non-performers (28.1 SOL if l_mcap else 'N/A'). Market cap filter is not a strong differentiator in this sample.

### **[OK]** min_description_length

Description length is similar across winners (0 chars) and non-performers (0 chars). Low predictive value in this dataset.

### **[OK]** max_dev_buy_pct

Dev buy % is similar for winners (3.34%) and non-performers (3.38%). Current setting is fine.

### **[RAISE]** min_buy_volume_sol_10s

Winners had **17.0820 SOL** avg first-60s buy volume vs 2.3538 SOL for non-performers (7.3x more). Raising `min_buy_volume_sol_10s` to **2.356** would filter low-interest tokens.

### **[RAISE]** min_unique_buyers_10s

Winners had more unique buyers in the first 60s (**29.7** vs 4.1). Organic multi-wallet buying is a strong signal. Try raising `min_unique_buyers_10s` to **18**.

### **[LOWER]** max_sell_buy_ratio_10s

Winners had a lower sell/buy ratio (**0.960**) vs non-performers (11.674). Buyers clearly dominated in winning tokens. Tighten `max_sell_buy_ratio_10s` to **0.7**.

### Suggested `filters` block for settings.json

```json
"filters": {
  "observation_window_seconds": 10,
  "min_buy_volume_sol_10s": 2.356,
  "min_unique_buyers_10s": 18,
  "max_sell_buy_ratio_10s": 0.8,
  "max_dev_buy_pct": 5.3,
  "min_description_length": 0
}
```

> Values derived from 263 monitored tokens over 30 minutes. Backtest before switching from paper to live trading.

---

## Key Metrics: Winners vs Non-Performers

| Metric | Winners (68) | Non-Performers (195) | Signal strength |
|--------|---------|-----------------|-----------------|
| Median initial mcap (SOL) | **28.58** | 28.15 | Weak |
| Avg dev initial buy % | **3.34%** | 3.38% | Weak |
| Avg description length (chars) | **0** | 0 | Weak |
| % with any social link | **0%** | 0% |  |
| % with Twitter | **0%** | 0% |  |
| % with Telegram | **0%** | 0% |  |
| Avg first-60s buy volume (SOL) | **17.0820** | 2.3538 | Strong |
| Avg first-60s unique buyers | **29.7** | 4.1 | Strong |
| Avg first-60s sell/buy ratio | **0.960** | 11.674 | Strong |
| Avg symbol length | **5.1** | 5.5 |  |
| Avg first-60s buy transactions | **46.9** | 7.1 |  |

---

## Peak Gain Distribution (263 monitored tokens)

  `Dead (0–1%)           `   91   34.6%  ████████████████████████
  `1–20%                 `   34   12.9%  █████████
  `20–50%                `   39   14.8%  ██████████
  `50–100%               `   31   11.8%  ████████
  `100–200%  (2x)        `   31   11.8%  ████████
  `200–500%  (5x)        `   23    8.7%  ██████
  `500–1000% (10x)       `    9    3.4%  ██
  `1000%+   (100x)       `    5    1.9%  █

---

## Winner Speed Statistics

- Winners observed hitting 100%+: **68** of 68
- Median time to 100%: **9s** (0.1 min)
- Fastest 100%: **1s**   Slowest: **283s**
- Median peak gain (winners): **225%**
- Max peak gain seen: **1235%**
- Median time to peak: **27s**

---

## Top Performing Tokens (this session)

| # | Symbol | Name | Peak | Time | Dev% | Desc | Socials | 60s Buys | 60s Buyers |
|---|--------|------|------|------|------|------|---------|----------|------------|
| 1 | `Tishka` | King Тишка       | **+1235%** | 267s | 5.0% | 0c | - | 241 | 208 |
| 2 | `HELPCJ` | HELPCJ           | **+1187%** | 313s | 1.4% | 0c | - | 71 | 57 |
| 3 | `DWP` | Devil Wears Pala | **+1172%** | 106s | 0.2% | 0c | - | 46 | 7 |
| 4 | `STEVE ` | STEVE THE MEME   | **+1157%** | 60s | 0.9% | 0c | - | 72 | 18 |
| 5 | `BEPE` | BEPE             | **+1043%** | 135s | 0.7% | 0c | - | 12 | 11 |
| 6 | `goldenh` | goldenhorse      | **+948%** | 149s | 1.4% | 0c | - | 10 | 3 |
| 7 | `Winnie` | Winnie (The Moo) | **+929%** | 378s | 6.6% | 0c | - | 2 | 2 |
| 8 | `Dust` | Never Sell Your  | **+741%** | 26s | 0.4% | 0c | - | 34 | 6 |
| 9 | `RISE` | RISE             | **+723%** | 305s | 9.8% | 0c | - | 79 | 57 |
| 10 | `OM` | Only Moon        | **+654%** | 111s | 0.5% | 0c | - | 26 | 4 |
| 11 | `JELLY` | Jelly fish       | **+622%** | 30s | 0.4% | 0c | - | 30 | 5 |
| 12 | `TRUST` | Mayhem Farmer    | **+586%** | 11s | 0.2% | 0c | - | 42 | 7 |
| 13 | `TRUST` | Mayhem Farmer    | **+514%** | 14s | 0.2% | 0c | - | 16 | 3 |
| 14 | `Rudi12` | ワンワン猫12          | **+514%** | 81s | 9.8% | 0c | - | 10 | 2 |
| 15 | `TREND` | Trending         | **+467%** | 131s | 0.2% | 0c | - | 36 | 5 |
| 16 | `HANTA` | Hanta-Kun        | **+437%** | 220s | 2.0% | 0c | - | 209 | 148 |
| 17 | `Tishka` | King Тишка       | **+368%** | 73s | 1.4% | 0c | - | 22 | 4 |
| 18 | `maxxing` | maxxing          | **+360%** | 265s | 1.3% | 0c | - | 67 | 64 |
| 19 | `ONESTAR` | ONE STAR         | **+351%** | 14s | 0.2% | 0c | - | 11 | 4 |
| 20 | `ONESTAR` | ONE STAR         | **+340%** | 20s | 0.2% | 0c | - | 29 | 4 |

---

## Common Keywords in Winner Names

Words that appear more frequently in winning token names vs non-performers.

| Word | In Winners | In Non-Performers | Verdict |
|------|-----------|------------------|---------|
| `mayhem` | 6 (9%) | 8 (4%) | similar |
| `king` | 4 (6%) | 3 (2%) | similar |
| `farmer` | 3 (4%) | 5 (3%) | similar |
| `x42` | 3 (4%) | 4 (2%) | similar |
| `make` | 3 (4%) | 4 (2%) | similar |
| `easy` | 3 (4%) | 4 (2%) | similar |
| `money` | 3 (4%) | 3 (2%) | similar |
| `sun` | 3 (4%) | 1 (1%) | similar |
| `devil` | 2 (3%) | 3 (2%) | similar |
| `wears` | 2 (3%) | 3 (2%) | similar |
| `palantir` | 2 (3%) | 3 (2%) | similar |
| `dust` | 2 (3%) | 2 (1%) | similar |
| `rise` | 2 (3%) | 2 (1%) | similar |
| `fish` | 2 (3%) | 0 (0%) | similar |
| `one` | 2 (3%) | 2 (1%) | similar |

---

## Aggregate Stats (all 378 tokens seen)

| Stat | Value |
|------|-------|
| Median initial mcap | 28.15 SOL |
| Avg dev initial buy | 3.26% |
| Avg description length | 0 chars |
| % with empty description | 100% |
| % with social links | 0% |
| % with Twitter | 0% |
| % with Telegram | 0% |
| Avg symbol length | 5.2 chars |
| % dev bought nothing | 19% |

---
*Generated by token_analyzer.py — PumpSniper filter optimizer*