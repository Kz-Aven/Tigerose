import SwiftUI

struct RememberProposalData: Equatable {
    var body: String
    var sourceGroups: [String]
}

enum RememberProposalParser {
    static func parse(meta: [String: JSONValue]?) -> RememberProposalData? {
        guard let meta else { return nil }
        if meta["remember_ignored"]?.boolValue == true { return nil }
        if meta["remember_confirmed"]?.boolValue == true { return nil }

        if let obj = meta["remember_proposal"]?.objectValue {
            let body = obj["body"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            guard !body.isEmpty else { return nil }
            let groups = obj["source_groups"]?.arrayValue?.compactMap(\.stringValue) ?? []
            return RememberProposalData(body: body, sourceGroups: groups)
        }
        if let s = meta["remember_proposal"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines),
           !s.isEmpty
        {
            return RememberProposalData(body: s, sourceGroups: [])
        }
        if meta["type"]?.stringValue == "remember_proposal",
           let body = meta["body"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines),
           !body.isEmpty
        {
            return RememberProposalData(body: body, sourceGroups: [])
        }
        return nil
    }

    static func isConfirmed(meta: [String: JSONValue]?) -> Bool {
        meta?["remember_confirmed"]?.boolValue == true
    }
}

struct RememberProposalCard: View {
    let proposal: RememberProposalData
    let maxWidth: CGFloat
    var confirmed: Bool = false
    let onIgnore: () -> Void
    let onConfirm: () -> Void

    var body: some View {
        if confirmed {
            Text("已写入记忆")
                .font(TigeroseTheme.caption)
                .foregroundStyle(TigeroseTheme.muted)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                Text("建议写入记忆")
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(TigeroseTheme.muted)
                Text(proposal.body)
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) {
                    Button("忽略", action: onIgnore)
                        .buttonStyle(TigeroseSecondaryButtonStyle())
                    Button("写入记忆", action: onConfirm)
                        .buttonStyle(TigerosePrimaryButtonStyle())
                }
            }
            .padding(10)
            .frame(maxWidth: maxWidth, alignment: .leading)
            .background(TigeroseTheme.surfaceSoft, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
            .overlay(
                RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                    .stroke(TigeroseTheme.hairline, lineWidth: 1)
            )
        }
    }
}
