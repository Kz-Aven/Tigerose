import SwiftUI

/// Read-only AskUserQuestion snapshot embedded in chat / feed history.
struct AskUserTranscriptCard: View {
    let snapshot: AskUserSnapshot
    var maxWidth: CGFloat = 420

    private var statusLabel: String {
        if snapshot.isCancelled { return "已取消" }
        if snapshot.isAnswered { return "已回答" }
        return "等待回答"
    }

    private var statusColor: Color {
        if snapshot.isCancelled { return TigeroseTheme.muted }
        if snapshot.isAnswered { return TigeroseTheme.primary }
        return TigeroseTheme.warning
    }

    var body: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
            HStack(spacing: TigeroseTheme.spaceXs) {
                Image(systemName: "questionmark.bubble.fill")
                    .foregroundStyle(TigeroseTheme.primary)
                Text("需要你的回答")
                    .font(TigeroseTheme.titleSm)
                    .foregroundStyle(TigeroseTheme.ink)
                Spacer()
                Text(statusLabel)
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(statusColor)
            }

            ForEach(snapshot.questions) { question in
                questionBlock(question)
            }

            if snapshot.isPending {
                Text("请在下方卡片中选择并提交")
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(TigeroseTheme.muted)
            }
        }
        .padding(TigeroseTheme.spaceMd)
        .frame(maxWidth: maxWidth, alignment: .leading)
        .background(TigeroseTheme.surfaceSoft)
        .tigeroseHairline(radius: TigeroseTheme.radiusLg)
    }

    @ViewBuilder
    private func questionBlock(_ question: AskUserQuestion) -> some View {
        let answer = snapshot.answer(for: question.id)
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceXs) {
            Text(question.header)
                .font(TigeroseTheme.caption)
                .foregroundStyle(TigeroseTheme.primary)
            Text(question.question)
                .font(TigeroseTheme.bodyMd)
                .foregroundStyle(TigeroseTheme.ink)
                .fixedSize(horizontal: false, vertical: true)

            ForEach(question.options, id: \.label) { option in
                let selected = answer?.selected_labels.contains(option.label) ?? false
                HStack(alignment: .top, spacing: TigeroseTheme.spaceXs) {
                    Image(systemName: selected
                          ? (question.multi_select ? "checkmark.square.fill" : "largecircle.fill.circle")
                          : (question.multi_select ? "square" : "circle"))
                        .foregroundStyle(selected ? TigeroseTheme.primary : TigeroseTheme.muted)
                        .frame(width: 18)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(option.label)
                            .font(TigeroseTheme.bodySm)
                            .foregroundStyle(TigeroseTheme.ink)
                        if !option.description.isEmpty {
                            Text(option.description)
                                .font(TigeroseTheme.caption)
                                .foregroundStyle(TigeroseTheme.muted)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Spacer(minLength: 0)
                }
                .opacity(snapshot.isAnswered && !selected ? 0.45 : 1)
            }

            if let other = answer?.other_text.trimmingCharacters(in: .whitespacesAndNewlines),
               !other.isEmpty
            {
                Text("其他：\(other)")
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.ink)
            }
        }
        .padding(TigeroseTheme.spaceSm)
        .background(TigeroseTheme.canvas, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
        .tigeroseHairline(radius: TigeroseTheme.radiusMd)
    }
}
