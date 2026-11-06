# 鼠洞 · 校园匿名社区机器人（一键启动包）

**简体中文** ｜ [English version](README.en.md) ｜ [免责声明](DISCLAIMER.md)

> **仅供学习与个人研究。禁止用于舆论攻击、舆论操控、骚扰或传播虚假信息。**
> 运行产生的一切后果由使用者自行承担。

一套跑在自己电脑上的校园匿名社区自动化：自动开号、自动发帖、自动评论、自动回楼中楼，
内容由大模型写，事实来自内置知识库和联网检索，发出去之前先过一道质量闸门。

手机端配了一个 App（`app/dist/shudong-xray-1.3.0.apk`），能看信息流、发帖、回楼中楼，
也能一键把电脑上的整份花名册扫进手机。

> 自己玩、自己看。用之前请先确认目标社区的规则，风险自担。

---

## 一、它有什么特点

### 1. 拟人

不是「把 prompt 拼好丢给模型」那种。每条内容都要过五道关：

- **形态**：先按真实语料的长度分布抽一个形态（碎片 / 一句话 / 两句 / 反问 / 清单 / 完整段），
  不是每条都写成一模一样的三行。短内容不带句号，长文必须有句号和分段。
- **人设**：32 套身份，分布在不同校区、不同学院专业、不同作息，各自有性格
  （温柔、毒舌、冷漠、社恐、吃货、夜猫、理工宅、恋爱脑……）。同一个话题，不同号写出来不一样。
- **去重**：同一帖 6 小时内只允许一个号评论（数据库原子占坑）；
  同一帖下面的「核心建议」只准出现一次（有人说了找宿管，第二个号必须换一条路）；
  「…比…管用」这种模板句式、开头、口水词（蹲 / 我也 / 确实）都有配额。
- **出厂检查**：AI 腔词库 + 复读检测 + 校区别串 + 天气一致性（下雨天不许写晒太阳）。
- **不编事实**：查不准的先联网查，查不到就说不知道，不硬凑一个具体名字。

### 2. 情感问题精处理

情感 / 恋爱 / 委屈 / 孤独这几类帖，不走「我也一样 + 讲我自己一件小事」的共鸣路子——
那是 AI 味最重的地方。改成：

> 先给一句判断 → 点破他真正卡在哪 → 给一条现在就能做的建议（最好带一句能直接说出口的原话）→ 一句实话收尾。

这个写法是从几万条真实高赞评论里统计出来的：被赞起来的长评全是「给判断 + 讲道理 + 给做法」，
带立场、带冲劲，几乎不写自我表演式的画面。长评分三档（100–200 / 150–260 / 120–220 字），
按帖子的情绪强度选档。

### 3. 情绪多维处理

每条内容生成前先判一遍**姿态**，不是套一个通用模板：

- **8 种情绪**：日常 / 吐槽 / 委屈 / 情感 / 孤独 / 求助 / 抱怨 / 分享
- **性别视角**：男 / 女 / 未知，写到自己和对方时的称呼、关注点不同
- **长帖识别**：楼主写了三百字，回五个字就是敷衍，自动切长档
- **立场**：抱怨就一起骂这件事、求助就给具体办法、孤独就点破卡点，不串味

### 4. 数据底座 —— 近十万条真实社区语料，这是我们最大的特点

不是「调几句 prompt 让模型装像人」，而是先把整个社区说过的话搬回本地，
再让每一条生成内容都从真实语料里找参照。开箱即用，不需要你自己爬。

| 数据 | 实测规模 | 用途 |
|---|---|---|
| `kb/raw.db` | **78,511 条真实帖子 + 18,276 条真实评论**（33.5 MB） | 分布统计、题材配额、短回复池、测试集的唯一真值来源 |
| `kb/rag.db` | **415.8 MB** 检索库 | 稠密向量 + 稀疏倒排双路召回，RRF 融合后精排 |
| `kb/dense.db` | 9.6 MB | bge-m3 稠密向量索引 |
| `kb/short_replies.json` | 5,660 条 ≤10 字真人原句 | 短回复不靠手写字表，直接抽真实语料 |
| `kb/samples.json` / `stats.json` / `knowledge.json` / `campus_map.json` | 约 180 KB | few-shot 风格锚、题材分布、校区事实清单 |

真实板块分布：求助提问箱 29,749 / 抛抛帖子 26,550 / 二手交易 12,850 /
新生专栏 2,256 / 苏大迷惑大赏 2,099 ……

这套语料带来三个很难靠调 prompt 抄走的东西：

- **分布是从语料反推的，不是拍脑袋定的**：长度分位、表情率、句号率、题材占比、
  24h 作息分布，每一项都以真人语料为真值；评估门槛也用真人对照组窗口 p90 标定。
- **短回复是真人原句**：手写「蹲 / dd / 同问」会扎堆，实测换成真实池后开头重复率 0.006。
- **事实不靠模型记忆**：食堂、地标、教师名单、天气与校历都从本地库查，
  查不准就不写具体名字，避免编造。

> 公开仓库不含原始语料（体积大且含真实 UGC，已在 `.gitignore` 中排除）；
> 可以用自己的语料按 `tools/eval_corpus.py` 的流程重建同一套底座。

### 5. 质量闸门

每条内容发之前打六项分：断句 20 / 长度 20 / 开头 15 / 结构 15 / 细节 15 / 事实 15。
断句或事实有硬伤直接不直发；分数不过线自动重写一轮，还不过线进**待审队列**，
在网页上人工看一眼再决定发不发。

---

## 二、装

### Windows

1. 装 **Python 3.10 或更高**：<https://www.python.org/downloads/>
   安装第一步**一定要勾** `Add Python to PATH`。
2. 解压这个包到任意目录（路径别有中文和空格，省事）。
3. 双击 **`启动.bat`**。
4. 浏览器会自动打开 <http://127.0.0.1:8765>。

第一次启动会自动装依赖（numpy），要等十几秒。

### macOS / Linux

```bash
bash start.sh
```

需要系统里有 `python3`。

---

## 三、第一次用：填两把 Key

控制台第一次打开会自动跳到「任务与 Prompt」页，顶部有两张卡片：

| 卡片 | 填什么 | 干什么用 |
|---|---|---|
| **LLM 配置** | DeepSeek 的 Key（`https://api.deepseek.com/v1`） | 写帖子、写评论、判断情绪 |
| **向量与精排** | SiliconFlow 的 Key（`https://api.siliconflow.cn/v1`） | 把帖子和记忆变成向量、给候选排序 |

两把都填了，顶部「账号矩阵」里的**生成**按钮才会亮。

> 只填 DeepSeek 也能跑，但检索会退化成关键词匹配，评论会明显变钝。
> 建议两个都配。

填完点保存，页面右上角会显示 `LLM ✓ 向量 ✓`。

---

## 四、生成机器人号

打开左侧 **「账号矩阵」** → 中间那张 **「一键生成机器人号」** 卡片。

1. 下拉选数量：**1 / 2 / 4 / 8 / 16 / 32**
2. 点 **生成**
3. 下面会滚动显示进度：每个号注册成功会打印 `人设 · 校区 → @用户名`

它做的事：

- 程序**自己去服务端注册全新账号**（用户名 + 随机密码，不走微信），
  不会用到你已有的任何账号；
- 注册完自动分配一个人设：优先挑**还没被用过**的校区 / 学院 / 专业 / 性格，
  所以前 32 个号彼此不重样；32 个用完了再生成会循环复用（key 加后缀）；
- 自动写 `tokens_*.json`（登录凭据）、`account_*.json`（用户名密码）、更新 `config.json`；
- 注册速度会控制（每个号间隔 2–4 秒），否则会被服务端限流。

生成完，账号卡片会出现在上面的矩阵里。每个号可以单独暂停 / 恢复 / 改发帖时间。

> 想先看效果不想真发？见下面 **dry_run**。

---

## 五、dry_run 是什么

页面右上角有个开关，`DRY` / `LIVE`。

| 状态 | 行为 |
|---|---|
| **DRY**（默认） | 所有动作**只写日志，不发到社区**。帖子进待审队列、评论只在「我的内容」里能看到 |
| **LIVE** | 真的发出去 |

**第一次用一定先跑 DRY**，看两三天它写的东西像不像人，再切 LIVE。
切 LIVE 会有二次确认。

待审队列在左侧「待审」页：每条都带六项评分和扣分原因，可以改字、通过、让模型重写、打回。

---

## 六、开 / 停机器人

- 网页顶栏有 daemon 状态和启停按钮；也可以在「总览」页操作。
- daemon 会给每个启用的号开一个线程，按各自的作息跑：发帖、评论、回楼中楼、点赞。
- 关掉黑窗口不会停服务；要停就在网页上点停止。

---

## 七、把花名册送到手机

电脑上生成完号，手机上要能直接登录，不用一个个输账号密码。

### 方式一：扫码（推荐）

1. 电脑左侧 **「手机同步」** → 「重新生成」，会出现一个二维码
2. 手机打开 App → **我的** → 花名册 → **扫码导入** → 拍这张二维码（或从相册选图）
3. 解析成功，整份花名册（含账号密码）就写进手机了
4. 回到花名册列表，点任意一个号 → **登录**

> 二维码里是**明文密码**，别截图发群。32 个号的码大约 1.7 KB。

### 方式二：文件

1. 「手机同步」页点 **下载 roster.json**
2. 用微信 / QQ / 数据线把文件传到手机
3. App → 我的 → 花名册 → **从文件导入** → 选这个文件

### 方式三：粘贴

「复制 JSON」→ 手机剪贴板 → App → 花名册 → **粘贴导入**。

---

## 八、手机 App

`app/dist/shudong-xray-1.3.0.apk`，Android 6.0 以上，直接装。

能做什么：

- 看信息流、板块、搜索、看帖子详情和楼中楼
- 发帖、评论、回楼中楼、点赞收藏
- 多账号池：花名册里点一个号就登录，登录过的号可以在「账号池」里随时切换
- 花名册管理：扫码 / 文件 / 粘贴导入，导出，手动加号
- 帖子里显示 base36 代号（`#XP76` 这种），方便在电脑上对照

装的时候要允许「安装未知来源应用」。

---

## 九、目录里都是什么

```
启动.bat            双击启动
start.sh            macOS / Linux 启动
README.md           这个文件
bot/
  run.py            入口（web / daemon / 各种命令）
  server.py         本地控制台
  manage.py         命令行批量管理
  szlike/           核心包
    spawn.py        自动开号
    antirepeat.py   去重（动作 / 句式 / 短语 / 开头 / 口水词）
    persona.py      32 套人设
    voices.py       人设语料
    human.py        形态、句号、AI 味检测
    longform.py     长评长帖档位
    mood.py         情绪 / 性别 / 立场
    quality.py      六项评分
    rag.py          混合召回
    dense.py        向量检索
    kb.py           知识库注入
    brain.py        模型调用
    client.py       社区 API 客户端
    ops.py          接口封装
  kb/               知识库 + 语料 + 向量库
  web/              控制台前端
  config.example.json
  accounts_roster.example.json
app/
  dist/             构建好的 APK
  assets/           前端
  java/             原生壳
  build.sh          重新构建
```

---

## 十、常见问题

**Q：双击启动.bat 一闪就没了**
没装 Python，或者装的时候没勾 `Add Python to PATH`。重装一次，勾上那个选项。

**Q：网页打不开**
看黑窗口里有没有报错。端口 8765 被占了就改 `bot/config.json` 里的 `web.port`，
或者启动时加参数 `python bot\run.py web --port 8766`。

**Q：生成号一直失败**
大概率被限流了。等十几分钟再试，或者一次少生成几个（先 4 个试试）。

**Q：帖子全是英文/乱码**
`config.json` 里 `llm.base_url` 和 `model` 不匹配。DeepSeek 用 `deepseek-chat`。

**Q：写出来的东西还是很像 AI**
1. 先确认两把 Key 都填了（向量没配，检索退化成关键词，示范样本会很差）；
2. 把 `dry_run` 保持 DRY 跑一天，去「待审」页看扣分项，看是断句、事实还是细节问题；
3. 「任务与 Prompt」页可以改每个号的 system prompt，改完点保存立即生效。

**Q：能改发帖时间吗**
能。账号卡片上有「发帖时间」，或者命令行 `python bot\manage.py times`。

**Q：数据会上传吗**
不会。所有内容只写在本机的 `bot/state.db` 和 `bot/logs/` 里，
只有生成文字这一步会把帖子原文发给模型 API（DeepSeek / SiliconFlow）。

---

## 十一、评估与测试成绩（全部实测）

本项目的拟人改动不靠感觉：每次改完跑一遍本地模拟考试，分数涨了才算改对。
考卷用固定种子（`EVAL_SEED=20261105`）可复现；下表数字全部由
`tools/make_eval_tables.py` 从结果 JSON 自动生成，不手写结论。

评分口径：**图灵 40**（5 选 1 让 LLM 裁判挑哪条是机器写的）+ **分布/题材 25**
+ **同质/形式 20** + **AI 味 15**。
注意 5 选 1 的**随机基线是 20%**（真人互测 18.1%），所以图灵项天然封顶约 32/40；
被抓率压到 20% 以下反而是过拟合伪影，本项目不追求。
除 L1 外还有 L2 账号时间序列 / L3 群体协同图谱评估，以及 AI 质疑、楼中楼上下文专项用例。

# 评估数据汇总表（自动生成）

> 由 `tools/make_eval_tables.py` 从各评估 JSON 与 `kb/raw.db` 现场汇总生成。
> 所有数字均取自数据源，不含手写数值或硬编码结论；缺失的数据源写「未生成」并跳过。
> 生成时间：2026-11-06 17:00:13

## 数据源状态

数据来源：tools/eval_last.json + tools/eval_baseline_fixed.json + tools/graph_eval_last.json + tools/graph_baseline.json + tools/eval_all_last.json + tools/scenarios_last.json + tools/thread_context_last.json，生成时间 2026-11-06 17:00:13

| 数据源文件 | 状态 | 数据时间 |
| --- | --- | --- |
| tools/eval_last.json | 已生成 | 2026-11-06 16:58:52（文件修改时间） |
| tools/eval_baseline_fixed.json | 已生成 | 2026-11-06 16:23:50（文件修改时间） |
| tools/graph_eval_last.json | 已生成 | 2026-11-05 21:41:37 |
| tools/graph_baseline.json | 已生成 | 2026-11-05T20:53:53+08:00 |
| tools/eval_all_last.json | 已生成 | 2026-11-05 21:41:37 |
| tools/scenarios_last.json | 已生成 | 2026-11-06 16:53:37 |
| tools/thread_context_last.json | 已生成 | 2026-11-06 16:53:38 |

## ① 三层评估总览表

数据来源：tools/eval_all_last.json（数据时间 2026-11-05 21:41:37） + tools/scenarios_last.json（数据时间 2026-11-06 16:53:37） + tools/thread_context_last.json（数据时间 2026-11-06 16:53:38），生成时间 2026-11-06 17:00:13

| 层级 | 评估项 | 判定 | 关键指标 | 来源文件 | 备注 |
| --- | --- | --- | --- | --- | --- |
| L1 文本层 | 评论/帖子/楼中楼文本 | 未通过 | 三项 total={"comment":82.1,"post":77.6,"reply":84.2}；被抓率={"comment":0.319,"post":0.308,"reply":0.239}；门槛 min_total=80；耗时=124.6s | tools/eval_all_last.json | 未通过 |
| L2/L3 行为图谱层 | 图检测 + 图灵投票 + gates | 未通过 | graph_pass=未通过；是否跳过 skipped=否；五组检出率={"real":0.333,"before":1.0,"before_textonly":0.167,"after":1.0,"after_gated":0.667}；checks=18 项 | tools/eval_all_last.json | 未通过 |
| 总体 | overall_pass | 未通过 | gate_blockers=2 项 | tools/eval_all_last.json | 未通过 |
| 新增能力 | AI 质疑场景识别/否认 | 通过 | PASS=4 FAIL=0 SKIP=0（共 4 组） | tools/scenarios_last.json | SKIP 不算通过；退出码 0 仅代表全部验收组通过 |
| 新增能力 | 楼中楼上下文三项齐全 | 通过 | PASS=9 FAIL=0 SKIP=0（共 9 项）；prompt 779 字 | tools/thread_context_last.json | SKIP 不算通过；上游拼装函数不可用时整轮标记 SKIP |

## ② L1 分项表

数据来源：tools/eval_last.json（数据时间 2026-11-06 16:58:52（文件修改时间）） + tools/eval_baseline_fixed.json（数据时间 2026-11-06 16:23:50（文件修改时间）），生成时间 2026-11-06 17:00:13

| 模式 | 样本数 n | 总分 total | 图灵分 turing | 图灵被抓率 catch_rate | 被抓率(%) | 分布 dist | 题材 topic | 同质化 dup | 形式 form | AI 味 flavor | 事实率 fact_rate | 固定考卷基线 total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| comment | 160 | 78.6 | 26.4 | 0.341 | 34.1% | 20.3 | — | 17.4 | — | 14.6 | 0.991 | 83.6 |
| post | 60 | 86.7 | 33.7 | 0.158 | 15.8% | — | 21 | — | 17 | 15 | — | 80 |
| reply | 40 | 82.5 | 28.7 | 0.283 | 28.3% | 19.3 | — | 19.5 | — | 15 | — | 79.3 |

### ②-a L1 分项明细（各 *_detail 的 JSON 原值）

数据来源：tools/eval_last.json（数据时间 2026-11-06 16:58:52（文件修改时间）） + tools/eval_baseline_fixed.json（数据时间 2026-11-06 16:23:50（文件修改时间）），生成时间 2026-11-06 17:00:13

| 模式 | 分项类别 | 指标 | JSON 原值 |
| --- | --- | --- | --- |
| comment | dist_detail | 长度中位 | 12 / 13 / 0.92 |
| comment | dist_detail | 表情率 | 0.27 / 0.25 / 0.94 |
| comment | dist_detail | 换行率 | 0 / 0 / 0.94 |
| comment | dist_detail | 句号率(>25字) | 0.09 / 0.21 / 0.44 |
| comment | dup_detail | 开头重复率 | 0.062 |
| comment | dup_detail | 3gram重复率 | 0.069 |
| post | topic_detail | 食堂/吃 | 7.5 / 15 / 7.5 |
| post | topic_detail | 图书馆/自习 | 7.8 / 10 / 2.3 |
| post | topic_detail | 雨/伞 | 1.2 / 0 / 1.2 |
| post | topic_detail | 考试/作业/绩点 | 16.5 / 11.7 / 4.8 |
| post | topic_detail | 宿舍/室友 | 5.5 / 10 / 4.5 |
| post | topic_detail | 车/充电 | 7 / 8.3 / 1.3 |
| post | topic_detail | 恋爱/情感 | 9.5 / 10 / 0.5 |
| post | form_detail | 长度中位 | 33 / 31 / 0.94 |
| post | form_detail | 时间开头率 | 0 / 0.01 / 0.81 |
| post | form_detail | 数字堆砌率 | 0.05 / 0.8 |
| post | dup_detail | 开头重复率 | 0.017 |
| post | dup_detail | 3gram重复率 | 0.084 |
| reply | dist_detail | 长度中位 | 17 / 13 / 0.69 |
| reply | dist_detail | 表情率 | 0.33 / 0.28 / 0.85 |
| reply | dist_detail | 换行率 | 0 / 0.01 / 0.92 |
| reply | dist_detail | 句号率(>25字) | 0 / 0.06 / 0.63 |
| reply | dup_detail | 开头重复率 | 0 |
| reply | dup_detail | 3gram重复率 | 0.032 |

## ③ 图灵测试对照表（含五组）

数据来源：tools/graph_eval_last.json（数据时间 2026-11-05 21:41:37），生成时间 2026-11-06 17:00:13

| 组别 | 说明 | 投票数 votes | 判为机器 bot_votes | 检出率 detection_rate | 折算百分比 | parse_failed | truncated |
| --- | --- | --- | --- | --- | --- | --- | --- |
| real | 真人基线 | 6 | 2 | 0.333 | 33.3% | 0 | 0 |
| before | 改造前 | 6 | 6 | 1 | 100% | 0 | 0 |
| before_textonly | 改造前·仅文本 | 6 | 1 | 0.167 | 16.7% | 0 | 0 |
| after | 改造后 | 6 | 6 | 1 | 100% | 0 | 0 |
| after_gated | 改造后·画像门控 | 6 | 4 | 0.667 | 66.7% | 0 | 0 |

### ③-a L2/L3 确定性指标

数据来源：tools/graph_eval_last.json（数据时间 2026-11-05 21:41:37），生成时间 2026-11-06 17:00:13

| 指标 | real | before | before_textonly | after | after_gated |
| --- | --- | --- | --- | --- | --- |
| n_actions | 388 | 873 | 873 | 873 | 873 |
| n_accounts | 146 | 33 | 33 | 33 | 33 |
| span_hours | 32.82 | 72.72 | 72.72 | 669.94 | 669.94 |
| accounts_per_post_max | 6 | 16 | 16 | 1 | 1 |
| multi_account_posts | 68 | 112 | 112 | 0 | 0 |
| burst5_max | 6 | 22 | 22 | 4 | 4 |
| burst10_max | 9 | 32 | 32 | 5 | 5 |
| default_avatar_rate | 0 | 1 | 1 | 1 | 0 |
| hour_entropy | 4 | 3.284 | 3.284 | 4.181 | 4.181 |
| actions_per_day_max | 195 | 338 | 338 | 41 | 41 |
| same_target_pairs | 179 | 1745 | 1745 | 0 | 0 |
| cross_account_lexicon | 0 | 3 | 3 | 3 | 3 |
| median_gap_minutes | 2.1 | 0.14 | 0.14 | 21.79 | 21.79 |

### ③-b L2/L3 checks 明细

数据来源：tools/graph_eval_last.json（数据时间 2026-11-05 21:41:37），生成时间 2026-11-06 17:00:13

| 类别 | 检查项 | 判定 | 明细 |
| --- | --- | --- | --- |
| structure | 重放条数守恒（before == after == after_gated） | 通过 | 873 / 873 / 873 |
| structure | 同帖单 owner（≤1 号） | 通过 | after=1 before=16 |
| structure | 时间铺开 ≥20 天 | 通过 | after=669.9h before=72.7h |
| structure | 同帖多号帖数归零 | 通过 | after=0 before=112 |
| calibrated | 窗口 p90 burst5_max 不超过真人基准 | 通过 | real=6 before=18 after=3 上限=8.5 |
| calibrated | 窗口 p90 burst10_max 不超过真人基准 | 通过 | real=8.1 before=26 after=4 上限=11.125 |
| calibrated | 窗口 p90 accounts_per_post_max 不超过真人基准 | 通过 | real=4 before=9 after=1 上限=6 |
| calibrated | 窗口 p90 multi_account_posts 不超过真人基准 | 通过 | real=10 before=11.1 after=0 上限=13.5 |
| calibrated | 窗口 p90 same_target_pairs 不超过真人基准 | 通过 | real=18 before=93.1 after=0 上限=23.5 |
| calibrated | 作息熵不低于真人基准的 90% | 通过 | real_p50=1.784 after_p50=3.779 |
| gate | 账号资料：默认 ID 比例对齐真人 | 未通过 | real=0.00 after=1.00 after_gated=0.00（目标态） |
| gate | 跨账号共享冷门实体对齐真人 | 未通过 | real=0 before=3 after=3（重放不伪造文本，故未改善） |
| llm | 评估有效：BEFORE 被判机器 ≥60% | 通过 | 100% |
| llm | 目标态有效：AFTER_GATED 被判机器 ≤30% | 未通过 | 67% |
| llm | 真人对照组误判 ≤30% | 未通过 | 33% |
| llm | 改善幅度 ≥30 个百分点 | 通过 | +33% |
| observe | 观察：隐藏账号/时间后的漏判幅度 | 通过 | before 100% vs textonly 17%，落差 +83% |
| observe | 观察：补齐昵称头像单独带来的降幅 | 通过 | after 100% → after_gated 67%，收益 +33% |

### ③-c L2/L3 汇总字段

数据来源：tools/graph_eval_last.json（数据时间 2026-11-05 21:41:37），生成时间 2026-11-06 17:00:13

| 项目 | 数值 |
| --- | --- |
| generated_at | 2026-11-05 21:41:37 |
| graph_pass | 未通过 |
| pass | 未通过 |
| baseline_available | 是 |
| hard_total | 16 |
| hard_pass | 12 |
| graph_total | 14 |
| graph_hard_pass | 12 |
| config.votes | 6 |
| config.window | 48 |
| config.samples | 200 |
| config.offline | 否 |
| config.seed | 7 |
| counts.real | 388 |
| counts.before | 873 |
| counts.before_textonly | 873 |
| counts.after | 873 |
| counts.after_gated | 873 |
| gate_blockers 数量 | 2 |
| gate_blocker.1 | 账号资料：默认 ID 比例对齐真人｜real=0.00 after=1.00 after_gated=0.00（目标态） |
| gate_blocker.2 | 跨账号共享冷门实体对齐真人｜real=0 before=3 after=3（重放不伪造文本，故未改善） |

## ④ 真人基准标定表

数据来源：tools/graph_baseline.json（数据时间 2026-11-05T20:53:53+08:00），生成时间 2026-11-06 17:00:13

| 指标 | p50 | p90 | max | mean |
| --- | --- | --- | --- | --- |
| burst5_max | 5 | 6 | 6 | 4.625 |
| burst10_max | 6 | 8.1 | 9 | 6.335 |
| accounts_per_post_max | 3 | 4 | 4 | 3.24 |
| multi_account_posts | 7 | 10 | 12 | 7.29 |
| same_target_pairs | 11 | 18 | 24 | 12.075 |
| hour_entropy | 1.784 | 2.574 | 3.05 | 1.905 |
| median_gap_minutes | 2.33 | 3.333 | 4.29 | 2.28 |
| default_avatar_rate | 0 | 0 | 0 | 0 |
| cross_account_lexicon | 0 | 0 | 0 | 0 |

### ④-a 真人全窗口 real_full

数据来源：tools/graph_baseline.json（数据时间 2026-11-05T20:53:53+08:00），生成时间 2026-11-06 17:00:13

| 指标 | 数值 |
| --- | --- |
| n_actions | 388 |
| n_accounts | 146 |
| span_hours | 32.82 |
| accounts_per_post_max | 6 |
| multi_account_posts | 68 |
| burst5_max | 6 |
| burst10_max | 9 |
| default_avatar_rate | 0 |
| hour_entropy | 4 |
| actions_per_day_max | 195 |
| same_target_pairs | 179 |
| cross_account_lexicon | 0 |
| median_gap_minutes | 2.1 |

### ④-b 标定参数

数据来源：tools/graph_baseline.json（数据时间 2026-11-05T20:53:53+08:00），生成时间 2026-11-06 17:00:13

| 参数 | 数值 |
| --- | --- |
| generated_at | 2026-11-05T20:53:53+08:00 |
| source | tools/graph_refs.json |
| window | 48 |
| samples | 200 |
| seed | 7 |
| n_real_actions | 388 |

## ⑤ 场景/上下文验收表

数据来源：tools/scenarios_last.json（数据时间 2026-11-06 16:53:37） + tools/thread_context_last.json（数据时间 2026-11-06 16:53:38），生成时间 2026-11-06 17:00:13

| 来源 | 验收组/检查项 | 状态 | 明细 |
| --- | --- | --- | --- |
| tools/scenarios_last.json | recognition | 通过 | 逐条断言 46 条（正例 28 / 反例 18）：对 46、错 0、SKIP 0，正确率 100.0%（阈值 100%） |
| tools/scenarios_last.json | denial_quality | 通过 | 检查 90 条回复，命中禁用腔 0 处（禁用词表 36 条） |
| tools/scenarios_last.json | denial_length | 通过 | 检查 90 条：超长 0 条（上限 25 字）、空回复 0 条；最长 9 字 |
| tools/scenarios_last.json | denial_dedup | 通过 | 连取 30 句，去重后 19 句，去重率 63.3%（阈值 60%）；各调用模式去重率：main=63.3%、seed=66.7%、plain=3.3% |
| tools/scenarios_last.json | SUMMARY(overall) | 通过 | PASS=4 FAIL=0 SKIP=0 |
| tools/thread_context_last.json | api_available | 通过 | 已定位线上拼装函数 szlike.tasks.build_reply_prompt |
| tools/thread_context_last.json | prompt_built | 通过 | build_reply_prompt 调用成功，system 374 字 / user 386 字 |
| tools/thread_context_last.json | post_full_present | 通过 | 原帖全文 210 字在最终 prompt 中 |
| tools/thread_context_last.json | post_not_truncated | 通过 | 【原帖】段 212 字（原文 210 字），尾部独特句在，判定阈值 >60 字且 ≥90% 原文长度 |
| tools/thread_context_last.json | their_present | 通过 | 被回复评论 33 字在最终 prompt 中 |
| tools/thread_context_last.json | thread_all_present | 通过 | 楼中楼 3 条，命中 3 条 |
| tools/thread_context_last.json | reply_to_present | 通过 | replyTo 3 个，命中 3 个 |
| tools/thread_context_last.json | order_correct | 通过 | user 内 marker 位置 原帖=0 / 对方说=216 / 楼里已说过=256；楼中楼三条位置=[268, 301, 335] |
| tools/thread_context_last.json | no_comment_as_post | 通过 | 【原帖】段=原帖正文（尾部独特句在，未混入一级评论/被回复评论=否）；【对方说】段含被回复评论=是 |
| tools/thread_context_last.json | SUMMARY(overall) | 通过 | PASS=9 FAIL=0 SKIP=0 |

## ⑥ 语料库规模表

数据来源：kb/raw.db（现场 SQL 查询）+ kb/ 目录文件（现场 stat），生成时间 2026-11-06 17:00:13

| 项目 | 数值 | 说明 |
| --- | --- | --- |
| posts 表行数 | 78511 | SELECT COUNT(*) FROM posts（现场查询） |
| comments 表行数 | 18276 | SELECT COUNT(*) FROM comments（现场查询） |
| state 表行数 | 26 | SELECT COUNT(*) FROM state（现场查询） |
| comments 中楼中楼(is_sub=1) | 0 | SELECT COUNT(*) FROM comments WHERE is_sub=1（现场查询） |
| posts 覆盖天数 day | 1145 | SELECT COUNT(DISTINCT day) FROM posts（现场查询） |
| kb/KNOWLEDGE.md | 11.9 KB (12211 B) | 文件大小（现场 stat） |
| kb/build.log | 1.5 KB (1570 B) | 文件大小（现场 stat） |
| kb/campus_map.json | 12.7 KB (13032 B) | 文件大小（现场 stat） |
| kb/dense.db | 9.6 MB (10088448 B) | 文件大小（现场 stat） |
| kb/dense.db-shm | 32.0 KB (32768 B) | 文件大小（现场 stat） |
| kb/dense.db-wal | 0 B (0 B) | 文件大小（现场 stat） |
| kb/dense.f32 | 358.3 MB (375660544 B) | 文件大小（现场 stat） |
| kb/dense_meta.json | 127 B (127 B) | 文件大小（现场 stat） |
| kb/events.json | 1004 B (1004 B) | 文件大小（现场 stat） |
| kb/knowledge.json | 17.1 KB (17495 B) | 文件大小（现场 stat） |
| kb/knowledge.json.bak_deflavor | 17.0 KB (17375 B) | 文件大小（现场 stat） |
| kb/rag.db | 415.8 MB (435986432 B) | 文件大小（现场 stat） |
| kb/raw.db | 33.4 MB (35069952 B) | 文件大小（现场 stat） |
| kb/samples.json | 114.9 KB (117620 B) | 文件大小（现场 stat） |
| kb/short_replies.json | 28.7 KB (29344 B) | 文件大小（现场 stat） |
| kb/stats.json | 30.4 KB (31082 B) | 文件大小（现场 stat） |
| kb/stats_report.md | 5.3 KB (5455 B) | 文件大小（现场 stat） |
| kb/style.json | 4.1 KB (4207 B) | 文件大小（现场 stat） |
| kb/ 目录文件合计 | 817.4 MB (857088666 B) | 18 个文件 |

---

## 十二、WebUI 一键考试与两套模型答卷

### 控制台「考试」栏目

控制台左侧新增「考试」：选模式（评论 / 帖子 / 楼中楼 / 全部）、样本数、候选数，
可临时换模型（API Key 用密码框、不回显、不写本地存储、只经服务端内存传给子进程），
点「开始考试」后台运行，实时进度 + 成绩表 + 逐题答卷对比，考完导出答卷与试卷。

| 导出文件 | 内容 |
|---|---|
| `试卷.json` | 题目（真实帖子正文）+ 人类标准答案（真人评论原句） |
| `答卷.json` | AI 答案 + 逐题图灵判定 + 汇总分数 |
| `对比.csv` | 逐题：题号 / 模式 / 题目 / 人类标准答案 / AI答案 / 是否被抓 |
| `成绩单.md` | 汇总表 + 图灵测试对照表 + 逐题并排对比 |
| `说明.txt` | 口径与字段说明 |

> 帖子是开放式生成，没有唯一标准答案，`试卷.json` 里给的是**真实帖子作风格参照**，已单独标注，不冒充标准答案。
> 所有导出不含 API Key 与账号凭据；临时接入的第三方网关地址一律脱敏，只保留模型名。

**以后的答卷会显示模型原输入**：设 `SZLIKE_EXAM_RECORD_INPUT=1` 再跑考试，
逐题答卷会多一个 `model_input` 字段（发给模型的 `system` / `user` 原文），导出时自动加列，
便于调试向量检索与 RAG 注入。本次两份样例答卷未开启该开关。

### 两套模型的完整答卷（同一套固定考卷，`EVAL_SEED=20261105`，各 260 题）

| 模型 | 完整逐题答卷 | 题数 |
|---|---|---|
| DeepSeek `deepseek-chat` | [答卷_deepseek.json](docs/exams/答卷_deepseek.json) | 260 |
| Gemini `gemini-2.5-flash-lite` | [答卷_gemini.json](docs/exams/答卷_gemini.json) | 260 |

[并排对比预览（前 15 题：题目 / 人类标准答案 / 两套 AI 答案 / 图灵被抓）](docs/exams/对比预览.md)

答卷含真实论坛帖子与真人评论原句作为题目与标准答案，是 L1 评估的原始材料。
**第三方接入网关地址已脱敏**，仅保留模型名供对照。

### 两套模型的 L1 成绩对照（实测，勿只看单轮）

| 轮次 | comment | post | reply | 平均 | 备注 |
|---|---|---|---|---|---|
| DeepSeek 固定考卷基线 | 83.6 | 80.0 | 79.3 | 81.0 | c1 |
| DeepSeek Round3（全合流） | 78.6 | 86.7 | 82.5 | 82.6 | c3 |
| DeepSeek Best-of-3 最佳轮 | 80.3 | 88.4 | 86.2 | 85.0 | c3 |
| Gemini c1（并发 8） | 83.5 | 80.7 | 86.5 | 83.6 | 干净 |
| Gemini c1（并发 2，最干净） | 86.7 | 85.9 | 89.3 | 87.3 | 0 次 4xx / 0 生成失败 |
| Gemini c3 | 81.2 | 84.9 | ~~90.3~~ | — | **reply 受 62 次 429 污染，不可采信** |

读法与限制：
- 换模型会**同时换掉考生与图灵裁判**（生成与裁判共用一个 Brain），分数差异不能 100% 归因于生成能力。用 DeepSeek 裁判重判同一批 Gemini 文本，被抓率 25.0%→20.8%，纯裁判因素约 −4.2pp，属噪声量级。
- Gemini 这轮**事实准确率 92%**，明显低于 DeepSeek 的 99.1–100%（多为把独墅湖写成「本部 / 东区」）。
- 同模型不同轮次波动约 3.7 分，优势区间应读作「平均领先约 +1 到 +6 分」，不是稳定定值。
- 早期曾出现「裁判调用失败被当成没被抓」的缺陷（限流时凭空推高图灵分），现已修：裁判失败返回独立哨兵、剔除出分母，并加了裁判侧失败率守卫。
