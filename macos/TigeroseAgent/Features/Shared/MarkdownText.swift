import AppKit
import MarkdownUI
import SwiftUI

/// Chat markdown via MarkdownUI (GFM: tables, code, lists, …).
struct MarkdownText: View {
    let source: String
    var isUser: Bool = false

    var body: some View {
        if isUser {
            Text(source)
                .font(TigeroseTheme.bodyMd)
                .foregroundStyle(TigeroseTheme.ink)
                .textSelection(.enabled)
                .multilineTextAlignment(.leading)
        } else if Self.isPlainParagraph(source) {
            SelectablePlainParagraph(source: source)
        } else {
            Markdown(Self.prepareChatMarkdown(source))
                .markdownTheme(Self.tigeroseTheme)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private static func isPlainParagraph(_ source: String) -> Bool {
        let lines = source.split(separator: "\n", omittingEmptySubsequences: false)
        let hasBlockMarkup = lines.contains { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            return trimmed.hasPrefix("#")
                || trimmed.hasPrefix("|")
                || trimmed.hasPrefix(">")
                || trimmed.hasPrefix("- ")
                || trimmed.hasPrefix("* ")
                || trimmed.hasPrefix("```")
                || trimmed.range(of: #"^\d+\. "#, options: .regularExpression) != nil
        }
        return !hasBlockMarkup
            && !source.contains("**")
            && !source.contains("`")
            && !source.contains("[")
    }

    /// Theme aligned with Tigerose cream/coral chat UI — roomy line/paragraph spacing for reading.
    private static let tigeroseTheme = Theme()
        .text {
            ForegroundColor(TigeroseTheme.body)
            FontSize(16)
        }
        .strong {
            FontWeight(.semibold)
            ForegroundColor(TigeroseTheme.bodyStrong)
        }
        .link {
            ForegroundColor(TigeroseTheme.primary)
        }
        .code {
            FontFamilyVariant(.monospaced)
            FontSize(13)
            ForegroundColor(TigeroseTheme.ink)
            BackgroundColor(TigeroseTheme.surfaceSoft)
        }
        .paragraph { configuration in
            configuration.label
                .fixedSize(horizontal: false, vertical: true)
                .relativeLineSpacing(.em(0.45))
                .markdownMargin(top: .zero, bottom: .em(0.85))
        }
        .listItem { configuration in
            configuration.label
                .relativeLineSpacing(.em(0.4))
                .markdownMargin(top: .em(0.2), bottom: .em(0.2))
        }
        .codeBlock { configuration in
            configuration.label
                .markdownTextStyle {
                    FontFamilyVariant(.monospaced)
                    FontSize(13)
                    ForegroundColor(TigeroseTheme.ink)
                }
                .relativeLineSpacing(.em(0.25))
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(TigeroseTheme.surfaceSoft, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
                .overlay(
                    RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                        .stroke(TigeroseTheme.hairline, lineWidth: 1)
                )
                .markdownMargin(top: .em(0.5), bottom: .em(0.75))
        }
        .blockquote { configuration in
            configuration.label
                .relativeLineSpacing(.em(0.4))
                .foregroundStyle(TigeroseTheme.muted)
                .padding(.vertical, 6)
                .padding(.leading, 14)
                .padding(.trailing, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(TigeroseTheme.surfaceSoft.opacity(0.6), in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm))
                .overlay(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(TigeroseTheme.primary.opacity(0.5))
                        .frame(width: 3)
                        .padding(.vertical, 2)
                }
                .markdownMargin(top: .em(0.4), bottom: .em(0.75))
        }
        .table { configuration in
            configuration.label
                .markdownTableBorderStyle(
                    .init(.horizontalBorders, color: TigeroseTheme.hairline, strokeStyle: .init(lineWidth: 1))
                )
                .markdownMargin(top: .em(0.6), bottom: .em(0.9))
                .clipShape(RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
                .overlay(
                    RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                        .stroke(TigeroseTheme.hairline, lineWidth: 1)
                )
        }
        .tableCell { configuration in
            configuration.label
                .markdownTextStyle {
                    FontSize(14)
                    if configuration.row == 0 {
                        FontWeight(.semibold)
                        ForegroundColor(TigeroseTheme.ink)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(configuration.row == 0 ? TigeroseTheme.surfaceCard : Color.clear)
        }
        .thematicBreak {
            Divider()
                .overlay(TigeroseTheme.hairline)
                .markdownMargin(top: 14, bottom: 14)
        }
        .heading1 { configuration in
            configuration.label
                .markdownMargin(top: .em(0.9), bottom: .em(0.5))
                .markdownTextStyle {
                    FontWeight(.bold)
                    FontSize(20)
                    ForegroundColor(TigeroseTheme.ink)
                }
        }
        .heading2 { configuration in
            configuration.label
                .markdownMargin(top: .em(0.9), bottom: .em(0.5))
                .markdownTextStyle {
                    FontWeight(.semibold)
                    FontSize(18)
                    ForegroundColor(TigeroseTheme.ink)
                }
        }
        .heading3 { configuration in
            configuration.label
                .markdownMargin(top: .em(0.75), bottom: .em(0.4))
                .markdownTextStyle {
                    FontWeight(.semibold)
                    FontSize(16)
                    ForegroundColor(TigeroseTheme.ink)
                }
        }

    /// Normalize chat model output for GFM (HTML breaks, soft line breaks).
    static func prepareChatMarkdown(_ raw: String) -> String {
        var text = raw.replacingOccurrences(of: "\r\n", with: "\n")
        // Common model/HTML leftovers → real newlines
        let brPatterns = ["<br/>", "<br />", "<br>", "</br>", "<BR/>", "<BR>", "</BR>"]
        for p in brPatterns {
            text = text.replacingOccurrences(of: p, with: "\n")
        }
        // Soft-break single newlines (MarkdownUI / GFM treat lone \n as space otherwise)
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        guard lines.count > 1 else { return text }

        var out: [String] = []
        out.reserveCapacity(lines.count)
        for (i, line) in lines.enumerated() {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let nextEmpty = i + 1 < lines.count && lines[i + 1].trimmingCharacters(in: .whitespaces).isEmpty
            let isLast = i == lines.count - 1
            // Don't mangle table / list / heading / fence lines
            let structural =
                trimmed.hasPrefix("|")
                || trimmed.hasPrefix("#")
                || trimmed.hasPrefix("- ")
                || trimmed.hasPrefix("* ")
                || trimmed.hasPrefix("> ")
                || trimmed.hasPrefix("```")
                || trimmed.range(of: #"^\d+\. "#, options: .regularExpression) != nil
            if line.isEmpty || isLast || nextEmpty || structural || line.hasSuffix("  ") || line.hasSuffix("\\") {
                out.append(line)
            } else {
                out.append(line + "  ")
            }
        }
        return out.joined(separator: "\n")
    }
}

private struct SelectablePlainParagraph: NSViewRepresentable {
    let source: String

    func makeNSView(context: Context) -> PlainParagraphTextView {
        PlainParagraphTextView()
    }

    func updateNSView(_ view: PlainParagraphTextView, context: Context) {
        view.setText(source)
    }
}

private final class PlainParagraphTextView: NSTextView {
    private var source = ""
    private var measuredWidth: CGFloat = 0

    init() {
        let storage = NSTextStorage()
        let layoutManager = NSLayoutManager()
        let container = NSTextContainer(containerSize: NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude))
        storage.addLayoutManager(layoutManager)
        layoutManager.addTextContainer(container)
        super.init(frame: .zero, textContainer: container)

        isEditable = false
        isSelectable = true
        isRichText = true
        drawsBackground = false
        allowsUndo = false
        isVerticallyResizable = true
        isHorizontallyResizable = false
        textContainerInset = NSSize(width: 0, height: 2)
        textContainer?.lineFragmentPadding = 0
        textContainer?.widthTracksTextView = true
        textContainer?.heightTracksTextView = false
        setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        setContentHuggingPriority(.defaultHigh, for: .vertical)
        focusRingType = .none
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError()
    }

    func setText(_ source: String) {
        guard source != self.source else { return }
        self.source = source
        let output = NSMutableAttributedString(string: source)
        let fullRange = NSRange(location: 0, length: output.length)
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineSpacing = 4
        paragraph.paragraphSpacing = 8
        output.addAttributes([
            .font: NSFont.systemFont(ofSize: 16),
            .foregroundColor: NSColor.labelColor,
            .paragraphStyle: paragraph,
        ], range: fullRange)
        textStorage?.setAttributedString(output)
        setSelectedRange(NSRange(location: 0, length: 0))
        invalidateIntrinsicContentSize()
        needsLayout = true
    }

    override func setFrameSize(_ newSize: NSSize) {
        let widthChanged = abs(newSize.width - measuredWidth) > 0.5
        super.setFrameSize(newSize)
        guard widthChanged else { return }
        measuredWidth = newSize.width
        textContainer?.containerSize = NSSize(width: max(1, newSize.width), height: .greatestFiniteMagnitude)
        invalidateIntrinsicContentSize()
        needsLayout = true
    }

    override var intrinsicContentSize: NSSize {
        guard let layoutManager, let textContainer else {
            return NSSize(width: NSView.noIntrinsicMetric, height: 18)
        }
        textContainer.containerSize = NSSize(width: max(1, bounds.width), height: .greatestFiniteMagnitude)
        layoutManager.ensureLayout(for: textContainer)
        let used = layoutManager.usedRect(for: textContainer)
        return NSSize(width: NSView.noIntrinsicMetric, height: max(18, ceil(used.height + textContainerInset.height * 2)) )
    }
}

enum ChatMessageTimestamp {
    private static let formatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter
    }()

    static func format(_ timestamp: Double) -> String {
        formatter.string(from: Date(timeIntervalSince1970: timestamp))
    }
}

struct MessageBubble: View {
    let content: String
    var isUser: Bool
    var renderMarkdown: Bool
    var themeHex: String = "#5db8a6"
    /// Cap = chat column × 70%.
    var maxWidth: CGFloat

    private let hPad: CGFloat = 14
    private let vPad: CGFloat = 12
    private let fontSize: CGFloat = 16

    init(
        content: String,
        isUser: Bool,
        renderMarkdown: Bool,
        themeHex: String = "#5db8a6",
        maxWidth: CGFloat = 420
    ) {
        self.content = content
        self.isUser = isUser
        self.renderMarkdown = renderMarkdown
        self.themeHex = themeHex
        self.maxWidth = maxWidth
    }

    /// Trim trailing blank lines so bubbles don't grow empty tails.
    private var displayContent: String {
        var t = content.replacingOccurrences(of: "\r\n", with: "\n")
        while t.hasSuffix("\n") { t.removeLast() }
        return t
    }

    private var textMax: CGFloat {
        let cap = max(maxWidth, 160)
        return max(cap - hPad * 2, 80)
    }

    var body: some View {
        Group {
            if renderMarkdown && !isUser {
                // MarkdownUI needs a real width budget (tables/code); don't shrink-wrap raw source.
                MarkdownText(source: displayContent)
                    .frame(width: textMax, alignment: .leading)
            } else {
                Text(displayContent)
                    .font(TigeroseTheme.bodyMd)
                    .foregroundStyle(TigeroseTheme.ink)
                    .textSelection(.enabled)
                    .multilineTextAlignment(.leading)
                    .lineLimit(nil)
                    .frame(width: measuredPlainWidth, alignment: .leading)
            }
        }
        .padding(.horizontal, isUser ? hPad : 0)
        .padding(.vertical, isUser ? vPad : 4)
        .background {
            if isUser {
                RoundedRectangle(cornerRadius: TigeroseTheme.radiusLg, style: .continuous)
                    .fill(TigeroseTheme.bubbleFill(hex: themeHex, isUser: true))
            }
        }
        .overlay {
            if isUser {
                RoundedRectangle(cornerRadius: TigeroseTheme.radiusLg, style: .continuous)
                    .stroke(TigeroseTheme.bubbleStroke(hex: themeHex, isUser: true), lineWidth: 1)
            }
        }
    }

    /// Intrinsic width for plain (user) bubbles.
    private var measuredPlainWidth: CGFloat {
        let font = NSFont.systemFont(ofSize: fontSize)
        let measureText = displayContent.isEmpty ? " " : displayContent
        let rect = (measureText as NSString).boundingRect(
            with: CGSize(width: textMax, height: CGFloat.greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: [.font: font]
        )
        return min(max(ceil(rect.width) + 1, 24), textMax)
    }
}

// MARK: - Measure chat column width (for 70% bubble cap)

private struct ChatColumnWidthKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

extension View {
    func reportChatColumnWidth() -> some View {
        background(
            GeometryReader { geo in
                Color.clear.preference(key: ChatColumnWidthKey.self, value: geo.size.width)
            }
        )
    }

    func onChatColumnWidthChange(_ handler: @escaping (CGFloat) -> Void) -> some View {
        onPreferenceChange(ChatColumnWidthKey.self, perform: handler)
    }
}
