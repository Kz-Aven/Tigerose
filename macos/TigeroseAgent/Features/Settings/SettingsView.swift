import SwiftUI
import AppKit

struct SettingsView: View {
    @Environment(AppState.self) private var app
    @State private var tab = 0
    @State private var profiles: [ManagedModelProfile] = []
    @State private var defaultId = ""
    @State private var error = ""
    @State private var mode: Mode = .list
    @State private var editingId: String?
    @State private var formModel = ""
    @State private var formBase = ""
    @State private var formKey = ""
    @State private var saving = false
    @State private var goalEvaluator: GoalEvaluatorModelSettings?
    @State private var goalEvaluatorSelection = ""
    @State private var savedGoalEvaluatorSelection = ""
    @State private var goalEvaluatorSaving = false
    @State private var goalEvaluatorNotice = ""

    @State private var tavily = TavilyWebStatus(configured: false)
    @State private var tavilyKey = ""
    @State private var tavilySaving = false
    @State private var tavilyNotice = ""
    @State private var dingtalk = DingTalkConnectorStatus(installed: false, connected: false, state: "not_installed")
    @State private var dingtalkSaving = false
    @State private var lark = ManagedConnectorStatus(connector_id: "lark", display_name: "飞书", installed: false, authenticated: false, health: "unknown")
    @State private var larkSaving = false

    enum Mode { case list, add, edit }

    var body: some View {
        HStack(spacing: 0) {
            settingsSidebar
            Rectangle().fill(TigeroseTheme.hairline).frame(width: 1)
            settingsContent
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(TigeroseTheme.canvas)
        .task { await reload() }
    }

    private var settingsSidebar: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
            settingsSection("运行") {
                settingsSidebarButton(title: "模型", systemImage: "cpu", tag: 0)
                settingsSidebarButton(title: "通用", systemImage: "gearshape", tag: 1)
            }
            settingsSection("能力") {
                settingsSidebarButton(title: "技能", systemImage: "sparkles", tag: 2)
                settingsSidebarButton(title: "插件", systemImage: "puzzlepiece.extension", tag: 3)
                settingsSidebarButton(title: "工具", systemImage: "wrench.and.screwdriver", tag: 4)
                settingsSidebarButton(title: "MCP", systemImage: "point.3.connected.trianglepath.dotted", tag: 5)
                settingsSidebarButton(title: "钩子", systemImage: "link", tag: 6)
            }
            settingsSection("连接") {
                settingsSidebarButton(title: "连接器", systemImage: "powerplug", tag: 7)
                settingsSidebarButton(title: "IM频道", systemImage: "bubble.left.and.bubble.right", tag: 8)
            }
            settingsSection("用量") {
                settingsSidebarButton(title: "统计", systemImage: "chart.bar", tag: 9)
            }
            Spacer()
        }
        .padding(TigeroseTheme.spaceMd)
        .frame(width: 196, alignment: .topLeading)
        .frame(maxHeight: .infinity, alignment: .topLeading)
        .background(TigeroseTheme.surfaceSoft)
    }

    private var settingsContent: some View {
        Group {
            if tab == 0 {
                if mode == .list {
                    modelsList
                } else {
                    modelForm
                }
            } else if tab == 1 {
                generalTab
            } else if tab == 6 {
                HooksSettingsPane(workspace: settingsWorkspace)
            } else if tab == 7 {
                connectorsTab
            } else if tab == 8 {
                ImChannelsSettingsPane()
            } else if tab == 9 {
                UsageSettingsPane()
            } else if let resourceTab = ResourceSettingsPanes.ResourceTab(rawValue: tab) {
                ResourceSettingsPanes(tab: resourceTab, workspace: settingsWorkspace)
            }
        }
        .padding(.top, TigeroseTheme.spaceMd)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(TigeroseTheme.canvas)
    }

    private func settingsSidebarButton(title: String, systemImage: String, tag: Int) -> some View {
        let selected = tab == tag
        return Button {
            selectSettingsTab(tag)
        } label: {
            HStack(spacing: 10) {
                Image(systemName: systemImage)
                    .font(.system(size: 15, weight: .semibold))
                    .frame(width: 20)
                Text(title)
                    .font(TigeroseTheme.bodySm.weight(.semibold))
                    .lineLimit(1)
                Spacer(minLength: 0)
            }
            .foregroundStyle(selected ? TigeroseTheme.ink : TigeroseTheme.muted)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                selected ? TigeroseTheme.surfaceCreamStrong : Color.clear,
                in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func settingsSection<Content: View>(
        _ title: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(TigeroseTheme.caption)
                .foregroundStyle(TigeroseTheme.muted)
                .padding(.horizontal, 12)
            content()
        }
    }

    private func selectSettingsTab(_ newTab: Int) {
        tab = newTab
        if newTab != 0 { resetForm() }
        if newTab == 1 { Task { await reloadTavily() } }
        if newTab == 7 { Task { await reloadDingTalk(); await reloadLark() } }
    }

    /// Prefer selected group workspace, else first group with a path, else default workspace pref.
    private var settingsWorkspace: String? {
        if case .group(let id) = app.surface,
           let g = app.groups.first(where: { $0.group_id == id }),
           !g.workspace_path.isEmpty {
            return g.workspace_path
        }
        if let g = app.groups.first(where: { !$0.workspace_path.isEmpty }) {
            return g.workspace_path
        }
        let pref = app.preferences.defaultWorkspacePath
        return pref.isEmpty ? nil : pref
    }
    private var modelsList: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
            HStack {
                Text("模型配置")
                    .font(TigeroseTheme.titleMd)
                    .foregroundStyle(TigeroseTheme.ink)
                Spacer()
                Button("添加模型") { startAdd() }
                    .buttonStyle(TigerosePrimaryButtonStyle())
            }
            .padding(.horizontal, TigeroseTheme.spaceLg)

            goalEvaluatorSection

            if !error.isEmpty {
                Text(error)
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.error)
                    .padding(.horizontal, TigeroseTheme.spaceLg)
            }

            if profiles.isEmpty {
                ContentUnavailableView(
                    "暂无模型",
                    systemImage: "cpu",
                    description: Text("点击「添加模型」录入。")
                )
                .foregroundStyle(TigeroseTheme.muted)
            } else {
                List {
                    ForEach(profiles) { p in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text(p.label)
                                    .font(TigeroseTheme.titleSm)
                                    .foregroundStyle(TigeroseTheme.ink)
                                if p.id == defaultId {
                                    Text("默认")
                                        .font(TigeroseTheme.caption)
                                        .foregroundStyle(TigeroseTheme.ink)
                                        .padding(.horizontal, 8)
                                        .padding(.vertical, 2)
                                        .background(TigeroseTheme.surfaceCreamStrong, in: Capsule())
                                }
                                Spacer()
                                Menu {
                                    Button("编辑") { startEdit(p) }
                                    Button("移除", role: .destructive) {
                                        Task { await remove(p.id) }
                                    }
                                } label: {
                                    Image(systemName: "ellipsis.circle")
                                        .foregroundStyle(TigeroseTheme.muted)
                                }
                                .menuStyle(.borderlessButton)
                                .help("模型操作")
                            }
                            Text("模型: \(p.id)")
                                .font(TigeroseTheme.caption)
                                .foregroundStyle(TigeroseTheme.muted)
                            Text("base_url: \(p.base_url)")
                                .font(TigeroseTheme.caption)
                                .foregroundStyle(TigeroseTheme.muted)
                            Text("api_key: \(p.has_api_key ? (p.api_key_masked.isEmpty ? "****" : p.api_key_masked) : "（未设置）")")
                                .font(TigeroseTheme.caption)
                                .foregroundStyle(TigeroseTheme.mutedSoft)
                        }
                        .padding(.vertical, 4)
                        .listRowInsets(EdgeInsets(
                            top: 8,
                            leading: TigeroseTheme.spaceLg,
                            bottom: 8,
                            trailing: TigeroseTheme.spaceLg
                        ))
                        .listRowBackground(TigeroseTheme.canvas)
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
                .background(TigeroseTheme.canvas)
            }
        }
    }

    private var goalEvaluatorSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Goal 评估模型")
                .font(TigeroseTheme.titleSm)
                .foregroundStyle(TigeroseTheme.ink)
            Text("用于独立判断 Goal 是否完成；不会复制或保存模型 API Key。")
                .font(TigeroseTheme.caption)
                .foregroundStyle(TigeroseTheme.muted)
            HStack {
                Picker("", selection: $goalEvaluatorSelection) {
                    Text("跟随当前主模型").tag("")
                    ForEach(profiles) { profile in
                        Text(profile.label).tag(profile.id)
                    }
                }
                .labelsHidden()
                .frame(width: 320, alignment: .leading)

                Spacer(minLength: TigeroseTheme.spaceMd)

                Button(goalEvaluatorSaving ? "保存中…" : "保存") {
                    Task { await saveGoalEvaluator() }
                }
                .buttonStyle(TigerosePrimaryButtonStyle(
                    disabled: goalEvaluatorSaving
                        || goalEvaluatorSelection == savedGoalEvaluatorSelection
                ))
                .disabled(
                    goalEvaluatorSaving
                        || goalEvaluatorSelection == savedGoalEvaluatorSelection
                )
            }
            .frame(maxWidth: .infinity)
            if goalEvaluator?.fallback == true {
                Text("原模型已删除，当前跟随主模型")
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(TigeroseTheme.primary)
            } else if !goalEvaluatorNotice.isEmpty {
                Text(goalEvaluatorNotice)
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(TigeroseTheme.muted)
            }
        }
        .padding(.horizontal, TigeroseTheme.spaceLg)
        .padding(.vertical, TigeroseTheme.spaceSm)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var modelForm: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
            Text(mode == .edit ? "编辑模型" : "添加模型")
                .font(TigeroseTheme.titleMd)
                .foregroundStyle(TigeroseTheme.ink)
            TextField("模型名称（id）", text: $formModel)
                .textFieldStyle(TigeroseTextFieldStyle())
                .disabled(mode == .edit)
            TextField("base_url", text: $formBase)
                .textFieldStyle(TigeroseTextFieldStyle())
            SecureField(mode == .edit ? "API Key（留空则不改）" : "API Key（可选）", text: $formKey)
                .textFieldStyle(TigeroseTextFieldStyle())
            if !error.isEmpty {
                Text(error)
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.error)
            }
            HStack {
                Button("取消") { resetForm() }
                    .buttonStyle(TigeroseSecondaryButtonStyle())
                Spacer()
                Button(mode == .edit ? "保存" : "添加") {
                    Task { await save() }
                }
                .buttonStyle(TigerosePrimaryButtonStyle(
                    disabled: saving
                        || formModel.trimmingCharacters(in: .whitespaces).isEmpty
                        || formBase.trimmingCharacters(in: .whitespaces).isEmpty
                ))
                .disabled(
                    saving
                        || formModel.trimmingCharacters(in: .whitespaces).isEmpty
                        || formBase.trimmingCharacters(in: .whitespaces).isEmpty
                )
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(TigeroseTheme.spaceLg)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(TigeroseTheme.canvas)
    }

    private var connectorsTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
                Text("连接器")
                    .font(TigeroseTheme.titleMd)
                    .foregroundStyle(TigeroseTheme.ink)
                Text("连接后，助理可按输入框中的开关使用连接器 Skills。凭据由对应 CLI 保存在 macOS Keychain。")
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.muted)

                VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Label("钉钉", systemImage: "message")
                                .font(TigeroseTheme.titleSm)
                                .foregroundStyle(TigeroseTheme.ink)
                            Text("DingTalk Workspace CLI")
                                .font(TigeroseTheme.caption)
                                .foregroundStyle(TigeroseTheme.muted)
                        }
                        Spacer()
                        Text(dingtalkLabel)
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(dingtalk.connected ? TigeroseTheme.primary : TigeroseTheme.muted)
                    }
                    if dingtalk.connected {
                        let identity = [dingtalk.account_name, dingtalk.corp_name]
                            .compactMap { $0?.isEmpty == false ? $0 : nil }
                            .joined(separator: " · ")
                        if !identity.isEmpty {
                            Text(identity)
                                .font(TigeroseTheme.bodySm)
                                .foregroundStyle(TigeroseTheme.muted)
                        }
                    } else if let message = dingtalk.message, !message.isEmpty {
                        Text(message)
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(TigeroseTheme.muted)
                    }
                    HStack {
                        if dingtalk.connected {
                            Button(dingtalkSaving ? "处理中…" : "重新授权") { Task { await connectDingTalk() } }
                                .buttonStyle(TigeroseSecondaryButtonStyle())
                            Button("断开连接", role: .destructive) { Task { await disconnectDingTalk() } }
                                .buttonStyle(TigeroseSecondaryButtonStyle())
                                .disabled(dingtalkSaving)
                        } else {
                            Button(dingtalkSaving ? "处理中…" : dingtalkActionTitle) { Task { await connectDingTalk() } }
                                .buttonStyle(TigerosePrimaryButtonStyle(disabled: dingtalkSaving))
                                .disabled(dingtalkSaving)
                        }
                        Spacer()
                    }
                }
                .padding(TigeroseTheme.spaceMd)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(TigeroseTheme.surfaceCard, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
                .overlay(RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd).stroke(TigeroseTheme.hairline, lineWidth: 1))

                VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Label("飞书", systemImage: "paperplane")
                                .font(TigeroseTheme.titleSm)
                                .foregroundStyle(TigeroseTheme.ink)
                            Text("Lark / Feishu CLI")
                                .font(TigeroseTheme.caption)
                                .foregroundStyle(TigeroseTheme.muted)
                        }
                        Spacer()
                        Text(larkLabel)
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(lark.authenticated ? TigeroseTheme.primary : TigeroseTheme.muted)
                    }
                    if lark.authenticated {
                        let identity = [lark.account_name, lark.corp_name]
                            .compactMap { $0?.isEmpty == false ? $0 : nil }
                            .joined(separator: " · ")
                        if !identity.isEmpty {
                            Text(identity)
                                .font(TigeroseTheme.bodySm)
                                .foregroundStyle(TigeroseTheme.muted)
                        }
                    } else if let message = lark.message, !message.isEmpty {
                        Text(message)
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(TigeroseTheme.muted)
                    }
                    if lark.installed {
                        let appID = lark.app_id?.isEmpty == false ? lark.app_id! : "未绑定"
                        let appName = lark.app_name?.isEmpty == false ? lark.app_name! : "未获取"
                        Text("应用 AppID：\(appID)")
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(TigeroseTheme.muted)
                            .textSelection(.enabled)
                        Text("应用名称：\(appName)")
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(TigeroseTheme.muted)
                    }
                    HStack {
                        if !lark.installed {
                            Button(larkSaving ? "处理中…" : "安装飞书 CLI") { Task { await installLark() } }
                                .buttonStyle(TigerosePrimaryButtonStyle(disabled: larkSaving))
                                .disabled(larkSaving)
                        } else if lark.authenticated {
                            Button(larkSaving ? "处理中…" : "重新授权") { Task { await reconnectLark() } }
                                .buttonStyle(TigeroseSecondaryButtonStyle())
                                .disabled(larkSaving)
                            Button("断开连接", role: .destructive) { Task { await disconnectLark() } }
                                .buttonStyle(TigeroseSecondaryButtonStyle())
                                .disabled(larkSaving)
                            Button("卸载", role: .destructive) { Task { await uninstallLark() } }
                                .buttonStyle(TigeroseSecondaryButtonStyle())
                                .disabled(larkSaving)
                        } else {
                            Button(larkSaving ? "处理中…" : "连接飞书") { Task { await connectLark() } }
                                .buttonStyle(TigerosePrimaryButtonStyle(disabled: larkSaving))
                                .disabled(larkSaving)
                            Button("卸载", role: .destructive) { Task { await uninstallLark() } }
                                .buttonStyle(TigeroseSecondaryButtonStyle())
                                .disabled(larkSaving)
                        }
                        Spacer()
                    }
                }
                .padding(TigeroseTheme.spaceMd)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(TigeroseTheme.surfaceCard, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
                .overlay(RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd).stroke(TigeroseTheme.hairline, lineWidth: 1))
            }
            .padding(TigeroseTheme.spaceLg)
        }
        .task { await reloadDingTalk(); await reloadLark() }
    }

    private var dingtalkLabel: String {
        switch dingtalk.state {
        case "connected": return "已连接"
        case "installing": return "安装中"
        case "authorizing": return "等待浏览器授权"
        case "failed": return "连接失败"
        case "auth_required": return "待授权"
        default: return "未安装"
        }
    }

    private var dingtalkActionTitle: String {
        dingtalk.installed ? "连接钉钉" : "安装并连接"
    }

    private func reloadDingTalk() async {
        guard app.backendStatus == .running else { return }
        do { dingtalk = try await app.api.getDingTalkConnector() }
        catch { self.error = error.localizedDescription }
    }

    private func connectDingTalk() async {
        dingtalkSaving = true
        defer { dingtalkSaving = false }
        do {
            dingtalk = try await app.api.connectDingTalk()
            for _ in 0..<90 where !dingtalk.connected && ["installing", "authorizing"].contains(dingtalk.state) {
                try? await Task.sleep(for: .seconds(1))
                dingtalk = try await app.api.getDingTalkConnector()
            }
        } catch { self.error = error.localizedDescription }
    }

    private func disconnectDingTalk() async {
        dingtalkSaving = true
        defer { dingtalkSaving = false }
        do { dingtalk = try await app.api.disconnectDingTalk() }
        catch { self.error = error.localizedDescription }
    }

    private var larkLabel: String {
        if lark.health == "degraded" { return lark.message ?? "处理中" }
        if lark.authenticated { return "已连接" }
        return lark.installed ? "待授权" : "未安装"
    }

    private func reloadLark() async {
        guard app.backendStatus == .running else { return }
        do { lark = try await app.api.getLarkConnector() }
        catch { self.error = error.localizedDescription }
    }

    private func installLark() async {
        larkSaving = true
        defer { larkSaving = false }
        do {
            lark = try await app.api.installLarkConnector()
            for _ in 0..<90 where lark.health == "degraded" {
                try? await Task.sleep(for: .seconds(1))
                lark = try await app.api.getLarkConnector()
            }
        } catch { self.error = error.localizedDescription }
    }

    private func connectLark() async {
        larkSaving = true
        defer { larkSaving = false }
        do {
            lark = try await app.api.connectLarkConnector()
            for _ in 0..<600 where !lark.authenticated && lark.health == "degraded" {
                try? await Task.sleep(for: .seconds(1))
                lark = try await app.api.getLarkConnector()
            }
        } catch { self.error = error.localizedDescription }
    }

    private func reconnectLark() async {
        larkSaving = true
        defer { larkSaving = false }
        do {
            lark = try await app.api.reconnectLarkConnector()
            for _ in 0..<600 where !lark.authenticated && lark.health == "degraded" {
                try? await Task.sleep(for: .seconds(1))
                lark = try await app.api.getLarkConnector()
            }
        } catch { self.error = error.localizedDescription }
    }

    private func disconnectLark() async {
        larkSaving = true
        defer { larkSaving = false }
        do { lark = try await app.api.disconnectLarkConnector() }
        catch { self.error = error.localizedDescription }
    }

    private func uninstallLark() async {
        larkSaving = true
        defer { larkSaving = false }
        do { lark = try await app.api.uninstallLarkConnector() }
        catch { self.error = error.localizedDescription }
    }

    private var generalTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
                infoRow("工作区", app.preferences.defaultWorkspacePath.isEmpty ? "—" : app.preferences.defaultWorkspacePath)
                infoRow("数据目录", TigerosePaths.home.path)
                infoRow("引擎", app.backendStatus.rawValue + (app.backendPort.map { " :\($0)" } ?? ""))
                HStack(spacing: TigeroseTheme.spaceSm) {
                    Button("打开引擎日志") { NSWorkspace.shared.open(TigerosePaths.backendLog) }
                        .buttonStyle(TigeroseSecondaryButtonStyle())
                    Button("在 Finder 中显示数据目录") { NSWorkspace.shared.open(TigerosePaths.home) }
                        .buttonStyle(TigeroseSecondaryButtonStyle())
                }

                Text("联网 · Tavily")
                    .font(TigeroseTheme.titleSm)
                    .foregroundStyle(TigeroseTheme.ink)
                    .padding(.top, TigeroseTheme.spaceSm)
                Text("读页（web_extract）使用 Tavily；搜索默认 DuckDuckGo（无需 Key）。Key 写入 TIGEROSE_HOME/.env。")
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(TigeroseTheme.muted)
                SecureField(
                    tavily.has_api_key == true
                        ? "TAVILY_API_KEY（留空则不改，当前 \(tavily.api_key_masked ?? "****")）"
                        : "TAVILY_API_KEY",
                    text: $tavilyKey
                )
                .textFieldStyle(TigeroseTextFieldStyle())
                HStack {
                    Text(tavily.configured ? "Tavily 已配置" : "未配置（读页不可用）")
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(tavily.configured ? TigeroseTheme.muted : TigeroseTheme.primary)
                    Spacer()
                    Button(tavilySaving ? "保存中…" : "保存 Key") {
                        Task { await saveTavilyKey() }
                    }
                    .buttonStyle(TigerosePrimaryButtonStyle(disabled: tavilySaving))
                    .disabled(tavilySaving)
                }
                if !tavilyNotice.isEmpty {
                    Text(tavilyNotice)
                        .font(TigeroseTheme.bodySm)
                        .foregroundStyle(TigeroseTheme.muted)
                }

                if !error.isEmpty {
                    Text(error)
                        .font(TigeroseTheme.bodySm)
                        .foregroundStyle(TigeroseTheme.error)
                }
            }
            .padding(TigeroseTheme.spaceLg)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(TigeroseTheme.canvas)
        .task {
            await reloadTavily()
        }
    }

    private func reloadTavily() async {
        guard app.backendStatus == .running else { return }
        do {
            tavily = try await app.api.getTavilyWeb()
            tavilyKey = ""
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func saveTavilyKey() async {
        let key = tavilyKey.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else {
            error = "请填写 Tavily API Key"
            return
        }
        tavilySaving = true
        defer { tavilySaving = false }
        do {
            tavily = try await app.api.setTavilyWebKey(key)
            tavilyKey = ""
            tavilyNotice = "已保存"
            error = ""
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func infoRow(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(TigeroseTheme.caption)
                .foregroundStyle(TigeroseTheme.muted)
            Text(value)
                .font(TigeroseTheme.bodySm)
                .foregroundStyle(TigeroseTheme.ink)
                .textSelection(.enabled)
                .lineLimit(3)
                .hoverFullText(value)
        }
        .padding(TigeroseTheme.spaceSm)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(TigeroseTheme.surfaceCard, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
        .overlay(
            RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                .stroke(TigeroseTheme.hairline, lineWidth: 1)
        )
    }

    private func startAdd() {
        editingId = nil
        formModel = ""
        formBase = "http://localhost:1234/v1"
        formKey = ""
        error = ""
        mode = .add
    }

    private func startEdit(_ p: ManagedModelProfile) {
        editingId = p.id
        formModel = p.id
        formBase = p.base_url
        formKey = ""
        error = ""
        mode = .edit
    }

    private func resetForm() {
        mode = .list
        editingId = nil
        formModel = ""
        formBase = ""
        formKey = ""
        error = ""
    }

    private func reload() async {
        guard app.backendStatus == .running else { return }
        do {
            let r = try await app.api.listManagedProfiles()
            let evaluator = try await app.api.getGoalEvaluatorModel()
            defaultId = r.defaultId
            profiles = r.profiles
            goalEvaluator = evaluator
            goalEvaluatorSelection = evaluator.profile_id
            savedGoalEvaluatorSelection = evaluator.profile_id
            goalEvaluatorNotice = ""
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func saveGoalEvaluator() async {
        goalEvaluatorSaving = true
        defer { goalEvaluatorSaving = false }
        do {
            let evaluator = try await app.api.setGoalEvaluatorModel(
                profileId: goalEvaluatorSelection
            )
            goalEvaluator = evaluator
            goalEvaluatorSelection = evaluator.profile_id
            savedGoalEvaluatorSelection = evaluator.profile_id
            goalEvaluatorNotice = "已保存"
            error = ""
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func save() async {
        let model = formModel.trimmingCharacters(in: .whitespacesAndNewlines)
        let base = formBase.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !model.isEmpty, !base.isEmpty else {
            error = "请填写模型名称与 base_url"
            return
        }
        saving = true
        defer { saving = false }
        do {
            if mode == .edit, let id = editingId {
                _ = try await app.api.updateManagedProfile(
                    id: id,
                    model: model,
                    baseURL: base,
                    apiKey: formKey.isEmpty ? nil : formKey,
                    label: model,
                    provider: nil
                )
            } else {
                _ = try await app.api.createManagedProfile(
                    model: model,
                    baseURL: base,
                    apiKey: formKey.isEmpty ? nil : formKey,
                    label: model,
                    provider: base.contains("deepseek.com") ? "deepseek" : "custom"
                )
            }
            resetForm()
            await reload()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func remove(_ id: String) async {
        do {
            try await app.api.deleteManagedProfile(id: id)
            if editingId == id { resetForm() }
            await reload()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct SettingsSheet: View {
    @Environment(\.dismiss) private var dismiss
    let hostSize: CGSize

    private var sheetWidth: CGFloat {
        max(512, hostSize.width * 0.8)
    }

    private var sheetHeight: CGFloat {
        max(448, hostSize.height * 0.8)
    }

    var body: some View {
        VStack(spacing: 0) {
            TigeroseSheetHeader(title: "设置", onClose: { dismiss() })
            Rectangle().fill(TigeroseTheme.hairline).frame(height: 1)
            SettingsView()
        }
        .frame(width: sheetWidth, height: sheetHeight)
        .background(TigeroseTheme.canvas)
    }
}
