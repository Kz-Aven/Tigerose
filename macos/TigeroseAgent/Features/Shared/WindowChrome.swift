import AppKit

enum WindowChrome {
    /// Matches Finder/Safari title-bar double-click, including the system preference
    /// (System Settings → Desktop & Dock → “Double-click a window’s title bar to”).
    static func performTitlebarDoubleClick(for window: NSWindow? = nil) {
        guard let window = window ?? NSApp.keyWindow ?? NSApp.windows.first(where: \.isVisible) else { return }
        let action = UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick") ?? "Maximize"
        switch action {
        case "Minimize":
            window.miniaturize(nil)
        case "None":
            break
        default:
            window.zoom(nil)
        }
    }
}

/// System title-bar strip double-click (the empty band under the traffic lights).
/// SwiftUI overlays are unreliable here once the toolbar title/background is removed.
enum TitlebarDoubleClickMonitor {
    private static var monitor: Any?

    static func install() {
        guard monitor == nil else { return }
        monitor = NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown) { event in
            guard event.clickCount == 2 else { return event }
            guard let window = event.window, window.styleMask.contains(.titled) else { return event }
            guard let content = window.contentView else { return event }

            let point = event.locationInWindow
            let band = titlebarBandHeight(in: window, content: content)
            // AppKit: y=0 at bottom.
            guard point.y >= content.bounds.height - band else { return event }
            // Leave traffic-light hit targets alone.
            guard point.x >= 78 else { return event }

            if isInteractiveControl(content.hitTest(point)) {
                return event
            }

            WindowChrome.performTitlebarDoubleClick(for: window)
            return nil
        }
    }

    private static func titlebarBandHeight(in window: NSWindow, content: NSView) -> CGFloat {
        let layout = window.contentLayoutRect
        let inset = content.bounds.height - layout.maxY
        // Hidden toolbar + full-bleed content often reports a small inset; keep a
        // Finder-like strip without covering the chat header controls below.
        return min(64, max(40, inset > 8 ? inset : 56))
    }

    private static func isInteractiveControl(_ view: NSView?) -> Bool {
        var current = view
        while let v = current {
            if v is NSControl || v is NSTextView || v is NSTextField {
                return true
            }
            let name = NSStringFromClass(type(of: v))
            if name.contains("Button") || name.contains("Menu") || name.contains("PopUp") {
                return true
            }
            current = v.superview
        }
        return false
    }
}
