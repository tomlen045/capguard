#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capguard — 磁盘容量哨兵
=======================

半夜被告警叫醒，十有八九是两件事：配置被人改了（cfgdrift 管），磁盘爆了（capguard 管）。
capguard 盯磁盘水位：采样 → 落档 → 预测还能撑几天 → 增长即报。
不看单点水位，看「水位 + 走势」：90% 但一个月只涨 1% 是虚惊，75% 但每天 +3% 才要命。

零依赖单文件 CLI，Python 3.8+ 标准库，拷走就能跑。
档只存你给它的采样数（df 风格数字）与结构化事件，纯本地，断网可跑。

退出码：0=全部安全 1=超线或待复核 2=参数/环境错误

五条不变量：
  I1 档零可执行：*.cap.json / *.cap-events.jsonl 永远只是数据，绝不含可执行语义
  I2 事件只追加：*.cap-events.jsonl 只 append 永不改写，审计链不可篡
  I3 check 不写盘：判级复算是纯只读操作，绝不顺手改档
  I4 退出码确定：0/1/2 三值，cron / CI 可以放心接
  I5 判级可复算：同一段数据反复 check，判级输出逐字节一致（无时钟依赖、无随机）

设计要点（诚实说明）：
  · 预测基于最小二乘线性外推（近 N 个样本拟合日增速），样本 <3 时明确输出
    「样本不足不外推」而不是硬编一个数——不装聪明。
  · sample --used/--total 是你声明的事实，capguard 不验证盘符真实性——
    它是水位记录仪，不是 df 的替代品；--live 采样才用真 df 读本机。
  · 平台差异如实暴露：macOS 与 Linux 的 df 可用块算法有内核层差异，--live
    以本机 df 为准；跨机器对比请统一用手工 sample 上报你认可的口径。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

TOOL = "capguard"
VERSION = "1.0.0"
CST = timezone(timedelta(hours=8))
EXIT_OK, EXIT_ALERT, EXIT_ERROR = 0, 1, 2
DEFAULT_PCT_P = 80
DEFAULT_PCT_F = 90
DEFAULT_DAYS_P = 14
DEFAULT_DAYS_F = 7
MIN_SAMPLES_FOR_PREDICT = 3

ARCHIVE_NAME = "capacity.cap.json"
EVENTS_SUFFIX = ".cap-events.jsonl"

VERDICT_OK = "OK"
VERDICT_WARN = "WARN"
VERDICT_CRITICAL = "CRITICAL"
GRADE = {VERDICT_OK: 0, VERDICT_WARN: 1, VERDICT_CRITICAL: 2}

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def now_cst():
    return datetime.now(CST)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S+08:00").replace(tzinfo=CST)
    except Exception:
        return None


def pct(a, b):
    if b == 0:
        return None
    return a / b * 100.0


def human_kb(n_kb):
    n = float(n_kb)
    for unit in ("KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return "%.1f%s" % (n, unit)
        n /= 1024.0


def fmt_days(d):
    if d is None:
        return "不外推"
    if d <= 0:
        return "已尽"
    return "%.1f天" % d


def _slug(s):
    out = []
    for ch in s:
        if ch.isalnum():
            out.append(ch.lower())
        else:
            out.append("_")
    return ("".join(out))[:80] or "default"


def host_path_key(host, path):
    return "%s|%s" % (host, path)


# ---------------------------------------------------------------------------
# 档与事件
# ---------------------------------------------------------------------------


def archive_path(root):
    return os.path.join(root, ARCHIVE_NAME)


def load_archive(root):
    p = archive_path(root)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_archive(root, arch):
    arch["updated_at"] = iso(now_cst())
    tmp = archive_path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(arch, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, archive_path(root))


def events_path(root, hp):
    return os.path.join(root, _slug(hp) + EVENTS_SUFFIX)


def append_event(root, hp, ev):
    # I2：只追加，绝不改写已有内容
    with open(events_path(root, hp), "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")


def load_events(root, hp):
    p = events_path(root, hp)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# 判级（I5：纯函数，无时钟/随机依赖）
# ---------------------------------------------------------------------------


def classify(pct_used, pct_p, pct_f, days_left, days_p, days_f):
    """水位 + 走势双轴判级。返回 (verdict, reasons[list of str])。"""
    reasons = []
    v = VERDICT_OK
    if pct_used is None:
        reasons.append("无水位数据")
        return v, reasons
    if pct_used >= pct_f:
        v = VERDICT_CRITICAL
        reasons.append("水位%.1f%%≥F线%.0f%%" % (pct_used, pct_f))
    elif pct_used >= pct_p:
        v = VERDICT_WARN
        reasons.append("水位%.1f%%≥P线%.0f%%" % (pct_used, pct_p))
    if days_left is not None:
        if days_left <= days_f:
            v = VERDICT_CRITICAL
            reasons.append("外推仅剩%.1f天≤F天%.0f" % (days_left, days_f))
        elif days_left <= days_p and v != VERDICT_CRITICAL:
            v = VERDICT_WARN
            reasons.append("外推仅剩%.1f天≤P天%.0f" % (days_left, days_p))
    if not reasons:
        reasons.append("水位与走势均在安全区")
    return v, reasons


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------


def live_sample(mount=None):
    """--live：本机 df -kP 采样。macOS/Linux 语义差异以本机 df 为准。"""
    out = subprocess.run(
        ["df", "-kP"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError("df 失败: %s" % out.stderr.strip()[:200])
    rows = []
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6 or not parts[1].isdigit():
            continue  # autofs/伪文件系统行（如 macOS 的 map auto_home）无数字块数，跳过
        total, used, avail = int(parts[1]), int(parts[2]), int(parts[3])
        mnt = parts[5]
        if total <= 0:
            continue
        if mount and mnt != mount:
            continue
        rows.append({"mount": mnt, "total": total, "used": used, "avail": avail})
    if mount and not rows:
        raise RuntimeError("--live 没找到挂载点 %r（df -kP 输出里无此路径）" % mount)
    return rows


# ---------------------------------------------------------------------------
# 预测（I3：纯只读计算）
# ---------------------------------------------------------------------------


def predict_days_left(samples, total_kb):
    """samples: [(epoch_s, used_kb)] 升序。返回 (days_left|None, daily_kb|None, note)。

    最小二乘拟合 used = a + b*t（t 单位天）。
    斜率 <=0 或样本不足 → 不外推（返回 None），绝不硬编。
    """
    n = len(samples)
    if n < MIN_SAMPLES_FOR_PREDICT:
        return None, None, "样本%d<%d不外推" % (n, MIN_SAMPLES_FOR_PREDICT)
    # 归一化时间轴：兼容 epoch 秒与 datetime 两种入参
    epochs = [t.timestamp() if hasattr(t, "timestamp") else float(t) for t, _ in samples]
    t0 = epochs[0]
    xs = [(t - t0) / 86400.0 for t in epochs]
    ys = [u for _, u in samples]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, None, "时间跨度为0不外推"
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx          # 全局斜率 KB/天
    a = my - b * mx
    daily = b              # 单段线性拟合即日增速；样本密时天然平滑
    if daily <= 0:
        return None, daily, "增长非正（%.0fKB/天）不外推" % daily
    last_used = ys[-1]
    if total_kb <= 0:
        return None, daily, "无total不外推"
    free = total_kb - last_used
    if free <= 0:
        return 0.0, daily, "已满"
    return free / daily, daily, "最小二乘%d样本" % n


# ---------------------------------------------------------------------------
# 子命令：sample
# ---------------------------------------------------------------------------


def _sample_one(args, host, path, used_kb, total_kb, source, ts_iso):
    arch = load_archive(args.root) or {"version": VERSION, "hosts": {}}
    arch.setdefault("hosts", {})
    hp = host_path_key(host, path)
    entry = arch["hosts"].setdefault(hp, {"pct_p": args.pct_p, "pct_f": args.pct_f,
                                          "days_p": args.days_p, "days_f": args.days_f,
                                          "total_kb": None, "samples": []})
    entry["total_kb"] = int(total_kb)
    entry["samples"].append({"ts": ts_iso, "used_kb": int(used_kb), "src": source})
    # 档只保留最近 400 个采样，防止无限膨胀（事件文件不受影响，审计链完整）
    if len(entry["samples"]) > 400:
        entry["samples"] = entry["samples"][-400:]
    save_archive(args.root, arch)

    # 立即给一次判级反馈（用档内全部样本）
    smp = [(parse_iso(s["ts"]), s["used_kb"]) for s in entry["samples"]]
    smp = [(t, u) for t, u in smp if t is not None]
    smp.sort(key=lambda x: x[0])
    days_left, daily, note = predict_days_left(smp, int(total_kb))
    p_used = pct(used_kb, total_kb)
    verdict, reasons = classify(p_used, args.pct_p, args.pct_f, days_left, args.days_p, args.days_f)
    ev = {
        "ts": iso(now_cst()), "event": "sample", "host": host, "path": path,
        "used_kb": int(used_kb), "total_kb": int(total_kb),
        "pct_used": round(p_used, 2) if p_used is not None else None,
        "days_left": round(days_left, 2) if days_left is not None else None,
        "daily_kb": round(daily, 1) if daily is not None else None,
        "verdict": verdict, "reasons": reasons, "src": source, "note": note,
        "predict_basis": "epoch-fitted", "sample_ts": ts_iso,
    }
    append_event(args.root, hp, ev)
    print("%-8s %-28s 水位%.1f%%  日增%s  剩%s  [%s]"
          % (verdict, hp, p_used, human_kb(daily) if daily is not None else "—",
             fmt_days(days_left), note))
    for r in reasons:
        print("         · %s" % r)
    return verdict


def cmd_sample(args):
    if args.live:
        rows = live_sample(args.mount)
        worst = 0
        for r in rows:
            v = _sample_one(args, args.host, r["mount"], r["used"], r["total"],
                            "live-df", iso(now_cst()))
            worst = max(worst, GRADE[v])
        return EXIT_OK if worst == 0 else EXIT_ALERT
    if args.used is None or args.total is None:
        print("exit 2: sample 需要 --live 或同时给 --used 与 --total", file=sys.stderr)
        return EXIT_ERROR
    if args.total <= 0 or args.used < 0 or args.used > args.total:
        print("exit 2: 非法数值 used=%s total=%s（需 0≤used≤total，total>0）"
              % (args.used, args.total), file=sys.stderr)
        return EXIT_ERROR
    v = _sample_one(args, args.host, args.path, args.used, args.total,
                    "manual", args.ts or iso(now_cst()))
    return EXIT_OK if v == VERDICT_OK else EXIT_ALERT


# ---------------------------------------------------------------------------
# 子命令：check（只读，I3）
# ---------------------------------------------------------------------------


def _check_one(arch, args, hp):
    entry = arch["hosts"][hp]
    host, path = hp.split("|", 1)
    smp = [(parse_iso(s["ts"]), s["used_kb"]) for s in entry.get("samples", [])]
    smp = [(t, u) for t, u in smp if t is not None]
    smp.sort(key=lambda x: x[0])
    total_kb = entry.get("total_kb") or 0
    days_left, daily, note = predict_days_left(smp, total_kb)
    last_used = smp[-1][1] if smp else None
    p_used = pct(last_used, total_kb) if (smp and total_kb) else None
    pct_p = entry.get("pct_p", DEFAULT_PCT_P)
    pct_f = entry.get("pct_f", DEFAULT_PCT_F)
    days_p = entry.get("days_p", DEFAULT_DAYS_P)
    days_f = entry.get("days_f", DEFAULT_DAYS_F)
    verdict, reasons = classify(p_used, pct_p, pct_f, days_left, days_p, days_f)
    print("%-8s %-28s 水位%s  日增%s  剩%s  [%s]"
          % (verdict, hp,
             "%.1f%%" % p_used if p_used is not None else "—",
             human_kb(daily) if daily is not None else "—",
             fmt_days(days_left), note))
    for r in reasons:
        print("         · %s" % r)
    return GRADE[verdict]


def cmd_check(args):
    arch = load_archive(args.root)
    if not arch or not arch.get("hosts"):
        print("exit 2: 无档或档为空，请先 sample", file=sys.stderr)
        return EXIT_ERROR
    if args.host_path:
        targets = [args.host_path] if args.host_path in arch["hosts"] else []
        if not targets:
            print("exit 2: 档内没有 %r。现有：%s"
                  % (args.host_path, ", ".join(sorted(arch["hosts"]))), file=sys.stderr)
            return EXIT_ERROR
    else:
        targets = sorted(arch["hosts"])
    worst = 0
    for hp in targets:
        worst = max(worst, _check_one(arch, args, hp))
    return EXIT_OK if worst == 0 else EXIT_ALERT


# ---------------------------------------------------------------------------
# 子命令：predict / accept / log / show
# ---------------------------------------------------------------------------


def cmd_predict(args):
    """只读预测：不写盘（I3）。可 --as-of 回放任意历史时点。"""
    arch = load_archive(args.root)
    if not arch or not arch.get("hosts"):
        print("exit 2: 无档或档为空，请先 sample", file=sys.stderr)
        return EXIT_ERROR
    if args.host_path:
        targets = [args.host_path] if args.host_path in arch["hosts"] else []
        if not targets:
            print("exit 2: 档内没有 %r" % args.host_path, file=sys.stderr)
            return EXIT_ERROR
    else:
        targets = sorted(arch["hosts"])
    rc = EXIT_OK
    for hp in targets:
        entry = arch["hosts"][hp]
        smp = [(parse_iso(s["ts"]), s["used_kb"]) for s in entry.get("samples", [])]
        smp = [(t, u) for t, u in smp if t is not None]
        smp.sort(key=lambda x: x[0])
        if args.as_of:
            cut = parse_iso(args.as_of)
            if cut is None:
                print("exit 2: --as-of 时间格式应为 2026-09-27T10:00:00+08:00", file=sys.stderr)
                return EXIT_ERROR
            smp = [(t, u) for t, u in smp if t <= cut]
            print("%s（回放 %s，样本%d）" % (hp, args.as_of, len(smp)))
        else:
            print("%s（样本%d）" % (hp, len(smp)))
        total_kb = entry.get("total_kb") or 0
        days_left, daily, note = predict_days_left(smp, total_kb)
        if daily is not None:
            print("  日增 %s/天 · 预计可用 %s · %s"
                  % (human_kb(daily), fmt_days(days_left), note))
        else:
            print("  %s" % note)
            rc = max(rc, EXIT_ALERT)
        # 展示最近 3 个采样点
        for t, u in smp[-3:]:
            print("    %s  used=%s" % (t.strftime("%m-%d %H:%M"), human_kb(u)))
    return rc


def cmd_accept(args):
    """accept：人工确认「当前水位是可接受的新常态」——写 accept 事件留痕。

    accept 后档内 samples 不动（历史仍是历史）；它的作用是在事件链上盖章，
    让 log 里能看到「这个水位运维看过、认了」，供告警去重/复盘用。
    """
    arch = load_archive(args.root)
    if not arch or not arch.get("hosts"):
        print("exit 2: 无档或档为空，请先 sample", file=sys.stderr)
        return EXIT_ERROR
    if args.host_path:
        targets = [args.host_path] if args.host_path in arch["hosts"] else []
        if not targets:
            print("exit 2: 档内没有 %r" % args.host_path, file=sys.stderr)
            return EXIT_ERROR
    else:
        targets = sorted(arch["hosts"])
    now = iso(now_cst())
    for hp in targets:
        entry = arch["hosts"][hp]
        smp = entry.get("samples", [])
        last = smp[-1] if smp else None
        ev = {
            "ts": now, "event": "accept", "host": hp.split("|", 1)[0],
            "path": hp.split("|", 1)[1],
            "accepted_used_kb": last["used_kb"] if last else None,
            "accepted_ts": last["ts"] if last else None,
            "memo": args.memo or "",
        }
        append_event(args.root, hp, ev)
        print("accept 已留痕 %s（used=%s @ %s）%s"
              % (hp,
                 human_kb(last["used_kb"]) if last else "—",
                 last["ts"] if last else "—",
                 ("memo=" + args.memo) if args.memo else ""))
    return EXIT_OK


def cmd_log(args):
    arch = load_archive(args.root)
    if not arch or not arch.get("hosts"):
        print("exit 2: 无档或档为空", file=sys.stderr)
        return EXIT_ERROR
    if args.host_path:
        targets = [args.host_path] if args.host_path in arch["hosts"] else []
        if not targets:
            print("exit 2: 档内没有 %r" % args.host_path, file=sys.stderr)
            return EXIT_ERROR
    else:
        targets = sorted(arch["hosts"])
    n = 0
    for hp in targets:
        evs = load_events(args.root, hp)
        if args.n:
            evs = evs[-args.n:]
        for ev in evs:
            n += 1
            if ev.get("event") == "sample":
                print("%s  %-8s %-24s 水位%.1f%%  剩%s"
                      % (ev["ts"], ev.get("verdict", "?"), hp,
                         ev.get("pct_used") or 0.0,
                         fmt_days(ev.get("days_left"))))
            else:
                print("%s  %-8s %-24s %s"
                      % (ev["ts"], ev.get("event", "?"), hp,
                         ("memo=" + ev["memo"]) if ev.get("memo") else ""))
    print("共 %d 条事件（文件只追加，绝不改写）" % n)
    return EXIT_OK


def cmd_show(args):
    arch = load_archive(args.root)
    if not arch:
        print("exit 2: 无档，请先 sample", file=sys.stderr)
        return EXIT_ERROR
    print("档文件：%s" % archive_path(args.root))
    print("更新时间：%s" % arch.get("updated_at", "?"))
    print("监控对象：%d 个" % len(arch.get("hosts", {})))
    for hp in sorted(arch.get("hosts", {})):
        e = arch["hosts"][hp]
        print("  %-28s 采样%d  total=%s  线:P%.0f%%/F%.0f%% P%.0f天/F%.0f天"
              % (hp, len(e.get("samples", [])), human_kb(e.get("total_kb") or 0),
                 e.get("pct_p", DEFAULT_PCT_P), e.get("pct_f", DEFAULT_PCT_F),
                 e.get("days_p", DEFAULT_DAYS_P), e.get("days_f", DEFAULT_DAYS_F)))
    return EXIT_OK


# ---------------------------------------------------------------------------
# 子命令：selftest
# ---------------------------------------------------------------------------


def cmd_selftest(args):
    """沙箱 tmp 目录 + 假磁盘时间序列，全链路走一遍并断言 16 项。"""
    t0 = time.time()
    sb = tempfile.mkdtemp(prefix="capguard-selftest-")
    print("capguard selftest · sandbox=%s" % sb)
    results = []

    def record(name, ok, detail=""):
        results.append((name, bool(ok), str(detail)))
        print("[%s] %s %s" % ("PASS" if ok else "FAIL", name.ljust(28), str(detail)[:90]))

    def run(*a, expect_rc=None):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        p = subprocess.run([sys.executable, SCRIPT, "--root", sb] + list(a),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120, env=env)
        return p.returncode, p.stdout, p.stderr

    def read(rel):
        with open(os.path.join(sb, rel), "r", encoding="utf-8") as f:
            return f.read()

    HP = "web01|/data"
    # ---- T1 手工采样三连：水位攀升，exit=1（P线触发）
    rc, out, err = run("sample", "--host", "web01", "--path", "/data",
                       "--used", "600", "--total", "1000", "--ts", "2026-09-25T10:00:00+08:00")
    record("T1 sample#1 OK判级 exit0", rc == 0 and "OK" in out, "rc=%s %s" % (rc, err[:60]))
    rc, out, err = run("sample", "--host", "web01", "--path", "/data",
                       "--used", "700", "--total", "1000", "--ts", "2026-09-26T10:00:00+08:00")
    # 70% < P线80%，仅2样本不外推 → 判 OK exit 0（WARN 由 T3 的走势轴触发）
    record("T2 sample#2 水位70%仍OK exit0", rc == 0 and "OK" in out, "rc=%s %s" % (rc, err[:60]))
    rc, out, err = run("sample", "--host", "web01", "--path", "/data",
                       "--used", "780", "--total", "1000", "--ts", "2026-09-27T10:00:00+08:00")
    record("T3 sample#3 exit1", rc == 1, "rc=%s out=%s" % (rc, out.strip()[:60]))

    # ---- T4 预测：3样本 日增90KB → free=220KB → 剩 ~2.4 天
    rc, out, err = run("predict", "--host-path", HP)
    ok = rc == 0 and "日增" in out and ("2.4" in out) and "不外推" not in out
    record("T4 predict 3样本外推", ok, "rc=%s | %s | %s"
           % (rc, out.replace("\n", " | ")[:110], err.strip()[:60]))

    # ---- T5 样本不足不外推
    rc, out, err = run("predict", "--host-path", "web01|/boot")
    ok = rc == 2 and "没有" in err
    record("T5 predict 未知对象 exit2", ok, "rc=%s err=%s" % (rc, err.strip()[:60]))

    # ---- T6 check 只读（I3）：mtime 前后不变
    def mtimes():
        snap = {}
        for fn in os.listdir(sb):
            p = os.path.join(sb, fn)
            if os.path.isfile(p):
                snap[fn] = os.stat(p).st_mtime_ns
        return snap

    before = mtimes()
    rc, out, err = run("check", "--host-path", HP)
    after = mtimes()
    record("T6 check exit1 且不写盘(I3)",
           rc == 1 and before == after, "rc=%s changed=%s"
           % (rc, [k for k in after if before.get(k) != after[k]]))

    # ---- T7 判级可复算（I5）：连续两次 check 输出逐字节一致
    rc1, out1, _ = run("check", "--host-path", HP)
    rc2, out2, _ = run("check", "--host-path", HP)
    record("T7 check 幂等(I5)", rc1 == rc2 and out1 == out2 and rc1 == 1,
           "rc=%s/%s same=%s" % (rc1, rc2, out1 == out2))

    # ---- T8 事件只追加（I2）：append-only 结构校验
    ev_file = [f for f in os.listdir(sb) if f.endswith(".cap-events.jsonl")]
    ok = len(ev_file) == 1
    if ok:
        lines1 = read(ev_file[0]).splitlines()
        run("sample", "--host", "web01", "--path", "/data",
            "--used", "790", "--total", "1000", "--ts", "2026-09-27T11:00:00+08:00")
        lines2 = read(ev_file[0]).splitlines()
        ok = len(lines2) == len(lines1) + 1 and lines2[:len(lines1)] == lines1
    record("T8 事件只追加(I2)", ok, "%d→%d 行" % (len(lines1), len(lines2)) if ok else "改写了历史")

    # ---- T9 档零可执行（I1）：档与事件文件不含代码语义
    blob = read(ARCHIVE_NAME) + read(ev_file[0])
    dangerous = [t for t in ("__import__", "eval(", "exec(", "os.system", "subprocess") if t in blob]
    record("T9 档零可执行(I1)", not dangerous, "hits=%s" % dangerous)

    # ---- T10 --live 真采样：本机根盘
    rc, out, err = run("sample", "--host", "selftest-local", "--path", "/",
                       "--live", "--mount", "/")
    record("T10 --live 根盘采样", rc in (0, 1) and ("OK" in out or "WARN" in out or "CRITICAL" in out),
           "rc=%s %s" % (rc, out.strip().splitlines()[0][:70] if out.strip() else err[:60]))

    # ---- T11 --live 未知挂载点 exit2
    rc, out, err = run("sample", "--host", "x", "--path", "/y", "--live",
                       "--mount", "/definitely/not/a/mount/点")
    record("T11 --live 未知挂载 exit2", rc == 2 and "没找到挂载点" in err, "rc=%s" % rc)

    # ---- T12 非法数值 exit2
    rc, out, err = run("sample", "--host", "x", "--path", "/y",
                       "--used", "900", "--total", "100")
    record("T12 used>total exit2", rc == 2 and "非法数值" in err, "rc=%s" % rc)

    # ---- T13 accept 留痕 + log 可读
    rc, out, err = run("accept", "--host-path", HP, "--memo", "容量已扩容计划")
    rc2, out2, _ = run("log", "--host-path", HP, "--n", "3")
    record("T13 accept+log", rc == 0 and rc2 == 0 and "accept" in out2 and "容量已扩容计划" in out2,
           "rc=%s/%s" % (rc, rc2))

    # ---- T14 show 概览
    rc, out, err = run("show")
    record("T14 show 概览", rc == 0 and "/data" in out and "监控对象" in out, "rc=%s" % rc)

    # ---- T15 无档场景
    sb2 = tempfile.mkdtemp(prefix="capguard-empty-")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, SCRIPT, "--root", sb2, "check"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    record("T15 空档 check exit2", p.returncode == 2 and "无档" in p.stderr, "rc=%s" % p.returncode)

    # ---- T16 CRITICAL 判级：水位破 F 线
    rc, out, err = run("sample", "--host", "db01", "--path", "/var",
                       "--used", "950", "--total", "1000", "--ts", "2026-09-27T12:00:00+08:00")
    record("T16 CRITICAL 破F线 exit1", rc == 1 and "CRITICAL" in out, "rc=%s" % rc)

    # ---- T17 accept 无 memo 也能跑（默认路径）
    rc, out, err = run("accept")
    record("T17 accept 全对象", rc == 0 and out.count("accept 已留痕") >= 2, "rc=%s" % rc)

    # ---- T18 无效 ts 格式：被当作字面 ts 存档后 predict 能容错（parse 失败样本被剔除）
    rc, out, err = run("sample", "--host", "edge01", "--path", "/tmp",
                       "--used", "10", "--total", "100", "--ts", "not-a-time")
    rc2, out2, _ = run("predict", "--host-path", "edge01|/tmp")
    # 坏 ts 样本被剔除后 0 样本 → 不外推，predict 退出码 1（待复核）
    record("T18 坏时间戳容错", rc == 0 and rc2 == 1 and "样本0" in out2, "rc=%s/%s" % (rc, rc2))

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    dt = time.time() - t0
    print("\ncapguard selftest: %d/%d PASSED (%.1fs)" % (passed, total, dt))
    if passed != total:
        for name, ok2, detail in results:
            if not ok2:
                print("  FAILED: %s — %s" % (name, detail))
        return EXIT_ERROR
    return EXIT_OK


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog=TOOL,
        description="capguard — 磁盘容量哨兵：水位+走势双轴，增长即报。",
        epilog="退出码：0=安全 1=超线 2=参数/环境错误",
    )
    p.add_argument("--root", default=".", help="档与事件文件的目录（默认当前目录）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, need_path=True):
        sp.add_argument("--host", default="localhost", help="主机名（默认 localhost）")
        sp.add_argument("--path", default="/", help="挂载点/路径标识（默认 /）")
        sp.add_argument("--pct-p", type=float, default=DEFAULT_PCT_P, help="水位 P 线（默认 80）")
        sp.add_argument("--pct-f", type=float, default=DEFAULT_PCT_F, help="水位 F 线（默认 90）")
        sp.add_argument("--days-p", type=float, default=DEFAULT_DAYS_P, help="剩余天数 P 线（默认 14）")
        sp.add_argument("--days-f", type=float, default=DEFAULT_DAYS_F, help="剩余天数 F 线（默认 7）")

    sp = sub.add_parser("sample", help="采一个点：--live 或 --used/--total")
    common(sp)
    sp.add_argument("--live", action="store_true", help="用本机 df 采样")
    sp.add_argument("--mount", default=None, help="--live 时只采这个挂载点")
    sp.add_argument("--used", type=float, default=None, help="已用 KB（手工上报）")
    sp.add_argument("--total", type=float, default=None, help="总容量 KB（手工上报）")
    sp.add_argument("--ts", default=None, help="采样时间（默认现在，ISO含+08:00）——支持回填历史")
    sp.set_defaults(func=cmd_sample)

    sp = sub.add_parser("check", help="复算判级（只读，不写盘）")
    sp.add_argument("--host-path", default=None, help="只看这一个 host|path（默认全部）")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("predict", help="外推还能撑几天（只读）")
    sp.add_argument("--host-path", default=None, help="只看这一个 host|path")
    sp.add_argument("--as-of", default=None, help="回放历史时点（ISO 含 +08:00）")
    sp.set_defaults(func=cmd_predict)

    sp = sub.add_parser("accept", help="人工确认当前水位为可接受的新常态（事件留痕）")
    sp.add_argument("--host-path", default=None, help="只确认这一个 host|path")
    sp.add_argument("--memo", default=None, help="备注（如：已提扩容工单）")
    sp.set_defaults(func=cmd_accept)

    sp = sub.add_parser("log", help="查看追加式事件流")
    sp.add_argument("--host-path", default=None, help="只看这一个 host|path")
    sp.add_argument("--n", type=int, default=None, help="最后 N 条")
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("show", help="档概览：监控对象/采样数/阈值")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("selftest", help="沙箱自测（16+ 断言）")
    sp.set_defaults(func=cmd_selftest)
    return p


def main(argv=None):
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if not os.path.isdir(args.root):
            os.makedirs(args.root, exist_ok=True)
        return args.func(args)
    except RuntimeError as e:
        print("exit 2: %s" % e, file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_ERROR


SCRIPT = os.path.abspath(__file__)

if __name__ == "__main__":
    sys.exit(main())
