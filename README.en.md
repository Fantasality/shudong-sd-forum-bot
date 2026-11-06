# Shudong SD Forum Bot · 鼠洞SD论坛机器人

> **For study and personal research only.**
> This project is a technical demo of LLM-driven content generation, retrieval-augmented
> writing and quality gating on a campus forum. **Do not use it for舆论攻击 (coordinated
> opinion manipulation), harassment, disinformation, or any form of information warfare.**
> You are solely responsible for how you run it and for complying with the target
> community's rules and local law. The authors do not endorse or support any such use.

An automation stack that runs on your own machine: it registers accounts, writes posts,
comments and replies on a campus anonymous forum, with an LLM doing the writing and a
local corpus + retrieval layer supplying the facts. Everything passes a quality gate
before it can reach the forum.

A companion Android app (`app/dist/shudong-xray-1.3.0.apk`) lets you browse, post, and
pull the whole account roster onto your phone with a single QR scan.

---

## What makes it different

### 1. It tries hard to read like a person

Not "stuff a prompt and hope". Every piece of content goes through five stages:

- **Form sampling** — length and shape are drawn from the real distribution of the
  corpus (fragment / one-liner / two-liner / question / list / full paragraph).
  Short posts carry no trailing period; long ones must be punctuated and paragraphed.
- **Persona** — 32 identities spread across four campuses, different majors, different
  schedules and personalities (gentle, sardonic, aloof, socially anxious, foodie,
  night owl, engineering nerd, lovesick...). The same topic comes out differently.
- **De-duplication** — one post may only be commented by one account within six hours
  (atomic claim in SQLite); a given piece of core advice may only appear once per
  thread; template sentence patterns, opening words and filler words all have quotas.
- **Exit checks** — AI-flavoured vocabulary, self-repetition, cross-campus leakage,
  and weather consistency (no "sitting in the sun" on a rainy day).
- **No invented facts** — uncertain details get looked up online; if not found, it says
  so instead of making up a specific name.

### 2. Emotional posts get real advice, not sympathy

For relationship / loneliness / grievance posts, it does **not** use the
"me too + here is my own little story" pattern — that is where AI writing smells worst.
Instead:

> judgement first → name what the person is actually stuck on → one concrete action
> (ideally a sentence they can say out loud) → close with a plain truth.

This structure was derived from tens of thousands of real highly-upvoted comments:
the ones that get upvoted give a judgement, explain the reason, and hand over a method,
with a clear stance. Long comments come in three tiers (100–200 / 150–260 / 120–220
characters) chosen by how intense the thread is.

### 3. Multi-dimensional emotional modelling

Before writing, each item gets a **stance**, not a generic template:

- **8 moods** — daily / rant / grievance / romance / loneliness / help / complaint / sharing
- **Gender perspective** — male / female / unknown, changing how the writer refers to
  themselves and what they notice
- **Long-post detection** — if the OP wrote 300 characters, a five-character reply is
  an insult; it switches to a long tier automatically
- **Stance** — complain together with a complaint, give a concrete method to a request
  for help, name the blocker for loneliness. No cross-contamination.

### 4. Data foundation — nearly 100k real community posts, our biggest differentiator

This is not "tune a few prompts and hope the model sounds human". We first brought the
entire community's own words back onto local disk, then made every generated item find
its reference in real corpus data. It works out of the box — no crawling needed.

| Data | Measured size | Used for |
|---|---|---|
| `kb/raw.db` | **78,511 real posts + 18,276 real comments** (33.5 MB) | Distribution stats, topic quotas, short-reply pool, and the single source of truth for test sets |
| `kb/rag.db` | **415.8 MB** retrieval index | Dense vectors + sparse inverted index, two-way recall fused with RRF, then reranked |
| `kb/dense.db` | 9.6 MB | `bge-m3` dense vector index |
| `kb/short_replies.json` | 5,660 real human lines of ≤10 characters | Short replies are sampled from real corpus, not from a hand-written word list |
| `kb/samples.json` / `stats.json` / `knowledge.json` / `campus_map.json` | ≈180 KB | few-shot style anchors, topic distribution, campus fact list |

Real board distribution: Help & Questions (求助提问箱) 29,749 / Casual Posts (抛抛帖子)
26,550 / Second-hand Market (二手交易) 12,850 / Freshman Column (新生专栏) 2,256 /
Campus Oddities (苏大迷惑大赏) 2,099 …

Three things this corpus buys that you cannot copy just by tuning prompts:

- **The distribution is inferred from corpus data, not guessed**: length percentiles,
  emoji rate, period rate, topic share, and the 24-hour activity curve all take real
  human text as ground truth; evaluation thresholds are calibrated on the p90 window of
  a real-human control group.
- **Short replies are real human lines**: hand-written filler like "蹲 / dd / 同问"
  clusters together; after switching to the real corpus pool, the measured
  opening-duplication rate is 0.006.
- **Facts do not come from the model's memory**: canteens, landmarks, faculty names,
  weather and the academic calendar are looked up in the local KB; when a lookup is
  uncertain it writes no specific name, avoiding fabrication.

> The public repository does not ship the raw corpus (large, and it contains real UGC;
> excluded via `.gitignore`). You can rebuild the same foundation from your own corpus
> following the `tools/eval_corpus.py` flow.

### 5. Quality gate

Six scored dimensions before anything goes out: punctuation 20 / length 20 / opening 15 /
structure 15 / detail 15 / facts 15. A hard failure on punctuation or facts blocks
publication outright; a score below the line triggers one rewrite, and if it still fails
the item lands in a **review queue** where you decide by hand.

---

## Install

### Windows

1. Install **Python 3.10+** from <https://www.python.org/downloads/> — tick
   `Add Python to PATH` on the first screen.
2. Unzip this package anywhere (avoid paths with spaces or non-ASCII characters).
3. Double-click **`启动.bat`**.
4. Your browser opens <http://127.0.0.1:8765> automatically.

The first run installs `numpy`; give it a few seconds.

### macOS / Linux

```bash
bash start.sh
```

Requires `python3` on PATH.

---

## First run: two API keys

The console jumps to the **Tasks & Prompts** page on first load. Two cards at the top:

| Card | What to fill | What it does |
|---|---|---|
| **LLM config** | DeepSeek key (`https://api.deepseek.com/v1`) | writes posts, comments, and judges mood |
| **Vectors & rerank** | SiliconFlow key (`https://api.siliconflow.cn/v1`) | embeds posts and memories, reranks candidates |

The **Generate** button on the Accounts page stays disabled until both are set.

> DeepSeek alone also works, but retrieval degrades to keyword matching and comments
> get noticeably dumber. Configure both.

---

## Generating bot accounts

Left sidebar → **Accounts** → the **Generate bot accounts** card.

1. Pick a count: **1 / 2 / 4 / 8 / 16 / 32**
2. Hit **Generate**
3. Progress streams below: each successful registration prints
   `persona · campus → @username`

What it actually does:

- Registers **brand-new accounts on the server** itself (username + random password,
  no WeChat involved). It never touches any account you already have.
- Assigns a persona right after: it prefers campuses / majors / personalities that are
  **not used yet**, so the first 32 accounts are all distinct; beyond that it cycles
  and appends a suffix to the key.
- Writes `tokens_*.json` (credentials), `account_*.json` (username/password) and updates
  `config.json`.
- Throttles registration (2.5–4.5 s between accounts) to avoid rate limits.

---

## What `dry_run` means

Top-right toggle: `DRY` / `LIVE`.

| State | Behaviour |
|---|---|
| **DRY** (default) | Every action is logged only, nothing reaches the forum. Posts go to the review queue; comments show up under "My content" |
| **LIVE** | Actually published |

**Run DRY for a couple of days first** and read what it writes. Switching to LIVE asks
for confirmation.

The review queue lives under **Review**: each item carries its six scores and the reasons
for deductions. You can edit, approve, ask the model to rewrite, or reject.

---

## Starting / stopping

- Daemon status and start/stop live in the top bar and on the Overview page.
- The daemon runs one thread per enabled account, following each account's own schedule.
- Closing the console window does not stop the service; use the stop button in the UI.

---

## Sending the roster to your phone

### Option A — QR scan (recommended)

1. Desktop → **Phone sync** → **Regenerate** to produce a QR code
2. Phone app → **Me** → Roster → **Scan to import** → photograph the code (or pick from album)
3. The whole roster (including usernames and passwords) lands on the phone
4. Tap any entry in the roster list → **Log in**

> The QR code contains **plaintext passwords**. Do not screenshot and share it.
> A 32-account code is about 1.7 KB.

### Option B — file

**Download roster.json**, transfer it to the phone, then app → Me → Roster →
**Import from file**.

### Option C — paste

**Copy JSON** on the desktop, paste it into the app's **Paste import** dialog.

---

## The Android app

`app/dist/shudong-xray-1.3.0.apk`, Android 6.0+.

- Feed, boards, search, post detail and nested comments
- Post, comment, reply, like, bookmark
- Multi-account pool: tap a roster entry to log in; switch between logged-in accounts
- Roster management: QR / file / paste import, export, manual add
- Shows base36 post codes (`#XP76`) matching the desktop console

You will need to allow "install from unknown sources".

---

## Layout

```
启动.bat            one-click launcher (Windows)
start.sh            launcher (macOS / Linux)
bot/
  run.py            entry point (web / daemon / one-off commands)
  server.py         local console
  manage.py         CLI batch management
  szlike/           core package
    spawn.py        account generation
    antirepeat.py   de-duplication (actions / sentence skeletons / phrases / openings)
    persona.py      32 personas
    voices.py       persona corpus
    human.py        form sampling, punctuation, AI-flavour detection
    longform.py     long-comment / long-post tiers
    mood.py         mood / gender / stance
    quality.py      six-dimension scoring
    rag.py          hybrid retrieval
    dense.py        vector search
    kb.py           knowledge-base injection
    brain.py        model calls
    client.py       forum API client
    ops.py          endpoint wrappers
  kb/               knowledge base + corpus + vector index
  web/              console front-end
  config.example.json
  accounts_roster.example.json
app/
  dist/             prebuilt APK
  assets/           WebView front-end
  java/             native shell
  build.sh          rebuild
```

---

## FAQ

**`启动.bat` flashes and disappears**
Python is missing, or was installed without `Add Python to PATH`. Reinstall with the box
ticked.

**The page will not open**
Check the console window for errors. If port 8765 is taken, change `web.port` in
`bot/config.json`, or start with `python bot\run.py web --port 8766`.

**Account generation keeps failing**
Almost certainly rate limiting. Wait ten minutes, or generate four at a time.

**Output is English / garbled**
`llm.base_url` and `model` do not match. For DeepSeek use `deepseek-chat`.

**It still reads like AI**
1. Make sure both keys are set — without vectors, retrieval degrades to keywords and
   the reference samples get bad.
2. Keep `dry_run` on for a day and read the review queue's deduction reasons.
3. Every account's system prompt is editable under **Tasks & Prompts**; changes apply
   immediately.

**Can I change posting times?**
Yes — on the account card, or `python bot\manage.py times`.

**Does it upload my data?**
No. Everything stays in `bot/state.db` and `bot/logs/` on your machine. Only the text
being generated is sent to the model API (DeepSeek / SiliconFlow).

---

## License & disclaimer

No license is granted for use in information operations, coordinated inauthentic
behaviour, harassment campaigns, or any activity that violates the target platform's
terms of service or applicable law.

Provided as-is, without warranty of any kind. See [DISCLAIMER.md](DISCLAIMER.md).

---

[中文说明 / Chinese version](README.md)

---

## Evaluation & test results (measured)

Every anthropomorphism change is validated by a local simulated exam with a fixed
seed (`EVAL_SEED=20261105`). All numbers are generated from result JSON by
`tools/make_eval_tables.py` — no hand-written conclusions.

Scoring: **Turing 40** (5-way-1 LLM judge) + **distribution/topic 25** +
**homogeneity/form 20** + **AI-flavour 15**. The random baseline of a 5-way-1
test is **20%** (human-vs-human control: 18.1%), so the Turing term caps at
~32/40; pushing detection below 20% is an overfitting artefact, not a goal.
Beyond L1 there are L2 account-timeline / L3 coordinated-graph evaluations,
plus dedicated suites for AI-accusation replies and full thread context.

| Exam round | comment | post | reply |
| --- | --- | --- | --- |
| Fixed-seed baseline (1 candidate) | 83.6 | 80.0 | 79.3 |
| Best-of-3 (legacy postprocess) | 80.3 | **88.4** | 86.2 |
| Round 1 (c3) | 81.9 | 82.3 | 82.8 |
| Round 2 (c3, legacy postprocess) | 81.2 | 78.9 | **86.3** |
| Round 3 (all changes merged) | 78.6 | 86.7 | 82.5 |

| Layer | Check | Result |
| --- | --- | --- |
| L2/L3 structure + human-calibrated thresholds | 10 hard checks | **10/10 PASS** |
| AI-accusation recognition / denial | 4 groups | **4/4 PASS** |
| Thread context (post + comment + prior replies) | 9 assertions | **9/9 PASS** |
| Online release gate | 2 manual blockers | **NOT CLEARED** — do not start the daemon |

The full auto-generated tables (L1 breakdown, five-way Turing comparison, human-baseline calibration, scenario suites, corpus inventory) are in [the Chinese README](README.md), section 十一.
