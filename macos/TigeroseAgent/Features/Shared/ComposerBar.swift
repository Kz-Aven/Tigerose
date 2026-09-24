import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct ComposerBar: View {
    @State private var showConnectorMenu = false
    @Binding var text: String
    var placeholder: String
    var canSend: Bool
    var onSend: () -> Void
    var isRunning: Bool = false
    var onStop: (() -> Void)?
    var onAttach: (() -> Void)?
    var hasAttachments = false
    var onAttachFiles: (([URL]) -> Void)?
    @Binding var webEnabled: Bool
    var onWebEnabledChange: ((Bool) -> Void)?
    var dingtalkEnabled = false
    var dingtalkConnected = false
    var onDingtalkEnabledChange: ((Bool) -> Void)?
    var larkEnabled = false
    var larkConnected = false
    var onLarkEnabledChange: ((Bool) -> Void)?
    var onConnectorMenuOpened: (() -> Void)?

    var body: some View {
        HStack(alignment: .bottom, spacing: TigeroseTheme.spaceXs) {
            VStack(alignment: .leading, spacing: 6) {
                ComposerTextView(
                    text: $text,
                    placeholder: placeholder,
                    canSend: canSend,
                    onSend: onSend,
                    hasAttachments: hasAttachments,
                    onAttachFiles: onAttachFiles
                )
                .frame(minHeight: 22, maxHeight: 100, alignment: .top)

                HStack(spacing: 10) {
                    if let onAttach {
                        Button(action: onAttach) {
                            Image(systemName: "paperclip")
                                .font(.system(size: 13, weight: .medium))
                                .foregroundStyle(TigeroseTheme.muted)
                                .frame(width: 28, height: 28)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .help("上传附件")
                    }
                    Toggle(isOn: Binding(
                        get: { webEnabled },
                        set: { newValue in
                            webEnabled = newValue
                            onWebEnabledChange?(newValue)
                        }
                    )) {
                        HStack(spacing: 4) {
                            Image(systemName: "globe")
                                .font(.system(size: 12, weight: .medium))
                            Text("联网")
                                .font(TigeroseTheme.caption)
                        }
                        .foregroundStyle(webEnabled ? TigeroseTheme.primary : TigeroseTheme.muted)
                    }
                    .toggleStyle(.switch)
                    .controlSize(.mini)
                    .help(webEnabled ? "已开启联网（持久）" : "已关闭，本助理/本群不会上网")
                    Button {
                        showConnectorMenu.toggle()
                        if showConnectorMenu {
                            onConnectorMenuOpened?()
                        }
                    } label: {
                        Label("连接器", systemImage: "powerplug")
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(dingtalkEnabled || larkEnabled ? TigeroseTheme.primary : TigeroseTheme.muted)
                    }
                    .buttonStyle(.plain)
                    .help(dingtalkConnected || larkConnected ? "选择持续启用的连接器" : "请先在设置中连接连接器")
                    .popover(isPresented: $showConnectorMenu, arrowEdge: .bottom) {
                        ConnectorPicker(
                            dingtalkEnabled: dingtalkEnabled,
                            dingtalkConnected: dingtalkConnected,
                            onDingtalkEnabledChange: onDingtalkEnabledChange,
                            larkEnabled: larkEnabled,
                            larkConnected: larkConnected,
                            onLarkEnabledChange: onLarkEnabledChange
                        )
                    }
                    Spacer(minLength: 0)
                }
            }
            .contentShape(Rectangle())

            Button(action: {
                if isRunning {
                    onStop?()
                } else {
                    onSend()
                }
            }) {
                Image(systemName: isRunning ? "stop.fill" : "arrow.up")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(TigeroseTheme.onPrimary)
                    .frame(width: 36, height: 36)
                    .background(
                        isRunning || (canSend && canSubmit)
                            ? TigeroseTheme.controlFill
                            : TigeroseTheme.primaryDisabled,
                        in: Circle()
                    )
            }
            .buttonStyle(.plain)
            .disabled(
                isRunning
                    ? onStop == nil
                    : (!canSend || !canSubmit)
            )
            .help(isRunning ? "停止当前运行" : "发送")
        }
        .padding(TigeroseTheme.spaceSm)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusLg))
        .overlay(
            RoundedRectangle(cornerRadius: TigeroseTheme.radiusLg)
                .stroke(TigeroseTheme.hairline, lineWidth: 1)
        )
        .padding(.horizontal, TigeroseTheme.spaceMd)
        .padding(.top, TigeroseTheme.spaceXs)
        .padding(.bottom, TigeroseTheme.spaceMd)
    }

    private var canSubmit: Bool {
        hasAttachments || !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}

private struct ConnectorPicker: View {
    let dingtalkEnabled: Bool
    let dingtalkConnected: Bool
    var onDingtalkEnabledChange: ((Bool) -> Void)?
    let larkEnabled: Bool
    let larkConnected: Bool
    var onLarkEnabledChange: ((Bool) -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Toggle("钉钉", isOn: Binding(
                get: { dingtalkEnabled },
                set: { onDingtalkEnabledChange?($0) }
            ))
            .toggleStyle(.switch)
            .disabled(!dingtalkConnected)
            Toggle("飞书", isOn: Binding(
                get: { larkEnabled },
                set: { onLarkEnabledChange?($0) }
            ))
                .toggleStyle(.switch)
                .disabled(!larkConnected)
            Toggle("企微", isOn: .constant(false))
                .toggleStyle(.switch)
                .disabled(true)
        }
        .font(TigeroseTheme.bodySm)
        .frame(width: 168, alignment: .leading)
        .padding(14)
    }
}

/// Multiline composer: Enter sends; Shift+Enter / Option+Enter inserts newline.
private struct ComposerTextView: NSViewRepresentable {
    @Binding var text: String
    var placeholder: String
    var canSend: Bool
    var onSend: () -> Void
    var hasAttachments: Bool
    var onAttachFiles: (([URL]) -> Void)?

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    func makeNSView(context: Context) -> ComposerHostView {
        let host = ComposerHostView()
        context.coordinator.host = host
        context.coordinator.parent = self
        host.onLayout = { [weak coord = context.coordinator] in
            coord?.layoutPlaceholder()
        }

        let tv = host.textView
        tv.delegate = context.coordinator
        tv.string = text
        tv.onPlainReturn = { [weak coord = context.coordinator] in
            coord?.handleReturn()
        }
        tv.onAttachFiles = { [weak coord = context.coordinator] urls in
            coord?.parent.onAttachFiles?(urls)
        }
        context.coordinator.textView = tv
        context.coordinator.setPlaceholder(placeholder)
        host.invalidateIntrinsicContentSize()
        host.layoutComposer()
        return host
    }

    func updateNSView(_ host: ComposerHostView, context: Context) {
        context.coordinator.parent = self
        context.coordinator.host = host
        host.onLayout = { [weak coord = context.coordinator] in
            coord?.layoutPlaceholder()
        }
        let tv = host.textView
        context.coordinator.textView = tv
        tv.onAttachFiles = { [weak coord = context.coordinator] urls in
            coord?.parent.onAttachFiles?(urls)
        }
        if tv.string != text {
            // External updates (@mention chips, clear-after-send): keep caret at end.
            tv.string = text
            let end = (tv.string as NSString).length
            tv.setSelectedRange(NSRange(location: end, length: 0))
            tv.scrollRangeToVisible(NSRange(location: end, length: 0))
            tv.window?.makeFirstResponder(tv)
        }
        context.coordinator.setPlaceholder(placeholder)
        host.invalidateIntrinsicContentSize()
        host.layoutComposer()
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: ComposerTextView
        weak var host: ComposerHostView?
        weak var textView: ComposerNSTextView?
        private var placeholderLabel: NSTextField?

        init(_ parent: ComposerTextView) {
            self.parent = parent
        }

        func handleReturn() {
            guard parent.canSend else { return }
            let trimmed = parent.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard parent.hasAttachments || !trimmed.isEmpty else { return }
            parent.onSend()
        }

        func setPlaceholder(_ text: String) {
            guard let host else { return }
            if placeholderLabel == nil {
                let label = PassThroughPlaceholder()
                label.isEditable = false
                label.isBordered = false
                label.drawsBackground = false
                label.textColor = NSColor.placeholderTextColor
                label.font = NSFont.systemFont(ofSize: 13)
                label.refusesFirstResponder = true
                host.addSubview(label)
                placeholderLabel = label
            }
            placeholderLabel?.stringValue = text
            placeholderLabel?.isHidden = !(textView?.string.isEmpty ?? true)
            layoutPlaceholder()
        }

        func layoutPlaceholder() {
            guard let host, let label = placeholderLabel, let tv = textView else { return }
            let inset = tv.textContainerInset
            let labelH: CGFloat = 18
            // AppKit y=0 is bottom; pin placeholder to the top-left of the host.
            label.frame = NSRect(
                x: inset.width,
                y: max(0, host.bounds.height - inset.height - labelH),
                width: max(40, host.bounds.width - inset.width * 2),
                height: labelH
            )
        }

        func textDidChange(_ notification: Notification) {
            guard let tv = textView else { return }
            parent.text = tv.string
            placeholderLabel?.isHidden = !tv.string.isEmpty
            host?.invalidateIntrinsicContentSize()
            host?.layoutComposer()
            layoutPlaceholder()
            // Keep caret visible when content grows past max height.
            tv.scrollRangeToVisible(tv.selectedRange())
        }
    }
}

/// Host that sizes the embedded NSTextView and keeps placeholder hits pass-through.
final class ComposerHostView: NSView {
    let textView: ComposerNSTextView
    var onLayout: (() -> Void)?
    private let scrollView: NSScrollView
    private let minTextHeight: CGFloat = 22
    private let maxTextHeight: CGFloat = 100

    override init(frame frameRect: NSRect) {
        let tv = ComposerNSTextView()
        tv.isRichText = false
        tv.allowsUndo = true
        tv.font = NSFont.systemFont(ofSize: 13)
        tv.textColor = NSColor.labelColor
        tv.backgroundColor = .clear
        tv.drawsBackground = false
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.alignment = .natural
        tv.textContainer?.widthTracksTextView = true
        tv.textContainer?.heightTracksTextView = false
        tv.textContainer?.lineFragmentPadding = 0
        tv.textContainer?.containerSize = NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude)
        // Small top inset so first line sits at top-left (not vertically centered).
        tv.textContainerInset = NSSize(width: 0, height: 2)
        tv.minSize = NSSize(width: 0, height: 0)
        tv.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        tv.autoresizingMask = []
        tv.registerForDraggedTypes([.fileURL])

        let scroll = NSScrollView()
        scroll.borderType = .noBorder
        scroll.drawsBackground = false
        scroll.backgroundColor = .clear
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        scroll.autoresizingMask = []
        scroll.documentView = tv

        self.textView = tv
        self.scrollView = scroll
        super.init(frame: frameRect)
        addSubview(scroll)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    override var intrinsicContentSize: NSSize {
        let width = max(bounds.width, 40)
        return NSSize(width: NSView.noIntrinsicMetric, height: visibleHeight(forWidth: width))
    }

    override func layout() {
        super.layout()
        layoutComposer()
        onLayout?()
    }

    func layoutComposer() {
        let width = max(bounds.width, 40)
        let measured = measuredContentHeight(forWidth: width)
        // Prefer current bounds (from SwiftUI intrinsic size); fall back while unbound.
        let visibleH = bounds.height > 1 ? bounds.height : visibleHeight(forWidth: width)

        // Host is bottom-left origin; pin scroll view to the top.
        let y = max(0, bounds.height - visibleH)
        scrollView.frame = NSRect(x: 0, y: y, width: width, height: visibleH)

        // Document must be at least as tall as the clip view so short text still
        // fills the hit area; taller content enables vertical scrolling.
        let docH = max(visibleH, measured)
        textView.minSize = NSSize(width: width, height: docH)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.frame = NSRect(x: 0, y: 0, width: width, height: docH)
        textView.textContainer?.containerSize = NSSize(
            width: max(0, width),
            height: CGFloat.greatestFiniteMagnitude
        )
    }

    private func visibleHeight(forWidth width: CGFloat) -> CGFloat {
        min(maxTextHeight, max(minTextHeight, measuredContentHeight(forWidth: width)))
    }

    private func measuredContentHeight(forWidth width: CGFloat) -> CGFloat {
        guard let lm = textView.layoutManager, let tc = textView.textContainer else {
            return minTextHeight
        }
        tc.containerSize = NSSize(width: max(0, width), height: CGFloat.greatestFiniteMagnitude)
        lm.ensureLayout(for: tc)
        let used = lm.usedRect(for: tc)
        return ceil(used.height + textView.textContainerInset.height * 2)
    }
}

final class ComposerNSTextView: NSTextView {
    var onPlainReturn: (() -> Void)?
    var onAttachFiles: (([URL]) -> Void)?

    override func keyDown(with event: NSEvent) {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if event.keyCode == 9, flags.contains(.command), !flags.contains(.shift), !flags.contains(.option), attachPasteboardItems() {
            return
        }
        // Enter sends; Shift/Option+Enter inserts newline.
        if event.keyCode == 36 {
            // Let the active input method commit its candidate before treating Return as send.
            if hasMarkedText() {
                super.keyDown(with: event)
                return
            }
            if flags.contains(.shift) || flags.contains(.option) {
                super.keyDown(with: event)
                return
            }
            if flags.isEmpty || flags == .function {
                onPlainReturn?()
                return
            }
        }
        super.keyDown(with: event)
    }

    override func paste(_ sender: Any?) {
        if !attachPasteboardItems() { super.paste(sender) }
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        fileURLs(from: sender.draggingPasteboard).isEmpty ? [] : .copy
    }

    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool {
        !fileURLs(from: sender.draggingPasteboard).isEmpty
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let urls = fileURLs(from: sender.draggingPasteboard)
        guard !urls.isEmpty else { return false }
        onAttachFiles?(urls)
        return true
    }

    private func fileURLs(from pasteboard: NSPasteboard) -> [URL] {
        (pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL]) ?? []
    }

    @discardableResult
    private func attachPasteboardItems() -> Bool {
        guard let onAttachFiles else { return false }
        let pasteboard = NSPasteboard.general
        let urls = fileURLs(from: pasteboard)
        if !urls.isEmpty {
            onAttachFiles(urls)
            return true
        }
        guard let image = pastedImage(from: pasteboard), let url = writePastedImage(image) else {
            return false
        }
        onAttachFiles([url])
        return true
    }

    private func pastedImage(from pasteboard: NSPasteboard) -> NSImage? {
        for type in pasteboard.types ?? [] {
            guard let contentType = UTType(type.rawValue), contentType.conforms(to: .image),
                  let data = pasteboard.data(forType: type), let image = NSImage(data: data)
            else { continue }
            return image
        }
        return nil
    }

    private func writePastedImage(_ image: NSImage) -> URL? {
        guard let tiff = image.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiff),
              let data = bitmap.representation(using: .png, properties: [:])
        else { return nil }
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("pasted-image-\(UUID().uuidString).png")
        do {
            try data.write(to: url, options: .atomic)
            return url
        } catch {
            return nil
        }
    }
}

/// Placeholder that never steals clicks from the text view beneath.
private final class PassThroughPlaceholder: NSTextField {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}
