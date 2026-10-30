// ocr.swift — 用 Apple Vision 本地 OCR 提取扫描件文字 (zh-Hans, accurate)
// 用法: swift ocr.swift img1 [img2 ...]  → 每张图输出 <name>.txt (带行坐标排序)
import Foundation
import Vision
import AppKit

func ocrImage(at path: String) -> String {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        return "[ERROR] cannot read \(path)"
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do {
        try handler.perform([request])
    } catch {
        return "[ERROR] Vision: \(error)"
    }

    guard let obs = request.results, !obs.isEmpty else { return "[no text]" }

    // 按行组织: 同一行 = y 中心接近且行高相近 → 合并, 按 x 排序
    struct Line { var y: CGFloat; var h: CGFloat; var items: [(x: CGFloat, s: String)] }
    var lines: [Line] = []
    for o in obs {
        guard let top = o.topCandidates(1).first else { continue }
        let b = o.boundingBox
        let yC = b.midY * 10000  // 放大避免浮点比较问题
        let h = b.height * 10000
        if let idx = lines.indices.first(where: {
            abs(lines[$0].y - yC) < max(lines[$0].h, h) * 0.6
        }) {
            lines[idx].items.append((b.minX * 10000, top.string))
        } else {
            lines.append(Line(y: yC, h: h, items: [(b.minX * 10000, top.string)]))
        }
    }
    // Vision 坐标系 y 轴向上: 行按 y 降序 = 阅读顺序自上而下
    lines.sort { $0.y > $1.y }
    var out: [String] = []
    for var line in lines {
        line.items.sort { $0.x < $1.x }
        out.append(line.items.map(\.s).joined(separator: "  "))
    }
    return out.joined(separator: "\n")
}

for path in CommandLine.arguments.dropFirst() {
    let text = ocrImage(at: path)
    let url = URL(fileURLWithPath: path)
    let outURL = url.deletingPathExtension().appendingPathExtension("txt")
    try? text.write(to: outURL, atomically: true, encoding: .utf8)
    let lines = text.components(separatedBy: "\n").count
    print("[OK] \(path) -> \(outURL.lastPathComponent) (\(lines) lines)")
}
