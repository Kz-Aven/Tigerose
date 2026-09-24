import SwiftUI

/// Case-insensitive search highlight for memory list bodies.
struct HighlightedText: View {
    let text: String
    let query: String
    var font: Font = TigeroseTheme.bodySm
    var foreground: Color = TigeroseTheme.ink
    var highlightBackground: Color = TigeroseTheme.primary.opacity(0.22)

    var body: some View {
        Text(buildAttributed())
            .font(font)
            .foregroundStyle(foreground)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func buildAttributed() -> AttributedString {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return AttributedString(text) }

        var result = AttributedString()
        var remaining = text
        while true {
            guard let range = remaining.range(of: q, options: .caseInsensitive) else {
                if !remaining.isEmpty {
                    result.append(AttributedString(remaining))
                }
                break
            }
            let before = String(remaining[..<range.lowerBound])
            if !before.isEmpty {
                result.append(AttributedString(before))
            }
            var hit = AttributedString(String(remaining[range]))
            hit.backgroundColor = highlightBackground
            result.append(hit)
            remaining = String(remaining[range.upperBound...])
        }
        return result
    }
}
