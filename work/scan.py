#!/usr/bin/env python3
"""手机照片 -> 扫描件: 文档检测 + 透视矫正 + 光照归一化增强. v2

改进: warp 后边缘裁修(去背景残留) / 大核背景估计 / per-channel percentile 拉伸 /
投影法 deskew / Otsu 黑白版 / 可选旋转 90 度.
"""
import argparse
import os
import sys

import cv2
import numpy as np


def order_points(pts):
    """按 TL, TR, BR, BL 排序 4 个点."""
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    d = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(d)]
    rect[3] = pts[np.argmax(d)]
    return rect


def find_document_quad(img, debug_dir=None, name=""):
    """定位纸张四边形(原图坐标). 亮区分割 -> 最大轮廓 -> 四点/minAreaRect."""
    h, w = img.shape[:2]
    scale = 1500.0 / max(h, w)
    small = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.dilate(mask, kernel, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, "no contours"
    cnt = max(contours, key=cv2.contourArea)
    if cv2.contourArea(cnt) / (sh * sw) < 0.15:
        return None, "largest contour too small"

    touches_border = any(
        x <= 2 or y <= 2 or x >= sw - 3 or y >= sh - 3
        for pt in cnt for x, y in [pt[0]]
    )

    peri = cv2.arcLength(cnt, True)
    quad = None
    if not touches_border:
        for eps in (0.01, 0.02, 0.03, 0.05):
            approx = cv2.approxPolyDP(cnt, eps * peri, True)
            if len(approx) == 4:
                quad = approx.reshape(4, 2).astype(np.float32)
                break
    if quad is None:
        box = cv2.boxPoints(cv2.minAreaRect(cnt))
        quad = box.astype(np.float32)
        # minAreaRect 贴边时可能略外扩, 向内收缩 1% 防止带入背景
        c = quad.mean(axis=0)
        quad = c + (quad - c) * 0.99

    quad = order_points(quad)
    if debug_dir:
        dbg = small.copy()
        cv2.drawContours(dbg, [quad.astype(np.int32)], -1, (0, 0, 255), 3)
        cv2.imwrite(os.path.join(debug_dir, f"debug_{name}.jpg"), dbg)
    return quad / scale, ("border" if touches_border else "quad")


def warp_document(img, quad, out_w=None):
    (tl, tr, br, bl) = quad
    w = int(round(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl))))
    h = int(round(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl))))
    if out_w:
        r = out_w / w
        w, h = int(w * r), int(h * r)
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(quad, dst)
    return cv2.warpPerspective(img, M, (w, h), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def trim_border(warped):
    """裁掉 warp 后残留的深色背景/黑边: Otsu 找纸面 bbox, 内缩 1% 裁剪."""
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    # 纸面亮: 用偏高阈值分离, 避免浅色文字干扰 bbox
    thr = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    thr = cv2.morphologyEx(thr, cv2.MORPH_CLOSE,
                           cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)))
    cnts, _ = cv2.findContours(thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return warped
    x, y, w, h = cv2.boundingRect(max(cnts, key=cv2.contourArea))
    if w * h < 0.5 * warped.shape[0] * warped.shape[1]:
        return warped
    mx, my = int(w * 0.01), int(h * 0.01)
    x0, y0 = min(x + mx, w), min(y + my, h)
    x1, y1 = max(x + w - mx, 0), max(y + h - my, 0)
    return warped[y0:y1, x0:x1]


def trim_document(warped):
    """裁掉 quad 外扩带入的深色背景边条.

    卡片几乎占满取景框时, 自动角点常贴到画面边缘, warp 后带入一圈深色
    背景+镜头暗角(身份证实测带入 11mm 条带, 观感像"未消除的阴影")。
    按行/列的亮像素覆盖率定位实心亮区(卡面/纸面)的边界, 四边独立裁剪。
    覆盖率法比轮廓 bbox 稳: 背景条带里混有细亮纹理(实测 28% 亮像素)会把
    轮廓连到画面边缘, 但其列覆盖率(20-37%)远低于卡面列(68-100%)。
    防护: coons 越界补白(纯 255)不算内容; 某边无 ≥1.5% 宽的低覆盖边带
    (选区贴合)或裁后 <40% 时不裁该边。
    """
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape[:2]
    thr = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    thr[gray >= 250] = 0
    thr = cv2.morphologyEx(thr, cv2.MORPH_OPEN,
                           cv2.getStructuringElement(cv2.MORPH_RECT, (21, 21)))
    mask = thr > 0

    def edge(cov, n, min_band, min_keep):
        """返回该边的裁剪起点(背景带 ≥1.5% 宽才裁), 无则 0/n."""
        idx = np.where(cov >= 0.5)[0]
        if len(idx) < n * min_keep:      # 亮区太少, 疑似误检, 不裁
            return 0, n
        a, b = idx[0], idx[-1] + 1
        if a > n * min_band:
            a = max(0, a - int(n * 0.005))
        else:
            a = 0                        # 贴边: 选区贴合, 不裁
        if b < n * (1 - min_band):
            b = min(n, b + int(n * 0.005))
        else:
            b = n
        return a, b

    x0, x1 = edge(mask.mean(axis=0), W, 0.015, 0.40)
    y0, y1 = edge(mask.mean(axis=1), H, 0.015, 0.40)
    if (x0, y0, x1, y1) == (0, 0, W, H):
        return warped
    return warped[y0:y1, x0:x1]


def illumination_normalize(bgr):
    """除法法光照归一化 + 行列白点剖面二段校正 + percentile 拉伸. 返回 (彩色, 灰度).

    性能: 光照是低频信号, 背景在长边≤600px 的缩略图上估计后放大回原尺寸 —
    视觉等效, 但避免在全分辨率图上跑超大核形态学(8MP 时要数秒)。
    两类问题与对策(均在真实身份证照片上实测标定):
    1. 过曝/人像洗白: 小核的背景估计跟着卡面人像(约占卡宽 1/3)变暗, 除法
       把人像当欠曝整体提亮(P20 到 218, 全卡发白)。→ 核加大到 min 边 1/2
       (矩形核可分离, 大核也快), 估计出纯光照场。
    2. 阴影残留: 大核会连"窄于核的阴影条"一起桥接掉, 拍摄时的手指/机身
       阴影(实测 120px 带)不再被消除。→ 二段行列白点剖面: 阴影条会拉低所在
       列(或行)全程 P92, 而人像/文字块只占列高的小比例不影响列 P92, 天然
       区分内容与阴影; 增益场 = 行剖面 ⊗ 列剖面(只提升, 上限 1.8)。
    另: 背景场用中值平滑(高斯会把阴影台阶糊成渐变带); 除法增益钳制(≤2.4)
    与拉伸黑点保护(lo ≤ hi*0.35)兜底极端情况。
    """
    h, w = bgr.shape[:2]
    scale = 600.0 / max(h, w)
    if scale < 1.0:
        small = cv2.resize(bgr, (max(1, round(w * scale)), max(1, round(h * scale))),
                           interpolation=cv2.INTER_AREA)
    else:
        small = bgr
    sh, sw = small.shape[:2]
    ksize = max(41, min(sh, sw) // 2) | 1
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (ksize, ksize))

    def bg_chan(ch):
        bg = cv2.morphologyEx(ch, cv2.MORPH_CLOSE, k)
        return cv2.medianBlur(bg, 31)

    bgs = [bg_chan(c) for c in cv2.split(small)]
    if scale < 1.0:
        bgs = [cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR) for bg in bgs]

    GAIN_MAX = 2.4
    chans = []
    for c, bg in zip(cv2.split(bgr), bgs):
        gain = np.clip(255.0 / np.maximum(bg.astype(np.float32), 1.0), 1.0, GAIN_MAX)
        chans.append(np.clip(c.astype(np.float32) * gain, 0, 255).astype(np.uint8))
    color = cv2.merge(chans)

    # 二段: 行/列可分离白点剖面 — 修大核桥接掉的窄阴影条(内容块不影响轴向 P92)
    gsm = cv2.resize(cv2.cvtColor(color, cv2.COLOR_BGR2GRAY), (sw, sh))
    ref = np.percentile(gsm, 92)

    def axis_profile(axis_len, along_rows):
        step = 12
        half = step // 2
        centers = np.arange(half, axis_len, step)
        vals = []
        for c in centers:
            lo, hi = max(0, c - half), min(axis_len, c + half)
            strip = gsm[lo:hi, :] if along_rows else gsm[:, lo:hi]
            vals.append(np.percentile(strip, 92))
        g = np.clip(ref / np.maximum(np.array(vals, np.float32), 1.0), 1.0, 1.8)
        if len(g) > 5:  # 阴影是低频的, 轻微平滑去采样纹
            g = cv2.GaussianBlur(g.reshape(1, -1), (0, 0), 0.8).ravel()
        return np.interp(np.arange(axis_len), centers, g).astype(np.float32)

    gain_col = axis_profile(sw, along_rows=False)
    gain_row = axis_profile(sh, along_rows=True)
    field = cv2.resize(gain_row[:, None] * gain_col[None, :], (w, h),
                       interpolation=cv2.INTER_LINEAR)[..., None]
    color = np.clip(color.astype(np.float32) * field, 0, 255).astype(np.uint8)

    # per-channel percentile 线性拉伸: 底色→接近纯白, 抗极值。
    # 黑点保护: 卡面文字/人像占比小, P3 本身偏高, 直接拉伸会把中间调碾到黑;
    # 限制 lo 不超过 hi 的 35%。
    out_ch = []
    for c in cv2.split(color):
        lo, hi = np.percentile(c, 3), np.percentile(c, 97)
        hi = max(hi, 200.0)
        lo = min(lo, hi * 0.35)
        if hi - lo < 10:
            out_ch.append(c)
            continue
        c = (c.astype(np.float32) - lo) * (255.0 / (hi - lo))
        out_ch.append(np.clip(c * 0.96, 0, 255).astype(np.uint8))  # 0.96 留一点灰度层次
    color = cv2.merge(out_ch)
    gray = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
    return color, gray


def deskew(gray, max_angle=3.0, step=0.5):
    """投影法微旋转校正: 文字行水平投影方差最大的角度即正."""
    thr = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    thr = cv2.medianBlur(thr, 3)
    best_a, best_v = 0.0, -1.0
    h, w = thr.shape
    scale = 1200.0 / max(h, w)
    if scale < 1:
        thr = cv2.resize(thr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_NEAREST)
    for a in np.arange(-max_angle, max_angle + 1e-9, step):
        M = cv2.getRotationMatrix2D((thr.shape[1] / 2, thr.shape[0] / 2), a, 1.0)
        rot = cv2.warpAffine(thr, M, (thr.shape[1], thr.shape[0]), flags=cv2.INTER_NEAREST)
        v = rot.sum(axis=1).var()  # 行投影方差: 行对齐时峰谷差最大
        if v > best_v:
            best_v, best_a = v, a
    if abs(best_a) < 0.1:
        return gray, 0.0
    M = cv2.getRotationMatrix2D((w / 2, h / 2), best_a, 1.0)
    return cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE), best_a


def bw_scan(gray_norm):
    """黑白版: 光照已归一化, Otsu 全局阈值即可干净二值化."""
    return cv2.threshold(gray_norm, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]


def process(path, outdir, debug_dir, out_w=2000, rot90=0):
    name = os.path.splitext(os.path.basename(path))[0]
    img = cv2.imread(path)
    if img is None:
        return None, "cannot read"

    quad, mode = find_document_quad(img, debug_dir, name)
    if quad is None:
        return None, mode
    warped = warp_document(img, quad, out_w)
    warped = trim_border(warped)
    if rot90:
        warped = np.rot90(warped, rot90 // 90).copy()
    color, gray = illumination_normalize(warped)
    gray, angle = deskew(gray)
    # deskew 角度同样应用到彩色版
    if abs(angle) >= 0.1:
        h, w = color.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        color = cv2.warpAffine(color, M, (w, h), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)
    bw = bw_scan(gray)

    base = os.path.join(outdir, name)
    cv2.imwrite(f"{base}_color.jpg", color, [cv2.IMWRITE_JPEG_QUALITY, 92])
    cv2.imwrite(f"{base}_bw.jpg", bw, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return mode, (f"{base}_color.jpg/{base}_bw.jpg "
                  f"{color.shape[1]}x{color.shape[0]} deskew={angle:+.1f} rot90={rot90}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--outdir", default="output")
    ap.add_argument("-w", "--width", type=int, default=2000)
    ap.add_argument("--rot90", type=int, default=0, choices=[0, 90, -90, 180],
                    help="矫正后整体旋转(图3内容横置时用)")
    args = ap.parse_args()

    debug_dir = os.path.join(args.outdir, "debug")
    os.makedirs(debug_dir, exist_ok=True)
    ok = True
    for p in args.inputs:
        mode, info = process(p, args.outdir, debug_dir, args.width, args.rot90)
        print(f"[{'OK ' if mode else 'FAIL'}] {p}: mode={mode} {info}")
        ok = ok and bool(mode)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
