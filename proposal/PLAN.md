# Senior Project Proposal — QuantLab

## Context

Chinmay needs a senior project proposal, matched to the Cal Poly template at
`~/Downloads/project_proposal (3).pdf`. The subject is the `~/quantlab` work
(a TradingView-driven strategy research engine) **plus a forward-looking scope**
that turns it into a product.

**The framing he chose:** what exists today is a *prototype* — he went from
scratch to live trading, but he already knew what he was doing. The senior
project makes that same workflow reusable by an **inexperienced retail
investor**: discover a strategy, validate it honestly, and go to paper/live
trading, with the statistical complexity hidden behind a plain-English verdict.

Decisions already settled with him:

| Question | Answer |
|---|---|
| Co-editing workflow | **Lavish HTML** live loop → LaTeX → PDF at the end |
| Compute layer | **Replace TradingView with own Python engine**; TV demoted to a cross-validation *oracle* |
| Product surface | **Web app** |
| Emphasis | Research-grounded systems project — *"I'm not a research student, the research is just for backing it up"* |
| Preliminary work | Include, but **briefly** |
| Timeline | **15 weeks** (14 build + 1 writeup) |
| Markets | **Both, crypto first** |
| Cover page | **Match the template exactly** — byline only, no advisor/course/quarter fields |

### The template's actual structure (extracted from the PDF)

Two pages, LaTeX-set, single column, Computer Modern. Section order:

1. Title — `Project Proposal: <Topic>`
2. Byline — `Firstname Lastname, Cal Poly Email` (**nothing else** — no advisor, course, or quarter)
3. **Problem Statement** — one dense paragraph: what's broken today, what artifacts it causes, then "To address this, the proposed project implements *<technique>*"
4. **Figure 1** with a descriptive caption
5. Background paragraph — how the technique works, why it helps, where it's used
6. **General Algorithm Overview** — numbered pipeline steps
7. Goals — imperative bullets (`Implement…`, `Integrate…`, `Optimize…`, `Demonstrate…`, `Optional: Extend with…`)
8. Prior work — 2–3 sentences naming key papers inline
9. **Inputs** / **Outputs** — one line each
10. Technical approach — 3 bullets
11. **User Interaction** — one line
12. Implementation targets — concrete, measurable (e.g. *"real-time frame rates (>30 fps)"*)
13. **References** — mixed: papers, a book, a tutorial site, course notes

We keep all of this and add two sections the template lacks but Chinmay asked
for: **Preliminary Work** (brief) and **Timeline**.

---

## Deliverable

`~/quantlab/proposal/` containing:

| File | Purpose |
|---|---|
| `proposal.html` | The working draft — the Lavish review surface, styled to look like the LaTeX output (Computer Modern via CDN, single column, numbered figure) |
| `proposal.tex` | Generated from the settled draft at the end |
| `proposal.pdf` | `latexmk -pdf` output — matches the template's typography |
| `figure1.svg` / `figure1.pdf` | The system-overview diagram |

Target length: **4–5 pages** (template is 2, but the timeline, preliminary
results and a real reference list justify the growth).

---

## Working loop

1. Write `proposal/proposal.html`.
2. `lavish-axi proposal/proposal.html` → opens in his browser.
3. He highlights sentences and comments; `lavish-axi poll` picks the feedback up.
4. Edit the HTML, he sees it live. Repeat until settled.
5. Only then: emit `proposal.tex`, run `latexmk -pdf`, hand him the PDF.

**Do not build the LaTeX until the content is agreed.** Maintaining two
representations through the iteration loop wastes effort and they drift.

Verified available: `lavish-axi` at `~/AppData/Roaming/npm/lavish-axi`;
`pdflatex`/`xelatex`/`latexmk` from TinyTeX (TeX Live 2025) at
`~/AppData/Roaming/TinyTeX/bin/windows`. **Not** available: pandoc,
`python-docx`. If he later wants `.docx`, that needs `pip install python-docx`
— ask first.

---

## Content plan, section by section

### Title & byline

> **Project Proposal: Guided Strategy Discovery and Validated Paper Trading for Retail Investors**
> Chinmay Karur, chinmaysk1@gmail.com

Byline only, per the template. Flag inline (HTML comment, stripped from the
PDF) that he may want his `@calpoly.edu` address instead.

### Problem Statement

Mirror the template's rhetorical shape. Retail investors have unprecedented
access to backtesting tools, and those tools are *systematically misleading*:
platforms default to zero commission and slippage; parameter sweeps are
presented as optimization rather than as a multiple-comparisons problem; a
single in-sample equity curve is the headline number. The result is that a
strategy which looks excellent on screen has no demonstrated edge. **To address
this, the proposed project implements a guided strategy-discovery and
validation pipeline** that runs the correct statistical protocol automatically
and reports a single plain-English verdict.

Anchor with two measured numbers from the existing repo (see Preliminary Work).

### Figure 1

Inline SVG, six stages left-to-right, styled like a textbook diagram:

```
Market data → Strategy library → Walk-forward sweep → Robustness verdict → Paper trading → Live (gated)
     ↑                                   ↓
  CCXT / equities API          TradingView cross-check (oracle)
```

Caption: *"Pipeline overview: raw market data is turned into a ranked,
out-of-sample-validated strategy recommendation, which the user can arm for
paper trading through a gated promotion path."*

### Background

How honest validation works and why each piece matters — walk-forward splitting,
regime segmentation, explicit cost modelling, and correcting for the fact that
searching a grid of *N* configurations inflates the best result. Cite
Bailey et al. and White here, in the template's inline style.

### General Pipeline Overview (numbered)

1. Ingest and cache calibrated OHLCV history for the selected market.
2. Instantiate a strategy family with a coarse, broad parameter grid.
3. Split history into walk-forward train/test windows sized to available bars.
4. Evaluate every configuration under explicit commission and slippage.
5. Rank on **out-of-sample** performance only; discard in-sample rank order.
6. Correct the winner's statistics for the number of configurations searched.
7. Emit a ROBUST / WEAK / OVERFIT verdict with a plain-English explanation.
8. Promote the accepted strategy to paper trading behind staged safety gates.

### Goals

Imperative bullets, matching the template's voice:

- **Implement** a vectorized backtest engine over locally cached OHLCV data, with explicit costs, stops, and position sizing.
- **Validate** the engine by cross-checking it against TradingView's Strategy Tester on identical strategies and windows, to a stated tolerance.
- **Integrate** a robustness layer — walk-forward, regime segmentation, probability of backtest overfitting, and a deflated performance statistic.
- **Translate** those statistics into a single verdict an investor with no statistics background can act on.
- **Build** a web application that guides a user from market selection to an armed paper-trading strategy without exposing a parameter grid.
- **Demonstrate** the full loop end to end on crypto, then show it generalizes to US equities and ETFs.
- **Optional:** extend with a market-regime classifier that filters which strategy families are recommended in the current environment.

The optional bullet is where the ML component lives — scoped as a stretch, so
missing it costs nothing. This mirrors the template's own
*"Optional: Extend with environment lighting or reflectance."*

### Prior Work

Two to three sentences, papers named inline: Brock, Lakonishok & LeBaron on
technical trading rules; Sullivan, Timmermann & White on data-snooping in
exactly this setting; Bailey, Borwein, López de Prado & Zhu on backtest
overfitting; Harvey & Liu on the multiple-testing haircut; Arnott, Harvey &
Markowitz's backtesting protocol as the practitioner-facing checklist the
product automates.

### Inputs / Outputs

- **Inputs:** OHLCV history for a chosen market and timeframe, a strategy family with a parameter grid, and a cost model.
- **Outputs:** a ranked, out-of-sample-validated strategy configuration with a robustness verdict, and a paper-trading deployment of it.

### Technical Approach

- Compute indicator series and entry/exit signals vectorized over cached bars, with Wilder-smoothed variants matched to platform semantics.
- Determine window boundaries from available history rather than requested dates, and score every configuration over an identical fixed window.
- Apply commission, slippage, and stop logic inside the fill model, not as a post-hoc adjustment.

### User Interaction

One line, template-style: the user picks a market, answers a short set of
plain-English questions about horizon and risk tolerance, reviews a ranked
shortlist with verdicts, and arms a strategy for paper trading — never seeing a
parameter grid.

### Implementation targets

Concrete and measurable, as the template does with `>30 fps`:

- Full sweep of a strategy family over one market/timeframe in **under 5 minutes** on a laptop.
- Local engine agrees with TradingView's Strategy Tester to within a stated tolerance on ≥95% of cross-checked configurations.
- Paper-trading cycle completes and records state in **under 10 seconds** per bar close.

### Preliminary Work (brief — one paragraph + a small table)

Keep it tight, per his answer. Facts, all verified from the repo:

| | |
|---|---|
| Recorded backtest runs | 9,070 |
| Strategies / symbols / timeframes | 30 / 19 / 7 |
| Correctness suite | 6 checks, all passing |
| Live execution gates passed | 2 of 4 |

Plus the two findings that motivate the whole project, stated in one sentence
each:

- The same parameter grid returned **52.01%** with zero costs and **5.04%** with 0.5% commission.
- On one strategy/symbol, the best-in-training parameters (+81% train) returned **+32%** out of sample while the *worst*-in-training parameters (−18% train) returned **+21%** — in-sample rank order did not survive.

> **Honesty note to carry into the draft:** quantlab is currently *untracked* —
> it sits inside the home-directory git repo of an unrelated class project, so
> there is no commit history for it. Don't claim a development timeline the
> repo can't back. Worth `git init`-ing it separately, but that's outside this
> proposal task.

### Timeline (15 weeks)

| Weeks | Focus | Deliverable |
|---|---|---|
| 1–2 | Data layer — CCXT for crypto, equities API for stocks; local cache; timezone and corporate-action handling | Reproducible datasets for BTC/ETH and SPY/QQQ |
| 3–5 | Local backtest engine, promoted from the existing prototype; costs, stops, sizing | Engine + cross-engine validation report vs. TradingView |
| 6–8 | Robustness layer — walk-forward, regime segmentation, PBO, deflated statistic, multiple-testing correction; verdict logic | Verdict engine, calibrated against the 9,070 existing runs |
| 9–11 | Web application — guided flow, plain-English results, no parameter grid exposed | End-to-end demo on crypto |
| 12–13 | Paper/live integration — reuse existing broker, guard, state and alert modules; staged promotion gates; deployment | Paper trading running unattended on a VM |
| 14 | Equities generalization, security review of key handling, usability pass with non-expert testers | Same pipeline demonstrated on SPY/QQQ |
| 15 | Final report, demo video, submission | Final deliverables |

Each row also gets a one-line **risk note** in the document (e.g. Weeks 3–5:
*"if cross-engine agreement is poor, the discrepancy analysis becomes the
result rather than a gate"*).

### References

**Scientific papers** — required by his instructions. Verify every citation
(authors, year, venue, volume, pages) with WebSearch before the final PDF; do
not ship an unverified citation.

1. Brock, Lakonishok & LeBaron (1992), *Simple Technical Trading Rules and the Stochastic Properties of Stock Returns*, Journal of Finance.
2. Sullivan, Timmermann & White (1999), *Data-Snooping, Technical Trading Rule Performance, and the Bootstrap*, Journal of Finance.
3. White (2000), *A Reality Check for Data Snooping*, Econometrica.
4. Bailey, Borwein, López de Prado & Zhu (2014), *Pseudo-Mathematics and Financial Charlatanism: The Effects of Backtest Overfitting on Out-of-Sample Performance*, Notices of the AMS.
5. Bailey & López de Prado (2014), *The Deflated Sharpe Ratio*, Journal of Portfolio Management.
6. Bailey, Borwein, López de Prado & Zhu (2016), *The Probability of Backtest Overfitting*, Journal of Computational Finance.
7. Harvey & Liu (2015), *Backtesting*, Journal of Portfolio Management.
8. Harvey, Liu & Zhu (2016), *…and the Cross-Section of Expected Returns*, Review of Financial Studies.
9. Arnott, Harvey & Markowitz (2019), *A Backtesting Protocol in the Era of Machine Learning*, Journal of Financial Data Science.
10. Bergmeir & Benítez (2012), *On the Use of Cross-Validation for Time Series Predictor Evaluation*, Information Sciences.
11. Lo (2004), *The Adaptive Markets Hypothesis*, Journal of Portfolio Management.
12. Almgren & Chriss (2001), *Optimal Execution of Portfolio Transactions*, Journal of Risk.

**Books, documentation, and APIs** — the template mixes these in freely:

- López de Prado (2018), *Advances in Financial Machine Learning* (combinatorial purged cross-validation).
- Pardo (2008), *The Evaluation and Optimization of Trading Strategies* (walk-forward analysis).
- CCXT unified exchange API documentation.
- Alpaca paper-trading API documentation.
- TradingView Pine Script v6 language reference.
- Chrome DevTools Protocol specification.

---

## Critical files

**Read before drafting** (source of every factual claim in the proposal):

- `~/quantlab/README.md` — measured throughput and the 7 numbered correctness findings.
- `~/quantlab/CLAUDE.md` — the 6 non-negotiable invariants and the Pine contract; these become the proposal's "what honest validation requires".
- `~/quantlab/docs/PHASE2_HANDOFF.md` — the surviving strategy spec, the live-trading gates, and the documented TradingView trade-count correction.
- `~/quantlab/runner/verify.py` — the 6 correctness checks, cited as preliminary work.
- `~/quantlab/runner/local_bt.py` — the prototype that Weeks 3–5 promote into the real engine.
- `~/quantlab/live/` — the modules Weeks 12–13 reuse rather than rewrite.
- `~/quantlab/results/runs.db` — read-only, for any number quoted in the proposal.

**To create:** `~/quantlab/proposal/proposal.html`, then `proposal.tex`,
`figure1.svg`, `proposal.pdf`.

---

## Verification

1. **Content fidelity** — every number in the proposal traceable to the repo or a cited paper; re-query `results/runs.db` read-only for anything quoted.
2. **Citations** — WebSearch each of the 12 papers; confirm authors, year, venue. Drop or replace any that doesn't check out.
3. **Template match** — put the extracted template text side by side with the draft's section list and confirm the order and voice line up.
4. **Live review** — `lavish-axi proposal/proposal.html`, then `lavish-axi poll` after he comments; iterate to sign-off.
5. **Build** — `latexmk -pdf proposal.tex` in `proposal/`; confirm zero errors, correct page count, figure placed, references rendered.
6. **Read the PDF back** before handing it over.
