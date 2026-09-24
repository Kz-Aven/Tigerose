import SwiftUI
import AppKit

/// Settings tabs for skills / plugins / tools / MCP global resource management.
struct ResourceSettingsPanes: View {
    @Environment(AppState.self) private var app
    let tab: ResourceTab
    var workspace: String?

    enum ResourceTab: Int {
        case skills = 2
        case plugins = 3
        case tools = 4
        case mcp = 5
    }

    @State private var items: [CapabilityItem] = []
    @State private var mcpServers: [McpServerConfig] = []
    @State private var error = ""
    @State private var notice = ""
    @State private var editing: CapabilityItem?
    @State private var editName = ""
    @State private var editDesc = ""
    @State private var mcpForm: McpForm?
    @State private var testingName: String?
    @State private var importing = false

    struct McpForm: Identifiable {
        var id: String { name }
        var name: String
        var type: String
        var command: String
        var argsText: String
        var url: String
        var description: String
        var isNew: Bool
    }

    private var kind: String {
        switch tab {
        case .skills: return "skill"
        case .plugins: return "plugin"
        case .tools: return "tool"
        case .mcp: return "mcp"
        }
    }

    private var title: String {
        switch tab {
        case .skills: return "技能"
        case .plugins: return "插件"
        case .tools: return "工具"
        case .mcp: return "MCP"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
            HStack {
                Text(title)
                    .font(TigeroseTheme.titleMd)
                    .foregroundStyle(TigeroseTheme.ink)
                Spacer()
                if tab == .skills || tab == .plugins {
                    Button(importing ? "导入中…" : "导入") { pickImport() }
                        .buttonStyle(TigerosePrimaryButtonStyle())
                        .disabled(importing)
                }
                if tab == .mcp {
                    Button("添加 MCP") {
                        mcpForm = McpForm(
                            name: "",
                            type: "stdio",
                            command: "",
                            argsText: "",
                            url: "",
                            description: "",
                            isNew: true
                        )
                    }
                    .buttonStyle(TigerosePrimaryButtonStyle())
                }
            }
            .padding(.horizontal, TigeroseTheme.spaceLg)

            if !error.isEmpty {
                Text(error)
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.error)
                    .padding(.horizontal, TigeroseTheme.spaceLg)
            }
            if !notice.isEmpty {
                Text(notice)
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.muted)
                    .padding(.horizontal, TigeroseTheme.spaceLg)
            }

            if tab == .mcp {
                mcpList
            } else {
                capabilityList
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .task(id: "\(tab.rawValue)-\(workspace ?? "")") { await reload() }
        .sheet(item: $editing) { item in
            VStack(spacing: 0) {
                TigeroseSheetHeader(title: "编辑 \(item.display_name)", onClose: { editing = nil })
                VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
                    TextField("显示名", text: $editName)
                        .textFieldStyle(TigeroseTextFieldStyle())
                    TextField("描述", text: $editDesc)
                        .textFieldStyle(TigeroseTextFieldStyle())
                    HStack {
                        Spacer()
                        Button("保存") { Task { await saveEdit(item) } }
                            .buttonStyle(TigerosePrimaryButtonStyle())
                    }
                }
                .padding(.horizontal, TigeroseTheme.spaceLg)
                .padding(.bottom, TigeroseTheme.spaceLg)
            }
            .frame(width: 420)
            .background(TigeroseTheme.canvas)
        }
        .sheet(item: $mcpForm) { form in
            mcpEditor(form)
        }
    }

    private var capabilityList: some View {
        Group {
            if items.isEmpty {
                ContentUnavailableView(
                    "暂无\(title)",
                    systemImage: "tray",
                    description: Text(emptyHint)
                )
                .foregroundStyle(TigeroseTheme.muted)
            } else {
                List {
                    ForEach(items) { item in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text(item.display_name.isEmpty ? item.name : item.display_name)
                                    .font(TigeroseTheme.titleSm)
                                    .foregroundStyle(TigeroseTheme.ink)
                                sourceBadge(item.source)
                                if item.runtime_required == true {
                                    Text("必备")
                                        .font(TigeroseTheme.caption)
                                        .foregroundStyle(TigeroseTheme.ink)
                                        .padding(.horizontal, 8)
                                        .padding(.vertical, 2)
                                        .background(TigeroseTheme.surfaceCreamStrong, in: Capsule())
                                }
                                if item.dangerous == true {
                                    Text("危险")
                                        .font(TigeroseTheme.caption)
                                        .foregroundStyle(TigeroseTheme.primary)
                                }
                                Spacer()
                                if item.editable == true, tab != .tools {
                                    Button("编辑") {
                                        editName = item.display_name
                                        editDesc = item.description
                                        editing = item
                                    }
                                    .buttonStyle(TigeroseSecondaryButtonStyle())
                                }
                                if item.deletable == true, tab != .tools {
                                    Button("移除", role: .destructive) {
                                        Task { await removeCapability(item) }
                                    }
                                    .foregroundStyle(TigeroseTheme.error)
                                }
                            }
                            Text(item.description)
                                .font(TigeroseTheme.caption)
                                .foregroundStyle(TigeroseTheme.muted)
                                .lineLimit(2)
                                .hoverFullText(item.description)
                            if let path = item.source_path, !path.isEmpty {
                                Text(path)
                                    .font(TigeroseTheme.caption)
                                    .foregroundStyle(TigeroseTheme.mutedSoft)
                                    .lineLimit(1)
                                    .hoverFullText(path)
                            }
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

    private var mcpList: some View {
        Group {
            if mcpServers.isEmpty {
                ContentUnavailableView(
                    "暂无 MCP",
                    systemImage: "tray",
                    description: Text("添加 stdio（command）或 Streamable HTTP（URL）MCP 服务器。")
                )
                .foregroundStyle(TigeroseTheme.muted)
            } else {
                List {
                    ForEach(mcpServers) { s in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text(s.name)
                                    .font(TigeroseTheme.titleSm)
                                    .foregroundStyle(TigeroseTheme.ink)
                                sourceBadge(s.source)
                                Spacer()
                                Button(testingName == s.name ? "测连中…" : "测连") {
                                    Task { await testMcp(s) }
                                }
                                .buttonStyle(TigeroseSecondaryButtonStyle())
                                .disabled(testingName != nil)
                                if s.editable == true {
                                    Button("编辑") {
                                        mcpForm = McpForm(
                                            name: s.name,
                                            type: s.type ?? (s.command != nil ? "stdio" : "streamable-http"),
                                            command: s.command ?? "",
                                            argsText: (s.args ?? []).joined(separator: " "),
                                            url: s.url ?? "",
                                            description: s.description ?? "",
                                            isNew: false
                                        )
                                    }
                                    .buttonStyle(TigeroseSecondaryButtonStyle())
                                }
                                if s.deletable == true {
                                    Button("移除", role: .destructive) {
                                        Task { await removeMcp(s) }
                                    }
                                    .foregroundStyle(TigeroseTheme.error)
                                }
                            }
                            Text(s.description ?? "")
                                .font(TigeroseTheme.caption)
                                .foregroundStyle(TigeroseTheme.muted)
                                .lineLimit(2)
                                .hoverFullText(s.description ?? "")
                            Text(s.command.map { "\($0) \((s.args ?? []).joined(separator: " "))" } ?? (s.url ?? ""))
                                .font(TigeroseTheme.caption)
                                .foregroundStyle(TigeroseTheme.mutedSoft)
                                .lineLimit(2)
                                .hoverFullText(s.command.map { "\($0) \((s.args ?? []).joined(separator: " "))" } ?? (s.url ?? ""))
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

    private func mcpEditor(_ form: McpForm) -> some View {
        McpEditorSheet(
            form: form,
            onClose: { mcpForm = nil },
            onSave: { updated in
                Task { await saveMcp(updated) }
            }
        )
    }

    private var emptyHint: String {
        switch tab {
        case .skills: return "点击「导入」选择含 SKILL.md 的目录。"
        case .plugins: return "点击「导入」选择含 plugin.yaml 的目录。"
        case .tools: return "内置工具由运行时注册，只读。"
        case .mcp: return "添加 MCP 服务器。"
        }
    }

    private func sourceBadge(_ source: String?) -> some View {
        let label: String
        switch source {
        case "project": label = "项目"
        case "user": label = "用户"
        default: label = "内置"
        }
        return Text(label)
            .font(TigeroseTheme.caption)
            .foregroundStyle(TigeroseTheme.muted)
            .padding(.horizontal, 8)
            .padding(.vertical, 2)
            .background(TigeroseTheme.surfaceCard, in: Capsule())
            .overlay(Capsule().stroke(TigeroseTheme.hairline, lineWidth: 1))
    }

    private func reload() async {
        error = ""
        notice = ""
        guard app.backendStatus == .running else { return }
        do {
            if tab == .mcp {
                mcpServers = try await app.api.listMcpServers(workspace: workspace)
            } else {
                items = try await app.api.listCapabilities(kind: kind, workspace: workspace)
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func pickImport() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.message = tab == .skills
            ? "选择 SKILL.md 文件或其所在的技能目录"
            : "选择 plugin.yaml 文件或其所在的插件目录"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        importing = true
        error = ""
        notice = ""
        Task {
            defer { importing = false }
            do {
                let item = try await app.api.importCapability(kind: kind, path: url.path)
                if let index = items.firstIndex(where: { $0.id == item.id }) {
                    items[index] = item
                } else {
                    items.append(item)
                    items.sort { $0.display_name.localizedCaseInsensitiveCompare($1.display_name) == .orderedAscending }
                }
                notice = "已导入 \(item.display_name.isEmpty ? item.name : item.display_name)"
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    private func removeCapability(_ item: CapabilityItem) async {
        do {
            try await app.api.deleteCapability(item.id)
            notice = "已移除"
            await reload()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func saveEdit(_ item: CapabilityItem) async {
        do {
            try await app.api.patchCapabilityRegistry(
                item.id,
                displayName: editName,
                description: editDesc
            )
            editing = nil
            await reload()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func removeMcp(_ s: McpServerConfig) async {
        do {
            try await app.api.deleteMcpServer(s.name)
            notice = "已移除"
            await reload()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func saveMcp(_ form: McpForm) async {
        let name = form.name.trimmingCharacters(in: .whitespacesAndNewlines)
        let command = form.command.trimmingCharacters(in: .whitespacesAndNewlines)
        let url = form.url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, !command.isEmpty || !url.isEmpty else {
            error = "请填写名称，并填写 command 或 URL"
            return
        }
        let args = form.argsText
            .split(whereSeparator: { $0.isWhitespace })
            .map(String.init)
        do {
            try await app.api.upsertMcpServer(
                name: name,
                command: command.isEmpty ? nil : command,
                args: args,
                env: [:],
                description: form.description,
                type: url.isEmpty ? "stdio" : "streamable-http",
                url: url.isEmpty ? nil : url
            )
            mcpForm = nil
            notice = "已保存"
            await reload()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func testMcp(_ s: McpServerConfig) async {
        testingName = s.name
        defer { testingName = nil }
        do {
            let r = try await app.api.testMcpServer(s.name, workspace: workspace)
            if r.ok {
                notice = "测连成功：\(r.tool_count ?? r.tools?.count ?? 0) 个工具"
                error = ""
            } else {
                error = r.error ?? "测连失败"
            }
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct McpEditorSheet: View {
    @State var form: ResourceSettingsPanes.McpForm
    var onClose: () -> Void
    var onSave: (ResourceSettingsPanes.McpForm) -> Void

    var body: some View {
        VStack(spacing: 0) {
            TigeroseSheetHeader(
                title: form.isNew ? "添加 MCP" : "编辑 MCP",
                onClose: onClose
            )
            VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
                TextField("名称", text: $form.name)
                    .textFieldStyle(TigeroseTextFieldStyle())
                    .disabled(!form.isNew)
                TextField("command（stdio，二选一）", text: $form.command)
                    .textFieldStyle(TigeroseTextFieldStyle())
                TextField("args（空格分隔）", text: $form.argsText)
                    .textFieldStyle(TigeroseTextFieldStyle())
                TextField("URL（Streamable HTTP，二选一）", text: $form.url)
                    .textFieldStyle(TigeroseTextFieldStyle())
                TextField("描述", text: $form.description)
                    .textFieldStyle(TigeroseTextFieldStyle())
                Text("stdio 可用 ${workspaceFolder} 表示当前工作区路径")
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(TigeroseTheme.muted)
                HStack {
                    Spacer()
                    Button("保存") { onSave(form) }
                        .buttonStyle(TigerosePrimaryButtonStyle())
                }
            }
            .padding(.horizontal, TigeroseTheme.spaceLg)
            .padding(.bottom, TigeroseTheme.spaceLg)
        }
        .frame(width: 480)
        .background(TigeroseTheme.canvas)
    }
}
