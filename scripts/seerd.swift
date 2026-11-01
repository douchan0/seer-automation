// seerd: persistent driver daemon for the XIN计划 Flash client (macOS)
// protocol: one JSON per line in stdin -> one JSON per line out stdout
import Foundation
import CoreGraphics
import Vision
import AppKit
import ApplicationServices

let WIN_LOCK = NSLock()
var cachedWin: (id: Int, x: Double, y: Double, w: Double, h: Double)? = nil

func out(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        print(s); fflush(stdout)
    }
}

func findWin() -> (Int, Double, Double, Double, Double)? {
    let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else { return nil }
    for w in list {
        let owner = w[kCGWindowOwnerName as String] as? String ?? ""
        let layer = w[kCGWindowLayer as String] as? Int ?? 0
        if owner == "SeerGame" && layer == 0 {
            let num = w[kCGWindowNumber as String] as? Int ?? -1
            if let bd = w[kCGWindowBounds as String] as? [String: Any],
               let x = bd["X"] as? Double, let y = bd["Y"] as? Double,
               let ww = bd["Width"] as? Double, let hh = bd["Height"] as? Double {
                return (num, x, y, ww, hh)
            }
        }
    }
    return nil
}

func activateApp() -> Bool {
    for a in NSWorkspace.shared.runningApplications {
        let s = (a.bundleIdentifier ?? "") + "|" + (a.localizedName ?? "")
        if s.lowercased().contains("yahaha") || s.lowercased().contains("seergame") {
            a.activate(options: [.activateAllWindows])
            return true
        }
    }
    return false
}

func axWindow() -> AXUIElement? {
    for a in NSWorkspace.shared.runningApplications {
        let s = (a.bundleIdentifier ?? "") + "|" + (a.localizedName ?? "")
        if s.lowercased().contains("yahaha") || s.lowercased().contains("seergame") {
            let app = AXUIElementCreateApplication(a.processIdentifier)
            var wins: CFTypeRef?
            guard AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &wins) == .success,
                  let arr = wins as? [AXUIElement], let w = arr.first else { continue }
            return w
        }
    }
    return nil
}

func pinWindow(_ x: Double, _ y: Double, _ w: Double, _ h: Double) -> [String: Any] {
    guard let win = axWindow() else { return ["error": "no ax window"] }
    var p = CGPoint(x: x, y: y)
    var sz = CGSize(width: w, height: h)
    AXUIElementSetAttributeValue(win, kAXPositionAttribute as CFString, AXValueCreate(.cgPoint, &p)!)
    AXUIElementSetAttributeValue(win, kAXSizeAttribute as CFString, AXValueCreate(.cgSize, &sz)!)
    usleep(120000)
    if let f = findWin() { return ["x": f.1, "y": f.2, "w": f.3, "h": f.4, "id": f.0] }
    return ["error": "pin failed"]
}

func shot(_ path: String, _ x: Double, _ y: Double, _ w: Double, _ h: Double) -> [String: Any] {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    p.arguments = ["-x", "-R", "\(Int(x)),\(Int(y)),\(Int(w)),\(Int(h))", path]
    do { try p.run() } catch { return ["error": "spawn"] }
    p.waitUntilExit()
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { return ["error": "read"] }
    return ["path": path, "w": img.width, "h": img.height]
}

func ocrFile(_ path: String, _ minConf: Float) -> [[String: Any]] {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { return [] }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    let handler = VNImageRequestHandler(cgImage: img, options: [:])
    do { try handler.perform([req]) } catch { return [] }
    var outArr: [[String: Any]] = []
    for obs in (req.results ?? []) {
        guard let c = obs.topCandidates(1).first else { continue }
        if c.confidence < minConf { continue }
        let bb = obs.boundingBox
        outArr.append(["text": c.string, "conf": Double(c.confidence),
                       "x": Double(bb.origin.x), "y": Double(bb.origin.y),
                       "w": Double(bb.size.width), "h": Double(bb.size.height)])
    }
    return outArr
}

func clickAt(_ gx: Double, _ gy: Double, _ clicks: Int) {
    let src = CGEventSource(stateID: .hidSystemState)
    let p = CGPoint(x: gx, y: gy)
    CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(15000)
    for i in 1...max(1, clicks) {
        let d = CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)
        let u = CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)
        d?.setIntegerValueField(.mouseEventClickState, value: Int64(i))
        u?.setIntegerValueField(.mouseEventClickState, value: Int64(i))
        d?.post(tap: .cghidEventTap); usleep(12000); u?.post(tap: .cghidEventTap)
        usleep(40000)
    }
}

func keyPress(_ code: UInt16) {
    let src = CGEventSource(stateID: .hidSystemState)
    CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: true)?.post(tap: .cghidEventTap)
    usleep(15000)
    CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: false)?.post(tap: .cghidEventTap)
}

func typeText(_ s: String) {
    for ch in s.unicodeScalars {
        var u = UniChar(ch.value)
        let src = CGEventSource(stateID: .hidSystemState)
        if let d = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: true) {
            d.keyboardSetUnicodeString(stringLength: 1, unicodeString: &u); d.post(tap: .cghidEventTap)
        }
        if let up = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: false) {
            up.keyboardSetUnicodeString(stringLength: 1, unicodeString: &u); up.post(tap: .cghidEventTap)
        }
        usleep(8000)
    }
}

func handle(_ obj: [String: Any]) -> [String: Any] {
    let op = obj["op"] as? String ?? ""
    switch op {
    case "ping": return ["ok": true]
    case "win":
        guard let f = findWin() else { return ["error": "no window"] }
        return ["id": f.0, "x": f.1, "y": f.2, "w": f.3, "h": f.4]
    case "pin":
        let x = obj["x"] as? Double ?? 300, y = obj["y"] as? Double ?? 120
        let w = obj["w"] as? Double ?? 1149, h = obj["h"] as? Double ?? 714
        _ = activateApp(); usleep(80000)
        return pinWindow(x, y, w, h)
    case "activate":
        _ = activateApp(); return ["ok": true]
    case "shot":
        let path = obj["path"] as? String ?? "/tmp/seerd_shot.png"
        var (x, y, w, h) = (0.0, 0.0, 0.0, 0.0)
        if let g = findWin() { (x, y, w, h) = (g.1, g.2, g.3, g.4) } else { return ["error": "no window"] }
        if let act = obj["activate"] as? Bool, act { _ = activateApp(); usleep(120000) }
        return shot(path, x, y, w, h)
    case "ocr":
        let path = obj["path"] as? String ?? "/tmp/seerd_shot.png"
        let minc = Float(obj["min"] as? Double ?? 0.3)
        return ["items": ocrFile(path, minc)]
    case "click":
        let x = obj["x"] as? Double ?? 0, y = obj["y"] as? Double ?? 0
        clickAt(x, y, obj["n"] as? Int ?? 1)
        return ["ok": true]
    case "clicklocal":
        guard let g = findWin() else { return ["error": "no window"] }
        let px = obj["x"] as? Double ?? 0, py = obj["y"] as? Double ?? 0
        let scale = obj["scale"] as? Double ?? 2.0
        clickAt(g.1 + px/scale, g.2 + py/scale, obj["n"] as? Int ?? 1)
        return ["ok": true, "gx": g.1 + px/scale, "gy": g.2 + py/scale]
    case "move":
        guard let g = findWin() else { return ["error": "no window"] }
        let px = obj["x"] as? Double ?? 0, py = obj["y"] as? Double ?? 0
        let scale = obj["scale"] as? Double ?? 2.0
        let src = CGEventSource(stateID: .hidSystemState)
        CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: CGPoint(x: g.1 + px/scale, y: g.2 + py/scale), mouseButton: .left)?.post(tap: .cghidEventTap)
        return ["ok": true]
    case "key": keyPress(UInt16(obj["code"] as? Int ?? 53)); return ["ok": true]
    case "type": typeText(obj["text"] as? String ?? ""); return ["ok": true]
    case "quit": return ["bye": true]
    default: return ["error": "unknown op \(op)"]
    }
}

// main loop
while let line = readLine(strippingNewline: true) {
    guard let data = line.data(using: .utf8),
          let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
        out(["error": "bad json"]); continue
    }
    let res = handle(obj)
    out(res)
    if (obj["op"] as? String) == "quit" { break }
}
