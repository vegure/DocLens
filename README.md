# DocLens — macOS 文档扫描工具

用手机随手拍的证件/文书照片,一键变成干净的"复印件"PDF:自动找边、曲面矫正、光照增强、
按文档实际尺寸(1:1)排版到标准纸张,支持本地 OCR。对标 Microsoft Lens / 扫描全能王,
纯本地运行,照片不出本机。

## 功能

- **自动检测 + 手动微调**:亮区分割定位文档四角;边缘模式下可拖动控制点,用 Catmull-Rom
  曲线贴合弯曲纸页;拖拽带放大镜,精确到像素。
- **Coons patch 曲面矫正**:以四条边界曲线插值出映射,能"展平"拍弯的纸页(普通透视
  矫正做不到);越界部分自动补白。
- **光照归一化(多次实测调参)**:
  - 大核(短边 1/2)形态学估计纯光照场,除法归一化——人像/文字不再被当"欠曝"洗白;
  - 行列可分离白点二段校正,消除窄于核的阴影条;
  - 背景边条自动裁剪(选区外扩带入的深色桌面/暗角);
  - 按文档类型物理比例强制校正(长边为基准),斜拍透视压缩不再残留;
  - 增益钳制 + 黑点保护兜底极端光照。
- **按实际尺寸排版导出**(复印件效果):每页扫描件独占一张纸,按所选文档类型
  (身份证 85.6×54 / 户口本 143×105 / A4 / A3 / A5 / B5 / Letter / 护照 / 一寸二寸照片 /
  自定义 mm)的真实尺寸 1:1 放在纸张左上角,四周留边距防打印裁边;超可打印区自动等比缩小。
  也支持"逐页原尺寸"传统模式。导出目录/文件名/纸张/边距全部记忆,下次默认继承。
- **多项目管理**:每个项目独立照片、标注、扫描页与导出配置;标注持久化,重启即恢复。
- **本地 OCR**:Apple Vision(简体中文 + 英文,accurate),保存时可选提取。
- **原生 App 体验**:Swift 启动器 + WKWebView 窗口,PyInstaller 打包后端,
  ad-hoc 签名即可本地使用;Developer ID + 公证分发见 `app/README-PUBLISH.md`。

## 目录结构

```
work/                 核心代码
  server.py           HTTP 服务(标准库 + OpenCV): 静态前端 + JSON API
  index.html          前端(原生 JS + Canvas, 单文件)
  scan.py             CV 算法: 文档检测/矫正/光照归一化/黑白化
  ocr.swift           Vision OCR 命令行工具(首次使用自动 swiftc 编译)
app/                  macOS App 打包(Launcher/签名/公证/DMG)
扫描件/               (不入库)个人扫描材料
```

## 从源码运行

依赖:Python 3.10+、OpenCV、NumPy、Pillow、macOS(Vision/AppleScript)。

```bash
cd work
python3.10 -m pip install opencv-python numpy pillow   # 若未装
python3.10 server.py --input <照片目录> --port 8765
# 打开 http://127.0.0.1:8765
```

数据目录:`~/Library/Application Support/DocLens/`(项目/标注/配置),
页缓存在 `~/Library/Caches/DocLens/`。

## 打包 macOS App

```bash
cd app
swiftc ../work/ocr.swift -o ../work/ocr_bin          # 预编译 OCR(可选)
python3.10 -m PyInstaller server-onedir.spec --noconfirm
./build_dist.sh                                     # ad-hoc 签名, 产出 ../dist/DocLens.app
# 分发: 按 README-PUBLISH.md 配 Developer ID 后 ./notarize.sh
```

## License

MIT
