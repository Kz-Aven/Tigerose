import SwiftUI

struct MeshCollaborationSettings: View {
    @Environment(AppState.self) private var app
    let assistantID: String
    @State private var configuration: MeshCollaboration?
    @State private var outgoing: Set<String> = []
    @State private var accepting = true
    @State private var busy = false
    @State private var error = ""
    @State private var notice = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("协作").font(TigeroseTheme.titleMd)
                Toggle("接收协作任务", isOn: $accepting)
                Text("关闭后不接收新任务，已有任务仍可继续。")
                    .font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                Divider()
                Text("允许该助理联系").font(TigeroseTheme.bodyMd.weight(.semibold))
                Text("授权后助理可自主发起任务；任务内双方可回复与追问。")
                    .font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                ForEach(app.assistants.filter { $0.template_id != assistantID }) { assistant in
                    Toggle(assistant.name, isOn: Binding(
                        get: { outgoing.contains(assistant.template_id) },
                        set: { if $0 { outgoing.insert(assistant.template_id) } else { outgoing.remove(assistant.template_id) } }
                    ))
                }
                Divider()
                Text("可联系当前助理").font(TigeroseTheme.bodyMd.weight(.semibold))
                Text((configuration?.incoming ?? []).map { id in app.assistants.first { $0.template_id == id }?.name ?? id }.joined(separator: "、").isEmpty ? "暂无助理" : (configuration?.incoming ?? []).map { id in app.assistants.first { $0.template_id == id }?.name ?? id }.joined(separator: "、"))
                    .foregroundStyle(TigeroseTheme.muted)
                if !error.isEmpty { Text(error).foregroundStyle(TigeroseTheme.error) }
                if !notice.isEmpty { Text(notice).foregroundStyle(TigeroseTheme.primary) }
                HStack {
                    Button("重新加载") { Task { await load() } }
                    Spacer()
                    Button("保存协作设置") { Task { await save() } }
                        .buttonStyle(TigerosePrimaryButtonStyle())
                }
            }
            .padding(TigeroseTheme.spaceMd)
        }
        .disabled(busy)
        .task(id: assistantID) { await load() }
    }

    private func load() async {
        busy = true
        defer { busy = false }
        do {
            let value = try await app.api.meshCollaboration(assistantID)
            configuration = value
            outgoing = Set(value.outgoing)
            accepting = value.accepting_tasks
            error = ""
        } catch { self.error = error.localizedDescription }
    }

    private func save() async {
        guard let configuration else { return }
        busy = true
        defer { busy = false }
        do {
            self.configuration = try await app.api.saveMeshCollaboration(assistantID, outgoing: outgoing.sorted(), accepting: accepting, revision: configuration.revision)
            error = ""
            notice = "协作设置已保存"
        } catch { self.error = error.localizedDescription; notice = "" }
    }
}
