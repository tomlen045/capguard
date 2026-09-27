# capguard

[中文](README.md) | [English](README_EN.md)

**磁盘容量哨兵** —— 半夜被告警叫醒，十有八九是两件事：配置被人改了（[cfgdrift](https://github.com/tomlen045/cfgdrift) 管），**磁盘爆了（capguard 管）**。

capguard 盯磁盘水位：采样 → 落档 → 预测还能撑几天 → 增长即报。
不看单点水位，看**「水位 + 走势」双轴**：90% 但一个月只涨 1% 是虚惊，75% 但每天 +3% 才要命。

[![tests](https://img.shields.io/badge/self--tests-18%2F18-green)]() [![deps](https://img.shields.io/badge/deps-zero-yellow)]() [![license](https://img.shields.io/badge/license-MIT-blue)]()

---

## 解决什么问题

* 备份盘悄悄涨到 92%，单看水位只觉得「快满了」，不知道**还剩 1.3 天** → capguard 用最小二乘拟合日增速，直接告诉你「按这个速度 X 天后爆盘」
* 老盘 90% 但半年不涨，告警天天狼来了 → **走势轴不外推（增长非正）就不虚报**；水位破 F 线它照样报——两轴独立，不互相冒充
* 采了样没下文，复盘时说不清「当时谁知道」→ **sample 每次判级都写进追加式事件文件（JSONL），accept 盖章留痕**，审计链完整
* 历史数据想回放？→ `predict --as-of` **回放任意历史时点**：用当时之前的数据，算当时本该得出的结论
* 基础数据从哪来？→ 两种口径都行：`--live` 直接读本机 `df`（macOS/Linux 通吃）；跨机器统一口径用 `--used/--total` 手工上报（监控平台/巡检脚本把 df 数字喂给它）
* 预测会不会瞎编？→ **样本 <3 明确输出「样本不足不外推」**，绝不硬编一个数

## 安装

```bash
# 方式一：直接拉单文件（仅标准库，Python 3.8+）
curl -fsSLO https://raw.githubusercontent.com/tomlen045/capguard/main/capguard.py

# 方式二：git clone 整仓（含 README / 演示素材）
git clone https://github.com/tomlen045/capguard.git
```

## 30 秒上手

```bash
# 1) 采一个点（手工上报口径：监控平台/巡检脚本喂 df 数字）
python3 capguard.py sample --host web01 --path /data --used 780 --total 1000

# 1') 或者直接读本机磁盘（--live）
python3 capguard.py sample --host web01 --live

# 2) 巡检：谁在超线？exit 0=安全 1=超线 2=参数错误
python3 capguard.py check

# 3) 预测：还能撑几天？
python3 capguard.py predict --host-path "web01|/data"

# 4) 历史回放：当时本该报警吗？
python3 capguard.py predict --host-path "web01|/data" --as-of "2026-09-25T10:00:00+08:00"

# 5) 确认这是可接受的新常态（如已提扩容工单）→ 盖章留痕
python3 capguard.py accept --host-path "web01|/backup" --memo "已提扩容工单"

# 6) 审计链：所有采样与 accept，只追加永不改写
python3 capguard.py log --n 20
```

## 双轴判级

| 轴 | 默认线 | 含义 |
|---|---|---|
| 水位 P 线 | `--pct-p 80` | 已用 ≥80% → WARN |
| 水位 F 线 | `--pct-f 90` | 已用 ≥90% → CRITICAL |
| 走势 P 线 | `--days-p 14` | 外推剩余 ≤14 天 → WARN |
| 走势 F 线 | `--days-f 7` | 外推剩余 ≤7 天 → CRITICAL |

任一轴触线即报；CRITICAL 优先于 WARN。退出码 `0/1/2` 三值确定（I4），cron / CI 直接接：

```cron
# 每天早上 8 点巡检，超线才输出（I3 保证 check 绝不写盘）
0 8 * * * /usr/bin/python3 /opt/capguard/capguard.py --root /opt/capguard check || echo "capguard: 磁盘超线" | mail -s "disk alert" ops@example.com
```

## 六个子命令

| 命令 | 作用 | 写盘？ |
|---|---|---|
| `sample` | 采一个点（`--live` 真盘 / `--used --total` 手工上报，`--ts` 支持回填历史） | 档+事件 |
| `check` | 对档内全部（或指定）对象复算判级 | **不写盘（I3）** |
| `predict` | 外推还能撑几天；`--as-of` 回放历史 | **不写盘（I3）** |
| `accept` | 人工确认当前水位为新常态，事件留痕 | 事件 |
| `log` | 查看追加式事件流（`--n` 最后 N 条） | 不写盘 |
| `show` | 档概览：对象/采样数/阈值 | 不写盘 |

## 五条不变量（selftest 18/18 全绿）

| # | 不变量 | 怎么验证的 |
|---|---|---|
| I1 | 档零可执行：`*.cap.json` / `*.cap-events.jsonl` 永远只是数据 | T9 扫描档与事件文件无任何代码语义 |
| I2 | 事件只追加：JSONL 只 append 永不改写 | T8 前后读回逐行比对 |
| I3 | check/predict 不落盘：纯只读计算 | T6 全目录 mtime 前后快照比对 |
| I4 | 退出码确定：0/1/2 三值 | T1-T18 全程断言 |
| I5 | 判级可复算：同数据反复 check 输出逐字节一致 | T7 连续两次输出 diff |

## 实战演示（偏态数据，异常 1/10）

`demo_skewed.py` 用 5 台主机 10 个挂载点、6 天巡检数据跑真场景：9 个对象一切正常，
唯独 backup01 的 /backup 被备份文件慢慢吃掉。capguard 的产出：

```text
OK       app01|/            水位30.9%  日增1.4KB  剩503.9天
...（其余 8 个正常对象全部 OK，零噪声）
CRITICAL backup01|/backup   水位92.0%  日增60.0KB  剩1.3天
         · 水位92.0%≥F线90%
         · 外推仅剩1.3天≤F天7
```

对照组：90% 但零增长的盘 → 水位轴照样 CRITICAL，走势轴如实写「不外推」，两轴互不冒充。
完整输出见 `scripts/out/demo-output.txt`（真实运行结果，非编造）。

## 文件布局

```text
capacity.cap.json            # 档：全部对象的水位历史（只存数字，不存任何盘上文件内容）
<host>_<path>.cap-events.jsonl  # 每对象一份追加式事件流（审计链）
```

## 设计说明（诚实边界）

* **capguard 是水位记录仪，不是 df 的替代品**：`--used/--total` 是你声明的事实，它不验证盘符真实性；`--live` 才读真盘
* **线性外推的边界**：磁盘增长若不是线性（如周期性日志轮转），拟合斜率会平滑掉周期——它回答「按近期平均速度还能撑几天」，不预测轮转尖峰
* **平台口径**：macOS 与 Linux 的 `df` 可用块算法有内核层差异，跨机器对比请统一用手工 sample 上报你认可的口径
* 档内每对象只保留最近 400 个采样（防无限膨胀）；事件文件不受影响，审计链完整

## 工具箱其它成员

| 工具 | 管什么 |
|---|---|
| [cronguard](https://github.com/tomlen045/cronguard) | 定时任务有没有偷偷死掉 |
| [logwhisperer](https://github.com/tomlen045/logwhisperer) | 日志考古 |
| [debugkit](https://github.com/tomlen045/debugkit) | 故障立案取证修复 |
| [porteye](https://github.com/tomlen045/porteye) | 端口暴露面 |
| [cfgdrift](https://github.com/tomlen045/cfgdrift) | 配置被谁改了 |
| **capguard** | **磁盘还能撑几天** |

## License

MIT
