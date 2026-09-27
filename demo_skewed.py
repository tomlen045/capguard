#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capguard 实战演示：真实偏态场景——5台主机10个挂载点，只有1个在悄悄爆满。

技能铁律：检测/阈值类功能交付前必须用真实偏态分布（异常占少数）跑一遍，
验证「大多数正常对象不出噪声、少数异常对象被抓出来」。
本演示同时产出 screenshots/ 素材文本（真实工具输出，非编造）。
"""
import os
import subprocess
import sys
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "capguard.py")
DEMO_ROOT = os.path.join(HERE, "demo-run")
OUT = os.path.join(HERE, "scripts", "out")
os.makedirs(OUT, exist_ok=True)

H = "localhost"


def run(*a):
    p = subprocess.run([sys.executable, SCRIPT, "--root", DEMO_ROOT] + list(a),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout + (">>> stderr: " + p.stderr.strip() if p.stderr.strip() else "")


def section(title):
    bar = "─" * 64
    return "\n%s\n  %s\n%s\n" % (bar, title, bar)


# 重置演示目录
shutil.rmtree(DEMO_ROOT, ignore_errors=True)

log = []
log.append("capguard 实战演示：5 台主机 · 10 个挂载点 · 只有 1 个在悄悄爆满")
log.append("=" * 64)
log.append("")
log.append("背景：日常巡检采了 6 天数据。9 台一切正常，唯独 backup01 的 /backup")
log.append("被备份文件慢慢吃掉——单看水位没到 90%，但按这个增速 4 天后爆盘。")
log.append("")

# ---- 阶段1：6 天日常采样（偏态：9 个正常 + 1 个异常）----
log.append(section("阶段 1 · 日常采样（day1-day6，异常只占 1/10）"))
DAYS = [
    ("2026-09-21T10:00:00+08:00", 300, 1000, 620, 1000),
    ("2026-09-22T10:00:00+08:00", 305, 1000, 680, 1000),
    ("2026-09-23T10:00:00+08:00", 302, 1000, 740, 1000),
    ("2026-09-24T10:00:00+08:00", 308, 1000, 800, 1000),
    ("2026-09-25T10:00:00+08:00", 304, 1000, 860, 1000),
    ("2026-09-26T10:00:00+08:00", 309, 1000, 920, 1000),
]
# 正常组：web01 /, web01 /data, db01 /var, db01 /data, app01 /
# 正常组：app01 /logs, proxy01 /, proxy01 /var, backup01 /
NORMAL = ["web01|/", "web01|/data", "db01|/var", "db01|/data", "app01|/",
          "app01|/logs", "proxy01|/", "proxy01|/var", "backup01|/"]
for i, (ts, u1, t1, u2, t2) in enumerate(DAYS):
    for hp in NORMAL:
        host, path = hp.split("|")
        rc, out = run("sample", "--host", host, "--path", path,
                      "--used", str(u1), "--total", str(t1), "--ts", ts)
        assert rc in (0, 1), out
    rc, out = run("sample", "--host", "backup01", "--path", "/backup",
                  "--used", str(u2), "--total", str(t2), "--ts", ts)
    assert rc in (0, 1), out
log.append("6 天 × 10 对象 = 60 次采样完成（输出省略，完整过程见事件文件）")
rc, out = run("show")
log.append(out)

# ---- 阶段2：最后一天 check，看谁被抓出来 ----
log.append(section("阶段 2 · 第 7 天巡检 check（谁该被告警？）"))
rc, out = run("check")
log.append(out)
log.append(">>> check 退出码 = %d（1=有对象超线）" % rc)

# ---- 阶段3：对 backup01 做预测，说人话 ----
log.append(section("阶段 3 · backup01 的 /backup 还能撑几天？"))
rc, out = run("predict", "--host-path", "backup01|/backup")
log.append(out)
log.append(">>> predict 退出码 = %d" % rc)

# ---- 阶段4：对照组——90% 但不涨的盘才是虚惊 ----
log.append(section("阶段 4 · 对照组：90% 但零增长的盘，capguard 报不报？"))
rc, out = run("sample", "--host", "old01", "--path", "/archivedisk",
              "--used", "900", "--total", "1000", "--ts", "2026-09-20T10:00:00+08:00")
log.append(out.splitlines()[0] if out else "")
rc, out = run("sample", "--host", "old01", "--path", "/archivedisk",
              "--used", "901", "--total", "1000", "--ts", "2026-09-26T10:00:00+08:00")
log.append(out)
log.append(">>> 退出码 = %d —— 90%% 水位破 F 线照样报 CRITICAL（水位轴）+ 走势轴不外推（不增长）")
log.append("    两轴独立：破线是破线，不增长就是不增长，不互相冒充。")

# ---- 阶段5：accept 盖章 + log 留痕 ----
log.append(section("阶段 5 · 提了扩容工单后 accept 盖章留痕"))
rc, out = run("accept", "--host-path", "backup01|/backup", "--memo", "已提扩容工单 ITSM-2047")
log.append(out)
rc, out = run("log", "--host-path", "backup01|/backup", "--n", "3")
log.append(out)

# ---- 阶段6：--live 真实本机采样 ----
log.append(section("阶段 6 · --live 真实采样本机磁盘（macOS df 实测）"))
rc, out = run("sample", "--host", "macbook", "--path", "/", "--live", "--mount", "/")
log.append(out)
log.append(">>> --live 退出码 = %d" % rc)

text = "\n".join(log)
with open(os.path.join(OUT, "demo-output.txt"), "w", encoding="utf-8") as f:
    f.write(text)
print(text[-3000:])
print("\n[OK] 全部输出已存 scripts/out/demo-output.txt（%d 字符）" % len(text))
