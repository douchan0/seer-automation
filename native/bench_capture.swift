// bench_capture.swift — in-process macOS capture / OCR latency benchmark harness
//
// Part of bench-engineer's R1-A work. Non-intrusive: never moves the mouse,
// never posts synthetic events, never activates or resizes any window.
//
// IMPORTANT SDK NOTE (measured, not assumed):
//   On this machine (macOS 15.7.7 / Swift 6.2.3 SDK) the classic CoreGraphics
//   capture APIs are HARD COMPILE ERRORS:
//     CGWindowListCreateImage(...)  -> "'CGWindowListCreateImage' is unavailable
//                                      in macOS: Please use ScreenCaptureKit instead."
//                                      (obsoleted in macOS 15.0)
//     CGDisplayCreateImage(...)     -> same class of error
//   So there is no "CGWindowList vs SCK" choice on this OS. The capture matrix
//   below is: ScreenCaptureKit (screenshot + stream) vs the screencapture
//   subprocess baseline that scripts/seerd.swift actually uses.
//   CGWindowListCopyWindowInfo() is still available and is measured separately
//   because seerd.findWin() calls it on every shot.
//
// Usage:
//   bench_capture probe
//   bench_capture capture  [--iters N] [--warmup N] [--loop-iters N]
//                          [--window auto|WID] [--roi x,y,w,h] [--only a,b,c]
//   bench_capture ocr --file PATH [--level fast|accurate] [--rect x,y,w,h]
//                     [--iters N] [--warmup N] [--langs zh-Hans,en-US]
//   bench_capture windows
//
// All timings are wall-clock monotonic (CLOCK_UPTIME_RAW) in milliseconds,
// measured around the operation only. Raw per-iteration samples are emitted so
// the Python orchestrator can recompute statistics independently.
//
// ScreenCaptureKit concurrency note: SCScreenshotManager and
// SCShareableContent are async APIs. Instead of fighting Swift 6
// async/Sendable checking inside a timing harness, this file bridges the
// 3-argument callback forms into synchronous calls with DispatchSemaphore +
// @unchecked Sendable boxes, so every measurement happens on one thread with
// no cooperative-pool scheduling noise folded into the number. SCStream
// delivers frames on its own serial queue; those timestamps are collected
// there and analysed after the fact.

import Foundation
import CoreGraphics
import ImageIO
import AppKit
import Vision
import ScreenCaptureKit
import CoreVideo
import QuartzCore

// MARK: - timing

@inline(__always) func monoMS() -> Double {
    return Double(clock_gettime_nsec_np(CLOCK_UPTIME_RAW)) / 1_000_000.0
}

/// Wait for a semaphore WITHOUT blocking the main run loop.
///
/// SCK's async completion can need the main run loop to be serviced (this is
/// required at least when the captured window belongs to this process). A bare
/// `sem.wait()` on the main thread therefore deadlocks: measured on this machine
/// as a 13-minute hang with the main thread parked in
/// `_dispatch_semaphore_wait_slow` under `sckScreenshotSync`.
///
/// So: poll the semaphore with a zero timeout and pump the run loop between
/// polls, and always give up after `timeoutMs` so nothing can hang forever.
func waitPumping(_ sem: DispatchSemaphore, timeoutMs: Double = 15000) -> Bool {
    let deadline = monoMS() + timeoutMs
    while monoMS() < deadline {
        if sem.wait(timeout: .now()) == .success { return true }
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.002))
    }
    return false
}

// MARK: - stats

func computeStats(_ xs: [Double]) -> [String: Double] {
    guard !xs.isEmpty else { return ["n": 0] }
    let s = xs.sorted()
    let mean = xs.reduce(0, +) / Double(xs.count)
    func pct(_ p: Double) -> Double {
        if s.count == 1 { return s[0] }
        let idx = p * Double(s.count - 1)
        let lo = Int(floor(idx)), hi = Int(ceil(idx))
        let frac = idx - Double(lo)
        return s[lo] * (1.0 - frac) + s[hi] * frac
    }
    return [
        "n": Double(xs.count),
        "mean": mean,
        "p50": pct(0.50),
        "p95": pct(0.95),
        "min": s[0],
        "max": s[s.count - 1],
    ]
}

// MARK: - output

func emit(_ obj: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

func logErr(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

func sysctlString(_ name: String) -> String? {
    var size = 0
    guard sysctlbyname(name, nil, &size, nil, 0) == 0, size > 0 else { return nil }
    var buf = [CChar](repeating: 0, count: size)
    guard sysctlbyname(name, &buf, &size, nil, 0) == 0 else { return nil }
    return String(cString: buf)
}

func utsNameMachine() -> String {
    var u = utsname()
    uname(&u)
    return withUnsafeBytes(of: &u.machine) { raw -> String in
        let bytes = raw.prefix(while: { $0 != 0 })
        return String(decoding: bytes, as: UTF8.self)
    }
}

// MARK: - args

struct Args {
    var positional: [String] = []
    var flags: [String: String] = [:]
    init(_ argv: [String]) {
        var i = 0
        while i < argv.count {
            let a = argv[i]
            if a.hasPrefix("--") {
                let key = String(a.dropFirst(2))
                if i + 1 < argv.count && !argv[i + 1].hasPrefix("--") {
                    flags[key] = argv[i + 1]
                    i += 2
                } else {
                    flags[key] = "1"
                    i += 1
                }
            } else {
                positional.append(a)
                i += 1
            }
        }
    }
    func int(_ k: String, _ d: Int) -> Int { flags[k].flatMap { Int($0) } ?? d }
    func str(_ k: String, _ d: String) -> String { flags[k] ?? d }
    func double(_ k: String, _ d: Double) -> Double { flags[k].flatMap { Double($0) } ?? d }
    func has(_ k: String) -> Bool { flags[k] != nil }
}

func parseRect(_ s: String) -> CGRect? {
    let parts = s.split(separator: ",").compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
    guard parts.count == 4 else { return nil }
    return CGRect(x: parts[0], y: parts[1], width: parts[2], height: parts[3])
}

// MARK: - environment probe

func displayInfo() -> [[String: Any]] {
    var count: UInt32 = 0
    CGGetActiveDisplayList(0, nil, &count)
    var ids = [CGDirectDisplayID](repeating: 0, count: Int(max(count, 1)))
    CGGetActiveDisplayList(count, &ids, &count)
    var out: [[String: Any]] = []
    for id in ids.prefix(Int(count)) {
        let b = CGDisplayBounds(id)
        out.append([
            "id": Int(id),
            "isMain": id == CGMainDisplayID(),
            "bounds_x": Double(b.origin.x),
            "bounds_y": Double(b.origin.y),
            "bounds_w": Double(b.width),
            "bounds_h": Double(b.height),
            "pixels_w": CGDisplayPixelsWide(id),
            "pixels_h": CGDisplayPixelsHigh(id),
        ])
    }
    return out
}

func screenInfo() -> [[String: Any]] {
    return NSScreen.screens.map { s in
        [
            "frame_x": Double(s.frame.origin.x),
            "frame_y": Double(s.frame.origin.y),
            "frame_w": Double(s.frame.width),
            "frame_h": Double(s.frame.height),
            "backingScaleFactor": Double(s.backingScaleFactor),
            "localizedName": s.localizedName,
        ]
    }
}

struct WinInfo {
    var id: Int
    var owner: String
    var title: String
    var layer: Int
    var x: Double
    var y: Double
    var w: Double
    var h: Double
    var pid: Int
    var alpha: Double
}

func enumerateWindows() -> [WinInfo] {
    let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else { return [] }
    var out: [WinInfo] = []
    for w in list {
        let owner = w[kCGWindowOwnerName as String] as? String ?? ""
        let title = w[kCGWindowName as String] as? String ?? ""
        let num = w[kCGWindowNumber as String] as? Int ?? -1
        let layer = w[kCGWindowLayer as String] as? Int ?? 0
        let pid = w[kCGWindowOwnerPID as String] as? Int ?? -1
        let alpha = w[kCGWindowAlpha as String] as? Double ?? 1.0
        var x = 0.0, y = 0.0, ww = 0.0, hh = 0.0
        if let bd = w[kCGWindowBounds as String] as? [String: Any] {
            x = (bd["X"] as? Double) ?? 0
            y = (bd["Y"] as? Double) ?? 0
            ww = (bd["Width"] as? Double) ?? 0
            hh = (bd["Height"] as? Double) ?? 0
        }
        out.append(WinInfo(id: num, owner: owner, title: title, layer: layer,
                           x: x, y: y, w: ww, h: hh, pid: pid, alpha: alpha))
    }
    return out
}

func probe() -> [String: Any] {
    let windows = enumerateWindows()
    let myPid = Int(ProcessInfo.processInfo.processIdentifier)
    var candidates: [[String: Any]] = []
    for w in windows where w.layer == 0 && w.w > 400 && w.h > 300 && w.id > 0 && w.alpha > 0.5 {
        candidates.append([
            "id": w.id, "owner": w.owner, "title": w.title,
            "x": w.x, "y": w.y, "w": w.w, "h": w.h,
            "pid": w.pid, "isSelf": w.pid == myPid,
        ])
    }
    candidates.sort { ($0["w"] as! Double) * ($0["h"] as! Double) > ($1["w"] as! Double) * ($1["h"] as! Double) }

    var d: [String: Any] = [:]
    d["os_version"] = ProcessInfo.processInfo.operatingSystemVersionString
    d["machine_arch"] = utsNameMachine()
    d["hw_model"] = sysctlString("hw.model") ?? "unknown"
    d["cpu_brand"] = sysctlString("machdep.cpu.brand_string") ?? "unknown"
    d["logical_cpus"] = ProcessInfo.processInfo.processorCount
    d["physical_memory_bytes"] = Int(ProcessInfo.processInfo.physicalMemory)
    d["process_id"] = myPid
    d["process_name"] = ProcessInfo.processInfo.processName
    d["screen_recording_granted"] = CGPreflightScreenCaptureAccess()
    d["displays"] = displayInfo()
    d["screens"] = screenInfo()
    d["main_display_id"] = Int(CGMainDisplayID())
    d["on_screen_window_count"] = windows.count
    d["candidate_windows"] = candidates
    let gameWins = windows.filter { $0.owner.lowercased().contains("seer") || $0.owner.lowercased().contains("yahaha") }
    d["seer_game_window_count"] = gameWins.count
    if let g = gameWins.first {
        d["seer_game_window"] = ["id": g.id, "owner": g.owner, "w": g.w, "h": g.h, "x": g.x, "y": g.y]
    }
    d["screencapture_exists"] = FileManager.default.fileExists(atPath: "/usr/sbin/screencapture")
    // Which capture API family is even compilable on this SDK (documented fact).
    d["cgwindowlistcreateimage_available"] = false
    d["cgdisplaycreateimage_available"] = false
    d["capture_api_note"] = "CGWindowListCreateImage/CGDisplayCreateImage are obsoleted in macOS 15.0 and fail to compile; ScreenCaptureKit is the only in-process option"
    return d
}

// MARK: - ScreenCaptureKit bridges (callback -> synchronous)

final class SCBox: @unchecked Sendable {
    var image: CGImage?
    var content: SCShareableContent?
    var error: Error?
}

@available(macOS 14.0, *)
func sckScreenshotSync(filter: SCContentFilter, config: SCStreamConfiguration) throws -> CGImage {
    let box = SCBox()
    let sem = DispatchSemaphore(value: 0)
    SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) { img, err in
        box.image = img
        box.error = err
        sem.signal()
    }
    guard waitPumping(sem) else {
        throw NSError(domain: "bench_capture", code: 99,
                      userInfo: [NSLocalizedDescriptionKey: "timeout waiting for ScreenCaptureKit"])
    }
    if let e = box.error { throw e }
    guard let img = box.image else {
        throw NSError(domain: "bench_capture", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "SCScreenshotManager returned nil image with no error"])
    }
    return img
}

func sckShareableSync(onScreenOnly: Bool) throws -> SCShareableContent {
    let box = SCBox()
    let sem = DispatchSemaphore(value: 0)
    SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: onScreenOnly) { c, e in
        box.content = c
        box.error = e
        sem.signal()
    }
    guard waitPumping(sem) else {
        throw NSError(domain: "bench_capture", code: 99,
                      userInfo: [NSLocalizedDescriptionKey: "timeout waiting for ScreenCaptureKit"])
    }
    if let e = box.error { throw e }
    guard let c = box.content else {
        throw NSError(domain: "bench_capture", code: 2,
                      userInfo: [NSLocalizedDescriptionKey: "SCShareableContent returned nil with no error"])
    }
    return c
}

// MARK: - SCStream frame sink

final class FrameSink: NSObject, SCStreamOutput, @unchecked Sendable {
    private let lock = NSLock()
    private(set) var times: [Double] = []
    private(set) var firstImage: CGImage?
    private(set) var stopError: Error?
    private(set) var statuses: [Int] = []
    let wantCount: Int
    let sem = DispatchSemaphore(value: 0)
    let wantsImage: Bool

    init(wantCount: Int, wantsImage: Bool) {
        self.wantCount = wantCount
        self.wantsImage = wantsImage
        super.init()
    }

    var count: Int { lock.lock(); defer { lock.unlock() }; return times.count }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen else { return }
        guard CMSampleBufferIsValid(sb) else { return }
        var statusCode = -1
        if let atts = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
           let raw = atts.first?[.status] as? Int {
            statusCode = raw
        }
        // only count fully composed frames
        if let s = SCFrameStatus(rawValue: statusCode), s != .complete {
            lock.lock(); statuses.append(statusCode); lock.unlock()
            return
        }
        let t = monoMS()
        var img: CGImage?
        if wantsImage {
            lock.lock()
            let need = (firstImage == nil)
            lock.unlock()
            if need, let pb = CMSampleBufferGetImageBuffer(sb) {
                img = cgImageCopy(from: pb)
            }
        }
        lock.lock()
        times.append(t)
        if let img = img, firstImage == nil { firstImage = img }
        statuses.append(statusCode)
        let n = times.count
        lock.unlock()
        if n >= wantCount { sem.signal() }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        lock.lock(); stopError = error; lock.unlock()
        sem.signal()
    }
}

func cgImageCopy(from pb: CVPixelBuffer) -> CGImage? {
    CVPixelBufferLockBaseAddress(pb, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
    let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb)
    guard let base = CVPixelBufferGetBaseAddress(pb) else { return nil }
    let bpr = CVPixelBufferGetBytesPerRow(pb)
    let rowBytes = w * 4
    var buf = [UInt8](repeating: 0, count: rowBytes * h)
    buf.withUnsafeMutableBytes { dst in
        guard let d = dst.baseAddress else { return }
        for y in 0..<h {
            memcpy(d.advanced(by: y * rowBytes), base.advanced(by: y * bpr), rowBytes)
        }
    }
    let cs = CGColorSpaceCreateDeviceRGB()
    let info = CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
    guard let provider = CGDataProvider(data: Data(buf) as CFData) else { return nil }
    return CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32,
                   bytesPerRow: rowBytes, space: cs, bitmapInfo: info,
                   provider: provider, decode: nil, shouldInterpolate: false,
                   intent: .defaultIntent)
}

// MARK: - latest-frame store (the "read the current frame" primitive)

/// Mirrors how a game loop would actually consume an SCStream: the stream
/// callback stores the newest frame into a reusable buffer, and the control
/// loop asks for "the latest frame" whenever it needs pixels. Measures the
/// consumer-side handoff cost and how stale the pixels are at request time.
final class LatestFrameStore: @unchecked Sendable {
    private let lock = NSLock()
    private var buf: [UInt8] = []
    private var consumer: [UInt8] = []
    private var w = 0, h = 0, rowBytes = 0
    private(set) var lastFrameAt: Double = 0
    private(set) var frames = 0
    private(set) var width = 0
    private(set) var height = 0

    func store(_ pb: CVPixelBuffer) {
        CVPixelBufferLockBaseAddress(pb, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
        let pw = CVPixelBufferGetWidth(pb), ph = CVPixelBufferGetHeight(pb)
        guard let base = CVPixelBufferGetBaseAddress(pb) else { return }
        let bpr = CVPixelBufferGetBytesPerRow(pb)
        let rb = pw * 4
        let t = monoMS()
        lock.lock()
        if w != pw || h != ph {
            w = pw; h = ph; rowBytes = rb
            buf = [UInt8](repeating: 0, count: rb * ph)
        }
        buf.withUnsafeMutableBytes { dst in
            guard let d = dst.baseAddress else { return }
            for y in 0..<ph {
                memcpy(d.advanced(by: y * rb), base.advanced(by: y * bpr), rb)
            }
        }
        lastFrameAt = t
        frames += 1
        width = pw
        height = ph
        lock.unlock()
    }

    /// Consumer copy of the newest frame. Returns (handoff ms, staleness ms, bytes).
    ///
    /// NOTE: the copy must be an explicit memcpy into a preallocated buffer.
    /// `[UInt8](buf)` looks like a copy but Array's CoW fast path makes it O(1),
    /// which silently measured ~0 ms until this was fixed.
    func readLatest() -> (ms: Double, staleness: Double, bytes: Int, frames: Int) {
        let t0 = monoMS()
        lock.lock()
        if consumer.count != buf.count {
            consumer = [UInt8](repeating: 0, count: buf.count)
        }
        var n = 0
        if !buf.isEmpty {
            buf.withUnsafeBytes { src in
                consumer.withUnsafeMutableBytes { dst in
                    if let s = src.baseAddress, let d = dst.baseAddress {
                        memcpy(d, s, src.count)
                        n = src.count
                    }
                }
            }
        }
        let stale = t0 - lastFrameAt
        let f = frames
        lock.unlock()
        let dt = monoMS() - t0
        return (dt, stale, n, f)
    }

    var isEmpty: Bool { lock.lock(); defer { lock.unlock() }; return buf.isEmpty }
}

/// Sink that feeds a LatestFrameStore instead of collecting timestamps.
final class LatestFrameSink: NSObject, SCStreamOutput, @unchecked Sendable {
    let store: LatestFrameStore
    private let lock = NSLock()
    private(set) var statuses: [Int] = []
    private(set) var stopError: Error?

    init(store: LatestFrameStore) {
        self.store = store
        super.init()
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, CMSampleBufferIsValid(sb) else { return }
        var statusCode = -1
        if let atts = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
           let raw = atts.first?[.status] as? Int {
            statusCode = raw
        }
        lock.lock(); statuses.append(statusCode); lock.unlock()
        if let s = SCFrameStatus(rawValue: statusCode), s != .complete { return }
        guard let pb = CMSampleBufferGetImageBuffer(sb) else { return }
        store.store(pb)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        lock.lock(); stopError = error; lock.unlock()
    }
}

// MARK: - image-backed window (real game content, synthetic window)

/// A borderless, non-activating window that displays a real screenshot at the
/// exact logical size of the game window (1149x714 pt -> 2298x1428 px at 2x).
/// This lets the capture+OCR loops run against REAL game content at REAL game
/// geometry without the game running. It is still a synthetic window: it is
/// labelled as such everywhere, and it is never presented as a live-game result.
final class ImageWindow {
    var window: NSWindow?
    private(set) var loaded: Bool = false

    init(imagePath: String, size: CGSize = CGSize(width: 1149, height: 714),
         origin: CGPoint = CGPoint(x: 300, y: 122)) {
        guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: imagePath) as CFURL, nil),
              let cg = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
            return
        }
        let w = NSWindow(contentRect: CGRect(origin: origin, size: size),
                         styleMask: [.borderless], backing: .buffered, defer: false)
        w.isOpaque = true
        w.backgroundColor = .black
        // .floating, not .normal: screencapture -R grabs the COMPOSITED screen
        // region, so a normal-level window gets covered by other apps and the
        // "game content" screenshot silently becomes whatever is on top.
        // That is exactly what happened on the 09:18 run (77.8 OCR items
        // instead of the expected 12). SCK's desktopIndependentWindow is not
        // affected, which made the two capture paths capture different pixels.
        w.level = .floating
        w.hasShadow = false
        w.ignoresMouseEvents = true
        w.isReleasedWhenClosed = false
        let view = NSImageView(frame: CGRect(origin: .zero, size: size))
        view.image = NSImage(cgImage: cg, size: size)
        view.imageScaling = .scaleAxesIndependently
        view.animates = false
        w.contentView = view
        self.window = w
        self.loaded = true
    }

    func show() {
        window?.orderFrontRegardless()
        pumpRunLoop(0.4)
    }

    func close() {
        window?.orderOut(nil)
        window?.close()
        window = nil
        pumpRunLoop(0.05)
    }

    var windowNumber: Int { Int(window?.windowNumber ?? 0) }
}

// MARK: - process memory

/// Spin the main run loop for a bounded time. Needed so AppKit can flush
/// deferred window ordering to the WindowServer.
func pumpRunLoop(_ seconds: Double) {
    let deadline = Date(timeIntervalSinceNow: seconds)
    while Date() < deadline {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.01))
    }
}

func residentMemoryMB() -> Double {
    var info = mach_task_basic_info()
    var count = mach_msg_type_number_t(MemoryLayout<mach_task_basic_info>.size / MemoryLayout<natural_t>.size)
    let kr = withUnsafeMutablePointer(to: &info) { ptr -> kern_return_t in
        ptr.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { intPtr in
            task_info(mach_task_self_, task_flavor_t(MACH_TASK_BASIC_INFO), intPtr, &count)
        }
    }
    guard kr == KERN_SUCCESS else { return -1 }
    return Double(info.resident_size) / (1024.0 * 1024.0)
}

// MARK: - self-rendered animating window (for SCK steady-state only)
/// A small borderless window owned by THIS process whose content is animated by
/// Core Animation on the render server. It exists only so the ScreenCaptureKit
/// stream has guaranteed changing content: SCK is change-driven and a static
/// window produces almost no frames. It never takes focus
/// (orderFrontRegardless, no key/main) and never generates input events.
final class AnimWindow {
    var window: NSWindow?
    let size: CGSize

    init(size: CGSize = CGSize(width: 320, height: 240), origin: CGPoint = CGPoint(x: 24, y: 24)) {
        self.size = size
        let w = NSWindow(contentRect: CGRect(origin: origin, size: size),
                         styleMask: [.borderless], backing: .buffered, defer: false)
        w.isOpaque = true
        w.backgroundColor = .black
        w.level = .normal
        w.hasShadow = false
        w.ignoresMouseEvents = true
        w.isReleasedWhenClosed = false
        let host = NSView(frame: CGRect(origin: .zero, size: size))
        host.wantsLayer = true
        host.layer = CALayer()
        host.layer?.backgroundColor = NSColor.black.cgColor
        let bar = CALayer()
        bar.frame = CGRect(x: 0, y: 0, width: 48, height: size.height)
        bar.backgroundColor = NSColor.white.cgColor
        host.layer?.addSublayer(bar)
        let anim = CABasicAnimation(keyPath: "position.x")
        anim.fromValue = 24.0
        anim.toValue = Double(size.width - 24)
        anim.duration = 0.4
        anim.autoreverses = true
        anim.repeatCount = .infinity
        anim.timingFunction = CAMediaTimingFunction(name: .linear)
        bar.add(anim, forKey: "slide")
        w.contentView = host
        self.window = w
    }

    func show() {
        window?.orderFrontRegardless()
        // AppKit defers window ordering to the next run-loop turn, and
        // SCShareableContent only lists windows the WindowServer has actually
        // composited. Without pumping, our own window is missing from
        // SCShareableContent ("animating window id N not present").
        pumpRunLoop(0.35)
    }

    func close() {
        window?.orderOut(nil)
        window?.close()
        window = nil
        pumpRunLoop(0.05)
    }

    var windowNumber: Int { Int(window?.windowNumber ?? 0) }
}

// MARK: - image helpers

func imageInfo(_ img: CGImage?) -> [String: Any] {
    guard let img = img else { return ["ok": false, "w": 0, "h": 0] }
    return ["ok": true, "w": img.width, "h": img.height, "bpp": img.bitsPerPixel,
            "bytesPerRow": img.bytesPerRow]
}

func pngData(_ img: CGImage) -> Data? {
    let data = NSMutableData()
    guard let dest = CGImageDestinationCreateWithData(data as CFMutableData, "public.png" as CFString, 1, nil) else {
        return nil
    }
    CGImageDestinationAddImage(dest, img, nil)
    guard CGImageDestinationFinalize(dest) else { return nil }
    return data as Data
}

func bgraExtract(_ img: CGImage) -> [UInt8]? {
    let w = img.width, h = img.height
    var buf = [UInt8](repeating: 0, count: w * h * 4)
    let ok: Bool = buf.withUnsafeMutableBytes { raw -> Bool in
        guard let ctx = CGContext(data: raw.baseAddress, width: w, height: h,
                                  bitsPerComponent: 8, bytesPerRow: w * 4,
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue
                                             | CGBitmapInfo.byteOrder32Little.rawValue) else { return false }
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
        return true
    }
    return ok ? buf : nil
}

@discardableResult
func runScreencapture(rect: CGRect?, path: String) -> Int32 {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    var args = ["-x"]
    if let r = rect {
        args += ["-R", "\(Int(r.origin.x)),\(Int(r.origin.y)),\(Int(r.width)),\(Int(r.height))"]
    }
    args.append(path)
    p.arguments = args
    p.standardError = FileHandle.nullDevice
    p.standardOutput = FileHandle.nullDevice
    do { try p.run() } catch { return -1 }
    p.waitUntilExit()
    return p.terminationStatus
}

func fileBytes(_ path: String) -> Int {
    return ((try? FileManager.default.attributesOfItem(atPath: path))?[.size] as? Int) ?? 0
}

// MARK: - capture context

final class Ctx {
    let args: Args
    let window: WinInfo?
    let roiLocal: CGRect
    let roiGlobal: CGRect
    let tmpDir: URL
    let scale: Double
    var cachedWindowImage: CGImage?
    var cachedRoiImage: CGImage?
    var pngBytes: Data?
    var pngFile: URL?
    var scFilter: SCContentFilter?
    var scConfigBest: SCStreamConfiguration?
    var scConfigNominal: SCStreamConfiguration?
    var scRoiConfig: SCStreamConfiguration?
    var scSetupError: String?
    var scDisplayFilter: SCContentFilter?
    var imageWindow: ImageWindow?

    init(args: Args) {
        self.args = args

        // Optional: stand up a synthetic window showing a real screenshot at the
        // real game window geometry, so capture+OCR loops run on real game
        // content. Must be created BEFORE the window enumeration below.
        var shownImageWin: ImageWindow? = nil
        if let imgPath = args.flags["image-window"], imgPath != "1" {
            let iw = ImageWindow(imagePath: imgPath)
            if iw.loaded {
                iw.show()
                shownImageWin = iw
                logErr("[bench_capture] image window shown: \(imgPath) number=\(iw.windowNumber)")
            } else {
                logErr("[bench_capture] could not load image window from \(imgPath)")
            }
        }
        self.imageWindow = shownImageWin

        let wins = enumerateWindows()
        let myPid = Int(ProcessInfo.processInfo.processIdentifier)
        let requested = args.str("window", "auto")
        var chosen: WinInfo? = nil
        if let iw = shownImageWin, iw.windowNumber > 0 {
            chosen = wins.first(where: { $0.id == iw.windowNumber })
            if chosen == nil {
                logErr("[bench_capture] image window \(iw.windowNumber) not in CGWindowList")
            }
        }
        if chosen == nil, requested != "auto", let wid = Int(requested) {
            chosen = wins.first(where: { $0.id == wid })
        }
        if chosen == nil {
            // Prefer a window that is entirely on the main display: a window
            // straddling two displays makes screencapture -R regions ambiguous
            // and can drag the WindowServer across display boundaries.
            let mainB = CGDisplayBounds(CGMainDisplayID())
            let cands = wins.filter { w in
                guard w.layer == 0, w.w > 400, w.h > 300, w.id > 0, w.pid != myPid else { return false }
                return w.x >= mainB.minX && w.y >= mainB.minY
                    && w.x + w.w <= mainB.maxX && w.y + w.h <= mainB.maxY
            }
            chosen = cands.max(by: { ($0.w * $0.h) < ($1.w * $1.h) })
                ?? wins.filter { $0.layer == 0 && $0.w > 400 && $0.h > 300 && $0.id > 0 && $0.pid != myPid }
                       .max(by: { ($0.w * $0.h) < ($1.w * $1.h) })
        }
        self.window = chosen
        self.scale = Double(NSScreen.main?.backingScaleFactor ?? 2.0)

        let winX = chosen?.x ?? 0, winY = chosen?.y ?? 0
        let winW = chosen?.w ?? 800, winH = chosen?.h ?? 600
        let local = parseRect(args.str("roi", "40,120,300,200")) ?? CGRect(x: 40, y: 120, width: 300, height: 200)
        let clampedW = max(min(local.width, winW - local.origin.x - 1), 16)
        let clampedH = max(min(local.height, winH - local.origin.y - 1), 16)
        self.roiLocal = CGRect(x: local.origin.x, y: local.origin.y, width: clampedW, height: clampedH)
        self.roiGlobal = CGRect(x: winX + local.origin.x, y: winY + local.origin.y,
                                width: clampedW, height: clampedH)

        let td = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("bench/tmp")
        try? FileManager.default.createDirectory(at: td, withIntermediateDirectories: true)
        self.tmpDir = td
    }

    func teardown() {
        imageWindow?.close()
        imageWindow = nil
    }

    var windowRect: CGRect {
        guard let w = window else { return CGRect(x: 0, y: 0, width: 800, height: 600) }
        return CGRect(x: w.x, y: w.y, width: w.w, height: w.h)
    }

    func windowMeta() -> [String: Any] {
        guard let w = window else { return ["window": "none"] }
        var m: [String: Any] = [
            "window_id": w.id, "window_owner": w.owner, "window_title": w.title,
            "window_x": w.x, "window_y": w.y, "window_w": w.w, "window_h": w.h,
            "window_layer": w.layer,
        ]
        if imageWindow != nil {
            m["window_label"] = "SYNTHETIC window displaying a real screenshot (artifacts/game.png) at game geometry — NOT the live game window"
            m["content"] = "real game screenshot"
            m["live_game"] = false
        } else {
            m["window_label"] = "generic window, NOT the game window"
            m["content"] = "unrelated application content"
            m["live_game"] = false
        }
        return m
    }

    /// Build (and cache) the SCK filter/config for the target window.
    @discardableResult
    func ensureSC() -> String? {
        if let e = scSetupError { return e }
        if scFilter != nil { return nil }
        if window == nil { scSetupError = "no target window"; return scSetupError }
        do {
            let content = try sckShareableSync(onScreenOnly: true)
            guard let target = content.windows.first(where: { $0.windowID == CGWindowID(window!.id) }) else {
                scSetupError = "SCShareableContent does not expose window id \(window!.id) (window may be excluded from shareable content)"
                return scSetupError
            }
            let filter = SCContentFilter(desktopIndependentWindow: target)
            let best = SCStreamConfiguration()
            best.width = Int(target.frame.width * scale)
            best.height = Int(target.frame.height * scale)
            best.showsCursor = false
            best.pixelFormat = kCVPixelFormatType_32BGRA
            best.captureResolution = .best
            let nominal = SCStreamConfiguration()
            nominal.width = Int(target.frame.width)
            nominal.height = Int(target.frame.height)
            nominal.showsCursor = false
            nominal.pixelFormat = kCVPixelFormatType_32BGRA
            nominal.captureResolution = .nominal
            let roi = SCStreamConfiguration()
            roi.sourceRect = roiLocal
            roi.width = Int(roiLocal.width * scale)
            roi.height = Int(roiLocal.height * scale)
            roi.showsCursor = false
            roi.pixelFormat = kCVPixelFormatType_32BGRA
            roi.captureResolution = .best
            scFilter = filter
            scConfigBest = best
            scConfigNominal = nominal
            scRoiConfig = roi
            return nil
        } catch {
            scSetupError = "SCShareableContent failed: \(error)"
            return scSetupError
        }
    }

    func ensureDisplayFilter() -> String? {
        if scDisplayFilter != nil { return nil }
        do {
            let content = try sckShareableSync(onScreenOnly: true)
            guard let d = content.displays.first(where: { $0.displayID == CGMainDisplayID() }) ?? content.displays.first else {
                return "no shareable display"
            }
            scDisplayFilter = SCContentFilter(display: d, excludingWindows: [])
            return nil
        } catch {
            return "SCShareableContent failed: \(error)"
        }
    }
}

// MARK: - backend result

struct R {
    var times: [Double] = []
    var meta: [String: Any] = [:]
    var error: String? = nil
}

func timeIt(_ n: Int, _ warmup: Int, _ body: () throws -> Any?) -> R {
    var r = R()
    for _ in 0..<warmup { _ = try? body() }
    var times: [Double] = []
    for _ in 0..<n {
        let t0 = monoMS()
        do {
            _ = try body()
            times.append(monoMS() - t0)
        } catch {
            r.error = "\(error)"
            return r
        }
    }
    r.times = times
    return r
}

// MARK: - Vision helper

func visionOCR(_ img: CGImage, level: VNRequestTextRecognitionLevel, langs: [String],
               minConf: Float = 0.3) throws -> Int {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = level
    req.usesLanguageCorrection = false
    req.recognitionLanguages = langs
    let handler = VNImageRequestHandler(cgImage: img, options: [:])
    try handler.perform([req])
    var n = 0
    for obs in (req.results ?? []) {
        guard let c = obs.topCandidates(1).first else { continue }
        if c.confidence >= minConf { n += 1 }
    }
    return n
}

// MARK: - capture backends

func buildBackends(_ ctx: Ctx) -> [(String, () -> R)] {
    let iters = ctx.args.int("iters", 20)
    let warmup = ctx.args.int("warmup", 2)
    let loopIters = ctx.args.int("loop-iters", 8)
    let winRect = ctx.windowRect
    let roiGlobal = ctx.roiGlobal
    let roiLocal = ctx.roiLocal
    let langs = ["zh-Hans", "en-US"]
    var list: [(String, () -> R)] = []

    // ---- 0. window enumeration: what seerd.findWin() pays on EVERY shot ----
    list.append(("windowlist_enum", {
        var r = timeIt(iters, warmup) {
            return CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
        }
        r.meta["note"] = "CGWindowListCopyWindowInfo(.optionOnScreenOnly, .excludeDesktopElements) — paid by scripts/seerd.swift findWin() on every shot"
        return r
    }))

    // ---- 1. SCK shareable-content enumeration only ----
    list.append(("sck_shareable_enum", {
        var r = R()
        r = timeIt(iters, warmup) { return try sckShareableSync(onScreenOnly: true) }
        r.meta["note"] = "SCShareableContent.getExcludingDesktopWindows(onScreenWindowsOnly: true) alone — the setup tax inside a naive per-shot SCK loop"
        return r
    }))

    // ---- 2. SCScreenshotManager, warm filter, best (retina) resolution ----
    list.append(("sck_screenshot_warm_2x", {
        var r = R()
        if let e = ctx.ensureSC() { r.error = e; return r }
        guard let filter = ctx.scFilter, let cfg = ctx.scConfigBest else { r.error = "sck not built"; return r }
        r = timeIt(iters, warmup) { return try sckScreenshotSync(filter: filter, config: cfg) }
        r.meta["note"] = "SCScreenshotManager.captureImage, pre-built SCContentFilter + SCStreamConfiguration, captureResolution=.best (retina px). Per-call setup excluded."
        r.meta["configured_w"] = cfg.width
        r.meta["configured_h"] = cfg.height
        if ctx.cachedWindowImage == nil {
            ctx.cachedWindowImage = try? sckScreenshotSync(filter: filter, config: cfg)
        }
        r.meta["image"] = imageInfo(ctx.cachedWindowImage)
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    // ---- 3. SCScreenshotManager, warm filter, nominal (1x) resolution ----
    list.append(("sck_screenshot_warm_1x", {
        var r = R()
        if let e = ctx.ensureSC() { r.error = e; return r }
        guard let filter = ctx.scFilter, let cfg = ctx.scConfigNominal else { r.error = "sck not built"; return r }
        r = timeIt(iters, warmup) { return try sckScreenshotSync(filter: filter, config: cfg) }
        r.meta["note"] = "SCScreenshotManager.captureImage, captureResolution=.nominal (1x point px) — size-matched to what screencapture -R produced in artifacts"
        r.meta["configured_w"] = cfg.width
        r.meta["configured_h"] = cfg.height
        r.meta["image"] = imageInfo(try? sckScreenshotSync(filter: filter, config: cfg))
        return r
    }))

    // ---- 4. SCScreenshotManager cold: fresh SCShareableContent each call ----
    list.append(("sck_screenshot_cold_2x", {
        var r = R()
        r = timeIt(max(3, loopIters), 1) {
            let content = try sckShareableSync(onScreenOnly: true)
            guard let target = content.windows.first(where: { $0.windowID == CGWindowID(ctx.window?.id ?? 0) }) else {
                throw NSError(domain: "bench_capture", code: 3,
                              userInfo: [NSLocalizedDescriptionKey: "target window not in SCShareableContent"])
            }
            let filter = SCContentFilter(desktopIndependentWindow: target)
            let cfg = SCStreamConfiguration()
            cfg.width = Int(target.frame.width * ctx.scale)
            cfg.height = Int(target.frame.height * ctx.scale)
            cfg.showsCursor = false
            cfg.pixelFormat = kCVPixelFormatType_32BGRA
            cfg.captureResolution = .best
            return try sckScreenshotSync(filter: filter, config: cfg)
        }
        r.meta["note"] = "SCScreenshotManager.captureImage WITH a fresh SCShareableContent enumeration every iteration — what a naive one-shot SCK implementation pays"
        return r
    }))

    // ---- 5. SCScreenshotManager ROI via sourceRect ----
    list.append(("sck_screenshot_roi_2x", {
        var r = R()
        if let e = ctx.ensureSC() { r.error = e; return r }
        guard let filter = ctx.scFilter, let cfg = ctx.scRoiConfig else { r.error = "sck not built"; return r }
        r = timeIt(iters, warmup) { return try sckScreenshotSync(filter: filter, config: cfg) }
        r.meta["note"] = "SCScreenshotManager.captureImage with sourceRect ROI (\(Int(roiLocal.width))x\(Int(roiLocal.height)) pts) — SCK-native ROI, no full frame captured"
        r.meta["sourceRect"] = [Double(roiLocal.origin.x), Double(roiLocal.origin.y),
                                Double(roiLocal.width), Double(roiLocal.height)]
        r.meta["image"] = imageInfo(try? sckScreenshotSync(filter: filter, config: cfg))
        return r
    }))

    // ---- 6. SCK full-display screenshot ----
    list.append(("sck_display_screenshot_2x", {
        var r = R()
        if let e = ctx.ensureDisplayFilter() { r.error = e; return r }
        guard let filter = ctx.scDisplayFilter else { r.error = "no display filter"; return r }
        let d = CGDisplayBounds(CGMainDisplayID())
        let cfg = SCStreamConfiguration()
        cfg.width = Int(d.width * ctx.scale)
        cfg.height = Int(d.height * ctx.scale)
        cfg.showsCursor = false
        cfg.pixelFormat = kCVPixelFormatType_32BGRA
        cfg.captureResolution = .best
        r = timeIt(iters, warmup) { return try sckScreenshotSync(filter: filter, config: cfg) }
        r.meta["note"] = "SCScreenshotManager.captureImage on the whole main display (.best) — full-screen alternative to screencapture -x"
        r.meta["configured_w"] = cfg.width
        r.meta["configured_h"] = cfg.height
        r.meta["image"] = imageInfo(try? sckScreenshotSync(filter: filter, config: cfg))
        return r
    }))

    // ---- 7. SCStream: per-call setup + first frame (cached filter) ----
    list.append(("sck_stream_warm_setup_first_frame", {
        var r = R()
        if let e = ctx.ensureSC() { r.error = e; return r }
        guard let filter = ctx.scFilter, let cfg = ctx.scConfigBest else { r.error = "sck not built"; return r }
        r = timeIt(max(3, loopIters), 1) {
            let sink = FrameSink(wantCount: 1, wantsImage: true)
            let stream = SCStream(filter: filter, configuration: cfg, delegate: nil)
            let q = DispatchQueue(label: "bench.stream")
            try stream.addStreamOutput(sink, type: .screen, sampleHandlerQueue: q)
            let t0 = monoMS()
            try awaitStartCaptureSync(stream)
            // wait for first complete frame or 3s
            var firstFrameAt: Double? = nil
            let deadline = monoMS() + 3000
            while monoMS() < deadline {
                if sink.count >= 1 { firstFrameAt = sink.times.first; break }
                usleep(500)
            }
            try? awaitStopCaptureSync(stream)
            guard let f = firstFrameAt else {
                throw NSError(domain: "bench_capture", code: 4,
                              userInfo: [NSLocalizedDescriptionKey: "no complete frame within 3s"])
            }
            _ = t0
            return f
        }
        r.meta["note"] = "SCStream per-call: build stream + addStreamOutput + startCapture + wait for first COMPLETE frame (cached filter/config). Measures stream setup + first-frame latency."
        if let img = R.firstFrameImage {
            r.meta["first_frame_image"] = imageInfo(img)
            ctx.cachedWindowImage = ctx.cachedWindowImage ?? img
        }
        return r
    }))

    // ---- 8. SCStream: per-call setup + first frame (fresh shareable content) ----
    list.append(("sck_stream_cold_setup_first_frame", {
        var r = R()
        r = timeIt(max(3, loopIters), 1) {
            let content = try sckShareableSync(onScreenOnly: true)
            guard let target = content.windows.first(where: { $0.windowID == CGWindowID(ctx.window?.id ?? 0) }) else {
                throw NSError(domain: "bench_capture", code: 5,
                              userInfo: [NSLocalizedDescriptionKey: "target window not in SCShareableContent"])
            }
            let filter = SCContentFilter(desktopIndependentWindow: target)
            let cfg = SCStreamConfiguration()
            cfg.width = Int(target.frame.width * ctx.scale)
            cfg.height = Int(target.frame.height * ctx.scale)
            cfg.showsCursor = false
            cfg.pixelFormat = kCVPixelFormatType_32BGRA
            cfg.captureResolution = .best
            cfg.queueDepth = 3
            let sink = FrameSink(wantCount: 1, wantsImage: false)
            let stream = SCStream(filter: filter, configuration: cfg, delegate: nil)
            let q = DispatchQueue(label: "bench.stream.cold")
            try stream.addStreamOutput(sink, type: .screen, sampleHandlerQueue: q)
            try awaitStartCaptureSync(stream)
            var firstFrameAt: Double? = nil
            let deadline = monoMS() + 3000
            while monoMS() < deadline {
                if sink.count >= 1 { firstFrameAt = sink.times.first; break }
                usleep(500)
            }
            try? awaitStopCaptureSync(stream)
            guard let f = firstFrameAt else {
                throw NSError(domain: "bench_capture", code: 6,
                              userInfo: [NSLocalizedDescriptionKey: "no complete frame within 3s"])
            }
            return f
        }
        r.meta["note"] = "SCStream per-call INCLUDING a fresh SCShareableContent enumeration + fresh filter/config each iteration"
        return r
    }))

    // ---- 9. SCStream steady state (animated content, pre-warmed stream) ----
    list.append(("sck_stream_steady_state_animated", {
        var r = R()
        let frames = ctx.args.int("stream-frames", 60)
        let budget = ctx.args.double("stream-budget", 6.0)
        // Own borderless non-activating window with a Core Animation slide so
        // SCK has real changing content. Labeled synthetic in meta.
        let anim = AnimWindow()
        anim.show()
        // give the window server a moment to composite the first frames
        usleep(300_000)
        defer { anim.close() }

        var firstFrameLatency: [Double] = []
        var intervals: [Double] = []
        var delivered = 0
        var setupMs = 0.0
        do {
            let content = try sckShareableSync(onScreenOnly: true)
            let wid = CGWindowID(anim.windowNumber)
            guard let target = content.windows.first(where: { $0.windowID == wid }) else {
                r.error = "animating window id \(wid) not present in SCShareableContent"
                return r
            }
            let filter = SCContentFilter(desktopIndependentWindow: target)
            let cfg = SCStreamConfiguration()
            cfg.width = Int(target.frame.width * ctx.scale)
            cfg.height = Int(target.frame.height * ctx.scale)
            cfg.showsCursor = false
            cfg.pixelFormat = kCVPixelFormatType_32BGRA
            cfg.captureResolution = .best
            cfg.queueDepth = 5
            cfg.minimumFrameInterval = CMTime(value: 1, timescale: 60)
            let sink = FrameSink(wantCount: frames, wantsImage: false)
            let stream = SCStream(filter: filter, configuration: cfg, delegate: nil)
            let q = DispatchQueue(label: "bench.stream.steady")
            try stream.addStreamOutput(sink, type: .screen, sampleHandlerQueue: q)
            let tSetup0 = monoMS()
            try awaitStartCaptureSync(stream)
            setupMs = monoMS() - tSetup0
            // wait for N frames or budget
            let deadline = monoMS() + budget * 1000
            while monoMS() < deadline && sink.count < frames {
                usleep(1000)
            }
            try? awaitStopCaptureSync(stream)
            let times = sink.times
            delivered = times.count
            if times.count >= 2 {
                firstFrameLatency = [times[0] - tSetup0]
                for i in 1..<times.count { intervals.append(times[i] - times[i - 1]) }
            }
            r.meta["frames_delivered"] = delivered
            r.meta["frames_requested"] = frames
            r.meta["budget_s"] = budget
            r.meta["start_capture_ms"] = setupMs
            r.meta["min_frame_interval"] = "1/60 s"
            r.meta["configured_w"] = cfg.width
            r.meta["configured_h"] = cfg.height
            if times.count >= 2, let first = times.first, let last = times.last, last > first {
                let fps = Double(times.count - 1) / ((last - first) / 1000.0)
                r.meta["achieved_fps"] = fps
                r.meta["frame_bytes_theoretical"] = cfg.width * cfg.height * 4
                r.meta["rss_mb_after_stream"] = residentMemoryMB()
            }
            r.meta["note"] = "PRE-WARMED SCStream steady state: startCapture then timestamps of consecutive COMPLETE frames. Content is a self-rendered 320x240 borderless non-activating window with a Core Animation slide (synthetic content, NOT the game window). Intervals below are frame PACING, not capture latency."
        } catch {
            r.error = "\(error)"
            return r
        }
        r.times = intervals
        if let f = firstFrameLatency.first { r.meta["first_frame_ms"] = f }
        return r
    }))

    // ---- 10. SCStream steady state on the STATIC target window ----
    list.append(("sck_stream_steady_state_static", {
        var r = R()
        let budget = ctx.args.double("stream-budget", 6.0)
        if let e = ctx.ensureSC() { r.error = e; return r }
        guard let filter = ctx.scFilter, let cfg = ctx.scConfigBest else { r.error = "sck not built"; return r }
        let sink = FrameSink(wantCount: 60, wantsImage: false)
        do {
            let stream = SCStream(filter: filter, configuration: cfg, delegate: nil)
            let q = DispatchQueue(label: "bench.stream.static")
            try stream.addStreamOutput(sink, type: .screen, sampleHandlerQueue: q)
            let t0 = monoMS()
            try awaitStartCaptureSync(stream)
            let deadline = monoMS() + budget * 1000
            while monoMS() < deadline && sink.count < 60 { usleep(1000) }
            try? awaitStopCaptureSync(stream)
            let times = sink.times
            var intervals: [Double] = []
            if times.count >= 2 {
                for i in 1..<times.count { intervals.append(times[i] - times[i - 1]) }
            }
            r.meta["frames_delivered"] = times.count
            r.meta["budget_s"] = budget
            r.meta["note"] = "PRE-WARMED SCStream on the STATIC generic window: documents that SCK is change-driven. Few frames here is expected and is itself the finding (a static window does not stream)."
            _ = t0
            r.times = intervals
        } catch {
            r.error = "\(error)"
        }
        return r
    }))

    // ---- 10b. SCStream "read the latest frame" primitive (the hot-loop read) ----
    list.append(("sck_stream_read_latest", {
        var r = R()
        let reads = ctx.args.int("read-iters", 100)
        let readGapUs = ctx.args.int("read-gap-us", 4000)
        let anim = AnimWindow()
        anim.show()
        usleep(300_000)
        defer { anim.close() }
        let rssBefore = residentMemoryMB()
        do {
            let content = try sckShareableSync(onScreenOnly: true)
            let wid = CGWindowID(anim.windowNumber)
            guard let target = content.windows.first(where: { $0.windowID == wid }) else {
                r.error = "animating window id \(wid) not present in SCShareableContent"
                return r
            }
            let filter = SCContentFilter(desktopIndependentWindow: target)
            let cfg = SCStreamConfiguration()
            cfg.width = Int(target.frame.width * ctx.scale)
            cfg.height = Int(target.frame.height * ctx.scale)
            cfg.showsCursor = false
            cfg.pixelFormat = kCVPixelFormatType_32BGRA
            cfg.captureResolution = .best
            cfg.queueDepth = 5
            cfg.minimumFrameInterval = CMTime(value: 1, timescale: 60)
            let store = LatestFrameStore()
            let sink = LatestFrameSink(store: store)
            let stream = SCStream(filter: filter, configuration: cfg, delegate: nil)
            let q = DispatchQueue(label: "bench.stream.latest")
            try stream.addStreamOutput(sink, type: .screen, sampleHandlerQueue: q)
            let tStart0 = monoMS()
            try awaitStartCaptureSync(stream)
            let startMs = monoMS() - tStart0
            // let the pipeline warm up and deliver a few frames
            usleep(300_000)
            var handoff: [Double] = []
            var staleness: [Double] = []
            var emptyReads = 0
            for _ in 0..<reads {
                if store.isEmpty { emptyReads += 1; usleep(UInt32(readGapUs)); continue }
                let (ms, stale, _, _) = store.readLatest()
                handoff.append(ms)
                staleness.append(stale)
                usleep(UInt32(readGapUs))
            }
            let frames = store.frames
            let w = store.width, h = store.height
            try? awaitStopCaptureSync(stream)
            r.times = handoff
            r.meta["note"] = "PRE-WARMED SCStream, consumer reads the newest frame into its own buffer (memcpy). This is the per-iteration cost of 'give me current pixels' in a stream-based loop. Content is a self-rendered 320x240 animating window (synthetic, NOT the game window)."
            r.meta["start_capture_ms"] = startMs
            r.meta["frame_w"] = w
            r.meta["frame_h"] = h
            r.meta["frame_bytes"] = w * h * 4
            r.meta["frames_delivered_during_run"] = frames
            r.meta["reads_attempted"] = reads
            r.meta["empty_reads"] = emptyReads
            r.meta["staleness_ms"] = computeStats(staleness)
            r.meta["rss_mb_before"] = rssBefore
            r.meta["rss_mb_after"] = residentMemoryMB()
            r.meta["queue_depth"] = 5
            r.meta["memory_note"] = "queueDepth 5 x frame_bytes is the theoretical in-flight buffer cost; rss delta is the measured process footprint change"
        } catch {
            r.error = "\(error)"
        }
        return r
    }))

    // ---- 10c. raw frame-handoff memcpy at real game resolution ----
    list.append(("memcpy_game_frame_bgra", {
        var r = R()
        // 1149x714 logical game window at scale 2.0 == 2298x1428 px == 13.1 MB BGRA.
        let w = ctx.args.int("game-px-w", 2298)
        let h = ctx.args.int("game-px-h", 1428)
        let bytes = w * h * 4
        let src = [UInt8](repeating: 0x5A, count: bytes)
        var dst = [UInt8](repeating: 0, count: bytes)
        r = timeIt(iters, warmup) {
            src.withUnsafeBytes { s in
                dst.withUnsafeMutableBytes { d in
                    if let sp = s.baseAddress, let dp = d.baseAddress { memcpy(dp, sp, bytes) }
                }
            }
            return bytes
        }
        r.meta["note"] = "single memcpy of one \(w)x\(h) BGRA frame (\(bytes / 1024 / 1024) MB) — the irreducible per-frame handoff cost at real game resolution"
        r.meta["frame_w"] = w
        r.meta["frame_h"] = h
        r.meta["frame_bytes"] = bytes
        return r
    }))

    // ---- 11. baseline: subprocess screencapture on the identical window region ----
    list.append(("subprocess_screencapture_window_png", {
        var r = R()
        let path = ctx.tmpDir.appendingPathComponent("sc_window.png").path
        r = timeIt(iters, warmup) { return runScreencapture(rect: winRect, path: path) }
        r.meta["note"] = "/usr/sbin/screencapture -x -R <window bounds> <file>.png — process spawn + PNG encode to disk. Decode NOT included."
        r.meta["region"] = [Double(winRect.origin.x), Double(winRect.origin.y),
                            Double(winRect.width), Double(winRect.height)]
        r.meta["file_bytes"] = fileBytes(path)
        if let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
           let img = CGImageSourceCreateImageAtIndex(src, 0, nil) {
            r.meta["image"] = imageInfo(img)
        }
        ctx.pngFile = URL(fileURLWithPath: path)
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    // ---- 12. baseline: subprocess screencapture full screen ----
    list.append(("subprocess_screencapture_fullscreen_png", {
        var r = R()
        let path = ctx.tmpDir.appendingPathComponent("sc_full.png").path
        r = timeIt(iters, warmup) { return runScreencapture(rect: nil, path: path) }
        r.meta["note"] = "/usr/sbin/screencapture -x <file>.png — full screen, process spawn + PNG encode to disk"
        r.meta["file_bytes"] = fileBytes(path)
        if let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
           let img = CGImageSourceCreateImageAtIndex(src, 0, nil) {
            r.meta["image"] = imageInfo(img)
        }
        return r
    }))

    // ---- 13. THE PRODUCTION BASELINE: exactly seerd.shot() ----
    list.append(("prod_shot_pipeline", {
        var r = R()
        let path = ctx.tmpDir.appendingPathComponent("prod_shot.png").path
        r = timeIt(iters, warmup) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            p.arguments = ["-x", "-R",
                           "\(Int(winRect.origin.x)),\(Int(winRect.origin.y)),\(Int(winRect.width)),\(Int(winRect.height))",
                           path]
            try p.run()
            p.waitUntilExit()
            guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
                  let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
                throw NSError(domain: "bench_capture", code: 7,
                              userInfo: [NSLocalizedDescriptionKey: "decode failed"])
            }
            return img
        }
        r.meta["note"] = "EXACT scripts/seerd.swift shot(): spawn screencapture -x -R <window> file.png, waitUntilExit, CGImageSourceCreateWithURL decode. This is the current production capture cost."
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    // ---- 14. PNG encode to memory ----
    list.append(("png_encode_memory", {
        var r = R()
        guard let img = ctx.cachedWindowImage else { r.error = "no cached window image"; return r }
        r = timeIt(iters, warmup) { return pngData(img) }
        if let d = pngData(img) { r.meta["png_bytes"] = d.count; ctx.pngBytes = d }
        r.meta["note"] = "CGImageDestination PNG encode of the in-memory window capture (the cost you add if you insist on PNG)"
        r.meta["image"] = imageInfo(img)
        return r
    }))

    // ---- 15. PNG decode ----
    // CGImageSourceCreateImageAtIndex is LAZY: it returns a handle and defers
    // pixel decoding. Timing it alone reports ~0.1 ms and badly understates the
    // real cost, so both the lazy handle and the forced decode are measured.
    list.append(("png_decode_lazy_handle", {
        var r = R()
        guard let d = ctx.pngBytes ?? (try? Data(contentsOf: ctx.pngFile ?? URL(fileURLWithPath: "/nonexistent"))) else {
            r.error = "no PNG data available"; return r
        }
        r = timeIt(iters, warmup) {
            let src = CGImageSourceCreateWithData(d as CFData, nil)
            return src.flatMap { CGImageSourceCreateImageAtIndex($0, 0, nil) }
        }
        r.meta["note"] = "CGImageSourceCreateWithData + CreateImageAtIndex ONLY — a lazy handle, pixels are NOT decoded yet. Do not report this as decode cost."
        r.meta["png_bytes"] = d.count
        return r
    }))

    list.append(("png_decode_forced", {
        var r = R()
        guard let d = ctx.pngBytes ?? (try? Data(contentsOf: ctx.pngFile ?? URL(fileURLWithPath: "/nonexistent"))) else {
            r.error = "no PNG data available"; return r
        }
        r = timeIt(iters, warmup) {
            let src = CGImageSourceCreateWithData(d as CFData, nil)
            guard let img = src.flatMap({ CGImageSourceCreateImageAtIndex($0, 0, nil) }) else { return nil }
            // drawing forces the real decode
            _ = bgraExtract(img)
            return img
        }
        r.meta["note"] = "PNG bytes -> decoded pixels (handle + forced BGRA draw). This is the cost the pipeline actually pays before it can look at pixels; subtract bgra_extract to isolate the codec."
        r.meta["png_bytes"] = d.count
        return r
    }))

    // ---- 16. BGRA extraction / numpy handoff ----
    list.append(("bgra_extract", {
        var r = R()
        guard let img = ctx.cachedWindowImage else { r.error = "no cached window image"; return r }
        r = timeIt(iters, warmup) { return bgraExtract(img) }
        r.meta["note"] = "CGContext draw into a freshly allocated BGRA buffer (alloc + draw). This is the raw pixel handoff to numpy."
        r.meta["image"] = imageInfo(img)
        return r
    }))

    // ---- 17. in-memory ROI crop ----
    list.append(("roi_crop_inmemory", {
        var r = R()
        guard let img = ctx.cachedWindowImage else { r.error = "no cached window image"; return r }
        let s = CGFloat(ctx.scale)
        let cropRect = CGRect(x: roiLocal.origin.x * s, y: roiLocal.origin.y * s,
                              width: roiLocal.width * s, height: roiLocal.height * s)
            .integral
            .intersection(CGRect(x: 0, y: 0, width: img.width, height: img.height))
        r = timeIt(iters, warmup) { return img.cropping(to: cropRect) }
        r.meta["note"] = "CGImage.cropping(to:) on an already-captured full-window image — ROI with zero capture cost"
        r.meta["crop_rect"] = [Double(cropRect.origin.x), Double(cropRect.origin.y),
                               Double(cropRect.width), Double(cropRect.height)]
        r.meta["image"] = imageInfo(img.cropping(to: cropRect))
        return r
    }))

    // ---- 18. optimized in-process pipeline: SCK capture + BGRA ----
    list.append(("inproc_pipeline_sck_bgra", {
        var r = R()
        if let e = ctx.ensureSC() { r.error = e; return r }
        guard let filter = ctx.scFilter, let cfg = ctx.scConfigBest else { r.error = "sck not built"; return r }
        r = timeIt(iters, warmup) {
            let img = try sckScreenshotSync(filter: filter, config: cfg)
            guard let px = bgraExtract(img) else {
                throw NSError(domain: "bench_capture", code: 8,
                              userInfo: [NSLocalizedDescriptionKey: "bgra extract failed"])
            }
            return px.count
        }
        r.meta["note"] = "optimized end-to-end capture: SCScreenshotManager + BGRA extract ready for numpy. Compare against prod_shot_pipeline."
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    // ---- 19. optimized in-process pipeline: SCK ROI capture + BGRA ----
    list.append(("inproc_pipeline_sck_roi_bgra", {
        var r = R()
        if let e = ctx.ensureSC() { r.error = e; return r }
        guard let filter = ctx.scFilter, let cfg = ctx.scRoiConfig else { r.error = "sck not built"; return r }
        r = timeIt(iters, warmup) {
            let img = try sckScreenshotSync(filter: filter, config: cfg)
            guard let px = bgraExtract(img) else {
                throw NSError(domain: "bench_capture", code: 9,
                              userInfo: [NSLocalizedDescriptionKey: "bgra extract failed"])
            }
            return px.count
        }
        r.meta["note"] = "optimized ROI end-to-end: SCK sourceRect capture + BGRA extract for a \(Int(roiGlobal.width))x\(Int(roiGlobal.height)) pt region"
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    // ---- 20..25. measured FULL LOOPS: capture + Vision OCR ----
    list.append(("loop_prod_accurate", {
        var r = R()
        let path = ctx.tmpDir.appendingPathComponent("loop_prod.png").path
        var items: [Int] = []
        r = timeIt(loopIters, 1) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            p.arguments = ["-x", "-R",
                           "\(Int(winRect.origin.x)),\(Int(winRect.origin.y)),\(Int(winRect.width)),\(Int(winRect.height))",
                           path]
            try p.run()
            p.waitUntilExit()
            guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
                  let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
                throw NSError(domain: "bench_capture", code: 10,
                              userInfo: [NSLocalizedDescriptionKey: "decode failed"])
            }
            let n = try visionOCR(img, level: .accurate, langs: langs)
            items.append(n)
            return n
        }
        r.meta["note"] = "CURRENT PRODUCTION LOOP: screencapture subprocess + PNG decode + Vision .accurate on the full window"
        r.meta["ocr_items_mean"] = items.isEmpty ? 0 : Double(items.reduce(0, +)) / Double(items.count)
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    list.append(("loop_prod_fast", {
        var r = R()
        let path = ctx.tmpDir.appendingPathComponent("loop_prod_fast.png").path
        var items: [Int] = []
        r = timeIt(loopIters, 1) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            p.arguments = ["-x", "-R",
                           "\(Int(winRect.origin.x)),\(Int(winRect.origin.y)),\(Int(winRect.width)),\(Int(winRect.height))",
                           path]
            try p.run()
            p.waitUntilExit()
            guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
                  let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
                throw NSError(domain: "bench_capture", code: 11,
                              userInfo: [NSLocalizedDescriptionKey: "decode failed"])
            }
            let n = try visionOCR(img, level: .fast, langs: langs)
            items.append(n)
            return n
        }
        r.meta["note"] = "production capture path (screencapture subprocess) + Vision .fast on the full window"
        r.meta["ocr_items_mean"] = items.isEmpty ? 0 : Double(items.reduce(0, +)) / Double(items.count)
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    list.append(("loop_inproc_accurate", {
        var r = R()
        if let e = ctx.ensureSC() { r.error = e; return r }
        guard let filter = ctx.scFilter, let cfg = ctx.scConfigBest else { r.error = "sck not built"; return r }
        var items: [Int] = []
        r = timeIt(loopIters, 1) {
            let img = try sckScreenshotSync(filter: filter, config: cfg)
            let n = try visionOCR(img, level: .accurate, langs: langs)
            items.append(n)
            return n
        }
        r.meta["note"] = "OPTIMIZED LOOP: in-process SCScreenshotManager + Vision .accurate on the full window. No subprocess, no PNG."
        r.meta["ocr_items_mean"] = items.isEmpty ? 0 : Double(items.reduce(0, +)) / Double(items.count)
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    list.append(("loop_inproc_fast", {
        var r = R()
        if let e = ctx.ensureSC() { r.error = e; return r }
        guard let filter = ctx.scFilter, let cfg = ctx.scConfigBest else { r.error = "sck not built"; return r }
        var items: [Int] = []
        r = timeIt(loopIters, 1) {
            let img = try sckScreenshotSync(filter: filter, config: cfg)
            let n = try visionOCR(img, level: .fast, langs: langs)
            items.append(n)
            return n
        }
        r.meta["note"] = "OPTIMIZED LOOP: in-process SCScreenshotManager + Vision .fast on the full window"
        r.meta["ocr_items_mean"] = items.isEmpty ? 0 : Double(items.reduce(0, +)) / Double(items.count)
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    list.append(("loop_inproc_fast_roi", {
        var r = R()
        if let e = ctx.ensureSC() { r.error = e; return r }
        guard let filter = ctx.scFilter, let cfg = ctx.scRoiConfig else { r.error = "sck not built"; return r }
        var items: [Int] = []
        r = timeIt(loopIters, 1) {
            let img = try sckScreenshotSync(filter: filter, config: cfg)
            let n = try visionOCR(img, level: .fast, langs: langs)
            items.append(n)
            return n
        }
        r.meta["note"] = "OPTIMIZED LOOP, ROI only: SCK sourceRect \(Int(roiLocal.width))x\(Int(roiLocal.height)) pts + Vision .fast"
        r.meta["ocr_items_mean"] = items.isEmpty ? 0 : Double(items.reduce(0, +)) / Double(items.count)
        r.meta["sourceRect"] = [Double(roiLocal.origin.x), Double(roiLocal.origin.y),
                                Double(roiLocal.width), Double(roiLocal.height)]
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    list.append(("loop_inproc_accurate_roi", {
        var r = R()
        if let e = ctx.ensureSC() { r.error = e; return r }
        guard let filter = ctx.scFilter, let cfg = ctx.scRoiConfig else { r.error = "sck not built"; return r }
        var items: [Int] = []
        r = timeIt(loopIters, 1) {
            let img = try sckScreenshotSync(filter: filter, config: cfg)
            let n = try visionOCR(img, level: .accurate, langs: langs)
            items.append(n)
            return n
        }
        r.meta["note"] = "OPTIMIZED LOOP, ROI only: SCK sourceRect \(Int(roiLocal.width))x\(Int(roiLocal.height)) pts + Vision .accurate"
        r.meta["ocr_items_mean"] = items.isEmpty ? 0 : Double(items.reduce(0, +)) / Double(items.count)
        r.meta["sourceRect"] = [Double(roiLocal.origin.x), Double(roiLocal.origin.y),
                                Double(roiLocal.width), Double(roiLocal.height)]
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    // ---- 26/27. INSTRUMENTED loops: per-stage timing from the SAME iterations ----
    //
    // The algebraic decomposition (loop - capture) is corrupted by load drift
    // between sequential backends. These two record a timestamp at every stage
    // boundary inside each iteration, so the split is measured, not inferred.
    list.append(("loop_prod_instrumented", {
        var r = R()
        let path = ctx.tmpDir.appendingPathComponent("loop_instr.png").path
        var capT: [Double] = [], decT: [Double] = [], ocrT: [Double] = []
        var totals: [Double] = []
        var items: [Int] = []
        var err: String? = nil
        for i in 0..<(loopIters + 1) {
            let t0 = monoMS()
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            p.arguments = ["-x", "-R",
                           "\(Int(winRect.origin.x)),\(Int(winRect.origin.y)),\(Int(winRect.width)),\(Int(winRect.height))",
                           path]
            do { try p.run() } catch { err = "spawn failed"; break }
            p.waitUntilExit()
            let t1 = monoMS()
            guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
                  let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
                err = "decode failed"; break
            }
            // force the real decode here so it is attributed to decode, not OCR
            _ = bgraExtract(img)
            let t2 = monoMS()
            let n: Int
            do { n = try visionOCR(img, level: .accurate, langs: langs) }
            catch { err = "ocr failed: \(error)"; break }
            let t3 = monoMS()
            if i == 0 { continue }   // warmup iteration, discarded
            capT.append(t1 - t0); decT.append(t2 - t1); ocrT.append(t3 - t2)
            totals.append(t3 - t0)
            items.append(n)
        }
        if let e = err { r.error = e; return r }
        r.times = totals
        r.meta["note"] = "INSTRUMENTED production loop: capture / forced PNG decode / Vision .accurate timed separately INSIDE each iteration. No cross-backend arithmetic."
        r.meta["stages"] = [
            "capture_subprocess": computeStats(capT),
            "png_decode_forced": computeStats(decT),
            "vision_ocr_accurate": computeStats(ocrT),
        ]
        r.meta["ocr_items_mean"] = items.isEmpty ? 0 : Double(items.reduce(0, +)) / Double(items.count)
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    list.append(("loop_inproc_instrumented", {
        var r = R()
        if let e = ctx.ensureSC() { r.error = e; return r }
        guard let filter = ctx.scFilter, let cfg = ctx.scConfigBest else { r.error = "sck not built"; return r }
        var capT: [Double] = [], ocrT: [Double] = []
        var totals: [Double] = []
        var items: [Int] = []
        var err: String? = nil
        for i in 0..<(loopIters + 1) {
            let t0 = monoMS()
            let img: CGImage
            do { img = try sckScreenshotSync(filter: filter, config: cfg) }
            catch { err = "sck capture failed: \(error)"; break }
            let t1 = monoMS()
            let n: Int
            do { n = try visionOCR(img, level: .accurate, langs: langs) }
            catch { err = "ocr failed: \(error)"; break }
            let t2 = monoMS()
            if i == 0 { continue }
            capT.append(t1 - t0); ocrT.append(t2 - t1)
            totals.append(t2 - t0)
            items.append(n)
        }
        if let e = err { r.error = e; return r }
        r.times = totals
        r.meta["note"] = "INSTRUMENTED in-process loop: SCK capture / Vision .accurate timed separately INSIDE each iteration."
        r.meta["stages"] = [
            "sck_capture": computeStats(capT),
            "vision_ocr_accurate": computeStats(ocrT),
        ]
        r.meta["ocr_items_mean"] = items.isEmpty ? 0 : Double(items.reduce(0, +)) / Double(items.count)
        r.meta.merge(ctx.windowMeta()) { a, _ in a }
        return r
    }))

    return list
}

// MARK: - SCStream start/stop synchronous bridges

func awaitStartCaptureSync(_ stream: SCStream) throws {
    let box = SCBox()
    let sem = DispatchSemaphore(value: 0)
    stream.startCapture { err in
        box.error = err
        sem.signal()
    }
    guard waitPumping(sem) else {
        throw NSError(domain: "bench_capture", code: 99,
                      userInfo: [NSLocalizedDescriptionKey: "timeout waiting for ScreenCaptureKit"])
    }
    if let e = box.error { throw e }
}

func awaitStopCaptureSync(_ stream: SCStream) throws {
    let box = SCBox()
    let sem = DispatchSemaphore(value: 0)
    stream.stopCapture { err in
        box.error = err
        sem.signal()
    }
    guard waitPumping(sem) else {
        throw NSError(domain: "bench_capture", code: 99,
                      userInfo: [NSLocalizedDescriptionKey: "timeout waiting for ScreenCaptureKit"])
    }
    if let e = box.error { throw e }
}

// MARK: - ocr mode

func runOCR(_ args: Args) -> [String: Any] {
    let path = args.str("file", "artifacts/game.png")
    let levelStr = args.str("level", "accurate")
    let iters = args.int("iters", 10)
    let warmup = args.int("warmup", 1)
    let langs = args.str("langs", "zh-Hans,en-US").split(separator: ",").map { String($0) }
    // Production (scripts/seerd.swift, scripts/seerctl.swift) sets this false.
    // Vision's DEFAULT is true, which is materially slower for .accurate.
    let langCorr = args.str("langcorr", "false").lowercased() == "true"

    var out: [String: Any] = ["mode": "ocr", "file": path, "level": levelStr,
                              "langs": langs, "iters": iters,
                              "usesLanguageCorrection": langCorr]

    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else {
        out["error"] = "cannot open image at \(path)"
        return out
    }
    let tDec0 = monoMS()
    guard let fullImg = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
        out["error"] = "cannot decode image at \(path)"
        return out
    }
    // CGImageSourceCreateImageAtIndex is LAZY: it returns a handle without
    // decoding pixels. Report that handle cost, then force a real decode so the
    // true decode cost is on the record too.
    out["image_source_handle_ms"] = monoMS() - tDec0
    let tForce0 = monoMS()
    let forced = bgraExtract(fullImg)
    out["forced_decode_ms"] = monoMS() - tForce0
    out["forced_decode_ok"] = forced != nil
    out["decode_ms"] = out["image_source_handle_ms"]
    out["decode_note"] = "image_source_handle_ms is the lazy CGImageSource handle; forced_decode_ms is a real full-pixel decode (BGRA draw)"
    out["source_image"] = imageInfo(fullImg)

    var img = fullImg
    if let rectStr = args.flags["rect"], let r = parseRect(rectStr) {
        let intRect = r.integral.intersection(CGRect(x: 0, y: 0, width: fullImg.width, height: fullImg.height))
        guard let cropped = fullImg.cropping(to: intRect) else {
            out["error"] = "crop \(rectStr) outside image bounds \(fullImg.width)x\(fullImg.height)"
            return out
        }
        img = cropped
        out["rect"] = [Double(intRect.origin.x), Double(intRect.origin.y),
                       Double(intRect.width), Double(intRect.height)]
    }
    out["ocr_image"] = imageInfo(img)
    out["pixels"] = img.width * img.height

    let level: VNRequestTextRecognitionLevel = (levelStr == "fast") ? .fast : .accurate

    func oneRun() throws -> (Int, [[String: Any]]) {
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = level
        req.usesLanguageCorrection = langCorr
        req.recognitionLanguages = langs
        let handler = VNImageRequestHandler(cgImage: img, options: [:])
        try handler.perform([req])
        var items: [[String: Any]] = []
        for obs in (req.results ?? []) {
            guard let c = obs.topCandidates(1).first else { continue }
            if c.confidence < 0.3 { continue }
            items.append(["text": c.string, "conf": Double(c.confidence)])
        }
        return (items.count, items)
    }

    // COLD first call: before any warmup. This includes Vision's one-time
    // framework/model initialisation, which a resident daemon pays exactly once.
    var coldMs: Double? = nil
    var coldItems = 0
    do {
        let t0 = monoMS()
        let (n, _) = try oneRun()
        coldMs = monoMS() - t0
        coldItems = n
        out["cold_first_call_ms"] = coldMs
        out["cold_first_call_items"] = coldItems
    } catch {
        out["error"] = "Vision failed on cold call: \(error)"
        return out
    }

    for _ in 0..<warmup { _ = try? oneRun() }

    var times: [Double] = []
    var lastItems: [[String: Any]] = []
    var lastCount = 0
    for _ in 0..<iters {
        let t0 = monoMS()
        do {
            let (n, items) = try oneRun()
            times.append(monoMS() - t0)
            lastCount = n
            lastItems = items
        } catch {
            out["error"] = "Vision failed: \(error)"
            return out
        }
    }
    out["times_ms"] = times
    for (k, v) in computeStats(times) { out["ocr_" + k] = v }
    out["items"] = lastCount
    out["sample_texts"] = lastItems.prefix(12).map { $0["text"] as? String ?? "" }
    return out
}

// MARK: - main

extension R {
    /// First frame image captured during a stream backend (process-global, set by the sink).
    static var firstFrameImage: CGImage? {
        get { return R._firstFrameImage }
        set { R._firstFrameImage = newValue }
    }
    private static var _firstFrameImage: CGImage?
}

// AppKit MUST be initialised before SCContentFilter(desktopIndependentWindow:)
// is used, otherwise the process aborts with
//   Assertion failed: (did_initialize), function CGS_REQUIRE_INIT
// This is a measured requirement on this machine, not a guess. A non-bundled
// binary gets activation policy .prohibited, so this creates no Dock icon, no
// menu bar and steals no focus.
_ = NSApplication.shared

let argv = Array(CommandLine.arguments.dropFirst())
guard let mode = argv.first else {
    logErr("usage: bench_capture probe|capture|ocr|windows [options]")
    exit(1)
}
let args = Args(Array(argv.dropFirst()))

switch mode {
case "probe":
    var d = probe()
    d["mode"] = "probe"
    emit(d)

case "windows":
    let wins = enumerateWindows().filter { $0.layer == 0 && $0.w > 100 }
    let arr: [[String: Any]] = wins.map {
        ["id": $0.id, "owner": $0.owner, "title": $0.title, "layer": $0.layer,
         "x": $0.x, "y": $0.y, "w": $0.w, "h": $0.h, "pid": $0.pid]
    }
    emit(["mode": "windows", "count": arr.count, "windows": arr])

case "sckwindows":
    // Debug: what does SCShareableContent actually expose? Optionally show a
    // self-owned animating window first so we can see whether it becomes
    // shareable once the run loop has composited it.
    var shown: AnimWindow? = nil
    if args.has("with-anim") {
        shown = AnimWindow()
        shown?.show()
        logErr("anim window number=\(shown?.windowNumber ?? -1)")
    }
    var d: [String: Any] = ["mode": "sckwindows"]
    do {
        let content = try sckShareableSync(onScreenOnly: !args.has("all"))
        d["display_count"] = content.displays.count
        d["window_count"] = content.windows.count
        d["displays"] = content.displays.map {
            ["displayID": Int($0.displayID), "width": $0.width, "height": $0.height,
             "frame_x": Double($0.frame.origin.x), "frame_y": Double($0.frame.origin.y)]
        }
        let myPid = ProcessInfo.processInfo.processIdentifier
        d["windows"] = content.windows.map { w -> [String: Any] in
            var e: [String: Any] = [
                "windowID": Int(w.windowID),
                "title": w.title ?? "",
                "app": w.owningApplication?.applicationName ?? "",
                "pid": Int(w.owningApplication?.processID ?? 0),
                "isSelf": Int(w.owningApplication?.processID ?? 0) == Int(myPid),
                "layer": w.windowLayer,
                "frame_x": Double(w.frame.origin.x), "frame_y": Double(w.frame.origin.y),
                "frame_w": Double(w.frame.width), "frame_h": Double(w.frame.height),
            ]
            if #available(macOS 14.0, *) {
                e["sharingState"] = w.windowLayer >= 0 ? "layer>=0" : "layer<0"
            }
            return e
        }
    } catch {
        d["error"] = "\(error)"
    }
    if let s = shown { s.close() }
    emit(d)

case "capture":
    let t0 = monoMS()
    let ctx = Ctx(args: args)
    let only = args.flags["only"]?.split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }
    let backends = buildBackends(ctx)
    var results: [[String: Any]] = []
    for (name, fn) in backends {
        if let only = only, !only.contains(name) { continue }
        let r = fn()
        var d: [String: Any] = ["backend": name, "meta": r.meta, "samples_ms": r.times]
        if let e = r.error { d["error"] = e }
        for (k, v) in computeStats(r.times) { d[k] = v }
        results.append(d)
        logErr("[bench_capture] \(name) n=\(r.times.count)\(r.error.map { " ERROR: \($0)" } ?? "")")
    }
    var out: [String: Any] = [
        "mode": "capture",
        "iters": args.int("iters", 20),
        "loop_iters": args.int("loop-iters", 8),
        "warmup": args.int("warmup", 2),
        "wall_ms": monoMS() - t0,
        "scale_factor": ctx.scale,
        "target_window": ctx.windowMeta(),
        "roi_local": [Double(ctx.roiLocal.origin.x), Double(ctx.roiLocal.origin.y),
                      Double(ctx.roiLocal.width), Double(ctx.roiLocal.height)],
        "roi_global": [Double(ctx.roiGlobal.origin.x), Double(ctx.roiGlobal.origin.y),
                       Double(ctx.roiGlobal.width), Double(ctx.roiGlobal.height)],
        "results": results,
    ]
    out["env"] = probe()
    ctx.teardown()
    emit(out)

case "ocr":
    emit(runOCR(args))

default:
    logErr("unknown mode \(mode)")
    exit(1)
}
