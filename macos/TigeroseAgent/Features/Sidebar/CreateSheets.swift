import SwiftUI
import AppKit

struct CreateAssistantSheet: View {
    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var role = ""
    @State private var prompt = ""
    @State private var avatarID = ""
    @State private var error = ""

    var body: some View {
        VStack(spacing: 0) {
            TigeroseSheetHeader(title: "新建助理", onClose: { dismiss() })
            VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
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
                    .frame(minHeight: 120)
                    .background(TigeroseTheme.canvas, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
                    .overlay(
                        RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                            .stroke(TigeroseTheme.hairline, lineWidth: 1)
                    )
                if !error.isEmpty {
                    Text(error)
                        .font(TigeroseTheme.bodySm)
                        .foregroundStyle(TigeroseTheme.error)
                }
                HStack {
                    Spacer()
                    Button("创建") {
                        Task { await create() }
                    }
                    .buttonStyle(TigerosePrimaryButtonStyle(
                        disabled: name.trimmingCharacters(in: .whitespaces).isEmpty
                    ))
                    .keyboardShortcut(.defaultAction)
                    .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(.horizontal, TigeroseTheme.spaceLg)
            .padding(.bottom, TigeroseTheme.spaceLg)
        }
        .frame(width: 440)
        .background(TigeroseTheme.canvas)
        .onAppear {
            if avatarID.isEmpty { avatarID = AvatarCatalog.randomID }
        }
    }

    private func create() async {
        do {
            let a = try await app.api.createAssistant(
                name: name,
                role: role,
                systemPrompt: prompt,
                avatarId: avatarID
            )
            try await app.refreshLists()
            app.surface = .assistant(a.template_id)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct CreateGroupSheet: View {
    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var workspace = ""
    @State private var selected: Set<String> = []
    @State private var error = ""

    var body: some View {
        VStack(spacing: 0) {
            TigeroseSheetHeader(title: "新建项目群", onClose: { dismiss() })
                .padding(.top, TigeroseTheme.spaceMd)
            VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
                TextField("名称", text: $name)
                    .textFieldStyle(TigeroseTextFieldStyle())
                HStack(spacing: TigeroseTheme.spaceXs) {
                    TextField("工作区路径", text: $workspace)
                        .textFieldStyle(TigeroseTextFieldStyle())
                    Button("选择…") { pick() }
                        .buttonStyle(TigeroseSecondaryButtonStyle())
                }
                Text("初始成员")
                    .font(TigeroseTheme.titleSm)
                    .foregroundStyle(TigeroseTheme.ink)
                List {
                    ForEach(app.assistants) { a in
                        Toggle(isOn: Binding(
                            get: { selected.contains(a.template_id) },
                            set: { on in
                                if on { selected.insert(a.template_id) } else { selected.remove(a.template_id) }
                            }
                        )) {
                            Text(a.name)
                                .foregroundStyle(TigeroseTheme.ink)
                        }
                        .listRowBackground(TigeroseTheme.canvas)
                    }
                }
                .scrollContentBackground(.hidden)
                .frame(height: 160)
                .background(TigeroseTheme.surfaceCard, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
                .overlay(
                    RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                        .stroke(TigeroseTheme.hairline, lineWidth: 1)
                )
                if !error.isEmpty {
                    Text(error)
                        .font(TigeroseTheme.bodySm)
                        .foregroundStyle(TigeroseTheme.error)
                }
                HStack {
                    Spacer()
                    Button("创建") { Task { await create() } }
                        .buttonStyle(TigerosePrimaryButtonStyle(
                            disabled: name.trimmingCharacters(in: .whitespaces).isEmpty
                        ))
                        .keyboardShortcut(.defaultAction)
                        .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(.horizontal, TigeroseTheme.spaceLg)
            .padding(.bottom, TigeroseTheme.spaceXl)
        }
        .frame(width: 480, height: 460)
        .background(TigeroseTheme.canvas)
        .onAppear {
            if workspace.isEmpty {
                workspace = app.preferences.defaultWorkspacePath
            }
        }
    }

    private func pick() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        if panel.runModal() == .OK, let url = panel.url {
            workspace = url.path
        }
    }

    private func create() async {
        do {
            let g = try await app.api.createGroup(
                name: name,
                workspacePath: workspace,
                memberTemplateIds: Array(selected)
            )
            try await app.refreshLists()
            app.surface = .group(g.group_id)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
