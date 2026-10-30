#!/usr/bin/env python3
"""DocLens — Microsoft Lens 风格文档扫描服务.

用法: python3 server.py [--input dir] [--out dir] [--port 8765]

功能: 静态前端 + JSON API (自动角点/曲线边缘检测, Coons patch 畸变校正,
光照增强, 黑白扫描, Vision OCR, PDF 导出).
坐标约定: 全部使用原图全分辨率像素坐标。
corners = [TL, TR, BR, BL]
edges = {top:[TL..TR], right:[TR..BR], bottom:[BL..BR], left:[TL..BL]}
每条边为首尾(角点)+若干内部控制点的折线列表, 渲染时按 Catmull-Rom 曲线过点。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

import scan  # 复用 v2 的角点检测与增强算法

ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------------- App 配置(独立于工程目录) ----------------

APP_SUPPORT = os.path.expanduser("~/Library/Application Support/DocLens")
CONFIG_PATH = os.path.join(APP_SUPPORT, "config.json")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    os.makedirs(APP_SUPPORT, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def default_save_dir():
    d = os.path.expanduser("~/Documents/DocLens")
    os.makedirs(d, exist_ok=True)
    return d


def pages_dir():
    """单页扫描件暂存目录(中间产物): 系统缓存目录, 按项目隔离."""
    d = os.path.expanduser(f"~/Library/Caches/DocLens/{CUR_PID}")
    os.makedirs(d, exist_ok=True)
    return d


def save_dialog(default_dir, default_name):
    """原生保存对话框(可输入文件名), 返回目标完整路径或 None(取消)."""
    try:
        os.makedirs(default_dir, exist_ok=True)
    except Exception:
        default_dir = os.path.expanduser("~/Documents")
    script = (f'set f to choose file name with prompt "导出 PDF" '
              f'default name "{default_name}" '
              f'default location (POSIX file "{default_dir}")\n'
              'return POSIX path of f')
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=600)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


CONFIG = load_config()
# 向后兼容: 旧版 save_dir 作为首次 PDF 导出目录的初值
if "save_dir" in CONFIG and "last_pdf_dir" not in CONFIG:
    CONFIG["last_pdf_dir"] = CONFIG.pop("save_dir")
    save_config(CONFIG)
# 图片注册表: app 模式下照片来源不绑定工程 input 目录, {id: {path,w,h}}
# 持久化到 App Support/photos.json — App 重启后已添加的照片不丢失
PHOTOS_PATH = os.path.join(APP_SUPPORT, "photos.json")
PHOTO_REG = {}
PHOTO_LOCK = threading.Lock()

# 标注注册表: {imgId: {corners,edges,mode,pads,style,rot,label,done,ocrText}}
# 持久化到 App Support/annotations.json — 重启后角点/边缘等提取结果不丢,
# 切图回来也直接恢复, 不必重新自动检测
ANNOS_PATH = os.path.join(APP_SUPPORT, "annotations.json")
ANNOS = {}
ANNO_LOCK = threading.Lock()

# ---------------- 多项目 ----------------
# 每个项目独立: 照片注册表/标注/扫描页缓存/PDF 记忆目录。
# 存储: App Support/projects.json (元数据+当前项目) + projects/{pid}/ 下各 json,
#       页缓存在 ~/Library/Caches/DocLens/{pid}/。
# 旧版单项目数据(全局 photos.json/annotations.json/缓存根目录)首次启动迁移进默认项目。

PROJECTS_PATH = os.path.join(APP_SUPPORT, "projects.json")
PROJECTS_DIR = os.path.join(APP_SUPPORT, "projects")
PROJECTS = {}          # {pid: {"name":..., "created":...}}
CUR_PID = None         # 当前项目 id


def _now():
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def load_projects():
    """读 projects.json; 不存在时初始化默认项目并迁移旧版全局数据."""
    global CUR_PID
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    try:
        with open(PROJECTS_PATH, encoding="utf8") as f:
            meta = json.load(f)
        PROJECTS.update(meta.get("projects", {}))
        CUR_PID = meta.get("current")
    except Exception:
        pass
    if not PROJECTS:
        pid = "default"
        PROJECTS[pid] = {"name": "默认项目", "created": _now()}
        CUR_PID = pid
        save_projects()
        migrate_legacy_data(pid)
    if CUR_PID not in PROJECTS:
        CUR_PID = sorted(PROJECTS)[0]
        save_projects()


def save_projects():
    try:
        with open(PROJECTS_PATH, "w", encoding="utf8") as f:
            json.dump({"projects": PROJECTS, "current": CUR_PID}, f,
                      ensure_ascii=False, indent=2)
    except Exception:
        pass


def migrate_legacy_data(pid):
    """旧版全局 photos.json/annotations.json/缓存根目录 -> 项目目录。一次性."""
    pdir = os.path.join(PROJECTS_DIR, pid)
    os.makedirs(pdir, exist_ok=True)
    for name in ("photos.json", "annotations.json"):
        src = os.path.join(APP_SUPPORT, name)
        dst = os.path.join(pdir, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    # 旧缓存根目录的扫描页 -> Caches/DocLens/{pid}/
    old_cache = os.path.expanduser("~/Library/Caches/DocLens")
    new_cache = os.path.expanduser(f"~/Library/Caches/DocLens/{pid}")
    if os.path.isdir(old_cache) and not os.path.isdir(new_cache):
        entries = [f for f in os.listdir(old_cache)
                   if f.endswith(".jpg") or f.endswith(".pdf")]
        if entries:
            os.makedirs(new_cache, exist_ok=True)
            for f in entries:
                try:
                    os.replace(os.path.join(old_cache, f), os.path.join(new_cache, f))
                except OSError:
                    pass


def project_dir(pid=None):
    return os.path.join(PROJECTS_DIR, pid or CUR_PID)


def switch_project(pid):
    """切换当前项目: 重新加载该项目照片注册表与标注, 更新页缓存目录."""
    global CUR_PID, PHOTO_REG, ANNOS
    if pid not in PROJECTS:
        raise ValueError(f"no such project: {pid}")
    CUR_PID = pid
    save_projects()
    with PHOTO_LOCK:
        PHOTO_REG = {}
    with ANNO_LOCK:
        ANNOS = {}
    _load_project_data()
    return {"pid": pid, "name": PROJECTS[pid]["name"]}


def create_project(name):
    pid = f"p{int(__import__('time').time() * 1000) % 10**9:09d}"
    while pid in PROJECTS:
        pid += "x"
    PROJECTS[pid] = {"name": name or f"项目{len(PROJECTS) + 1}", "created": _now()}
    pdir = project_dir(pid)
    os.makedirs(pdir, exist_ok=True)
    # 落盘空注册表: 标记该项目"已显式管理", 开发模式兜底扫描不再侵入
    for n in ("photos.json", "annotations.json"):
        fp = os.path.join(pdir, n)
        if not os.path.exists(fp):
            with open(fp, "w", encoding="utf8") as f:
                json.dump({}, f)
    save_projects()
    return pid


def delete_project(pid):
    """删除项目: 清数据目录与页缓存; 至少保留一个项目(删最后一个则建新的空项目)."""
    global CUR_PID
    if pid not in PROJECTS:
        raise ValueError(f"no such project: {pid}")
    del PROJECTS[pid]
    import shutil
    shutil.rmtree(project_dir(pid), ignore_errors=True)
    shutil.rmtree(os.path.expanduser(f"~/Library/Caches/DocLens/{pid}"),
                  ignore_errors=True)
    if CUR_PID == pid:
        if PROJECTS:
            switch_project(sorted(PROJECTS)[0])
        else:
            new = create_project("默认项目")
            switch_project(new)
    save_projects()


def _load_project_data():
    """按当前项目路径(重)载照片注册表与标注."""
    global PHOTO_REG, ANNOS
    pdir = project_dir()
    try:
        with open(os.path.join(pdir, "photos.json"), encoding="utf8") as f:
            reg = json.load(f)
        with PHOTO_LOCK:
            PHOTO_REG = {k: r for k, r in reg.items() if os.path.exists(r["path"])}
            global PHOTOS_PATH
            PHOTOS_PATH = os.path.join(pdir, "photos.json")
    except Exception:
        PHOTOS_PATH = os.path.join(pdir, "photos.json")
    with ANNO_LOCK:
        ANNOS = {}
        global ANNOS_PATH
        ANNOS_PATH = os.path.join(pdir, "annotations.json")
        try:
            with open(ANNOS_PATH, encoding="utf8") as f:
                ANNOS.update(json.load(f))
        except Exception:
            pass


def load_annos():
    with ANNO_LOCK:
        ANNOS.update({})
    _load_project_data()


def persist_annos():
    try:
        with ANNO_LOCK:
            snap = dict(ANNOS)
        with open(ANNOS_PATH, "w", encoding="utf8") as f:
            json.dump(snap, f, ensure_ascii=False)
    except Exception:
        pass


def load_photos():
    """启动时恢复注册表 — 多项目模式下由 _load_project_data 统一处理."""
    if PROJECTS:
        return
    try:
        with open(PHOTOS_PATH, encoding="utf8") as f:
            reg = json.load(f)
        for pid, r in reg.items():
            if os.path.exists(r["path"]):
                PHOTO_REG[pid] = r
    except Exception:
        pass


def persist_photos():
    try:
        os.makedirs(APP_SUPPORT, exist_ok=True)
        with open(PHOTOS_PATH, "w", encoding="utf8") as f:
            json.dump(PHOTO_REG, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def register_photo(path):
    """注册一张照片(heic 先转 jpg 缓存到 App Support), 返回 id 与尺寸."""
    pid = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1].lower()
    with PHOTO_LOCK:
        if pid in PHOTO_REG:
            return PHOTO_REG[pid]
        if ext in (".heic", ".heif"):
            cache_dir = os.path.join(APP_SUPPORT, "photos")
            os.makedirs(cache_dir, exist_ok=True)
            cache = os.path.join(cache_dir, pid + ".jpg")
            if not os.path.exists(cache):
                subprocess.run(["sips", "-s", "format", "jpeg", path, "--out", cache],
                               check=True, stdout=subprocess.DEVNULL)
            path = cache
        img = cv2.imread(path)
        if img is None:
            raise ValueError(f"cannot read image: {path}")
        PHOTO_REG[pid] = {"path": path, "w": img.shape[1], "h": img.shape[0]}
        persist_photos()
        return PHOTO_REG[pid]


def choose_dir_dialog(default_dir=None):
    """原生目录选择对话框(osascript), 返回所选目录或 None.
    default_dir: 上次导出目录(记忆), 无则 Documents。"""
    loc = f'(POSIX file "{default_dir}")' if default_dir else "(path to documents folder)"
    script = (
        'set chosen to choose folder with prompt "选择扫描件保存目录" '
        f'default location {loc}\n'
        'return POSIX path of chosen'
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rstrip("/")
    return None


def open_files_dialog():
    """原生文件选择对话框(多选图片), 返回路径列表."""
    script = (
        'set chosen to choose file of type {"public.image"} with multiple selections allowed '
        'with prompt "选择要扫描的照片"\n'
        'set out to ""\n'
        'repeat with f in chosen\n'
        '  set out to out & POSIX path of f & linefeed\n'
        'end repeat\n'
        'return out'
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

# ---------------- 曲线几何 ----------------

def catmull_rom(points, per_seg=40):
    """Catmull-Rom 样条插值, points (n,2) -> dense (m,2)."""
    P = np.asarray(points, float)
    if len(P) < 2:
        return P
    if len(P) == 2:
        t = np.linspace(0, 1, per_seg)[:, None]
        return P[0] * (1 - t) + P[1] * t
    # 端点条件: 只做一次复制(与前端 JS catmullRom 一致)。
    # 此前曾在此再叠虚拟外推点, 产生首尾 P0→P0 退化段: 曲线在角点处冲出选区再折回,
    # Coons 弧长重采样把选区外内容正反各扫一遍 = 预览四周的镜像环。勿再加外推。
    Pt = np.vstack([P[:1], P, P[-1:]])
    segs = []
    for i in range(len(Pt) - 3):
        p0, p1, p2, p3 = Pt[i], Pt[i + 1], Pt[i + 2], Pt[i + 3]
        t = np.linspace(0, 1, per_seg, endpoint=False)[:, None]
        seg = 0.5 * ((2 * p1) + (-p0 + p2) * t
                     + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t ** 2
                     + (-p0 + 3 * p1 - 3 * p2 + p3) * t ** 3)
        segs.append(seg)
    segs.append(P[-1:])
    return np.vstack(segs)


def arclen(dense):
    d = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(dense, axis=0), axis=1))])
    return d


def resample_arclen(dense, n):
    """按弧长均匀重采样为 n 点."""
    d = arclen(dense)
    if d[-1] <= 0:
        return np.repeat(dense[:1], n, axis=0)
    t = d / d[-1]
    u = np.linspace(0, 1, n)
    return np.stack([np.interp(u, t, dense[:, 0]),
                     np.interp(u, t, dense[:, 1])], axis=1)


# ---------------- 边缘曲线检测 ----------------

def sample_profile(gray, base, nrm, offsets):
    """沿法向双线性采样亮度剖面."""
    xs = base[0] + offsets * nrm[0]
    ys = base[1] + offsets * nrm[1]
    h, w = gray.shape
    x0 = np.floor(xs).astype(int)
    y0 = np.floor(ys).astype(int)
    valid = (x0 >= 0) & (y0 >= 0) & (x0 < w - 1) & (y0 < h - 1)
    x0c, y0c = np.clip(x0, 0, w - 2), np.clip(y0, 0, h - 2)
    fx, fy = xs - x0c, ys - y0c
    p00, p01 = gray[y0c, x0c].astype(float), gray[y0c, x0c + 1].astype(float)
    p10, p11 = gray[y0c + 1, x0c].astype(float), gray[y0c + 1, x0c + 1].astype(float)
    val = (p00 * (1 - fx) * (1 - fy) + p01 * fx * (1 - fy)
           + p10 * (1 - fx) * fy + p11 * fx * fy)
    return np.where(valid, val, np.nan)


def edge_curve_points(gray, A, B):
    """在 A->B 连线法向搜索明暗跳变, 返回 5 个控制点 [A,c1,c2,c3,B]."""
    A, B = np.asarray(A, float), np.asarray(B, float)
    L = np.linalg.norm(B - A)
    if L < 10:
        return np.array([A, B])
    d = (B - A) / L
    nrm = np.array([-d[1], d[0]])
    R = max(24, int(0.035 * min(gray.shape)))
    offsets = np.arange(-R, R + 1, 2.0)
    if len(offsets) < 7:
        return np.array([A, B])
    kern = np.array([1, 2, 3, 4, 3, 2, 1], float)
    kern /= kern.sum()

    detected = []
    for s in np.linspace(0.1, 0.9, 7):
        base = A + s * (B - A)
        prof = sample_profile(gray, base, nrm, offsets)
        ok = ~np.isnan(prof)
        if ok.sum() < len(prof) * 0.6:  # 大半出图 -> 该采样点放弃
            continue
        prof = np.where(ok, prof, np.nanmedian(prof))
        # edge 复制填充卷积: 零填充会在剖面两端伪造暗边, 诱使 argmax 锁到窗口边缘
        pk = np.pad(prof, len(kern) // 2, mode="edge")
        sm = np.convolve(pk, kern, mode="valid")
        grad = np.gradient(sm)
        k2 = np.array([1, 2, 1], float) / 3
        gsm = np.convolve(np.pad(grad, 1, mode="edge"), k2, mode="valid")
        cand = np.argmax(np.abs(gsm[2:-2])) + 2
        contrast = sm[:cand].mean() - sm[cand:].mean()
        if abs(contrast) < 18:  # 对比度太弱, 不可信
            continue
        detected.append(base + offsets[cand] * nrm)
    detected.insert(0, A)   # 端点=角点: Coons 要求边与角点闭合,
    detected.append(B)      # 检测内部点若偏移会让面片在角部渲染选区外内容
    if len(detected) < 3:
        mid = (A + B) / 2
        return np.array([A, (A + mid) / 2, mid, (mid + B) / 2, B])
    dense = catmull_rom(np.array(detected))
    return resample_arclen(dense, 5)


def detect_document(img):
    """自动角点 + 四条边曲线检测."""
    quad, mode = scan.find_document_quad(img)
    h, w = img.shape[:2]
    if quad is None:
        m = 0.10  # 检测失败兜底: 居中 80% 四边形, 角点留在画面内便于拖拽
        quad = np.array([[w * m, h * m], [w * (1 - m), h * m],
                         [w * (1 - m), h * (1 - m)], [w * m, h * (1 - m)]], float)
    else:
        quad = np.asarray(quad, float)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    pairs = [("top", 0, 1), ("right", 1, 2), ("bottom", 3, 2), ("left", 0, 3)]
    edges = {}
    for name, ia, ib in pairs:
        edges[name] = edge_curve_points(gray, quad[ia], quad[ib]).tolist()
    return {"corners": quad.tolist(), "edges": edges, "mode": mode or "manual"}


# ---------------- Coons patch 畸变校正 ----------------

def coons_remap(src, corners, edges, out_w):
    """双线性 Coons patch: 四条边界曲线插值出 (u,v)->(x,y) 映射并 remap.

    边界外(角点在图外)的区域填白 —— 即"没拍到的纸角"用白色补齐。
    返回 (color, gray), 已按 out_w 缩放。
    """
    corners = np.asarray(corners, float).reshape(4, 2)
    # 防御: 边端点强制钉在角点上。Coons 公式的边界连续性依赖此约定,
    # 前端任何同步缺陷导致端点漂移时, 面片会在角部折叠渲染出选区外内容。
    pairs = {"top": (0, 1), "right": (1, 2), "bottom": (3, 2), "left": (0, 3)}
    edges = {k: np.asarray(v, float).reshape(-1, 2) for k, v in edges.items()}
    for name, (ia, ib) in pairs.items():
        edges[name][0] = corners[ia]
        edges[name][-1] = corners[ib]
    top_d = catmull_rom(edges["top"])
    bottom_d = catmull_rom(edges["bottom"])
    left_d = catmull_rom(edges["left"])
    right_d = catmull_rom(edges["right"])
    W0 = int(round(max(arclen(top_d)[-1], arclen(bottom_d)[-1])))
    H0 = int(round(max(arclen(left_d)[-1], arclen(right_d)[-1])))
    if out_w:
        r = out_w / W0
        W, H = int(W0 * r), int(H0 * r)
    else:
        W, H = W0, H0

    topW = resample_arclen(top_d, W)      # TL->TR
    botW = resample_arclen(bottom_d, W)   # BL->BR
    leftH = resample_arclen(left_d, H)    # TL->BL
    rightH = resample_arclen(right_d, H)  # TR->BR
    P00, P10, P11, P01 = corners  # TL TR BR BL

    u = np.linspace(0, 1, W)[None, :, None]
    v = np.linspace(0, 1, H)[:, None, None]
    S = ((1 - v) * topW[None, :, :] + v * botW[None, :, :]
         + (1 - u) * leftH[:, None, :] + u * rightH[:, None, :]
         - ((1 - u) * (1 - v) * P00 + u * (1 - v) * P10
            + u * v * P11 + (1 - u) * v * P01))
    map_x = S[..., 0].astype(np.float32)
    map_y = S[..., 1].astype(np.float32)
    out = cv2.remap(src, map_x, map_y, cv2.INTER_CUBIC,
                    cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    return out


def render_scan(src, corners, edges, pads, out_w, mode="color", rot=0, doc_wh=None):
    """Coons 校正 + 裁背景 + 比例校正 + 旋转 + pad 白边 + 光照增强.

    pads={t,r,b,l} 为占输出宽高的比例. rot 为顺时针角度 0/90/180/270,
    作用于矫正后输出(标注坐标始终在原图系).
    doc_wh=(w,h)mm 为文档类型的物理尺寸: 比例偏差>4%时按真实比例校正
    (角点贴画面边缘时矫正的是"取景框"而非卡片, 斜拍的透视压缩会残留).
    """
    color, gray = render_scan_pair(src, corners, edges, pads, out_w, rot, doc_wh)
    if mode == "original":
        return coons_remap(src, corners, edges, out_w)  # 仅调试用
    return gray if mode == "gray" else color


def render_scan_pair(src, corners, edges, pads, out_w, rot=0, doc_wh=None):
    """单次渲染同时产出 (彩色增强, 灰度增强): warp/裁背景/比例/旋转/pad/归一化各只跑一遍."""
    warped = coons_remap(src, corners, edges, out_w)
    warped = scan.trim_document(warped)   # 裁掉选区外扩带入的深色背景边条
    k = ({0: 0, 90: -1, 180: 2, 270: 1})[int(rot) % 360]  # np.rot90 k: CCW 步数
    if k:
        warped = np.ascontiguousarray(np.rot90(warped, k))
    if doc_wh:   # 按文档类型真实比例校正(长边为基准, 短边缩放到目标比例)
        tgt = max(doc_wh) / min(doc_wh)
        h, w = warped.shape[:2]
        cur = max(w, h) / min(w, h)
        # 容差 0.5%: 同类型页面比例完全一致, 排版落版尺寸才一致(户口本实测
        # 1.373/1.390 两页落版差 1.2mm; 4% 容差下各页比例仍随拍摄抖动)
        if max(cur / tgt, tgt / cur) > 1.005:
            if w >= h:
                h2 = max(1, round(w / tgt))
                if h2 != h:
                    warped = cv2.resize(warped, (w, h2), interpolation=cv2.INTER_CUBIC)
            else:
                w2 = max(1, round(h / tgt))
                if w2 != w:
                    warped = cv2.resize(warped, (w2, h), interpolation=cv2.INTER_CUBIC)
    h, w = warped.shape[:2]
    pt = int(h * pads.get("t", 0)); pb = int(h * pads.get("b", 0))
    pl = int(w * pads.get("l", 0)); pr = int(w * pads.get("r", 0))
    if pt or pb or pl or pr:
        warped = cv2.copyMakeBorder(warped, pt, pb, pl, pr,
                                    cv2.BORDER_CONSTANT, value=(255, 255, 255))
    return scan.illumination_normalize(warped)


# ---------------- OCR / PDF ----------------

OCR_BIN = None
OCR_LOCK = threading.Lock()


def ocr_file(path):
    """调用 Vision OCR (本地), 返回文本."""
    global OCR_BIN
    with OCR_LOCK:
        if OCR_BIN is None:
            bin_path = os.path.join(ROOT, "ocr_bin")
            try:
                subprocess.run(["swiftc", os.path.join(ROOT, "ocr.swift"), "-o", bin_path],
                               check=True, timeout=180,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                OCR_BIN = bin_path
            except Exception:
                OCR_BIN = "swift"
    if OCR_BIN == "swift":
        subprocess.run(["swift", os.path.join(ROOT, "ocr.swift"), path],
                       check=True, timeout=300)
    else:
        subprocess.run([OCR_BIN, path], check=True, timeout=300)
    txt = path.rsplit(".", 1)[0] + ".txt"
    with open(txt, encoding="utf8") as f:
        return f.read()


def export_pdf(save_dir, name):
    from PIL import Image
    files = sorted(f for f in os.listdir(save_dir)
                   if f.endswith(".jpg") and not f.endswith("_bw.jpg")
                   and not f.startswith("."))
    if not files:
        return None
    imgs = [Image.open(os.path.join(save_dir, f)).convert("RGB") for f in files]
    out = os.path.join(save_dir, f"{name}.pdf")
    imgs[0].save(out, save_all=True, append_images=imgs[1:],
                 resolution=200.0, quality=88)
    return out
    # 注: 只收 *.jpg, pdf 不会进入下轮合并


# ---------------- 尺寸预设与排版导出 ----------------

# 常见文档的实际物理尺寸(mm): 排版导出时每页扫描件按其"文档类型"1:1 还原到纸上
DOC_TYPES = [
    {"id": "A4",       "name": "A4 纸",      "w": 210,   "h": 297},
    {"id": "A3",       "name": "A3 纸",      "w": 297,   "h": 420},
    {"id": "A5",       "name": "A5 纸",      "w": 148,   "h": 210},
    {"id": "B5",       "name": "B5 纸",      "w": 176,   "h": 250},
    {"id": "letter",   "name": "Letter",    "w": 215.9, "h": 279.4},
    {"id": "idcard",   "name": "身份证",    "w": 85.6,  "h": 54},
    {"id": "hukou",    "name": "户口本内页", "w": 143,   "h": 105},
    {"id": "passport", "name": "护照",      "w": 125,   "h": 88},
    {"id": "photo1",   "name": "一寸照片",  "w": 25,    "h": 35},
    {"id": "photo2",   "name": "二寸照片",  "w": 35,    "h": 49},
]
DOC_BY_ID = {d["id"]: d for d in DOC_TYPES}
PAPER_SIZES = [
    {"id": "A4",     "name": "A4 (210×297mm)",   "w": 210,   "h": 297},
    {"id": "A3",     "name": "A3 (297×420mm)",   "w": 297,   "h": 420},
    {"id": "A5",     "name": "A5 (148×210mm)",   "w": 148,   "h": 210},
    {"id": "B5",     "name": "B5 (176×250mm)",   "w": 176,   "h": 250},
    {"id": "letter", "name": "Letter (216×279mm)", "w": 215.9, "h": 279.4},
]
PAPER_BY_ID = {p["id"]: p for p in PAPER_SIZES}


def doc_wh_of(body):
    """请求体 -> 文档物理尺寸 (w,h)mm; 未知/未设置返回 None."""
    dt = body.get("docType")
    if dt == "custom" and body.get("docW") and body.get("docH"):
        return float(body["docW"]), float(body["docH"])
    p = DOC_BY_ID.get(dt)
    return (p["w"], p["h"]) if p else None


def export_pdf_layout(save_dir, name, paper_w, paper_h, margin_mm=10.0, gap_mm=5.0):
    """按纸张排版导出: 每页扫描件独占一张纸, 按文档类型实际尺寸(1:1)置于左上角.

    - 目标纸张统一为 paper_w×paper_h mm, 四周留 margin_mm 边距(防打印裁切)
    - 模拟复印件/扫描仪效果: 文档放在纸张左上角(边距内), 排不满自然留白
    - 单项超出可打印区时等比缩小到刚好放下(保证内容不丢)
    - 每页物理尺寸取自其标注 docType(自定义用 docW×docH), 无标注默认 A4;
      盒子横竖方向自动跟随图片方向(竖拍的证件 -> 竖盒), 图在盒内 contain 居中
    - gap_mm 仅为旧调用兼容保留(单页单文档后无项间间距)
    """
    from PIL import Image
    files = sorted(f for f in os.listdir(save_dir)
                   if f.endswith(".jpg") and not f.endswith("_bw.jpg")
                   and not f.startswith("."))
    if not files:
        return None
    # label -> 标注(取已保存页的文档类型/样式)
    label_anno = {}
    with ANNO_LOCK:
        for img_id, a in ANNOS.items():
            if a.get("done"):
                label_anno.setdefault(a.get("label") or img_id, a)

    DPI = 300.0

    def mm(v):
        return int(round(v * DPI / 25.4))

    pw, ph = mm(paper_w), mm(paper_h)
    mx = mm(margin_mm)
    area_w, area_h = pw - 2 * mx, ph - 2 * mx
    if area_w <= 0 or area_h <= 0:
        raise ValueError("纸张尺寸太小或页边距太大")

    RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS
    items = []
    for f in files:
        base = f[:-4]
        label = base.split("-", 1)[1] if "-" in base else base
        a = label_anno.get(label) or {}
        img = Image.open(os.path.join(save_dir, f))
        if a.get("style") == "bw":   # 页样式为黑白时用 _bw 版本
            bwp = os.path.join(save_dir, base + "_bw.jpg")
            if os.path.exists(bwp):
                img = Image.open(bwp)
        img = img.convert("RGB")
        dt = a.get("docType")
        if dt == "custom" and a.get("docW") and a.get("docH"):
            bw_, bh_ = float(a["docW"]), float(a["docH"])
        else:
            p = DOC_BY_ID.get(dt) or DOC_BY_ID["A4"]
            bw_, bh_ = float(p["w"]), float(p["h"])
        # 盒子方向跟随图片方向: 取 contain 缩放比更大的那种摆放
        if min(bh_ / img.width, bw_ / img.height) > min(bw_ / img.width, bh_ / img.height):
            bw_, bh_ = bh_, bw_
        # 统一换算成像素后再判断/缩放(混比 mm 与 px 会让超尺寸判断永远为假)
        bw_px, bh_px = mm(bw_), mm(bh_)
        if bw_px > area_w or bh_px > area_h:    # 超出可打印区 -> 等比缩小
            k = min(area_w / bw_px, area_h / bh_px)
            bw_px = min(round(bw_px * k), area_w)
            bh_px = min(round(bh_px * k), area_h)
        items.append((img, bw_px, bh_px))

    # 每页扫描件独占一张纸, 置于左上角(边距内) — 模拟复印件/扫描仪效果
    pages = [[(img, mx, mx, iw, ih)] for img, iw, ih in items]

    out_imgs = []
    for page in pages:
        canvas = Image.new("RGB", (pw, ph), (255, 255, 255))
        for img, px, py, bw_px, bh_px in page:
            k = min(bw_px / img.width, bh_px / img.height)   # contain 居中
            w2 = max(1, round(img.width * k))
            h2 = max(1, round(img.height * k))
            canvas.paste(img.resize((w2, h2), RESAMPLE),
                         (px + (bw_px - w2) // 2, py + (bh_px - h2) // 2))
        out_imgs.append(canvas)
    out = os.path.join(save_dir, f"{name}.pdf")
    out_imgs[0].save(out, save_all=True, append_images=out_imgs[1:],
                     resolution=DPI, quality=88)
    return out


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    input_dir = ROOT
    save_dir = ROOT
    html_path = os.path.join(ROOT, "index.html")

    def log_message(self, *a):  # 安静模式
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _full_img(self, img_id):
        """原图(BGR). app 模式从注册表取, 开发模式扫 input 目录."""
        with PHOTO_LOCK:
            reg = PHOTO_REG.get(img_id)
        if reg is not None:
            return cv2.imread(reg["path"]), reg["path"]
        for ext in (".jpg", ".jpeg", ".png", ".heic"):
            p = os.path.join(self.input_dir, img_id + ext)
            if os.path.exists(p):
                if ext == ".heic":
                    cache = os.path.join(self.input_dir, ".cache_" + img_id + ".jpg")
                    if not os.path.exists(cache):
                        subprocess.run(["sips", "-s", "format", "jpeg", p,
                                        "--out", cache], check=True,
                                       stdout=subprocess.DEVNULL)
                    p = cache
                return cv2.imread(p), p
        return None, None

    def _save_dir(self):
        """当前保存目录: 每次从 config.json 现读(对话框/手动编辑文件都即时生效)."""
        cfg = load_config()
        if cfg.get("save_dir"):
            CONFIG["save_dir"] = cfg["save_dir"]
            return cfg["save_dir"]
        return CONFIG.get("save_dir") or self.save_dir

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(Handler.html_path, "rb") as f:
                return self._bytes(f.read(), "text/html; charset=utf-8")
        m = re.match(r"^/api/image/([^/]+)$", u.path)
        if m:
            q = urllib.parse.parse_qs(u.query)
            w = int(q.get("w", ["2000"])[0])
            # 路径段是百分号编码的, 注册表键是原始文件名(中文图片名必须解码)
            img, _ = self._full_img(urllib.parse.unquote(m.group(1)))
            if img is None:
                return self._json({"error": "not found"}, 404)
            h = img.shape[0]
            if img.shape[1] > w:
                r = w / img.shape[1]
                img = cv2.resize(img, (w, int(h * r)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
            return self._bytes(buf.tobytes(), "image/jpeg")
        if u.path == "/api/images":
            ids = [{"id": pid, "name": os.path.basename(r["path"]),
                    "w": r["w"], "h": r["h"]}
                   for pid, r in sorted(PHOTO_REG.items())]
            # 开发模式兜底: 仅当该项目没有自己的 photos.json(从未显式管理)时
            # 扫 input 目录 — 避免新建空项目被其它项目的照片污染
            has_own = os.path.exists(os.path.join(project_dir(), "photos.json"))
            if not ids and not has_own and os.path.isdir(self.input_dir):
                try:
                    for f in sorted(os.listdir(self.input_dir)):
                        if f.lower().endswith((".jpg", ".jpeg", ".png", ".heic")) \
                                and not f.startswith("."):
                            try:
                                r = register_photo(os.path.join(self.input_dir, f))
                                ids.append({"id": f.rsplit(".", 1)[0],
                                            "name": f, "w": r["w"], "h": r["h"]})
                            except Exception:
                                pass
                except Exception:
                    pass
            return self._json({"images": ids})
        if u.path == "/api/annos":
            with ANNO_LOCK:
                return self._json({"annos": ANNOS})
        if u.path == "/api/projects":
            return self._json({
                "projects": [{"pid": pid, "name": m["name"], "created": m["created"]}
                             for pid, m in sorted(PROJECTS.items())],
                "current": CUR_PID,
            })
        if u.path == "/api/config":
            return self._json({"pages_dir": pages_dir(),
                               "last_pdf_dir": CONFIG.get("last_pdf_dir"),
                               "export": CONFIG.get("export"),
                               "project": {"pid": CUR_PID,
                                           "name": PROJECTS[CUR_PID]["name"]}})
        if u.path == "/api/presets":
            return self._json({"docs": DOC_TYPES, "papers": PAPER_SIZES})
        if u.path == "/api/saves":
            sd = pages_dir()
            files = sorted(f for f in os.listdir(sd)
                           if f.endswith(".jpg")) if os.path.isdir(sd) else []
            return self._json({"saves": files})
        return self._json({"error": "unknown"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/projects":
                # 项目操作: {op: create|switch|rename|delete, name?, pid?}
                body = self._body()
                op = body.get("op")
                if op == "create":
                    pid = create_project(body.get("name"))
                    switch_project(pid)
                    return self._json({"ok": True, "pid": pid,
                                       "name": PROJECTS[pid]["name"]})
                if op == "switch":
                    if body.get("pid") == CUR_PID:
                        return self._json({"ok": True, "pid": CUR_PID, "unchanged": True})
                    r = switch_project(body["pid"])
                    return self._json({"ok": True, **r})
                if op == "rename":
                    pid = body["pid"]
                    if pid not in PROJECTS:
                        return self._json({"error": "no such project"}, 404)
                    PROJECTS[pid]["name"] = body.get("name") or PROJECTS[pid]["name"]
                    save_projects()
                    return self._json({"ok": True})
                if op == "delete":
                    delete_project(body["pid"])
                    return self._json({"ok": True, "current": CUR_PID})
                return self._json({"error": "unknown op"}, 400)
            if u.path == "/api/anno":
                # 保存/更新一张图的标注(切图时与点"本页就绪"时都会落盘)
                body = self._body()
                img_id = body["id"]
                anno = {k: body[k] for k in
                        ("corners", "edges", "mode", "pads", "style", "rot",
                         "label", "done", "ocrText",
                         "docType", "docW", "docH") if k in body}
                with ANNO_LOCK:
                    ANNOS[img_id] = anno
                persist_annos()
                return self._json({"ok": True})
            if u.path == "/api/unsave":
                # 撤销保存: 清掉该图在缓存目录的页文件与 done/OCR 状态, 标注保留供重新微调
                body = self._body()
                img_id = body["id"]
                label = body.get("label") or img_id
                sd = pages_dir()
                removed = []
                if os.path.isdir(sd):
                    for f in os.listdir(sd):
                        if f.endswith(f"-{label}.jpg") or f.endswith(f"-{label}_bw.jpg"):
                            try:
                                os.remove(os.path.join(sd, f))
                                removed.append(f)
                            except OSError:
                                pass
                with ANNO_LOCK:
                    anno = ANNOS.get(img_id)
                    if anno:
                        anno["done"] = False
                        anno.pop("ocrText", None)
                persist_annos()
                return self._json({"ok": True, "removed": len(removed)})
            if u.path == "/api/choose_dir":
                d = choose_dir_dialog(CONFIG.get("last_pdf_dir"))
                if d:
                    CONFIG["last_pdf_dir"] = d
                    save_config(CONFIG)
                return self._json({"last_pdf_dir": CONFIG.get("last_pdf_dir")})
            if u.path == "/api/add_photos":
                body = self._body()
                # body.paths 提供时直接注册(程序化/测试); 否则弹原生文件对话框
                paths = body.get("paths") or open_files_dialog()
                added = []
                for p in paths:
                    try:
                        r = register_photo(p)
                        added.append({"id": os.path.splitext(os.path.basename(p))[0],
                                      "name": os.path.basename(p),
                                      "w": r["w"], "h": r["h"]})
                    except Exception:
                        pass
                return self._json({"added": added})
            if u.path == "/api/detect":
                body = self._body()
                img, _ = self._full_img(body["id"])
                if img is None:
                    return self._json({"error": "not found"}, 404)
                return self._json(detect_document(img))
            if u.path in ("/api/preview", "/api/save"):
                body = self._body()
                img, _ = self._full_img(body["id"])
                if img is None:
                    return self._json({"error": "not found"}, 404)
                pads = body.get("pads", {})
                mode = body.get("mode", "color")
                rot = int(body.get("rot", 0))
                if u.path == "/api/preview":
                    out = render_scan(img, body["corners"], body["edges"], pads,
                                      int(body.get("out_w", 1000)), mode, rot,
                                      doc_wh=doc_wh_of(body))
                    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    self._bytes(buf.tobytes(), "image/jpeg")
                    return
                # save: 全分辨率 + 彩色/黑白 + 可选 OCR
                # 只 warp 一次: gray/bw 由同一份归一化结果派生(此前 color/gray
                # 各跑一遍 remap+归一化, 保存耗时直接翻倍)
                out_w = int(body.get("out_w", 2400))
                color, gray = render_scan_pair(img, body["corners"], body["edges"],
                                               pads, out_w, rot,
                                               doc_wh=doc_wh_of(body))
                bw = scan.bw_scan(gray)
                sd = pages_dir()  # 单页是中间产物 → 系统缓存目录, PDF 导出时才落用户路径
                os.makedirs(sd, exist_ok=True)
                idx = body.get("index", 0)
                label = body.get("label", body["id"])
                base = os.path.join(sd, f"{idx:02d}-{label}")
                cpath, bpath = base + ".jpg", base + "_bw.jpg"
                # 同图重存: 清掉该图片旧 index 的文件, 防止同图以不同序号重复进入 PDF
                for old in os.listdir(sd):
                    if old.endswith(f"-{label}.jpg") or old.endswith(f"-{label}_bw.jpg"):
                        if not old.startswith(f"{idx:02d}-"):
                            os.remove(os.path.join(sd, old))
                cv2.imwrite(cpath, color, [cv2.IMWRITE_JPEG_QUALITY, 92])
                cv2.imwrite(bpath, bw, [cv2.IMWRITE_JPEG_QUALITY, 92])
                resp = {"color": cpath, "bw": bpath}
                if body.get("ocr"):
                    resp["ocr_text"] = ocr_file(cpath)
                # 保存成功即落盘标注(含 done/OCR 文本), 重启后可恢复
                # 注: body.pads={t,b,l,r} 比例 与前端 S.pads 同构; mode_src 才是
                # corner/edge( body.mode 是渲染模式 color/gray )
                with ANNO_LOCK:
                    anno = ANNOS.get(body["id"]) or {}
                    anno["corners"] = body["corners"]
                    anno["edges"] = body["edges"]
                    anno["mode"] = body.get("mode_src", "corner")
                    anno["pads"] = body.get("pads")
                    anno["style"] = body.get("style", "color")
                    anno["rot"] = rot
                    anno["label"] = label
                    # 扫描对象类型(排版导出按实际尺寸 1:1), 未提供时不覆盖旧值
                    for k in ("docType", "docW", "docH"):
                        if body.get(k) is not None:
                            anno[k] = body[k]
                    anno["done"] = True
                    if resp.get("ocr_text"):
                        anno["ocrText"] = resp["ocr_text"]
                    ANNOS[body["id"]] = anno  # 新建时必须挂回(get or {} 的坑)
                persist_annos()
                return self._json(resp)
            if u.path == "/api/pdf":
                body = self._body()
                name = body.get("name", "扫描件") or "扫描件"
                if not name.lower().endswith(".pdf"):
                    name += ".pdf"
                # 目标路径三层来源: 测试直传 target > 弹窗选的 dir+name > 记忆目录+name。
                # 不再用原生"存储"对话框: 其 default location 不可靠(总从桌面开始)。
                target = body.get("target")
                if not target:
                    d = (body.get("dir") or CONFIG.get("last_pdf_dir")
                         or os.path.expanduser("~/Documents"))
                    # 容错: 去引号/空白/尾斜杠; 以 .pdf 结尾说明用户把完整文件
                    # 路径粘进了目录栏 — 直接作为目标, 拆出目录与文件名
                    d = d.strip().strip('"').strip("'").rstrip("/")
                    if d.lower().endswith(".pdf"):
                        target = d
                    else:
                        if not os.path.isdir(d):
                            try:      # 目录不存在则按用户指定的去向创建
                                os.makedirs(d, exist_ok=True)
                            except Exception as e:
                                return self._json(
                                    {"error": f"无法创建保存目录 {d}: {e}"}, 400)
                        target = os.path.join(d, name)
                # 确保目标父目录存在(完整路径粘入/新目录场景), 再做覆盖保护
                try:
                    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                except Exception as e:
                    return self._json({"error": f"无法创建保存目录: {e}"}, 400)
                # 覆盖保护: 已存在文件先询问(body.overwrite 确认后放行)
                if os.path.exists(target) and not body.get("overwrite"):
                    return self._json({"exists": True, "pdf": target})
                mode = body.get("mode", "image")
                exp = dict(CONFIG.get("export") or {})   # 合并式记忆, 不丢 dir/fname
                exp["mode"] = mode
                if mode == "layout":
                    # 按纸张排版: 解析纸张尺寸(预设/自定义) + 横竖方向
                    paper = body.get("paper", "A4")
                    if paper == "custom" and body.get("paper_w") and body.get("paper_h"):
                        pw, ph = float(body["paper_w"]), float(body["paper_h"])
                    else:
                        p = PAPER_BY_ID.get(paper) or PAPER_BY_ID["A4"]
                        pw, ph = float(p["w"]), float(p["h"])
                    if body.get("orient") == "l":
                        pw, ph = ph, pw
                    margin = max(0.0, float(body.get("margin", 10) or 0))
                    margin = max(0.0, min(margin, min(pw, ph) / 2 - 1))
                    out = export_pdf_layout(pages_dir(), "__merge_tmp__",
                                            pw, ph, margin)
                    exp.update({"paper": paper, "paper_w": body.get("paper_w"),
                                "paper_h": body.get("paper_h"),
                                "orient": body.get("orient", "p"), "margin": margin})
                else:
                    out = export_pdf(pages_dir(), "__merge_tmp__")
                if not out:
                    return self._json({"error": "no saves"})
                os.replace(out, target)
                # 记忆本次目录与导出设置, 下次默认继承
                CONFIG["last_pdf_dir"] = os.path.dirname(target)
                exp["dir"] = os.path.dirname(target)
                if body.get("fname"):        # 仅前端确认过"用户自定义"的文件名才记忆
                    exp["fname"] = body["fname"]
                CONFIG["export"] = exp
                save_config(CONFIG)
                return self._json({"pdf": target})
            return self._json({"error": "unknown"}, 404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=os.path.join(ROOT, "input"))
    ap.add_argument("--out", default=None,
                    help="默认保存目录; 未指定且 config 无 save_dir 时用 ~/Documents/DocLens")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--html", default=None, help="index.html 路径(app 打包用)")
    ap.add_argument("--ocr-bin", default=None, help="预编译 ocr_bin 路径(app 打包用, 免运行时 swiftc)")
    args = ap.parse_args()
    Handler.input_dir = args.input
    Handler.save_dir = args.out or CONFIG.get("save_dir") or default_save_dir()
    CONFIG.setdefault("save_dir", Handler.save_dir)
    save_config(CONFIG)
    Handler.html_path = args.html or os.path.join(ROOT, "index.html")
    if args.ocr_bin:
        global OCR_BIN
        OCR_BIN = args.ocr_bin
    load_projects()   # 多项目初始化(首次迁移旧数据到默认项目并加载其照片/标注)
    load_photos()     # 多项目模式下为 no-op, 兼容旧路径
    load_annos()      # 同上
    os.makedirs(Handler.save_dir, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    # windowed (console=False) 模式下 sys.stdout 可能为 NullWriter, print/flush 会阻塞:
    # 打包版只写日志文件, 终端版才 print
    banner = f"DocLens http://127.0.0.1:{args.port}  input={args.input} out={Handler.save_dir}"
    try:
        log_dir = os.path.expanduser("~/Library/Logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "DocLens.log"), "a", encoding="utf8") as f:
            f.write(banner + "\n")
    except Exception:
        pass
    if sys.stderr is not None and not isinstance(sys.stderr, type(None)):
        try:
            print(banner, flush=True)
        except Exception:
            pass
    srv.serve_forever()


if __name__ == "__main__":
    main()
