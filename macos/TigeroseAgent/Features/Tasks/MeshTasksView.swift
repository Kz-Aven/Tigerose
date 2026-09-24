import SwiftUI
import AppKit

struct MeshTasksPage: View {
    @Environment(AppState.self) private var app
    @State private var assistantID = ""
    @State private var groupID = ""
    @State private var status = ""
    @State private var refreshID = 0

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button { app.surface = app.meshReturnSurface } label: { Label("返回", systemImage: "chevron.left") }
                    .buttonStyle(.plain)
                Text("全局任务").font(TigeroseTheme.titleMd)
                Spacer()
                Picker("状态", selection: $status) {
                    Text("全部状态").tag("")
                    ForEach(MeshTask.statuses, id: \.self) { Text(MeshTask.statusTitle($0)).tag($0) }
                }.frame(maxWidth: 140)
                Picker("助理", selection: $assistantID) {
                    Text("全部助理").tag("")
                    ForEach(app.assistants) { Text($0.name).tag($0.template_id) }
                }.frame(maxWidth: 170)
                Picker("来源", selection: $groupID) {
                    Text("全部来源").tag("")
                    ForEach(app.groups) { Text($0.name).tag($0.group_id) }
                }.frame(maxWidth: 170)
                Button { refreshID &+= 1 } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.plain)
                    .help("刷新任务")
            }.padding(16)
            Divider()
            HSplitView {
                MeshTaskList(assistantID: assistantID, groupID: groupID, status: $status, refreshID: refreshID, showsListFilters: false, selection: Binding(get: { app.meshSelectedTaskID }, set: { app.meshSelectedTaskID = $0 }))
                    .frame(minWidth: 240, idealWidth: 300, maxWidth: 380)
                if let id = app.meshSelectedTaskID {
                    MeshTaskDetail(taskID: id, compact: false, onSelect: { app.meshSelectedTaskID = $0 })
                        .id(id)
                        .frame(minWidth: 320, maxWidth: .infinity)
                } else {
                    ContentUnavailableView("选择一个任务", systemImage: "checklist", description: Text("查看沟通、任务关系与交付结果"))
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
        }.background(TigeroseTheme.canvas)
    }
}

struct MeshAssistantPanel: View {
    @Environment(AppState.self) private var app
    let assistantID: String
    var onClose: () -> Void
    @State private var selected: String?
    @State private var status = ""
    @State private var refreshID = 0

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                if selected != nil { Button { selected = nil } label: { Image(systemName: "chevron.left") }.buttonStyle(.plain) }
                Text("助理任务").font(TigeroseTheme.titleMd)
                Spacer()
                Button(action: onClose) { Image(systemName: "xmark") }.buttonStyle(.plain).help("关闭任务面板")
            }.padding(16)
            Divider()
            if let selected {
                Button("查看完整任务") { app.openMeshTasks(taskID: selected) }.padding(10)
                MeshTaskDetail(taskID: selected, compact: true, onSelect: { self.selected = $0 }).id(selected)
            } else {
                MeshTaskList(assistantID: assistantID, groupID: "", status: $status, refreshID: refreshID, showsListFilters: true, selection: $selected)
            }
        }
        .frame(maxHeight: .infinity, alignment: .top)
        .background(TigeroseTheme.canvas)
        .onChange(of: assistantID) { _, _ in selected = nil }
    }
}

private struct MeshTaskList: View {
    @Environment(AppState.self) private var app
    let assistantID: String
    let groupID: String
    @Binding var status: String
    let refreshID: Int
    let showsListFilters: Bool
    @Binding var selection: String?
    @State private var role = "all"
    @State private var tasks: [MeshTask] = []
    @State private var cursor = ""
    @State private var error = ""
    @State private var loading = false
    @State private var expanded: Set<String> = []
    @State private var events = SSEClient()
    private var workflows: [String] { tasks.reduce(into: []) { if !$0.contains($1.workflow_id) { $0.append($1.workflow_id) } } }
    private var filterKey: String { "\(assistantID)|\(groupID)|\(role)|\(status)|\(refreshID)" }

    var body: some View {
        VStack(spacing: 10) {
            if !assistantID.isEmpty {
                Picker("角色", selection: $role) {
                    Text("全部").tag("all")
                    Text("我发起").tag("sent")
                    Text("我接收").tag("received")
                }.pickerStyle(.segmented)
            }
            if showsListFilters {
                HStack {
                    Picker("状态", selection: $status) {
                        Text("全部状态").tag("")
                        ForEach(MeshTask.statuses, id: \.self) { Text(MeshTask.statusTitle($0)).tag($0) }
                    }
                    Button { Task { await load() } } label: { Image(systemName: "arrow.clockwise") }.help("刷新任务")
                }
            }
            if !error.isEmpty { Text(error).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.error) }
            if tasks.isEmpty {
                if loading { ProgressView().frame(maxHeight: .infinity) }
                else { ContentUnavailableView("暂无任务", systemImage: "checklist", description: Text("助理发起协作后，任务会出现在这里。")) .frame(maxHeight: .infinity) }
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        if assistantID.isEmpty {
                            ForEach(workflows, id: \.self) { workflow in
                                let members = tasks.filter { $0.workflow_id == workflow }
                                VStack(alignment: .leading, spacing: 4) {
                                    Button {
                                        if expanded.contains(workflow) { expanded.remove(workflow) }
                                        else { expanded.insert(workflow) }
                                    } label: {
                                        HStack(alignment: .top, spacing: 8) {
                                            Image(systemName: expanded.contains(workflow) ? "chevron.down" : "chevron.right")
                                                .font(.system(size: 11, weight: .semibold))
                                                .foregroundStyle(TigeroseTheme.muted)
                                                .frame(width: 16, height: 20)
                                            Image(systemName: workflowComplete(members) ? "checkmark.circle.fill" : "circle.inset.filled")
                                                .foregroundStyle(workflowComplete(members) ? TigeroseTheme.primary : TigeroseTheme.muted)
                                                .frame(height: 20)
                                            VStack(alignment: .leading, spacing: 4) {
                                                Text(workflowTitle(members)).font(TigeroseTheme.bodyMd.weight(.semibold)).lineLimit(2)
                                                Text(workflowProgress(members)).font(TigeroseTheme.caption)
                                                    .foregroundStyle(workflowComplete(members) ? TigeroseTheme.primary : TigeroseTheme.muted)
                                            }
                                            Spacer(minLength: 0)
                                        }
                                        .contentShape(Rectangle())
                                    }
                                    .buttonStyle(.plain)
                                    .help(expanded.contains(workflow) ? "收起任务" : "展开任务")
                                    if expanded.contains(workflow) {
                                        ForEach(globalTaskRows(members)) { row in
                                            globalTaskRow(row)
                                        }
                                    }
                                }
                                .padding(.vertical, 6)
                            }
                        } else {
                            ForEach(tasks) { taskRow($0) }
                        }
                        if !cursor.isEmpty { Button("加载更多") { Task { await load(more: true) } }.disabled(loading) }
                    }
                }
            }
        }
        .padding(12)
        .task(id: filterKey) {
            events.subscribe(baseURL: app.api.baseURL, channel: "mesh") { type, _ in
                if type == "mesh.changed", !loading { Task { await load() } }
            }
            defer { events.cancel() }
            tasks = []; cursor = ""
            await load()
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(5)) } catch { return }
                await load()
            }
        }
    }

    private func name(_ id: String?) -> String { app.assistants.first { $0.template_id == id }?.name ?? id ?? "源请求" }

    private func taskRow(_ task: MeshTask) -> some View {
        Button { selection = task.task_id } label: {
            VStack(alignment: .leading, spacing: 6) {
                Text(task.title).font(TigeroseTheme.bodyMd.weight(.medium)).lineLimit(2)
                Text("\(name(task.caller_id)) → \(name(task.target_id))")
                    .font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                MeshStatusBadge(status: task.status)
                if let coordination = task.coordinationTitle {
                    Text(coordination)
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.primary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let reason = task.wait_reason, !reason.isEmpty { Text(reason).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted) }
            }
            .frame(maxWidth: .infinity, alignment: .leading).padding(12)
            .background(selection == task.task_id ? TigeroseTheme.secondary : TigeroseTheme.surfaceSoft, in: RoundedRectangle(cornerRadius: 8))
        }.buttonStyle(.plain)
    }

    private struct TaskTreeRow: Identifiable {
        let task: MeshTask
        let depth: Int
        let hasChildren: Bool
        var id: String { task.task_id }
    }

    private func globalTaskRows(_ members: [MeshTask]) -> [TaskTreeRow] {
        let ids = Set(members.map(\.task_id))
        let roots = members.filter { !ids.contains($0.parent_task_id ?? "") }
        var rows: [TaskTreeRow] = []
        func append(_ task: MeshTask, depth: Int) {
            let children = members.filter { $0.parent_task_id == task.task_id }
            rows.append(TaskTreeRow(task: task, depth: depth, hasChildren: !children.isEmpty))
            for child in children { append(child, depth: depth + 1) }
        }
        for root in roots { append(root, depth: 1) }
        return rows
    }

    private func globalTaskRow(_ row: TaskTreeRow) -> some View {
        let task = row.task
        return Button { selection = task.task_id } label: {
            HStack(alignment: .top, spacing: 8) {
                VStack(spacing: 0) {
                    Image(systemName: task.status == "completed" ? "checkmark.circle.fill" : statusIcon(task.status))
                        .foregroundStyle(task.status == "completed" ? TigeroseTheme.primary : statusColor(task.status))
                    if row.hasChildren { Rectangle().fill(TigeroseTheme.secondary).frame(width: 1).frame(maxHeight: .infinity) }
                }.frame(width: 16)
                VStack(alignment: .leading, spacing: 4) {
                    Text(task.title).font(TigeroseTheme.bodySm.weight(.medium)).lineLimit(2)
                    Text(MeshTask.statusTitle(task.status)).font(TigeroseTheme.caption).foregroundStyle(statusColor(task.status))
                }
                Spacer(minLength: 6)
                Text(name(task.target_id)).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted).lineLimit(1)
            }
            .padding(.vertical, 7)
            .padding(.horizontal, 8)
            .background(selection == task.task_id ? TigeroseTheme.secondary : Color.clear, in: RoundedRectangle(cornerRadius: 6))
        }
        .buttonStyle(.plain)
        .padding(.leading, CGFloat(row.depth) * 18)
    }

    private func workflowTitle(_ members: [MeshTask]) -> String {
        let ids = Set(members.map(\.task_id))
        return members.first(where: { !ids.contains($0.parent_task_id ?? "") })?.title ?? members.first?.title ?? "协作任务"
    }

    private func workflowComplete(_ members: [MeshTask]) -> Bool { !members.isEmpty && members.allSatisfy { $0.status == "completed" } }

    private func workflowProgress(_ members: [MeshTask]) -> String {
        let complete = members.filter { $0.status == "completed" }.count
        if complete == members.count { return "\(complete)/\(members.count) 已完成" }
        let active = members.filter { ["running", "queued", "waiting", "submitted", "revision_requested"].contains($0.status) }.count
        return "\(complete)/\(members.count) 已完成 · \(active) 进行中"
    }

    private func statusIcon(_ status: String) -> String {
        ["running": "circle.inset.filled", "queued": "circle.dotted", "waiting": "pause.circle", "submitted": "checkmark.circle", "failed": "exclamationmark.circle", "cancelled": "xmark.circle", "timeout": "clock.badge.exclamation"][status] ?? "circle"
    }

    private func statusColor(_ status: String) -> Color {
        ["failed", "timeout", "cancelled"].contains(status) ? TigeroseTheme.error : (["running", "submitted", "revision_requested"].contains(status) ? TigeroseTheme.primary : TigeroseTheme.muted)
    }

    private func load(more: Bool = false) async {
        let key = filterKey
        loading = true
        defer { loading = false }
        do {
            var page = try await app.api.meshTasks(assistantID: assistantID, role: role, status: status, groupID: groupID, cursor: more ? cursor : "")
            if !more {
                let count = tasks.count
                while page.items.count < count, let next = page.next_cursor, !next.isEmpty {
                    let following = try await app.api.meshTasks(assistantID: assistantID, role: role, status: status, groupID: groupID, cursor: next)
                    page.items += following.items
                    page.next_cursor = following.next_cursor
                }
            }
            guard !Task.isCancelled, key == filterKey else { return }
            if more { tasks += page.items.filter { item in !tasks.contains { $0.id == item.id } } }
            else { tasks = page.items }
            cursor = page.next_cursor ?? ""
            error = ""
        } catch { if !Task.isCancelled { self.error = error.localizedDescription } }
    }
}

struct MeshStatusBadge: View {
    let status: String
    var body: some View {
        Text(MeshTask.statusTitle(status))
            .font(TigeroseTheme.caption)
            .foregroundStyle(["failed", "timeout"].contains(status) ? TigeroseTheme.error : TigeroseTheme.primary)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background(TigeroseTheme.secondary, in: Capsule())
    }
}
