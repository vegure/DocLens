#!/usr/bin/env python3
"""DocLens 图标生成 — 参考 Microsoft Lens 设计语言.
Fluent 风格: 蓝色渐变圆角底 + 白色文档梯形(透视, 呼应扫描矫正) +
四角取景角标(呼应 App 的角点拖拽) + 扫描高光.
4x 超采样绘制后 LANCZOS 缩小, 保证边缘干净.
"""
from PIL import Image, ImageDraw, ImageFilter

S = 1024          # 最终尺寸
SS = 4            # 超采样倍数
N = S * SS        # 画布


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# ---------- 1. 圆角方形底 + 对角蓝渐变 (MS Fluent 亮蓝) ----------
R = int(N * 0.225)  # macOS 圆角比例
top, bot = (0, 120, 212), (16, 200, 250)      # #0078D4 → #10C8FA
grad = Image.new("RGBA", (N, N))
gd = ImageDraw.Draw(grad)
for y in range(N):
    t = y / N
    # 对角混一点 x 权重
    gd.line([(0, y), (N, y)], fill=lerp(top, bot, t))
mask = Image.new("L", (N, N), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, N - 1, N - 1], R, fill=255)
base = Image.composite(grad, img.copy(), mask)
img.alpha_composite(base)

# 底部深一点的弧光(Fluent 常见的底边光)
shine = Image.new("RGBA", (N, N), (0, 0, 0, 0))
sd = ImageDraw.Draw(shine)
sd.ellipse([-N * 0.25, N * 0.62, N * 1.25, N * 1.35], fill=(120, 235, 255, 70))
shine.putalpha(shine.split()[3].point(lambda a: a))
img.alpha_composite(Image.composite(shine, Image.new("RGBA", (N, N), (0, 0, 0, 0)), mask))

# ---------- 2. 白色文档梯形(右下透视, 体现"拍歪→矫正") ----------
doc = Image.new("RGBA", (N, N), (0, 0, 0, 0))
dd = ImageDraw.Draw(doc)
m = N * 0.20                     # 边距
paper = [                        # 梯形: 左边高、右边低(透视感)
    (N * 0.28, N * 0.24),
    (N * 0.78, N * 0.30),
    (N * 0.78, N * 0.78),
    (N * 0.28, N * 0.72),
]
# 投影
shadow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
ImageDraw.Draw(shadow).polygon([(x + N * 0.018, y + N * 0.022) for x, y in paper],
                               fill=(0, 40, 80, 110))
shadow = shadow.filter(ImageFilter.GaussianBlur(N * 0.02))
img.alpha_composite(shadow)
# 纸(近白,微冷)
dd.polygon(paper, fill=(252, 253, 255, 255))
# 纸上的"文字行"(4 行粗条, 小尺寸仍可读; 长短不齐→扫描件文本)
for i in range(4):
    y0 = N * 0.315 + i * N * 0.088
    xw = N * (0.40 - (i % 3) * 0.07)
    dd.rounded_rectangle([N * 0.335, y0, N * 0.335 + xw, y0 + N * 0.034],
                         N * 0.012, fill=(140, 190, 225, 255))
# 纸底部的红色小方章(呼应医疗单据场景, 稍大且与文字行留空隙)
dd.rounded_rectangle([N * 0.585, N * 0.665, N * 0.735, N * 0.74],
                     N * 0.012, fill=(235, 80, 80, 255))
img.alpha_composite(doc)

# ---------- 3. 四角取景角标(白, 粗, 呼应角点拖拽) ----------
corner = Image.new("RGBA", (N, N), (0, 0, 0, 0))
cd = ImageDraw.Draw(corner)
L = N * 0.11                      # 角标臂长
W = N * 0.045                     # 线宽
p = N * 0.085                     # 距边
corners_pts = [
    (p, p, +1, +1), (N - p, p, -1, +1),
    (p, N - p, +1, -1), (N - p, N - p, -1, -1),
]
def arm(x0, y0, x1, y1):
    """粗线臂: 矩形 + 两端圆帽(避开 PIL rounded_rectangle 的边界 bug)."""
    cd.rectangle([min(x0, x1) - W / 2, min(y0, y1) - W / 2,
                  max(x0, x1) + W / 2, max(y0, y1) + W / 2], fill=(255, 255, 255, 255))
    for (ex, ey) in ((x0, y0), (x1, y1)):
        cd.ellipse([ex - W / 2, ey - W / 2, ex + W / 2, ey + W / 2],
                   fill=(255, 255, 255, 255))

for cx, cy, sx, sy in corners_pts:
    arm(cx, cy, cx + sx * L, cy)   # 横臂
    arm(cx, cy, cx, cy + sy * L)   # 竖臂
img.alpha_composite(corner)

# ---------- 4. 扫描高光带(贴合文档宽度, 置于文字行之间, 上下羽化) ----------
y = N * 0.545                       # 落在文字行间隙, 不压行
xl, xr = N * 0.30, N * 0.775        # 文档左右边界内
glow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
ImageDraw.Draw(glow).rectangle([xl, y - N * 0.030, xr, y + N * 0.030],
                               fill=(120, 235, 255, 150))
glow = glow.filter(ImageFilter.GaussianBlur(N * 0.020))
img.alpha_composite(glow)
scan = Image.new("RGBA", (N, N), (0, 0, 0, 0))
ImageDraw.Draw(scan).rectangle([xl, y - N * 0.005, xr, y + N * 0.005],
                               fill=(255, 255, 255, 240))
img.alpha_composite(scan)

# ---------- 缩小输出 ----------
img = img.resize((S, S), Image.LANCZOS)
img.save("/Users/yulei/Projects/photo-lens/app/icon_1024.png")
print("icon_1024.png saved")
