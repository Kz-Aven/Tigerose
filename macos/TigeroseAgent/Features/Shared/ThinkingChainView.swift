import AppKit
import SwiftUI

/// Cursor-like collapsible tool/hook chain.
struct ThinkingChainView: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var titleRunning: String = "深度思考中..."
    var steps: [ThinkingStep]
    var durationMs: Int
    var isRunning: Bool
    var budgetSummary: String? = nil
    var runOutcome: String? = nil
    var executionStatus: String? = nil
    var evidenceStatus: String? = nil
    var resultVerdict: String? = nil
    var maxWidth: CGFloat = TigeroseTheme.bubbleMaxWidth
    @State private var expanded = false

    private var headerTitle: String {
        if isRunning {
            if let budgetSummary, !budgetSummary.isEmpty {
                return "\(titleRunning) · \(budgetSummary)"
            }
            return titleRunning
        }
        let secs = max(1, Int((Double(durationMs) / 1000.0).rounded()))
        let status = [runOutcomeLabel, evidenceStatusLabel, resultVerdictLabel]
            .compactMap { $0 }
            .joined(separator: " · ")
        if !status.isEmpty {
            if let budgetSummary, !budgetSummary.isEmpty {
                return "\(status) · \(budgetSummary) · \(secs)s"
            }
            return "\(status) · \(secs)s"
        }
        if let budgetSummary, !budgetSummary.isEmpty {
            return "\(budgetSummary) · \(secs)s"
        }
        return "已工作 \(secs)s"
    }

    private var runOutcomeLabel: String? {
        switch executionStatus ?? runOutcome {
        case "completed", "accepted": return "已完成"
        case "partial", "partially_completed": return "部分完成"
        case "waiting_for_user": return "等待回答"
        case "blocked_runtime": return "运行时阻断"
        case "blocked_user_action": return "需重新授权"
        case "budget_exhausted": return "已达预算"
        case "failed": return "执行失败"
        case "cancelled": return "已取消"
        default: return nil
        }
    }

    private var evidenceStatusLabel: String? {
        switch evidenceStatus {
        case "verified": return "交付已验证"
        case "observed": return "已生成交付物"
        case "unverified": return "交付未验证"
        default: return nil
        }
    }

    private var resultVerdictLabel: String? {
        switch resultVerdict {
        case "pass": return "结论：通过"
        case "fail": return "结论：未通过"
        case "inconclusive": return "结论：待定"
        default: return nil
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                if reduceMotion {
                    expanded.toggle()
                } else {
                    withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() }
                }
            } label: {
                HStack(spacing: 6) {
                    if isRunning {
                        ProgressView()
                            .controlSize(.mini)
                    }
                    Text(headerTitle)
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.muted)
                    Spacer(minLength: 4)
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(TigeroseTheme.mutedSoft)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if expanded {
                ScrollView(.vertical, showsIndicators: true) {
                    SelectableThinkingLog(steps: steps)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 288)
                .padding(.horizontal, 10)
                .padding(.bottom, 8)
            }
        }
        .frame(maxWidth: maxWidth, alignment: .leading)
        .background(TigeroseTheme.surfaceSoft, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm))
        .overlay(
            RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm)
                .stroke(TigeroseTheme.hairline, lineWidth: 1)
        )
    }

}

/// One native text surface for the whole log. NSTextView provides continuous
/// multi-line selection, Command-C, and reliable per-row system tooltips.
private struct SelectableThinkingLog: NSViewRepresentable {
    var steps: [ThinkingStep]

    func makeNSView(context: Context) -> ThinkingLogTextView {
        ThinkingLogTextView()
    }

    func updateNSView(_ view: ThinkingLogTextView, context: Context) {
        view.setSteps(steps)
    }
}

private final class ThinkingLogTextView: NSTextView {
    private struct Entry {
        var range: NSRange
        var tooltip: String
    }

    private var entries: [Entry] = []
    private var tooltipTags: [NSView.ToolTipTag] = []
    private let tooltipOwner = ThinkingLogTooltipOwner()
    private var contentSignature = 0
    private var measuredWidth: CGFloat = 0

    init() {
        let storage = NSTextStorage()
        let layoutManager = NSLayoutManager()
        let container = NSTextContainer(
            containerSize: NSSize(
                width: 0,
                height: CGFloat.greatestFiniteMagnitude
            )
        )
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
        textContainer?.containerSize = NSSize(
            width: 0,
            height: CGFloat.greatestFiniteMagnitude
        )
        setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        setContentHuggingPriority(.defaultHigh, for: .vertical)
        focusRingType = .none
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError()
    }

    func setSteps(_ steps: [ThinkingStep]) {
        let signature = steps.hashValue
        guard signature != contentSignature else { return }
        contentSignature = signature

        let output = NSMutableAttributedString()
        var nextEntries: [Entry] = []
        let font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byTruncatingTail
        paragraph.paragraphSpacing = 4

        let base: [NSAttributedString.Key: Any] = [
            .font: font,
            .paragraphStyle: paragraph,
        ]

        for (index, step) in steps.enumerated() {
            let start = output.length
            let label = step.label.padding(
                toLength: 16,
                withPad: " ",
                startingAt: 0
            )
            output.append(
                NSAttributedString(
                    string: label,
                    attributes: base.merging([
                        .foregroundColor: NSColor.tertiaryLabelColor,
                    ]) { _, new in new }
                )
            )
            output.append(
                NSAttributedString(
                    string: step.name,
                    attributes: base.merging([
                        .foregroundColor: NSColor.labelColor,
                    ]) { _, new in new }
                )
            )
            if !step.detail.isEmpty {
                let detail = step.detail
                    .replacingOccurrences(of: "\r\n", with: " ↵ ")
                    .replacingOccurrences(of: "\n", with: " ↵ ")
                    .replacingOccurrences(of: "\r", with: " ↵ ")
                output.append(
                    NSAttributedString(
                        string: "  \(detail)",
                        attributes: base.merging([
                            .foregroundColor: NSColor.secondaryLabelColor,
                        ]) { _, new in new }
                    )
                )
            }

            let range = NSRange(location: start, length: output.length - start)
            nextEntries.append(
                Entry(range: range, tooltip: Self.fullText(step))
            )
            if index < steps.count - 1 {
                output.append(
                    NSAttributedString(string: "\n", attributes: base)
                )
            }
        }

        entries = nextEntries
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
        textContainer?.containerSize = NSSize(
            width: max(1, newSize.width),
            height: CGFloat.greatestFiniteMagnitude
        )
        invalidateIntrinsicContentSize()
        needsLayout = true
    }

    override var intrinsicContentSize: NSSize {
        guard let layoutManager, let textContainer else {
            return NSSize(width: NSView.noIntrinsicMetric, height: 18)
        }
        let width = max(1, bounds.width)
        textContainer.containerSize = NSSize(
            width: width,
            height: CGFloat.greatestFiniteMagnitude
        )
        layoutManager.ensureLayout(for: textContainer)
        let used = layoutManager.usedRect(for: textContainer)
        return NSSize(
            width: NSView.noIntrinsicMetric,
            height: max(18, ceil(used.height + textContainerInset.height * 2))
        )
    }

    override func layout() {
        super.layout()
        rebuildTooltips()
    }

    private func rebuildTooltips() {
        tooltipTags.forEach(removeToolTip)
        tooltipTags.removeAll(keepingCapacity: true)
        tooltipOwner.textByTag.removeAll(keepingCapacity: true)

        guard let layoutManager, let textContainer, bounds.width > 0 else {
            return
        }
        layoutManager.ensureLayout(for: textContainer)
        let origin = textContainerOrigin

        for entry in entries where entry.range.length > 0 {
            let glyphRange = layoutManager.glyphRange(
                forCharacterRange: entry.range,
                actualCharacterRange: nil
            )
            var rect = layoutManager.boundingRect(
                forGlyphRange: glyphRange,
                in: textContainer
            )
            rect.origin.x = 0
            rect.origin.y += origin.y
            rect.size.width = bounds.width
            rect.size.height = max(rect.height, 16)
            let tag = addToolTip(rect, owner: tooltipOwner, userData: nil)
            tooltipTags.append(tag)
            tooltipOwner.textByTag[tag] = entry.tooltip
        }
    }

    private static func fullText(_ step: ThinkingStep) -> String {
        if step.detail.isEmpty {
            return "\(step.label) \(step.name)"
        }
        return "\(step.label) \(step.name)\n\(step.detail)"
    }
}

private final class ThinkingLogTooltipOwner: NSObject {
    var textByTag: [NSView.ToolTipTag: String] = [:]

    @objc(view:stringForToolTip:point:userData:)
    func view(
        _ view: NSView,
        stringForToolTip tag: NSView.ToolTipTag,
        point: NSPoint,
        userData data: UnsafeMutableRawPointer?
    ) -> String {
        textByTag[tag] ?? ""
    }
}
