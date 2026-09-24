import SwiftUI

struct SessionSwitcher: View {
    let title: String
    let canRename: Bool
    let unreadCount: Int
    let items: [AssistantSession]
    let onNew: () -> Void
    let onSelect: (String) -> Void
    let onDelete: (String) -> Void
    let onRename: (String) -> Void

    @State private var showMenu = false
    @State private var hoveringCluster = false

    private static let dateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd  HH:mm:ss"
        return f
    }()

    private var displayTitle: String {
        title.isEmpty ? "新对话" : title
    }

    var body: some View {
        // Title + chevron as one unit (centered by parent).
        // Edit sits in trailing padding so hover never drops while moving onto it.
        HStack(spacing: 3) {
            Button {
                showMenu.toggle()
            } label: {
                HStack(spacing: 3) {
                    Text(displayTitle)
                        .font(TigeroseTheme.bodySm)
                        .foregroundStyle(TigeroseTheme.ink)
                        .lineLimit(1)
                    UnreadBadge(count: unreadCount)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(TigeroseTheme.muted)
                }
                .padding(.vertical, 6)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(displayTitle)
            .popover(isPresented: $showMenu, arrowEdge: .bottom) {
                sessionMenu
                    .frame(width: 320)
                    .padding(8)
            }

            if canRename {
                Button {
                    onRename(displayTitle)
                } label: {
                    Image(systemName: "square.and.pencil")
                        .font(.system(size: 12))
                        .foregroundStyle(TigeroseTheme.muted)
                        .frame(width: 24, height: 24)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("编辑对话名称")
                .opacity(hoveringCluster || showMenu ? 1 : 0)
                .allowsHitTesting(hoveringCluster || showMenu)
            }
        }
        .padding(.horizontal, 6)
        .contentShape(Rectangle())
        .onHover { hoveringCluster = $0 }
        .animation(.easeOut(duration: 0.12), value: hoveringCluster)
    }

    private var sessionMenu: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                showMenu = false
                onNew()
            } label: {
                HStack {
                    Image(systemName: "plus")
                    Text("新对话")
                    Spacer()
                }
                .font(TigeroseTheme.bodySm)
                .foregroundStyle(TigeroseTheme.ink)
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .background(TigeroseTheme.surfaceSoft, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm))

            if !items.isEmpty {
                Divider().padding(.vertical, 4)
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(items) { item in
                            sessionRow(item)
                        }
                    }
                }
                .frame(maxHeight: 280)
            }
        }
    }

    private func sessionRow(_ item: AssistantSession) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Button {
                showMenu = false
                onSelect(item.session_id)
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 4) {
                        Text(item.title.isEmpty ? "新对话" : item.title)
                            .font(TigeroseTheme.bodySm)
                            .foregroundStyle(TigeroseTheme.ink)
                            .lineLimit(1)
                        UnreadBadge(count: item.unread_count ?? 0)
                    }
                    Text(formatDate(item.last_message_at))
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.muted)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(item.title)
            .frame(maxWidth: .infinity, alignment: .leading)

            Button {
                showMenu = false
                onDelete(item.session_id)
            } label: {
                Image(systemName: "trash")
                    .font(.system(size: 12))
                    .foregroundStyle(TigeroseTheme.muted)
                    .frame(width: 28, height: 28)
            }
            .buttonStyle(.plain)
            .help("删除该对话")
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(TigeroseTheme.canvas, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm))
    }

    private func formatDate(_ ts: Double?) -> String {
        guard let ts, ts > 0 else { return "" }
        return Self.dateFormatter.string(from: Date(timeIntervalSince1970: ts))
    }
}

struct UnreadBadge: View {
    let count: Int

    var body: some View {
        if count > 0 {
            Text(count > 99 ? "99+" : "\(count)")
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(.white)
                .frame(minWidth: 17, minHeight: 17)
                .padding(.horizontal, count > 9 ? 3 : 0)
                .background(TigeroseTheme.error, in: Capsule())
        }
    }
}
