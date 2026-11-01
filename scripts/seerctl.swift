import Foundation
import CoreGraphics
import ImageIO
import Vision
import AppKit
import ApplicationServices

// seerctl: window list, window capture, mouse/keyboard input, OCR
func jsonPrint(_ obj: Any) {
    if let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]),
       let s = String(data: data, encoding: .utf8) { print(s) }
}

func windowList() {
    let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else { return }
    var out: [[String: Any]] = []
    for w in list {
        let owner = w[kCGWindowOwnerName as String] as? String ?? ""
        let name = w[kCGWindowName as String] as? String ?? ""
        let num = w[kCGWindowNumber as String] as? Int ?? -1
        let layer = w[kCGWindowLayer as String] as? Int ?? 0
        var b: [String: Any] = [:]
        if let bd = w[kCGWindowBounds as String] as? [String: Any] {
            b = ["x": bd["X"] ?? 0, "y": bd["Y"] ?? 0, "w": bd["Width"] ?? 0, "h": bd["Height"] ?? 0]
        }
        out.append(["id": num, "owner": owner, "title": name, "layer": layer, "bounds": b])
    }
    jsonPrint(out)
}

func ocr(_ path: String, _ minConf: Float) {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { exit(2) }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    let handler = VNImageRequestHandler(cgImage: img, options: [:])
    do { try handler.perform([req]) } catch { exit(4) }
    var out: [[String: Any]] = []
    for obs in (req.results ?? []) {
        guard let c = obs.topCandidates(1).first else { continue }
        if c.confidence < minConf { continue }
        let bb = obs.boundingBox
        out.append(["text": c.string, "conf": Double(c.confidence),
                    "x": Double(bb.origin.x), "y": Double(bb.origin.y),
                    "w": Double(bb.size.width), "h": Double(bb.size.height)])
    }
    jsonPrint(out)
}

func postMouse(_ x: Double, _ y: Double, _ kind: String) {
    let p = CGPoint(x: x, y: y)
    let src = CGEventSource(stateID: .hidSystemState)
    if kind == "move" {
        CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
        return
    }
    if kind == "down" {
        CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    } else if kind == "up" {
        CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    } else if kind == "rightdown" {
        CGEvent(mouseEventSource: src, mouseType: .rightMouseDown, mouseCursorPosition: p, mouseButton: .right)?.post(tap: .cghidEventTap)
    } else if kind == "rightup" {
        CGEvent(mouseEventSource: src, mouseType: .rightMouseUp, mouseCursorPosition: p, mouseButton: .right)?.post(tap: .cghidEventTap)
    }
}

func click(_ x: Double, _ y: Double, _ count: Int) {
    let src = CGEventSource(stateID: .hidSystemState)
    let p = CGPoint(x: x, y: y)
    CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(30000)
    for i in 1...max(1, count) {
        let down = CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)
        let up = CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)
        down?.setIntegerValueField(.mouseEventClickState, value: Int64(i))
        up?.setIntegerValueField(.mouseEventClickState, value: Int64(i))
        down?.post(tap: .cghidEventTap); usleep(20000); up?.post(tap: .cghidEventTap)
        usleep(60000)
    }
}

func keyTap(_ code: UInt16) {
    let src = CGEventSource(stateID: .hidSystemState)
    CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: true)?.post(tap: .cghidEventTap)
    usleep(20000)
    CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: false)?.post(tap: .cghidEventTap)
}

func typeText(_ s: String) {
    for ch in s.unicodeScalars {
        var u = UniChar(ch.value)
        let src = CGEventSource(stateID: .hidSystemState)
        if let down = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: true) {
            down.keyboardSetUnicodeString(stringLength: 1, unicodeString: &u)
            down.post(tap: .cghidEventTap)
        }
        if let up = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: false) {
            up.keyboardSetUnicodeString(stringLength: 1, unicodeString: &u)
            up.post(tap: .cghidEventTap)
        }
        usleep(12000)
    }
}

func scroll(_ x: Double, _ y: Double, _ dy: Int32) {
    let src = CGEventSource(stateID: .hidSystemState)
    let p = CGPoint(x: x, y: y)
    CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    let e = CGEvent(scrollWheelEvent2Source: src, units: .pixel, wheelCount: 1, wheel1: dy, wheel2: 0, wheel3: 0)
    e?.location = p
    e?.post(tap: .cghidEventTap)
}

func activateApp(_ name: String) {
    let apps = NSWorkspace.shared.runningApplications
    for a in apps where (a.localizedName ?? "") == name {
        a.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
        usleep(400000)
        print("{\"activated\":\"\(name)\",\"pid\":\(a.processIdentifier)}")
        return
    }
    print("{\"error\":\"not found\"}")
}


func pos() {
    let e = CGEvent(source: nil)
    let l = e?.location ?? .zero
    print("{\"x\":\(l.x),\"y\":\(l.y)}")
}

func findAppWindow(_ bundleSub: String) -> AXUIElement? {
    for a in NSWorkspace.shared.runningApplications {
        let bid = (a.bundleIdentifier ?? "") + "|" + (a.localizedName ?? "")
        if bid.lowercased().contains(bundleSub.lowercased()) {
            let app = AXUIElementCreateApplication(a.processIdentifier)
            var wins: CFTypeRef?
            guard AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &wins) == .success,
                  let arr = wins as? [AXUIElement], let w = arr.first else { continue }
            return w
        }
    }
    return nil
}

func winSet(_ bundleSub: String, _ x: Double, _ y: Double, _ w: Double, _ h: Double) {
    guard let win = findAppWindow(bundleSub) else { print("{\"error\":\"no window\"}"); exit(2) }
    var p = CGPoint(x: x, y: y)
    var sz = CGSize(width: w, height: h)
    let pv = AXValueCreate(.cgPoint, &p)!
    let sv = AXValueCreate(.cgSize, &sz)!
    AXUIElementSetAttributeValue(win, kAXPositionAttribute as CFString, pv)
    AXUIElementSetAttributeValue(win, kAXSizeAttribute as CFString, sv)
    usleep(250000)
    var b: CFTypeRef?
    AXUIElementCopyAttributeValue(win, kAXPositionAttribute as CFString, &b)
    var bb: CFTypeRef?
    AXUIElementCopyAttributeValue(win, kAXSizeAttribute as CFString, &bb)
    var pos = CGPoint.zero; var size = CGSize.zero
    if let v = b { AXValueGetValue(v as! AXValue, .cgPoint, &pos) }
    if let v = bb { AXValueGetValue(v as! AXValue, .cgSize, &size) }
    print("{\"x\":\(pos.x),\"y\":\(pos.y),\"w\":\(size.width),\"h\":\(size.height)}")
}

func axTrusted() {
    print("{\"axTrusted\":\(AXIsProcessTrusted())}")
}

let args = Array(CommandLine.arguments.dropFirst())
guard let cmd = args.first else { print("usage: wl|shot|ocr|click|key|type|scroll|activate|axtrusted"); exit(1) }
switch cmd {
case "wl": windowList()
case "ocr": ocr(args[1], Float(args.count > 2 ? args[2] : "0.3")!)
case "click": click(Double(args[1])!, Double(args[2])!, args.count > 3 ? Int(args[3])! : 1)
case "mouse": postMouse(Double(args[1])!, Double(args[2])!, args[3])
case "key": keyTap(UInt16(args[1])!)
case "type": typeText(args[1])
case "scroll": scroll(Double(args[1])!, Double(args[2])!, Int32(args[3])!)
case "activate": activateApp(args[1])
case "pos": pos()
case "winset": winSet(args[1], Double(args[2])!, Double(args[3])!, Double(args[4])!, Double(args[5])!)
case "axtrusted": axTrusted()
default: print("unknown cmd"); exit(1)
}
