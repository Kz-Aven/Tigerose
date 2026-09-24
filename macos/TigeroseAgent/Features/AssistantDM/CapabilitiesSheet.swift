import SwiftUI

struct CapabilitiesSheet: View {
    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss
    let template: AgentTemplate

    @State private var tab = 0
    @State private var caps = AssistantCapabilities()
    @State private var catalog: [CapabilityItem] = []
    @State private var name: String = ""
    @State private var role: String = ""
    @State private var prompt: String = ""
    @State private var avatarID: String = ""
    @State private var error = ""
    @State private var saving = false
    @State private var fileAccess = "ask"
    @State private var dangerPolicy = "deny"
    @State private var monitorDangerousBash = true
    @State private var confirmFullAccess = false

    private let codingDefaults = [
        "read_file", "write_file", "edit_file", "glob", "bash",
        "get_skill", "todo_write", "compact", "remember", "task",
        "create_task", "list_tasks", "get_task", "claim_task", "complete_task",
        "fail_task", "cancel_task", "release_task", "delete_task", "archive_completed_tasks",
        "schedule_cron", "list_crons", "cancel_cron",
        "spawn_teammate", "send_message", "check_inbox", "request_shutdown",
        "request_plan", "review_plan",
        "create_worktree", "remove_worktree", "keep_worktree",
    ]

    var body: some View {
        VStack(spacing: 0) {
            TigeroseSheetHeader(title: "能力配置 · \(template.name)", onClose: { dismiss() })
                .layoutPriority(1)
            Picker("类别", selection: $tab) {
                Text("基础").tag(0)
                Text("权限").tag(6)
                Text("协作").tag(8)
                Text("记忆").tag(7)
                Text("技能 \(caps.skills.count)").tag(1)
                Text("插件 \(caps.plugins.count)").tag(2)
                Text("工具 \(caps.tools.count)").tag(3)
                Text("MCP \(caps.mcp_servers.count)").tag(4)
                Text("IM频道").tag(5)
            }
            .pickerStyle(.menu)
            .padding(.horizontal, TigeroseTheme.spaceLg)
            .layoutPriority(1)

            if tab == 3, caps.tools.isEmpty {
                HStack {
                    Text("当前未启用任何工具，模型只能纯聊天。")
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.primary)
                    Spacer()
                    Button("启用默认全套工具") {
                        caps.tools = codingDefaults
                    }
                    .buttonStyle(TigeroseSecondaryButtonStyle())
                }
                .padding(.horizontal, TigeroseTheme.spaceMd)
                .padding(.top, TigeroseTheme.spaceXs)
            }

            Group {
                switch tab {
                case 0:
                    VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
                        AvatarPicker(selection: $avatarID, name: name)
                        TextField("名称", text: $name)
                            .textFieldStyle(TigeroseTextFieldStyle())
                        TextField("角色", text: $role)
                            .textFieldStyle(TigeroseTextFieldStyle())
                        TextEditor(text: $prompt)
                            .font(TigeroseTheme.bodyMd)
                            .foregroundStyle(TigeroseTheme.ink)
                            .scrollContentBackground(.hidden)
                            .padding(10)
                            .frame(minHeight: 160)
                            .background(TigeroseTheme.canvas, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
                            .overlay(
                                RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                                    .stroke(TigeroseTheme.hairline, lineWidth: 1)
                            )
                    }
                    .padding(TigeroseTheme.spaceMd)
                case 6:
                    permissionTab
                case 7:
                    MemoryManagementView(templateId: template.template_id)
                case 8:
                    MeshCollaborationSettings(assistantID: template.template_id)
                case 5:
                    ImAssistantBotsPane(templateId: template.template_id)
                default:
                    let kind = ["", "skill", "plugin", "tool", "mcp"][tab]
                    let items = catalog.filter { $0.kind == kind }
                    if items.isEmpty {
                        ContentUnavailableView(
                            emptyTitle(kind),
                            systemImage: "tray",
                            description: Text(emptyDescription(kind))
                        )
                        .foregroundStyle(TigeroseTheme.muted)
                    } else {
                        List {
                            ForEach(items) { item in
                                Toggle(isOn: binding(for: item, kind: kind)) {
                                    VStack(alignment: .leading, spacing: 2) {
                                        HStack(spacing: 6) {
                                            Text(item.display_name.isEmpty ? item.name : item.display_name)
                                                .foregroundStyle(TigeroseTheme.ink)
                                            if item.dangerous == true {
                                                Text("危险")
                                                    .font(TigeroseTheme.caption)
                                                    .foregroundStyle(TigeroseTheme.primary)
                                            }
                                            if item.missing == true {
                                                Text("缺失")
                                                    .font(TigeroseTheme.caption)
                                                    .foregroundStyle(TigeroseTheme.error)
                                            }
                                        }
                                        Text(item.description)
                                            .font(TigeroseTheme.caption)
                                            .foregroundStyle(TigeroseTheme.muted)
                                    }
                                }
                                .listRowBackground(TigeroseTheme.canvas)
                            }
                        }
                        .scrollContentBackground(.hidden)
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)

            if !error.isEmpty {
                Text(error)
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.error)
                    .padding(.horizontal, TigeroseTheme.spaceMd)
            }
            HStack {
                if tab == 3 {
                    Button("全选工具") {
                        caps.tools = catalog.filter { $0.kind == "tool" }.map(\.name)
                    }
                    .buttonStyle(TigeroseSecondaryButtonStyle())
                    Button("清空工具") { caps.tools = [] }
                        .buttonStyle(TigeroseSecondaryButtonStyle())
                }
                Spacer()
                if tab != 5 && tab != 7 {
                    Button("保存") { Task { await save() } }
                        .buttonStyle(TigerosePrimaryButtonStyle(disabled: saving))
                        .disabled(saving)
                        .keyboardShortcut(.defaultAction)
                }
            }
            .padding(TigeroseTheme.spaceMd)
            .layoutPriority(1)
        }
        .frame(width: 680, height: 580)
        .background(TigeroseTheme.canvas)
        .task { await load() }
        .confirmationDialog(
            "开启完全访问？",
            isPresented: $confirmFullAccess,
            titleVisibility: .visible
        ) {
            Button("确认开启完全访问", role: .destructive) {
                fileAccess = "full"
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("助理将可读写本机任意路径的文件，不再询问授权。请确认你信任该助理的操作。")
        }
    }


    private var permissionTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: TigeroseTheme.spaceLg) {
                VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
                    Text("文件访问")
                        .font(TigeroseTheme.titleSm)
                        .foregroundStyle(TigeroseTheme.ink)
                    Text("适用于 read_file / write_file / edit_file / excel_*（带路径的工具）")
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.muted)

                    permissionChoice(
                        title: "工作空间",
                        subtitle: "只能访问工作区内文件，区外自动拒绝",
                        selected: fileAccess == "workspace"
                    ) {
                        fileAccess = "workspace"
                    }
                    permissionChoice(
                        title: "授权访问（推荐）",
                        subtitle: "访问工作区外时弹窗询问",
                        selected: fileAccess == "ask"
                    ) {
                        fileAccess = "ask"
                    }
                    permissionChoice(
                        title: "完全访问",
                        subtitle: "可访问本机任意路径，不再询问",
                        selected: fileAccess == "full"
                    ) {
                        if fileAccess != "full" {
                            confirmFullAccess = true
                        }
                    }
                }
                .padding(TigeroseTheme.spaceMd)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(TigeroseTheme.surfaceSoft, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))

                VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
                    Text("高危操作")
                        .font(TigeroseTheme.titleSm)
                        .foregroundStyle(TigeroseTheme.ink)
                    Text("与路径无关的危险动作（如毁灭性 shell）")
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.muted)

                    permissionChoice(
                        title: "禁止（推荐）",
                        subtitle: "一律拒绝，授权条也不能放行",
                        selected: dangerPolicy == "deny"
                    ) {
                        dangerPolicy = "deny"
                    }
                    permissionChoice(
                        title: "每次确认",
                        subtitle: "弹窗询问后可执行本次操作",
                        selected: dangerPolicy == "ask"
                    ) {
                        dangerPolicy = "ask"
                    }

                    Text("监控条目")
                        .font(TigeroseTheme.bodySm.weight(.medium))
                        .foregroundStyle(TigeroseTheme.ink)
                        .padding(.top, 6)
                    Toggle(isOn: $monitorDangerousBash) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("危险 bash")
                                .foregroundStyle(TigeroseTheme.ink)
                            Text("rm -rf /、sudo、关机、管道灌 shell 等")
                                .font(TigeroseTheme.caption)
                                .foregroundStyle(TigeroseTheme.muted)
                        }
                    }
                    .toggleStyle(.checkbox)
                }
                .padding(TigeroseTheme.spaceMd)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(TigeroseTheme.surfaceSoft, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
            }
            .padding(TigeroseTheme.spaceMd)
        }
    }

    private func permissionChoice(
        title: String,
        subtitle: String,
        selected: Bool,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(selected ? TigeroseTheme.primary : TigeroseTheme.muted)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(TigeroseTheme.bodySm.weight(.medium))
                        .foregroundStyle(TigeroseTheme.ink)
                    Text(subtitle)
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.muted)
                }
                Spacer(minLength: 0)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.vertical, 4)
    }

    private func emptyTitle(_ kind: String) -> String {
        switch kind {
        case "skill": return "暂无技能"
        case "plugin": return "暂无插件"
        case "tool": return "暂无工具目录"
        default: return "暂无 MCP"
        }
    }

    private func emptyDescription(_ kind: String) -> String {
        switch kind {
        case "skill": return "把技能放到仓库 skills/ 目录后会出现在这里。"
        case "plugin": return "把插件放到仓库 plugins/ 目录后会出现在这里。"
        case "tool": return "服务端工具注册表为空，请检查 server/runtime/tools。"
        default: return "在配置中声明 MCP server 后会出现在这里；勾选后下一轮对话会自动连接。"
        }
    }

    private func binding(for item: CapabilityItem, kind: String) -> Binding<Bool> {
        Binding(
            get: {
                switch kind {
                case "skill": return caps.skills.contains(item.id) || caps.skills.contains(item.name)
                case "plugin": return caps.plugins.contains(item.id) || caps.plugins.contains(item.name)
                case "tool": return caps.tools.contains(item.id) || caps.tools.contains(item.name)
                default: return caps.mcp_servers.contains(item.id) || caps.mcp_servers.contains(item.name)
                }
            },
            set: { on in
                func toggle(_ arr: inout [String], preferId: Bool) {
                    let key = preferId ? item.id : item.name
                    let alt = preferId ? item.name : item.id
                    if on {
                        arr.removeAll { $0 == key || $0 == alt }
                        arr.append(key)
                    } else {
                        arr.removeAll { $0 == key || $0 == alt }
                    }
                }
                switch kind {
                case "skill": toggle(&caps.skills, preferId: true)
                case "plugin": toggle(&caps.plugins, preferId: true)
                case "tool": toggle(&caps.tools, preferId: false)
                default: toggle(&caps.mcp_servers, preferId: true)
                }
            }
        )
    }

    private func load() async {
        name = template.name
        role = template.role
        prompt = template.system_prompt
        avatarID = template.avatar_id
        fileAccess = template.fileAccess
        dangerPolicy = template.dangerPolicy
        monitorDangerousBash = template.dangerRules.contains("dangerous_bash")
        do {
            let got = try await app.api.getAssistantCapabilities(template.template_id)
            caps = got.capabilities
            if let fresh = try? await app.api.getAssistant(template.template_id) {
                avatarID = fresh.avatar_id
                fileAccess = fresh.fileAccess
                dangerPolicy = fresh.dangerPolicy
                monitorDangerousBash = fresh.dangerRules.contains("dangerous_bash")
            }
            catalog = try await app.api.listCapabilities()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            _ = try await app.api.updateAssistant(template.template_id, body: [
                "name": name,
                "role": role,
                "system_prompt": prompt,
                "avatar_id": avatarID,
            ])
            if tab == 6 {
                var rules: [String] = []
                if monitorDangerousBash { rules.append("dangerous_bash") }
                _ = try await app.api.setAssistantPermissionPolicy(
                    template.template_id,
                    fileAccess: fileAccess,
                    dangerPolicy: dangerPolicy,
                    dangerRules: rules
                )
            } else if (1 ... 4).contains(tab) {
                _ = try await app.api.putAssistantCapabilities(template.template_id, caps: caps)
            }
            try await app.refreshLists()
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
