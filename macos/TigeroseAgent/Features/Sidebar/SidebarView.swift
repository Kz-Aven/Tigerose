import SwiftUI
import AppKit

struct ContentView: View {
    @Environment(AppState.self) private var app
    @State private var showCreateAssistant = false
    @State private var showCreateGroup = false
    @State private var showSettings = false

    var body: some View {
        GeometryReader { proxy in
            NavigationSplitView {
                SidebarView(
                    onCreateAssistant: { showCreateAssistant = true },
                    onCreateGroup: { showCreateGroup = true },
                    onOpenSettings: { showSettings = true }
                )
                .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 320)
                .background(TigeroseTheme.surfaceSoft)
                .toolbar(removing: .sidebarToggle)
            } detail: {
                ZStack {
                    TigeroseTheme.canvas.ignoresSafeArea()
                    switch app.contentSurface {
                    case .empty:
                        emptyState
                    case .assistant(let id):
                        AssistantDMView(templateId: id)
                            .id(id)
                    case .group(let id):
                        GroupFeedView(groupId: id)
                            .id(id)
                    case .tasks:
                        emptyState
                    }
                    if app.surface == .tasks {
                        MeshTasksPage()
                            .background(TigeroseTheme.canvas)
                    }
                }
                // Drop the detail title bar ("Tigerose") so chat header sits at the top.
                .navigationTitle("")
                .modifier(HideWindowTitleModifier())
                .toolbarBackground(.hidden, for: .windowToolbar)
            }
            .navigationSplitViewStyle(.balanced)
            .background(TigeroseTheme.canvas)
            .sheet(isPresented: $showCreateAssistant) {
                CreateAssistantSheet()
            }
            .sheet(isPresented: $showCreateGroup) {
                CreateGroupSheet()
            }
            .sheet(isPresented: $showSettings) {
                SettingsSheet(hostSize: proxy.size)
            }
            .onReceive(NotificationCenter.default.publisher(for: .tigeroseNewAssistant)) { _ in
                showCreateAssistant = true
            }
            .onReceive(NotificationCenter.default.publisher(for: .tigeroseNewGroup)) { _ in
                showCreateGroup = true
            }
            .onReceive(NotificationCenter.default.publisher(for: .tigeroseOpenSettings)) { _ in
                showSettings = true
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: TigeroseTheme.spaceMd) {
            Image(systemName: "rectangle.3.group.bubble.left")
                .font(.system(size: 34, weight: .regular))
                .foregroundStyle(TigeroseTheme.brandRose)
            Text("选择助理或项目群")
                .font(TigeroseTheme.titleLg)
                .foregroundStyle(TigeroseTheme.ink)
            Text("从侧栏继续已有对话，或创建一个新的工作空间。")
                .font(TigeroseTheme.bodySm)
                .foregroundStyle(TigeroseTheme.muted)
            HStack(spacing: TigeroseTheme.spaceSm) {
                Button("新建助理") { showCreateAssistant = true }
                    .buttonStyle(TigerosePrimaryButtonStyle())
                Button("新建项目群") { showCreateGroup = true }
                    .buttonStyle(TigeroseSecondaryButtonStyle())
            }
        }
        .frame(maxWidth: 420)
        .multilineTextAlignment(.center)
    }
}

struct SidebarView: View {
    @Environment(AppState.self) private var app
    var onCreateAssistant: () -> Void
    var onCreateGroup: () -> Void
    var onOpenSettings: () -> Void

    /// Matches horizontal inset of the selected list row.
    private let rowInset: CGFloat = 12
    private let cardPad: CGFloat = 8
    private let avatarWithSubtitle: CGFloat = 32
    private let avatarTitleOnly: CGFloat = 24

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: TigeroseTheme.spaceLg) {
                    sectionBlock(title: "助理", onAdd: onCreateAssistant) {
                        if app.assistants.isEmpty {
                            emptySectionRow("暂无助理")
                        } else {
                            ForEach(app.assistants) { a in
                                sidebarRow(
                                    title: a.name,
                                    subtitle: sidebarPreview(a.last_message_preview) ?? "暂无对话",
                                    colorHex: a.theme_color,
                                    avatarID: a.avatar_id,
                                    square: false,
                                    members: [],
                                    selected: app.surface == .assistant(a.template_id),
                                    busy: app.busyKey == "assistant:\(a.template_id)",
                                    unreadCount: a.unread_count ?? 0
                                ) {
                                    app.surface = .assistant(a.template_id)
                                }
                            }
                        }
                    }

                    sectionBlock(title: "项目群", onAdd: onCreateGroup) {
                        if app.groups.isEmpty {
                            emptySectionRow("暂无项目群")
                        } else {
                            ForEach(app.groups) { g in
                                sidebarRow(
                                    title: g.name,
                                    subtitle: sidebarPreview(g.last_message_preview)
                                        ?? g.member_count.map { "\($0) 成员" },
                                    colorHex: "#5B8DEF",
                                    avatarID: "",
                                    square: true,
                                    members: g.members ?? [],
                                    selected: app.surface == .group(g.group_id),
                                    busy: app.busyKey == "group:\(g.group_id)"
                                ) {
                                    app.surface = .group(g.group_id)
                                }
                            }
                        }
                    }
                }
                .padding(.top, TigeroseTheme.spaceSm)
                .padding(.bottom, TigeroseTheme.spaceMd)
            }

            brandBar
        }
        .background(TigeroseTheme.surfaceSoft)
        .task {
            try? await app.refreshLists()
        }
    }

    private func sidebarPreview(_ raw: String?) -> String? {
        let t = (raw ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }

    private var brandBar: some View {
        GeometryReader { proxy in
            HStack(spacing: 8) {
                BrandLogo(size: 24, markOnly: true)
                if proxy.size.width >= 250 {
                    Text("Tigerose")
                        .font(TigeroseTheme.bodySm.weight(.semibold))
                        .foregroundStyle(TigeroseTheme.ink)
                        .lineLimit(1)
                }
                Spacer(minLength: 4)
                Button { app.openMeshTasks() } label: {
                    Image(systemName: "checklist")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundStyle(TigeroseTheme.primary)
                        .frame(width: 28, height: 28)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("全局任务")
                Button(action: onOpenSettings) {
                    Image(systemName: "gearshape")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundStyle(TigeroseTheme.muted)
                        .frame(width: 28, height: 28)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("设置")
            }
        }
        .frame(height: 28)
        .padding(.horizontal, rowInset)
        .padding(.vertical, TigeroseTheme.spaceSm)
        .background(TigeroseTheme.surfaceSoft)
        .overlay(alignment: .top) {
            Rectangle().fill(TigeroseTheme.hairline).frame(height: 1)
        }
    }

    private func sectionBlock<Content: View>(
        title: String,
        onAdd: @escaping () -> Void,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(title.uppercased())
                    .font(TigeroseTheme.caption)
                    .tracking(1.2)
                    .foregroundStyle(TigeroseTheme.muted)
                Spacer(minLength: 0)
                Button(action: onAdd) {
                    Image(systemName: "plus")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(TigeroseTheme.primary)
                        .frame(width: 22, height: 22)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("添加\(title)")
            }
            // Align + with the trailing edge of selected card chrome.
            .padding(.horizontal, rowInset - 4 + cardPad)

            VStack(spacing: 4) {
                content()
            }
            .padding(.horizontal, rowInset - 4)
        }
    }

    private func sidebarRow(
        title: String,
        subtitle: String?,
        colorHex: String,
        avatarID: String,
        square: Bool,
        members: [GroupMember],
        selected: Bool,
        busy: Bool,
        unreadCount: Int = 0,
        action: @escaping () -> Void
    ) -> some View {
        let hasSubtitle = !(subtitle ?? "").isEmpty
        let avatarSize = hasSubtitle ? avatarWithSubtitle : avatarTitleOnly
        return Button(action: action) {
            HStack(alignment: .top, spacing: 10) {
                if square {
                    GroupAvatarView(
                        members: members,
                        fallbackName: title,
                        fallbackColor: colorHex,
                        busy: busy,
                        size: avatarSize
                    )
                } else {
                    AvatarView(
                        name: title,
                        colorHex: colorHex,
                        avatarID: avatarID,
                        busy: busy,
                        size: avatarSize
                    )
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(TigeroseTheme.titleSm)
                        .foregroundStyle(TigeroseTheme.ink)
                        .lineLimit(1)
                        .hoverFullText(title)
                    if let subtitle, !subtitle.isEmpty {
                        Text(subtitle)
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(TigeroseTheme.muted)
                            .lineLimit(1)
                            .hoverFullText(subtitle)
                    }
                }
                .frame(minHeight: avatarSize, alignment: .topLeading)
                Spacer(minLength: 0)
                if unreadCount > 0 {
                    VStack(spacing: 0) {
                        Spacer(minLength: 0)
                        UnreadBadge(count: unreadCount)
                        Spacer(minLength: 0)
                    }
                }
            }
            .padding(.horizontal, cardPad)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd, style: .continuous)
                    .fill(selected ? TigeroseTheme.surfaceCreamStrong : Color.clear)
            )
            .contentShape(RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd, style: .continuous))
        }
        .buttonStyle(.plain)
    }

    private func emptySectionRow(_ title: String) -> some View {
        Text(title)
            .font(TigeroseTheme.caption)
            .foregroundStyle(TigeroseTheme.muted)
            .padding(.horizontal, cardPad)
            .padding(.vertical, 6)
    }
}

/// Hides the NavigationSplitView detail window title on macOS 15+.
private struct HideWindowTitleModifier: ViewModifier {
    func body(content: Content) -> some View {
        if #available(macOS 15.0, *) {
            content.toolbar(removing: .title)
        } else {
            content
        }
    }
}
