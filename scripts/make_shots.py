#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capguard 终端实拍截图生成（证据图，内容 100% 真实）。

用 /usr/bin/python3 跑（系统 python 才有 PIL）。
渲染真实工具输出为 macOS 终端风截图：macOS 窗框 + One Dark 配色。
"""
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
SCRIPT = os.path.join(ROOT, "capguard.py")
OUT_DIR = os.path.join(ROOT, "screenshots")
DEMO_OUT = os.path.join(ROOT, "scripts", "out", "demo-output.txt")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

# One Dark 配色
BG = (40, 44, 52)
FG = (171, 178, 191)
GREEN = (152, 195, 121)
RED = (224, 108, 117)
YELLOW = (229, 192, 123)
BLUE = (97, 175, 239)
GRAY = (92, 99, 112)
TITLEBAR = (58, 62, 70)
FONT_PATH = "/System/Library/Fonts/Menlo.ttc"
CJK_PATH = "/System/Library/Fonts/STHeiti Medium.ttc"
FONT_SIZE = 22
LINE_H = 30
PAD_X = 28
PAD_Y = 52  # 窗框高度+余量
WIN_H = 28

_cjk_font = None


def font():
    return ImageFont.truetype(FONT_PATH, FONT_SIZE)


def cjk():
    global _cjk_font
    if _cjk_font is None:
        _cjk_font = ImageFont.truetype(CJK_PATH, FONT_SIZE)
    return _cjk_font


def has_cjk(s):
    return any('\u4e00' <= ch <= '\u9fff' for ch in s)


def draw_mixed(d, xy, text, fill, max_x):
    """混合字体逐行绘制：含 CJK 用黑体，纯 ASCII 用 Menlo。"""
    x, y = xy
    f = cjk() if has_cjk(text) else font()
    d.text((x, y), text, font=f, fill=fill)


def measure(text):
    f = cjk() if has_cjk(text) else font()
    b = ImageDraw.Draw(Image.new("RGB", (10, 10))).textbbox((0, 0), text, font=f)
    return b[2] - b[0]


def render(lines, out_path, title, width=1280):
    pad = PAD_X
    f_small = ImageFont.truetype(CJK_PATH, 15)
    tmp = Image.new("RGB", (width, 100))
    td = ImageDraw.Draw(tmp)
    h = PAD_Y + len(lines) * LINE_H + 40
    img = Image.new("RGB", (width, h), BG)
    d = ImageDraw.Draw(img)
    # macOS 窗框
    d.rectangle([0, 0, width, WIN_H], fill=TITLEBAR)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([18 + i * 26, 8, 32 + i * 26, 22], fill=c)
    tw = td.textbbox((0, 0), title, font=f_small)[2]
    d.text(((width - tw) // 2, 6), title, font=f_small, fill=GRAY)
    # 内容
    y = PAD_Y
    for text, color in lines:
        draw_mixed(d, (pad, y), text, color, width - pad)
        y += LINE_H
    img.save(out_path)
    print("saved", out_path, img.size)


def colorize(line):
    """给真实输出行按语义上色。返回 (text, color)。"""
    s = line.rstrip("\n")
    if s.startswith("CRITICAL"):
        return (s, RED)
    if s.startswith("WARN"):
        return (s, YELLOW)
    if s.startswith("OK"):
        return (s, GREEN)
    if s.startswith("[PASS]"):
        return (s, GREEN)
    if s.startswith("[FAIL]"):
        return (s, RED)
    if "selftest:" in s and "PASSED" in s:
        return (s, GREEN)
    if s.startswith("capguard") or s.startswith("───"):
        return (s, BLUE)
    if s.strip().startswith("·"):
        return (s, YELLOW)
    if s.startswith(">>>") or s.strip().startswith("#"):
        return (s, GRAY)
    if s.strip().startswith(("日增", "水位", "accept 已留痕")):
        return (s, FG)
    if "$" in s[:4]:
        return (s, FG)
    return (s, FG)


def main():
    # 生成真实输出来源
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.run([sys.executable, os.path.join(HERE, "demo_skewed.py")],
                   capture_output=True, text=True, env=env, timeout=300)

    demo_text = open(DEMO_OUT, "r", encoding="utf-8").read()

    # ---- 截图1：30 秒上手（真实命令+真实输出）----
    sb = "/tmp/capguard-shot-1"
    subprocess.run(["rm", "-rf", sb])
    R = [sys.executable, SCRIPT, "--root", sb]
    real = []
    for label, cmd_args in (
        ("sample --host backup01 --path /backup --used 620 --total 1000 --ts 2026-09-23T10:00:00+08:00",
         ["sample", "--host", "backup01", "--path", "/backup", "--used", "620", "--total", "1000",
          "--ts", "2026-09-23T10:00:00+08:00"]),
        ("sample --host backup01 --path /backup --used 780 --total 1000 --ts 2026-09-25T10:00:00+08:00",
         ["sample", "--host", "backup01", "--path", "/backup", "--used", "780", "--total", "1000",
          "--ts", "2026-09-25T10:00:00+08:00"]),
        ("sample --host backup01 --path /backup --used 920 --total 1000 --ts 2026-09-27T10:00:00+08:00",
         ["sample", "--host", "backup01", "--path", "/backup", "--used", "920", "--total", "1000",
          "--ts", "2026-09-27T10:00:00+08:00"]),
    ):
        p = subprocess.run(R + cmd_args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        real.append(("$ python3 capguard.py " + label, "cmd"))
        if p.returncode not in (0, 1):
            raise RuntimeError("shot-1 采样失败 rc=%s: %s" % (p.returncode, (p.stderr or "")[:200]))
        for ln in p.stdout.splitlines():
            real.append((ln, "out"))
    lines = []
    for t, kind in real:
        if kind == "cmd":
            lines.append((t, BLUE))
        else:
            lines.append(colorize(t))
    render(lines, os.path.join(OUT_DIR, "01-sample.png"), "capguard — sample: 三天采样")

    # ---- 截图2：check 真实偏态巡检 ----
    lines = []
    capture = False
    for ln in demo_text.splitlines():
        if "阶段 2" in ln:
            capture = True
            continue
        if "阶段 3" in ln:
            break
        if capture:
            lines.append(colorize(ln))
    lines = [l for l in lines if l[0].strip()]
    render(lines, os.path.join(OUT_DIR, "02-check.png"), "capguard — check: 10 个对象 1 个异常")

    # ---- 截图3：predict ----
    lines = []
    capture = False
    for ln in demo_text.splitlines():
        if "阶段 3" in ln:
            capture = True
            continue
        if "阶段 4" in ln:
            break
        if capture:
            lines.append(colorize(ln))
    lines = [l for l in lines if l[0].strip()]
    render(lines, os.path.join(OUT_DIR, "03-predict.png"), "capguard — predict: 还能撑几天")

    # ---- 截图4：selftest 全绿 ----
    p = subprocess.run([sys.executable, SCRIPT, "selftest"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    lines = [colorize(ln) for ln in (p.stdout + p.stderr).splitlines() if ln.strip()]
    render(lines, os.path.join(OUT_DIR, "04-selftest.png"), "capguard — selftest 18/18")

    # ---- 截图5：accept + log 审计链 ----
    lines = []
    capture = False
    for ln in demo_text.splitlines():
        if "阶段 5" in ln:
            capture = True
            continue
        if "阶段 6" in ln:
            break
        if capture:
            lines.append(colorize(ln))
    lines = [l for l in lines if l[0].strip()]
    render(lines, os.path.join(OUT_DIR, "05-accept-log.png"), "capguard — accept/log: 审计留痕")

    print("ALL DONE")


if __name__ == "__main__":
    main()
