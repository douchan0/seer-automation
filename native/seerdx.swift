// seerdx — native ScreenCaptureKit capture/input daemon for the XIN计划 client (macOS).
//
// Speaks the JSON-lines protocol documented in seerauto/PROTOCOL.md.
//
// Design notes that matter (all learned the hard way, see docs/PERFORMANCE_BASELINE.md):
//
//  * ScreenCaptureKit is CHANGE-DRIVEN. A static window emits a frame only every
//    ~350 ms; an animating one at ~17 ms. `capture.latest` therefore NEVER waits
//    for new content — it returns whatever the daemon already holds, with seq/ts.
//
//  * NEVER block the main thread on an SCK async result. That deadlocks
//    (_dispatch_semaphore_wait_slow, measured 13-minute hang) because the
//    completion needs the run loop pumped. Instead the main thread runs a pump
//    loop and drains a command queue; stdin is read on a background thread.
//
//  * Frames are handed to Python as RAW BGRA, not PNG. PNG encode+decode would
//    cost ~75 ms/frame and destroy the whole point. Raw is a single memcpy.
//    Python caches by `seq`, so an unchanged frame costs it nothing at all.
//
//  * The pixel buffer is allocated ONCE and reused. Per-frame allocation roughly
//    doubles the handoff cost (0.89 ms -> 0.52 ms measured).
//
// Protocol extensions over PROTOCOL.md v1 (additive, never breaking):
//    capture.latest / capture.roi reply with:
//      "fmt": "bgra", "path": <raw file>, "stride": <bytes per row>, "w", "h",
//      "seq", "ts_ns"
//    A Python client that does not understand "fmt" simply keeps using the
//    PNG `shot` path.

import Foundation
import AppKit
import CoreGraphics
import CoreVideo
import ScreenCaptureKit
import Vision
import ImageIO
import UniformTypeIdentifiers

// --------------------------------------------------------------------------
// logging -> stderr only; stdout is the protocol channel
// --------------------------------------------------------------------------
func log(_ s: String) {
    FileHandle.standardError.write(("[seerdx] " + s + "\n").data(using: .utf8)!)
}

func monoNs() -> UInt64 { DispatchTime.now().uptimeNanoseconds }

func emit(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj, options: []),
       let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

// --------------------------------------------------------------------------
// frame store — allocated once, reused forever
// --------------------------------------------------------------------------
final class FrameStore {
    private let lock = NSLock()
    private var buf: UnsafeMutableRawPointer?
    private var capacity = 0
    private(set) var w = 0
    private(set) var h = 0
    private(set) var stride = 0
    private(set) var seq: UInt64 = 0
    private(set) var tsNs: UInt64 = 0

    private var writtenSeq: UInt64 = .max
    private let framePath: String

    init(framePath: String) {
        self.framePath = framePath
    }

    func store(_ pixelBuffer: CVPixelBuffer) {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }
        guard let src = CVPixelBufferGetBaseAddress(pixelBuffer) else { return }
        let bw = CVPixelBufferGetWidth(pixelBuffer)
        let bh = CVPixelBufferGetHeight(pixelBuffer)
        let bs = CVPixelBufferGetBytesPerRow(pixelBuffer)
        let need = bs * bh

        lock.lock()
        if capacity < need || buf == nil {
            buf?.deallocate()
            buf = UnsafeMutableRawPointer.allocate(byteCount: need, alignment: 64)
            capacity = need
        }
        buf!.copyMemory(from: src, byteCount: need)
        w = bw; h = bh; stride = bs
        seq &+= 1
        tsNs = monoNs()
        lock.unlock()
    }

    /// Copy the newest frame to `path` (only when the sequence advanced) and
    /// describe it. Returns nil when no frame has ever arrived.
    func materialize() -> [String: Any]? {
        lock.lock(); defer { lock.unlock() }
        guard let b = buf, h > 0 else { return nil }
        if writtenSeq != seq {
            let data = Data(bytesNoCopy: b, count: stride * h, deallocator: .none)
            try? data.write(to: URL(fileURLWithPath: framePath), options: .atomic)
            writtenSeq = seq
        }
        return [
            "fmt": "bgra", "path": framePath, "w": w, "h": h, "stride": stride,
            "seq": Int(seq), "ts_ns": Int(tsNs),
        ]
    }

    /// Crop a game-px rect out of the newest frame into `path`.
    func materializeROI(x: Int, y: Int, rw: Int, rh: Int, path: String) -> [String: Any]? {
        lock.lock(); defer { lock.unlock() }
        guard let b = buf, h > 0 else { return nil }
        let cx = max(0, min(x, w)), cy = max(0, min(y, h))
        let cw = max(1, min(rw, w - cx)), ch = max(1, min(rh, h - cy))
        let out = UnsafeMutableRawPointer.allocate(byteCount: cw * ch * 4, alignment: 64)
        defer { out.deallocate() }
        for row in 0..<ch {
            let src = b.advanced(by: (cy + row) * stride + cx * 4)
            let dst = out.advanced(by: row * cw * 4)
            dst.copyMemory(from: src, byteCount: cw * 4)
        }
        let data = Data(bytesNoCopy: out, count: cw * ch * 4, deallocator: .none)
        try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
        return [
            "fmt": "bgra", "path": path, "w": cw, "h": ch, "stride": cw * 4,
            "seq": Int(seq), "ts_ns": Int(tsNs),
            "rect": [cx, cy, cw, ch],
        ]
    }

    func reset() {
        lock.lock(); buf?.deallocate(); buf = nil; capacity = 0
        w = 0; h = 0; stride = 0; seq = 0; tsNs = 0; writtenSeq = .max
        lock.unlock()
    }
}

// --------------------------------------------------------------------------
// stream output
// --------------------------------------------------------------------------
final class Tap: NSObject, SCStreamOutput {
    let store: FrameStore
    init(store: FrameStore) { self.store = store }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sb.isValid else { return }
        guard let pb = CMSampleBufferGetImageBuffer(sb) else { return }
        store.store(pb)
    }
}

// --------------------------------------------------------------------------
// async bridge — pumps the run loop instead of blocking it
// --------------------------------------------------------------------------
enum AsyncBridge {
    /// Run an async operation from the (pumping) main thread.
    static func run<T>(timeout: Double, _ body: @escaping () async throws -> T) -> Result<T, Error> {
        var result: Result<T, Error>?
        let done = DispatchSemaphore(value: 0)
        Task.detached {
            do { result = .success(try await body()) } catch { result = .failure(error) }
            done.signal()
        }
        let deadline = Date().addingTimeInterval(timeout)
        while done.wait(timeout: .now()) == .timedOut {
            if Date() > deadline {
                return .failure(NSError(domain: "seerdx", code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "operation timed out after \(timeout)s"]))
            }
            RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.002))
        }
        return result ?? .failure(NSError(domain: "seerdx", code: 2,
            userInfo: [NSLocalizedDescriptionKey: "operation produced no result"]))
    }
}

// --------------------------------------------------------------------------
// window helpers
// --------------------------------------------------------------------------
let GAME_OWNER_HINTS = ["seergame", "yahaha", "seer"]

/// Which window to operate on. Defaults to the game; `--target <substring>`
/// lets the daemon be exercised against any other on-screen window, which is
/// how this can be tested without the game client running at all.
var TARGET_HINTS = GAME_OWNER_HINTS

func listWindows() -> [[String: Any]] {
    let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    return (CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]]) ?? []
}

func findGameWindow(in windows: [[String: Any]] = listWindows())
    -> (id: Int, pid: Int, x: Double, y: Double, w: Double, h: Double)? {
    // Never take "the first window whose owner matches": the game app also owns
    // small helper windows (observed: a 52x20 window titled "Window" that came to
    // the front and made `pin` resize the wrong thing). Pick the LARGEST match
    // instead, which is the actual game canvas.
    var best: (id: Int, pid: Int, x: Double, y: Double, w: Double, h: Double)? = nil
    var bestArea = 0.0
    for w in windows {
        let owner = (w[kCGWindowOwnerName as String] as? String ?? "").lowercased()
        let layer = w[kCGWindowLayer as String] as? Int ?? 0
        guard layer == 0 else { continue }
        guard TARGET_HINTS.contains(where: { owner.contains($0) }) else { continue }
        guard let num = w[kCGWindowNumber as String] as? Int, num > 0,
              let pid = w[kCGWindowOwnerPID as String] as? Int, pid > 0 else { continue }
        guard let bd = w[kCGWindowBounds as String] as? [String: Any],
              let x = bd["X"] as? Double, let y = bd["Y"] as? Double,
              let ww = bd["Width"] as? Double, let hh = bd["Height"] as? Double else { continue }
        guard ww >= 200, hh >= 200 else { continue }   // ignore helper/HUD windows
        let area = ww * hh
        if area > bestArea {
            bestArea = area
            best = (num, pid, x, y, ww, hh)
        }
    }
    return best
}

func windowBounds(_ window: [String: Any]) -> CGRect? {
    guard let bounds = window[kCGWindowBounds as String] as? [String: Any] else { return nil }
    return CGRect(dictionaryRepresentation: bounds as CFDictionary)
}

func isFrontmostGameWindow(_ id: Int, _ pid: Int, in windows: [[String: Any]]) -> Bool {
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid_t(pid) else {
        return false
    }
    let top = windows.first { window in
        guard (window[kCGWindowLayer as String] as? Int) == 0 else { return false }
        if (window[kCGWindowOwnerPID as String] as? Int) == pid,
           let bounds = windowBounds(window), bounds.width <= 52, bounds.height <= 20 {
            return false // Electron's small auxiliary window does not cover the canvas.
        }
        return true
    }
    return (top?[kCGWindowNumber as String] as? Int) == id
        && (top?[kCGWindowOwnerPID as String] as? Int) == pid
}

func topWindow(at point: CGPoint, in windows: [[String: Any]]) -> Int? {
    let top = windows.first { window in
        let alpha = window[kCGWindowAlpha as String] as? Double ?? 1
        // The Dock's layer-20 compositing surface covers the entire screen.
        return (window[kCGWindowLayer as String] as? Int) == 0
            && alpha > 0 && (windowBounds(window)?.contains(point) ?? false)
    }
    return top?[kCGWindowNumber as String] as? Int
}

func gameApp() -> NSRunningApplication? {
    // Prefer the process that actually OWNS the game window.  The app ships helper
    // processes ("SeerGame Helper (Renderer)" …) whose names also contain the hint;
    // activating one of those brings nothing forward and the real window drops off
    // the window list — which looked like "the client vanished" for a whole session.
    if let pid = gamePid() ?? nil, let app = NSRunningApplication(processIdentifier: pid) {
        return app
    }
    return NSWorkspace.shared.runningApplications.first { app in
        let s = ((app.bundleIdentifier ?? "") + "|" + (app.localizedName ?? "")).lowercased()
        guard TARGET_HINTS.contains(where: { s.contains($0) }) else { return false }
        return !s.contains("helper")
    }
}

/// The AX window that corresponds to the game canvas (largest), not `arr.first`.
func axWindow() -> AXUIElement? {
    guard let app = gameApp() else { return nil }
    let ax = AXUIElementCreateApplication(app.processIdentifier)
    var wins: CFTypeRef?
    guard AXUIElementCopyAttributeValue(ax, kAXWindowsAttribute as CFString, &wins) == .success,
          let arr = wins as? [AXUIElement] else { return nil }
    var best: AXUIElement? = nil
    var bestArea = 0.0
    for w in arr {
        var szRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(w, kAXSizeAttribute as CFString, &szRef) == .success,
              let v = szRef, CFGetTypeID(v) == AXValueGetTypeID() else { continue }
        var sz = CGSize.zero
        AXValueGetValue(v as! AXValue, .cgSize, &sz)
        guard sz.width >= 200, sz.height >= 200 else { continue }
        let area = Double(sz.width * sz.height)
        if area > bestArea { bestArea = area; best = w }
    }
    return best ?? arr.first
}

func pinWindow(_ x: Double, _ y: Double, _ w: Double, _ h: Double) -> [String: Any] {
    guard let win = axWindow() else { return ["error": "no window"] }
    var p = CGPoint(x: x, y: y)
    var sz = CGSize(width: w, height: h)
    AXUIElementSetAttributeValue(win, kAXPositionAttribute as CFString, AXValueCreate(.cgPoint, &p)!)
    AXUIElementSetAttributeValue(win, kAXSizeAttribute as CFString, AXValueCreate(.cgSize, &sz)!)
    usleep(120_000)
    guard let f = findGameWindow() else { return ["error": "no window"] }
    return ["id": f.id, "x": f.x, "y": f.y, "w": f.w, "h": f.h]
}

// --------------------------------------------------------------------------
// input (CGEvent) — offsets mirror scripts/seerd.swift so behaviour is familiar
// --------------------------------------------------------------------------
func click(_ gx: Double, _ gy: Double, _ n: Int) {
    let src = CGEventSource(stateID: .hidSystemState)
    let p = CGPoint(x: gx, y: gy)
    CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?
        .post(tap: .cghidEventTap)
    usleep(8_000)                       // was 15 ms in seerd; 8 ms is enough for Flash hit-test
    for i in 1...max(1, n) {
        let d = CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)
        let u = CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)
        d?.setIntegerValueField(.mouseEventClickState, value: Int64(i))
        u?.setIntegerValueField(.mouseEventClickState, value: Int64(i))
        d?.post(tap: .cghidEventTap)
        usleep(10_000)
        u?.post(tap: .cghidEventTap)
        usleep(12_000)                  // was 40 ms; spacing between double-clicks only
    }
}

// MARK: - background input (no cursor movement, window may be occluded)

//: CGEventPostToPid delivers the event to the target process only: the system
//: cursor never moves and the app does not have to be frontmost, so a run can
//: continue while the machine is used for something else.  Whether the Flash
//: plugin inside Electron accepts such events is an empirical question — verified
//: by clicking a known button and looking for its effect (see tools/ctl.py).
func gamePid() -> pid_t? {
    for w in listWindows() {
        let owner = (w[kCGWindowOwnerName as String] as? String ?? "").lowercased()
        guard TARGET_HINTS.contains(where: { owner.contains($0) }) else { continue }
        if let pid = w[kCGWindowOwnerPID as String] as? Int, pid > 0 {
            return pid_t(pid)
        }
    }
    return nil
}

//: Stamp the target window into the event.  Chromium (Electron) routes a posted
//: event to the window named in these two fields instead of the key window, which
//: is what makes a background click land while another app holds the focus.
func stampWindow(_ event: CGEvent?, _ winID: Int) {
    guard let e = event, winID > 0 else { return }
    e.setIntegerValueField(.mouseEventWindowUnderMousePointer, value: Int64(winID))
    e.setIntegerValueField(.mouseEventWindowUnderMousePointerThatCanHandleThisEvent,
                           value: Int64(winID))
}

func gameWindowID() -> Int {
    return findGameWindow()?.id ?? 0
}

func clickPid(_ gx: Double, _ gy: Double, _ n: Int) -> [String: Any] {
    guard let pid = gamePid() else { return ["error": "no window"] }
    let winID = gameWindowID()
    let src = CGEventSource(stateID: .hidSystemState)
    let p = CGPoint(x: gx, y: gy)
    let mv = CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: p,
                     mouseButton: .left)
    stampWindow(mv, winID)
    mv?.postToPid(pid)
    usleep(8_000)
    for i in 1...max(1, n) {
        let d = CGEvent(mouseEventSource: src, mouseType: .leftMouseDown,
                        mouseCursorPosition: p, mouseButton: .left)
        let u = CGEvent(mouseEventSource: src, mouseType: .leftMouseUp,
                        mouseCursorPosition: p, mouseButton: .left)
        d?.setIntegerValueField(.mouseEventClickState, value: Int64(i))
        u?.setIntegerValueField(.mouseEventClickState, value: Int64(i))
        stampWindow(d, winID)
        stampWindow(u, winID)
        d?.postToPid(pid)
        usleep(10_000)
        u?.postToPid(pid)
        usleep(12_000)
    }
    return ["ok": true, "pid": Int(pid)]
}

//: Drag with the pointer posted to the game process and the real cursor untouched.
//: Scrollbars in this client are draggable thumbs; a click on the track does
//: nothing, which is why paging a server list needs an actual drag.
func dragPid(_ x1: Double, _ y1: Double, _ x2: Double, _ y2: Double,
             _ steps: Int) -> [String: Any] {
    guard let pid = gamePid() else { return ["error": "no window"] }
    let winID = gameWindowID()
    let src = CGEventSource(stateID: .hidSystemState)
    let a = CGPoint(x: x1, y: y1)
    let b = CGPoint(x: x2, y: y2)
    let n = max(2, steps)
    let down = CGEvent(mouseEventSource: src, mouseType: .leftMouseDown,
                       mouseCursorPosition: a, mouseButton: .left)
    stampWindow(down, winID)
    down?.postToPid(pid)
    usleep(60_000)
    for i in 1...n {
        let t = Double(i) / Double(n)
        let p = CGPoint(x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t)
        let mv = CGEvent(mouseEventSource: src, mouseType: .leftMouseDragged,
                         mouseCursorPosition: p, mouseButton: .left)
        stampWindow(mv, winID)
        mv?.postToPid(pid)
        usleep(28_000)
    }
    usleep(60_000)
    let up = CGEvent(mouseEventSource: src, mouseType: .leftMouseUp,
                     mouseCursorPosition: b, mouseButton: .left)
    stampWindow(up, winID)
    up?.postToPid(pid)
    return ["ok": true]
}

func movePid(_ gx: Double, _ gy: Double) -> [String: Any] {
    guard let pid = gamePid() else { return ["error": "no window"] }
    let src = CGEventSource(stateID: .hidSystemState)
    let e = CGEvent(mouseEventSource: src, mouseType: .mouseMoved,
                    mouseCursorPosition: CGPoint(x: gx, y: gy), mouseButton: .left)
    stampWindow(e, gameWindowID())
    e?.postToPid(pid)
    return ["ok": true]
}

func keyPid(_ code: UInt16) -> [String: Any] {
    guard let pid = gamePid() else { return ["error": "no window"] }
    let src = CGEventSource(stateID: .hidSystemState)
    CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: true)?.postToPid(pid)
    usleep(10_000)
    CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: false)?.postToPid(pid)
    return ["ok": true]
}

//: Drag with REAL events (HID tap): the cursor moves, the app sees a genuine press/
//: move/release stream.  This is what a Flash scrollbar needs — the posted variant
//: never reaches the plugin.
func drag(_ x1: Double, _ y1: Double, _ x2: Double, _ y2: Double, _ steps: Int) {
    let src = CGEventSource(stateID: .hidSystemState)
    let a = CGPoint(x: x1, y: y1)
    let b = CGPoint(x: x2, y: y2)
    let n = max(2, steps)
    CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: a,
            mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(80_000)
    CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: a,
            mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(80_000)
    for i in 1...n {
        let t = Double(i) / Double(n)
        let p = CGPoint(x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t)
        CGEvent(mouseEventSource: src, mouseType: .leftMouseDragged, mouseCursorPosition: p,
                mouseButton: .left)?.post(tap: .cghidEventTap)
        usleep(25_000)
    }
    usleep(60_000)
    CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: b,
            mouseButton: .left)?.post(tap: .cghidEventTap)
}

func move(_ gx: Double, _ gy: Double) {
    let src = CGEventSource(stateID: .hidSystemState)
    CGEvent(mouseEventSource: src, mouseType: .mouseMoved,
            mouseCursorPosition: CGPoint(x: gx, y: gy), mouseButton: .left)?
        .post(tap: .cghidEventTap)
}

func keyPress(_ code: UInt16) {
    let src = CGEventSource(stateID: .hidSystemState)
    CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: true)?.post(tap: .cghidEventTap)
    usleep(10_000)
    CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: false)?.post(tap: .cghidEventTap)
}

func typeText(_ s: String) {
    let src = CGEventSource(stateID: .hidSystemState)
    for ch in s.unicodeScalars {
        var u = UniChar(ch.value)
        if let d = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: true) {
            d.keyboardSetUnicodeString(stringLength: 1, unicodeString: &u); d.post(tap: .cghidEventTap)
        }
        if let up = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: false) {
            up.keyboardSetUnicodeString(stringLength: 1, unicodeString: &u); up.post(tap: .cghidEventTap)
        }
        usleep(6_000)
    }
}

// --------------------------------------------------------------------------
// OCR (Vision) — same normalised boxes as seerctl
// --------------------------------------------------------------------------
func ocrFile(_ path: String, _ minConf: Float) -> [[String: Any]]? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { return nil }
    // force a real decode: the handle alone is lazy and would make this "free"
    let wpx = img.width, hpx = img.height
    var decoded = [UInt8](repeating: 0, count: wpx * hpx * 4)
    guard let ctx = CGContext(data: &decoded, width: wpx, height: hpx,
                              bitsPerComponent: 8, bytesPerRow: wpx * 4,
                              space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
    ctx.draw(img, in: CGRect(x: 0, y: 0, width: wpx, height: hpx))

    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    do { try VNImageRequestHandler(cgImage: img, options: [:]).perform([req]) } catch { return nil }
    var out: [[String: Any]] = []
    for obs in (req.results ?? []) {
        guard let c = obs.topCandidates(1).first, c.confidence >= minConf else { continue }
        let bb = obs.boundingBox
        out.append(["text": c.string, "conf": Double(c.confidence),
                    "x": Double(bb.origin.x), "y": Double(bb.origin.y),
                    "w": Double(bb.size.width), "h": Double(bb.size.height)])
    }
    return out
}

// --------------------------------------------------------------------------
// daemon state
// --------------------------------------------------------------------------
final class Daemon {
    let store: FrameStore
    var stream: SCStream?
    var tap: Tap?
    var sessionRect: CGRect?

    init(framePath: String) {
        store = FrameStore(framePath: framePath)
    }

    func caps() -> [String] {
        var ops = ["ping", "caps", "permissions", "win", "pin", "activate", "quit",
                   "shot", "ocr", "click", "click.guarded", "move", "key", "type",
                   // background lane: events posted straight to the game process,
                   // so the cursor never moves and the window need not be frontmost
                   "clickpid", "movepid", "keypid", "drag", "dragpid", "cursorpos"]
        if stream != nil {
            ops += ["capture.latest", "capture.roi", "capture.stop"]
        }
        ops.append("capture.start")
        return ops
    }

    // -- SCK session ------------------------------------------------------- //
    func startStream(rect: CGRect?) -> [String: Any] {
        if stream != nil, sessionRect == rect { return ["ok": true, "seq": Int(store.seq)] }
        stopStream()

        let content: SCShareableContent
        switch AsyncBridge.run(timeout: 8.0, { try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true) }) {
        case .success(let c): content = c
        case .failure(let e): return ["error": "permission denied or SCK unavailable: \(e.localizedDescription)"]
        }

        guard let f = findGameWindow() else { return ["error": "no window"] }
        guard let win = content.windows.first(where: { $0.windowID == CGWindowID(f.id) }) else {
            return ["error": "no window (not composited yet; is the game visible on screen?)"]
        }

        let filter = SCContentFilter(desktopIndependentWindow: win)
        let cfg = SCStreamConfiguration()
        let scale = max(1.0, Double(filter.pointPixelScale))
        let rectPx: CGRect
        if let r = rect {
            // rect is game_px (device pixels of the reference capture)
            rectPx = r
        } else {
            rectPx = CGRect(x: 0, y: 0, width: win.frame.width * scale, height: win.frame.height * scale)
        }
        cfg.width = Int(rectPx.width.rounded())
        cfg.height = Int(rectPx.height.rounded())
        cfg.pixelFormat = kCVPixelFormatType_32BGRA
        cfg.showsCursor = false
        cfg.queueDepth = 3
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 60)
        if rect != nil {
            // sourceRect is in the *window's* point space, not device pixels
            cfg.sourceRect = CGRect(x: rectPx.origin.x / scale, y: rectPx.origin.y / scale,
                                    width: rectPx.width / scale, height: rectPx.height / scale)
        }

        let s = SCStream(filter: filter, configuration: cfg, delegate: nil)
        let t = Tap(store: store)
        do {
            try s.addStreamOutput(t, type: .screen, sampleHandlerQueue: DispatchQueue(label: "seerdx.tap"))
        } catch {
            return ["error": "addStreamOutput failed: \(error.localizedDescription)"]
        }
        switch AsyncBridge.run(timeout: 10.0, { try await s.startCapture() }) {
        case .failure(let e):
            return ["error": "startCapture failed: \(e.localizedDescription)"]
        case .success:
            break
        }
        stream = s; tap = t; sessionRect = rect

        // Give the first frame a bounded chance to arrive so the caller does not
        // immediately see "no frame yet". This is a startup cost, not a per-tick one.
        let deadline = Date().addingTimeInterval(2.0)
        while store.seq == 0 && Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.01))
        }
        return ["ok": true, "seq": Int(store.seq), "w": store.w, "h": store.h]
    }

    func stopStream() {
        guard let s = stream else { return }
        _ = AsyncBridge.run(timeout: 5.0, { try await s.stopCapture() })
        stream = nil; tap = nil; sessionRect = nil
        store.reset()
    }

    // -- one-shot screenshot (needs the pumping bridge) --------------------- //
    func shot(_ path: String, rect: CGRect?) -> [String: Any] {
        let content: SCShareableContent
        switch AsyncBridge.run(timeout: 8.0, { try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true) }) {
        case .success(let c): content = c
        case .failure(let e): return ["error": "permission denied or SCK unavailable: \(e.localizedDescription)"]
        }
        guard let f = findGameWindow() else { return ["error": "no window"] }
        guard let win = content.windows.first(where: { $0.windowID == CGWindowID(f.id) }) else {
            return ["error": "no window (not composited yet)"]
        }
        let filter = SCContentFilter(desktopIndependentWindow: win)
        let cfg = SCStreamConfiguration()
        let scale = max(1.0, Double(filter.pointPixelScale))
        let r = rect ?? CGRect(x: 0, y: 0, width: win.frame.width * scale, height: win.frame.height * scale)
        cfg.width = Int(r.width.rounded()); cfg.height = Int(r.height.rounded())
        cfg.showsCursor = false
        if rect != nil {
            cfg.sourceRect = CGRect(x: r.origin.x / scale, y: r.origin.y / scale,
                                    width: r.width / scale, height: r.height / scale)
        }
        switch AsyncBridge.run(timeout: 10.0, { try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg) }) {
        case .failure(let e):
            return ["error": "captureImage failed: \(e.localizedDescription)"]
        case .success(let img):
            guard let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: path) as CFURL,
                                                             UTType.png.identifier as CFString, 1, nil) else {
                return ["error": "cannot write \(path)"]
            }
            CGImageDestinationAddImage(dest, img, nil)
            guard CGImageDestinationFinalize(dest) else { return ["error": "PNG encode failed"] }
            return ["path": path, "w": img.width, "h": img.height]
        }
    }

    // -- dispatch ---------------------------------------------------------- //
    func handle(_ obj: [String: Any]) -> [String: Any] {
        let op = obj["op"] as? String ?? ""
        switch op {
        case "ping": return ["ok": true]
        case "caps": return ["ops": caps()]
        case "permissions":
            return ["screen_capture": CGPreflightScreenCaptureAccess(),
                    "post_event": CGPreflightPostEventAccess()]
        case "win":
            let windows = listWindows()
            guard let f = findGameWindow(in: windows) else { return ["error": "no window"] }
            return ["id": f.id, "pid": f.pid, "x": f.x, "y": f.y, "w": f.w, "h": f.h,
                    "frontmost": isFrontmostGameWindow(f.id, f.pid, in: windows)]
        case "pin":
            return pinWindow(obj["x"] as? Double ?? 300, obj["y"] as? Double ?? 120,
                             obj["w"] as? Double ?? 1149, obj["h"] as? Double ?? 714)
        case "activate":
            gameApp()?.activate(options: [.activateAllWindows])
            return ["ok": true]
        case "capture.start":
            var rect: CGRect? = nil
            if let r = obj["rect"] as? [Double], r.count == 4 {
                rect = CGRect(x: r[0], y: r[1], width: r[2], height: r[3])
            }
            return startStream(rect: rect)
        case "capture.latest":
            guard stream != nil else { return ["error": "no stream (call capture.start first)"] }
            guard let reply = store.materialize() else { return ["error": "no frame yet"] }
            return reply
        case "capture.roi":
            guard stream != nil else { return ["error": "no stream (call capture.start first)"] }
            guard let r = obj["rect"] as? [Double], r.count == 4 else { return ["error": "rect required"] }
            let path = (obj["path"] as? String) ?? "/tmp/seerdx_roi_\(getpid()).bgra"
            guard let reply = store.materializeROI(x: Int(r[0]), y: Int(r[1]),
                                                   rw: Int(r[2]), rh: Int(r[3]), path: path) else {
                return ["error": "no frame yet"]
            }
            return reply
        case "capture.stop":
            stopStream(); return ["ok": true]
        case "shot":
            var rect: CGRect? = nil
            if let r = obj["rect"] as? [Double], r.count == 4 {
                rect = CGRect(x: r[0], y: r[1], width: r[2], height: r[3])
            }
            return shot((obj["path"] as? String) ?? "/tmp/seerdx_shot.png", rect: rect)
        case "ocr":
            let path = (obj["path"] as? String) ?? "/tmp/seerdx_shot.png"
            guard let items = ocrFile(path, Float(obj["min"] as? Double ?? 0.3)) else {
                return ["error": "OCR could not read the image"]
            }
            return ["items": items]
        case "click.guarded":
            guard CGPreflightPostEventAccess() else {
                return ["error": "macOS Accessibility denies posting input events"]
            }
            let windows = listWindows()
            guard let f = findGameWindow(in: windows),
                  f.id == obj["id"] as? Int, f.pid == obj["pid"] as? Int,
                  isFrontmostGameWindow(f.id, f.pid, in: windows),
                  let bounds = obj["bounds"] as? [Double], bounds.count == 4,
                  zip([f.x, f.y, f.w, f.h], bounds).allSatisfy({ abs($0.0 - $0.1) <= 2 }),
                  let x = obj["x"] as? Double, let y = obj["y"] as? Double,
                  x >= 0, y >= 0, x < f.w, y < f.h,
                  !FileManager.default.fileExists(atPath: "/tmp/seerauto.master.pause"),
                  !FileManager.default.fileExists(atPath: "/tmp/seerauto.pause"),
                  !FileManager.default.fileExists(atPath: "/tmp/seerauto.stop") else {
                return ["error": "guarded click refused"]
            }
            let point = CGPoint(x: f.x + x, y: f.y + y)
            guard topWindow(at: point, in: windows) == f.id else {
                return ["error": "guarded click point is covered"]
            }
            click(point.x, point.y, 1)
            return ["ok": true]
        case "click":
            click(obj["x"] as? Double ?? 0, obj["y"] as? Double ?? 0, obj["n"] as? Int ?? 1)
            return ["ok": true]
        case "move":
            move(obj["x"] as? Double ?? 0, obj["y"] as? Double ?? 0)
            return ["ok": true]
        case "cursorpos":
            let p = CGEvent(source: nil)?.location ?? .zero
            return ["x": Double(p.x), "y": Double(p.y)]
        case "drag":
            drag(obj["x1"] as? Double ?? 0, obj["y1"] as? Double ?? 0,
                 obj["x2"] as? Double ?? 0, obj["y2"] as? Double ?? 0,
                 obj["steps"] as? Int ?? 14)
            return ["ok": true]
        case "dragpid":
            return dragPid(obj["x1"] as? Double ?? 0, obj["y1"] as? Double ?? 0,
                           obj["x2"] as? Double ?? 0, obj["y2"] as? Double ?? 0,
                           obj["steps"] as? Int ?? 14)
        case "clickpid":
            return clickPid(obj["x"] as? Double ?? 0, obj["y"] as? Double ?? 0,
                            obj["n"] as? Int ?? 1)
        case "movepid":
            return movePid(obj["x"] as? Double ?? 0, obj["y"] as? Double ?? 0)
        case "keypid":
            return keyPid(UInt16(obj["code"] as? Int ?? 53))
        case "key":
            keyPress(UInt16(obj["code"] as? Int ?? 53))
            return ["ok": true]
        case "type":
            typeText(obj["text"] as? String ?? "")
            return ["ok": true]
        case "quit":
            stopStream()
            return ["bye": true]
        default:
            return ["error": "unknown op \(op)"]
        }
    }
}

// --------------------------------------------------------------------------
// main: stdin on a background thread, pumping run loop on the main thread
// --------------------------------------------------------------------------
// Explicit operator actions. Ordinary capture and input never request permissions.
if CommandLine.arguments.contains("--request-capture-access") {
    emit(["screen_capture": CGRequestScreenCaptureAccess()])
    exit(0)
}
if CommandLine.arguments.contains("--request-input-access") {
    emit(["post_event": CGRequestPostEventAccess()])
    exit(0)
}

if let i = CommandLine.arguments.firstIndex(of: "--target"), i + 1 < CommandLine.arguments.count {
    TARGET_HINTS = [CommandLine.arguments[i + 1].lowercased()]
    log("target override: \(TARGET_HINTS)")
}

// Per-process frame path.  A single fixed path is shared by every daemon on the
// machine: with two sessions alive (e.g. a foreground operator session and a
// background nohup run) one daemon can read the *other* daemon's frame file while
// trusting its own seq/w/h/stride — which is exactly the "constant dark frame in
// the background process" report of 2026-11-07.  Python reads reply["path"], so a
// per-pid path is transparent to every client.
let framePath = "/tmp/seerdx_frame_\(getpid()).bgra"
let daemon = Daemon(framePath: framePath)
let commandQueue = DispatchQueue(label: "seerdx.commands")
var pending: [(String?, DispatchSemaphore)] = []   // (line, done) — guarded by queue
let pendingLock = NSLock()
var shouldQuit = false

Thread.detachNewThread {
    while let line = readLine(strippingNewline: true) {
        if line.isEmpty { continue }
        // hand the line to the main thread and wait for it to be processed
        let done = DispatchSemaphore(value: 0)
        pendingLock.lock(); pending.append((line, done)); pendingLock.unlock()
        done.wait()
        if line.contains("\"quit\"") { break }
    }
    shouldQuit = true
}

_ = NSApplication.shared          // required: gives the process a CG/WindowServer connection
NSApp.setActivationPolicy(.accessory)

log("up — \(daemon.caps().joined(separator: ","))")

while !shouldQuit {
    pendingLock.lock()
    let batch = pending
    pending.removeAll()
    pendingLock.unlock()

    for (line, done) in batch {
        defer { done.signal() }
        guard let line = line,
              let data = line.data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            emit(["error": "bad json"]); continue
        }
        let reply = daemon.handle(obj)
        emit(reply)
        if (obj["op"] as? String) == "quit" { shouldQuit = true }
    }

    RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.002))
}
log("bye")
