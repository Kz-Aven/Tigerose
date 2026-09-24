import SwiftUI

struct HooksSettingsPane: View {
    @Environment(AppState.self) private var app
    var workspace: String?
    @State private var hooks: [HookDefinition] = []
    @State private var editing: HookDefinition?
    @State private var error = ""
    @State private var testingID: String?
    @State private var testResult = ""

    private let events = ["RunStarted", "UserPromptSubmit", "PreToolUse", "PostToolUse", "TurnEnd", "RunCancelled", "RunFinished"]

    var body: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
            HStack {
                Text("钩子").font(TigeroseTheme.titleMd).foregroundStyle(TigeroseTheme.ink)
                Spacer()
                Button("添加钩子") {
                    editing = HookDefinition(hook_id: "", display_name: "", event: "PreToolUse", handler: HookHandler())
                }.buttonStyle(TigerosePrimaryButtonStyle())
            }.padding(.horizontal, TigeroseTheme.spaceLg)
            if !error.isEmpty { Text(error).font(TigeroseTheme.bodySm).foregroundStyle(TigeroseTheme.error).padding(.horizontal, TigeroseTheme.spaceLg) }
            if !testResult.isEmpty { Text(testResult).font(TigeroseTheme.bodySm).foregroundStyle(TigeroseTheme.muted).padding(.horizontal, TigeroseTheme.spaceLg) }
            if hooks.isEmpty {
                ContentUnavailableView("暂无钩子", systemImage: "link", description: Text("在生命周期节点运行校验、通知或记录。"))
            } else {
                List(hooks) { hook in
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(hook.display_name).font(TigeroseTheme.titleSm)
                            Text("\(hook.event) · \(hook.handler.mode) · \(hook.source == "project" ? "项目" : "全局")").font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                        }
                        Spacer()
                        Button(testingID == hook.id ? "测试中…" : "测试") { Task { await test(hook) } }.buttonStyle(TigeroseSecondaryButtonStyle()).disabled(testingID != nil)
                        Button("编辑") { editing = hook }.buttonStyle(TigeroseSecondaryButtonStyle())
                        Button("移除", role: .destructive) { Task { await remove(hook) } }
                    }.padding(.vertical, 4).listRowBackground(TigeroseTheme.canvas)
                }.listStyle(.plain).scrollContentBackground(.hidden)
            }
        }
        .task(id: workspace ?? "") { await reload() }
        .sheet(item: $editing) { hook in editor(hook) }
    }

    private func editor(_ initial: HookDefinition) -> some View {
        HookEditor(initial: initial, events: events, workspace: workspace) { hook, scope in
            do { _ = try await app.api.saveHook(hook, scope: scope, workspace: workspace); editing = nil; await reload() }
            catch let caught { self.error = caught.localizedDescription }
        }
        .frame(width: 500).background(TigeroseTheme.canvas)
    }

    private func reload() async { do { hooks = try await app.api.listHooks(workspace: workspace); error = "" } catch let caught { self.error = caught.localizedDescription } }
    private func test(_ hook: HookDefinition) async { testingID = hook.id; defer { testingID = nil }; do { let r = try await app.api.testHook(hook); testResult = "\(hook.display_name)：\(r.execution)，耗时 \(r.duration_ms)ms" } catch let caught { self.error = caught.localizedDescription } }
    private func remove(_ hook: HookDefinition) async { do { try await app.api.deleteHook(hook.hook_id, scope: hook.source == "project" ? "project" : "user", workspace: workspace); await reload() } catch let caught { self.error = caught.localizedDescription } }
}

private struct HookEditor: View {
    @Environment(\.dismiss) private var dismiss
    @State private var hook: HookDefinition
    @State private var scope: String
    let events: [String]
    let workspace: String?
    let save: (HookDefinition, String) async -> Void

    init(initial: HookDefinition, events: [String], workspace: String?, save: @escaping (HookDefinition, String) async -> Void) {
        _hook = State(initialValue: initial); _scope = State(initialValue: initial.source == "project" ? "project" : "user")
        self.events = events; self.workspace = workspace; self.save = save
    }
    var body: some View {
        VStack(spacing: 0) {
            TigeroseSheetHeader(title: hook.hook_id.isEmpty ? "添加钩子" : "编辑钩子", onClose: { dismiss() })
            VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
                TextField("稳定 ID，例如 workspace-policy", text: $hook.hook_id).textFieldStyle(TigeroseTextFieldStyle())
                TextField("名称", text: $hook.display_name).textFieldStyle(TigeroseTextFieldStyle())
                Picker("作用域", selection: $scope) { Text("全局默认").tag("user"); if workspace != nil { Text("当前项目").tag("project") } }.pickerStyle(.segmented)
                Picker("触发节点", selection: $hook.event) { ForEach(events, id: \.self) { Text($0).tag($0) } }
                Picker("模式", selection: $hook.handler.mode) { Text("旁路观察").tag("observe"); if hook.event == "UserPromptSubmit" || hook.event == "PreToolUse" { Text("允许/拒绝").tag("decision") } }.pickerStyle(.segmented)
                TextField("命令绝对路径", text: $hook.handler.command).textFieldStyle(TigeroseTextFieldStyle())
                TextField("参数，以空格分隔", text: Binding(get: { hook.handler.args.joined(separator: " ") }, set: { hook.handler.args = $0.split(separator: " ").map(String.init) })).textFieldStyle(TigeroseTextFieldStyle())
                if hook.event == "UserPromptSubmit" || hook.event == "PreToolUse" { Picker("失败策略", selection: $hook.failure_policy) { Text("允许继续").tag("allow"); Text("拒绝").tag("deny") }.pickerStyle(.segmented) }
                Toggle("启用", isOn: $hook.enabled)
                HStack { Spacer(); Button("保存") { Task { await save(hook, scope) } }.buttonStyle(TigerosePrimaryButtonStyle()).disabled(hook.hook_id.isEmpty || hook.handler.command.isEmpty) }
            }.padding(TigeroseTheme.spaceLg)
        }
    }
}
