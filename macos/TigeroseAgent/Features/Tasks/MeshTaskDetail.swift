import SwiftUI
import AppKit
import QuickLookUI

struct MeshTaskDetail: View {
    @Environment(AppState.self) private var app
    let taskID: String
    let compact: Bool
    var onSelect: (String) -> Void
    @State private var task: MeshTask?
    @State private var messages: [MeshMessage] = []
    @State private var graph: MeshGraph?
    @State private var error = ""
    @State private var cancelling = false
    @State private var confirmCancel = false
    @State private var cancelWorkflow = false
    @State private var artifactURL: URL?
    @State private var events = SSEClient()
    @State private var refreshing = false
    @State private var showDetails = false
    @State private var expandedRecordIDs: Set<String> = []

    var body: some View {
        ScrollView {
            if let task {
                VStack(alignment: .leading, spacing: 18) {
                    taskHeader(task)
                    acceptanceCard(task)
                    if !compact, let graph {
                        detailCard { graphSection(graph) }
                            .overlay(RoundedRectangle(cornerRadius: 8).stroke(TigeroseTheme.hairlineSoft, lineWidth: 1))
                    }
                    executionCard(task)
                    if let artifacts = task.artifacts, !artifacts.isEmpty {
                        detailCard {
                            Text("交付物").font(TigeroseTheme.bodyMd.weight(.semibold))
                            ForEach(artifacts) { artifact in
                                Button { Task { await openArtifact(artifact) } } label: { Label(artifact.displayName, systemImage: "doc") }
                            }
                        }
                    }
                    if !error.isEmpty { Text(error).foregroundStyle(TigeroseTheme.error) }
                }.padding(18)
            } else if !error.isEmpty {
                Text(error).foregroundStyle(TigeroseTheme.error).padding()
                Button("重试") { Task { await refresh() } }
            } else { ProgressView().padding() }
        }
        .task(id: taskID) {
            events.subscribe(baseURL: app.api.baseURL, channel: "mesh") { type, _ in
                if type == "mesh.changed" { Task { await refresh() } }
            }
            defer { events.cancel() }
            await refresh()
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(4)) } catch { return }
                await refresh()
            }
        }
        .quickLookPreview($artifactURL)
        .confirmationDialog(cancelWorkflow ? "取消整次协作？" : "取消此任务及其下游任务？", isPresented: $confirmCancel, titleVisibility: .visible) {
            Button("确认取消", role: .destructive) { Task { await cancel() } }
            Button("保留任务", role: .cancel) {}
        } message: {
            Text("将取消 \(affectedCount) 个未结束助理任务。\(cancelWorkflow ? "整次协作的汇总也会停止。" : "")执行中的操作会请求停止，已产生的结果与历史记录保留。")
        }
    }

    @ViewBuilder
    private func detailCard<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        content()
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(TigeroseTheme.surfaceSoft, in: RoundedRectangle(cornerRadius: 8))
    }

    private func taskHeader(_ task: MeshTask) -> some View {
        detailCard {
            VStack(alignment: .leading, spacing: 10) {
                Text(task.title).font(TigeroseTheme.titleMd).lineLimit(2).textSelection(.enabled)
                HStack(spacing: 8) {
                    MeshStatusBadge(status: task.status)
                    Text(participantName(task.target_id, snapshot: task.target_snapshot))
                        .font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                        .padding(.horizontal, 8).padding(.vertical, 4)
                        .background(TigeroseTheme.secondary, in: Capsule())
                    Spacer()
                    if !task.isTerminal {
                        Button("取消任务", role: .destructive) { cancelWorkflow = false; confirmCancel = true }.disabled(cancelling)
                    }
                }
                if let objective = task.objective, !objective.isEmpty {
                    Text("任务描述").font(TigeroseTheme.caption.weight(.semibold)).foregroundStyle(TigeroseTheme.muted)
                    Text(objective).font(TigeroseTheme.bodySm).lineLimit(showDetails ? nil : 2).textSelection(.enabled)
                    if objective.contains("\n") || objective.count > 90 {
                        Button(showDetails ? "收起描述" : "展开描述") { showDetails.toggle() }
                            .buttonStyle(.plain).foregroundStyle(TigeroseTheme.primary)
                    }
                }
                if let state = task.coordinationTitle { Text(state).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.primary) }
                if let reason = task.wait_reason, !reason.isEmpty {
                    Text(["user_decision": "等待你的决定", "user_input": "等待你的回答", "permission": "等待操作授权"][reason] ?? reason)
                        .font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                }
            }
        }
    }

    private func acceptanceCard(_ task: MeshTask) -> some View {
        let criteria = acceptanceItems(task)
        let accepted = task.status == "completed"
        return detailCard {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("任务验收").font(TigeroseTheme.bodyMd.weight(.semibold))
                    Text("\(accepted ? criteria.count : 0)/\(criteria.count)").font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                    Spacer()
                }
                ForEach(Array(criteria.enumerated()), id: \.offset) { index, criterion in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: accepted ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(accepted ? TigeroseTheme.primary : TigeroseTheme.muted)
                            Text(criterion).font(TigeroseTheme.bodySm)
                            Spacer()
                            if !(task.submissions ?? []).isEmpty {
                                Button(expandedRecordIDs.contains("criterion-\(index)") ? "收起" : "详情") {
                                    if expandedRecordIDs.contains("criterion-\(index)") { expandedRecordIDs.remove("criterion-\(index)") }
                                    else { expandedRecordIDs.insert("criterion-\(index)") }
                                }.buttonStyle(.plain).foregroundStyle(TigeroseTheme.primary)
                            }
                        }
                        if expandedRecordIDs.contains("criterion-\(index)"), let submission = task.submissions?.last {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("提交版本 \(submission.revision)").font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                                Text(submission.summary).font(TigeroseTheme.bodySm).textSelection(.enabled)
                            }.padding(10).background(TigeroseTheme.canvas, in: RoundedRectangle(cornerRadius: 6))
                        }
                    }
                }
            }
        }
    }

    private func executionCard(_ task: MeshTask) -> some View {
        detailCard {
            VStack(alignment: .leading, spacing: 10) {
                Text("执行记录").font(TigeroseTheme.bodyMd.weight(.semibold))
                if messages.isEmpty { Text("暂无执行记录").font(TigeroseTheme.bodySm).foregroundStyle(TigeroseTheme.muted) }
                ForEach(messages) { message in
                    let isExpanded = expandedRecordIDs.contains(message.id)
                    Button {
                        if isExpanded { expandedRecordIDs.remove(message.id) } else { expandedRecordIDs.insert(message.id) }
                    } label: {
                        VStack(alignment: .leading, spacing: 5) {
                            HStack(spacing: 8) {
                                Text(recordTime(message.created_at)).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                                Text(name(message.sender_id)).font(TigeroseTheme.caption.weight(.semibold))
                                Text(message.kindTitle).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.primary)
                                Spacer()
                                Image(systemName: isExpanded ? "chevron.up" : "chevron.down").font(.caption).foregroundStyle(TigeroseTheme.muted)
                            }
                            Text(messageSummary(message)).font(TigeroseTheme.bodySm).lineLimit(isExpanded ? nil : 1).multilineTextAlignment(.leading)
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 7)
                    }.buttonStyle(.plain)
                    Divider()
                }
                if let usage = task.usage {
                    Text("Token：\(usage["total_tokens"]?.meshText ?? "未知") · 缓存输入：\(usage["cached_input_tokens"]?.meshText ?? "未知") · 成本：\(usage["cost"]?.numberValue.map { String(format: "¥%.4f", $0) } ?? "未知")")
                        .font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                }
            }
        }
    }

    private func acceptanceItems(_ task: MeshTask) -> [String] {
        if let values = task.acceptance_criteria?.arrayValue {
            let items = values.map(\.meshText).filter { !$0.isEmpty }
            if !items.isEmpty { return items }
        }
        let value = task.acceptance_criteria?.meshText ?? "完成任务要求"
        return value.isEmpty ? ["完成任务要求"] : value.components(separatedBy: "\n").filter { !$0.isEmpty }
    }

    private func recordTime(_ timestamp: Double?) -> String {
        guard let timestamp else { return "刚刚" }
        return Date(timeIntervalSince1970: timestamp).formatted(date: .omitted, time: .shortened)
    }

    private func messageSummary(_ message: MeshMessage) -> String {
        let text = message.body.meshText.replacingOccurrences(of: "\n", with: " ")
        return text.isEmpty ? message.kindTitle : text
    }

    private func section(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(TigeroseTheme.bodyMd.weight(.semibold))
            Text(value).font(TigeroseTheme.bodySm).textSelection(.enabled)
        }
    }

    private func name(_ id: String?) -> String { app.assistants.first { $0.template_id == id }?.name ?? id ?? "系统" }

    private func participantName(_ id: String?, snapshot: [String: JSONValue]?) -> String {
        if let assistant = app.assistants.first(where: { $0.template_id == id }) { return assistant.name }
        if let name = snapshot?["name"]?.stringValue { return "\(name)（已删除）" }
        return id ?? "系统"
    }

    private func graphSection(_ graph: MeshGraph) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("协作关系").font(TigeroseTheme.bodyMd.weight(.semibold))
                Spacer()
                Button("取消整次协作", role: .destructive) { cancelWorkflow = true; confirmCancel = true }
                    .disabled(graph.isTerminal)
            }
            Text("派发、执行、依赖与最终汇总")
                .font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
            WorkflowDAGView(
                graph: graph,
                assistantNames: Dictionary(uniqueKeysWithValues: app.assistants.map { ($0.template_id, $0.name) }),
                onSelect: onSelect
            )
            .frame(maxWidth: .infinity, minHeight: 340, maxHeight: 460)
            .clipShape(RoundedRectangle(cornerRadius: 6))
        }
    }

    private var affectedCount: Int {
        guard let graph else { return task?.isTerminal == false ? 1 : 0 }
        if cancelWorkflow { return graph.nodes.filter { !$0.isTerminal }.count }
        var ids: Set<String> = [taskID]
        var previous = 0
        while previous != ids.count {
            previous = ids.count
            for node in graph.nodes where node.kind == "task" && ids.contains(node.parent_task_id ?? "") {
                if let taskID = node.task_id { ids.insert(taskID) }
            }
        }
        return graph.nodes.filter { $0.kind == "task" && $0.task_id.map(ids.contains) == true && !$0.isTerminal }.count
    }

    private func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            let snapshot = try await app.api.meshTask(taskID)
            let page = try await app.api.meshMessages(taskID, after: messages.last?.sequence ?? 0)
            let newGraph = try await app.api.meshGraph(snapshot.workflow_id)
            guard !Task.isCancelled else { return }
            task = snapshot
            messages += page.items.filter { item in !messages.contains { $0.id == item.id } }
            graph = newGraph
            error = ""
        } catch { if !Task.isCancelled { self.error = error.localizedDescription } }
    }

    private func cancel() async {
        guard let task else { return }
        cancelling = true
        defer { cancelling = false }
        do { try await app.api.cancelMeshTask(cancelWorkflow ? task.workflow_id : taskID, workflow: cancelWorkflow); await refresh() }
        catch { self.error = error.localizedDescription }
    }

    private func openArtifact(_ artifact: MeshArtifact) async {
        do { artifactURL = try await app.api.meshArtifact(artifact.artifact_id) }
        catch { self.error = error.localizedDescription }
    }
}

struct MeshDecisionCard: View {
    @Environment(AppState.self) private var app
    let decision: MeshDecision
    var permissionDialog = false
    var onAnswered: () -> Void
    @State private var answer = ""
    @State private var amount = ""
    @State private var busy = false
    @State private var error = ""
    @State private var key = UUID().uuidString

    var body: some View {
        if let request = decision.questionRequest {
            VStack(alignment: .leading, spacing: 8) {
                if let name = decision.requester_name { Text("请求助理：\(name)").font(TigeroseTheme.bodySm).padding(.horizontal, 24) }
                if let title = decision.task_title { Text(title).font(TigeroseTheme.bodySm).padding(.horizontal, 24) }
                AskUserQuestionCard(request: request, onSubmit: { answers in
                    let data = try JSONEncoder().encode(answers)
                    let value = try JSONDecoder().decode(JSONValue.self, from: data)
                    try await app.api.meshDecide(decision, action: "answer", payload: ["answers": value], key: key)
                    onAnswered()
                }, onCancel: {
                    try await app.api.meshDecide(decision, action: "cancel", payload: [:], key: key)
                    onAnswered()
                })
            }.padding(.vertical, permissionDialog ? 24 : 0)
        } else if permissionDialog && ["permission", "budget", "limit_reached"].contains(decision.reason) {
            VStack(spacing: 0) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(decision.reason == "permission" ? "协作任务需要授权" : "协作任务需要调整额度").font(TigeroseTheme.titleMd)
                    if let name = decision.requester_name { Text("请求助理：\(name)").font(TigeroseTheme.bodySm) }
                    if let title = decision.task_title { Text(title).font(TigeroseTheme.bodySm).lineLimit(2) }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(24)
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(decision.reason == "permission" ? "仅授权本次操作，拒绝后助理将收到拒绝结果。" : "本次协作已达到额度上限。填写新额度后继续，或取消任务。")
                            .font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                        if decision.reason == "permission" {
                            decisionField("操作", "tool")
                            decisionField("操作内容", "args")
                            decisionField("说明", "detail")
                        } else {
                            decisionField("额度类型", "limit")
                            decisionField("当前额度", "current")
                            decisionField("最低所需额度", "required")
                            TextField("本次新额度", text: $amount).textFieldStyle(.roundedBorder)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(24)
                }.frame(maxHeight: .infinity)
                Divider()
                VStack(spacing: 10) {
                    if !error.isEmpty { Text(error).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.error) }
                    HStack(spacing: 12) {
                        ForEach(decision.allowed_actions, id: \.self) { action in
                            Button { Task { await submit(action) } } label: {
                                Text(actionTitle(action)).frame(maxWidth: .infinity).padding(.vertical, 6)
                            }.disabled(busy || (action == "raise_limit" && (Int(amount) ?? 0) < max(1, Int(decision.payload?["required"]?.numberValue ?? 1))))
                        }
                    }
                }.padding(20)
            }
        } else {
            cardBody.padding(permissionDialog ? 24 : 0)
        }
    }

    private var cardBody: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("需要你的决定", systemImage: "questionmark.bubble")
                .font(TigeroseTheme.bodyMd.weight(.semibold))
            Text(["permission": "此操作需要你的授权", "user_input": "助理需要补充信息", "user_decision": "协作任务需要你决定如何继续", "budget": "本次协作达到预算上限", "limit_reached": "本次协作达到限额", "reconciliation": "请核对操作是否已发生"][decision.reason] ?? "协作任务等待处理").font(TigeroseTheme.bodySm)
            if let title = decision.task_title { Text(title).font(TigeroseTheme.bodySm) }
            if decision.reason == "user_decision" || decision.reason == "reconciliation" {
                Text(decision.explanation).font(TigeroseTheme.bodySm).textSelection(.enabled)
            }
            if decision.reason == "permission" {
                decisionField("操作", "tool")
                decisionField("操作内容", "args")
                decisionField("说明", "detail")
            }
            if decision.reason == "user_input" { decisionField("问题", "questions") }
            if !decision.isInterruptedTurn && decision.allowed_actions.contains(where: { ["answer", "confirm_success", "retry"].contains($0) }) {
                TextField("答复或确认依据", text: $answer, axis: .vertical).textFieldStyle(.roundedBorder)
            }
            if decision.allowed_actions.contains("raise_limit") {
                decisionField("额度类型", "limit")
                decisionField("当前额度", "current")
                decisionField("最低所需额度", "required")
                TextField("本次新额度", text: $amount).textFieldStyle(.roundedBorder)
            }
            ForEach(decision.allowed_actions, id: \.self) { action in
                Button(actionTitle(action)) { Task { await submit(action) } }
                    .disabled(busy || (["answer", "confirm_success", "retry"].contains(action) && !decision.isInterruptedTurn && answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) || (action == "raise_limit" && (Int(amount) ?? 0) <= 0))
            }
            if !error.isEmpty { Text(error).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.error) }
        }
        .padding(14).background(TigeroseTheme.secondary, in: RoundedRectangle(cornerRadius: 10))
    }

    private func actionTitle(_ action: String) -> String {
        if action == "answer" && decision.isInterruptedTurn { return "继续处理" }
        return ["answer": "提交答复", "cancel": "取消任务", "raise_limit": "提高本次额度", "confirm_success": "确认已成功", "retry": "确认未发生并重试", "approve": "允许操作", "deny": "拒绝操作"][action] ?? action
    }

    @ViewBuilder
    private func decisionField(_ title: String, _ key: String) -> some View {
        if let value = decision.payload?[key], !value.meshText.isEmpty {
            Text("\(title)：\(decisionValue(value))")
                .font(TigeroseTheme.caption).textSelection(.enabled)
        }
    }

    private func decisionValue(_ value: JSONValue) -> String {
        if let number = value.numberValue { return String(Int(number.rounded())) }
        return value.meshText
    }

    private func submit(_ action: String) async {
        busy = true
        defer { busy = false }
        do {
            var payload: [String: JSONValue] = [:]
            if ["answer", "confirm_success", "retry"].contains(action) {
                let text = decision.isInterruptedTurn && action == "answer"
                    ? "继续处理原任务，完成尚未完成的提交、验收与汇总；保留已有结果，不重复已完成操作。"
                    : answer.trimmingCharacters(in: .whitespacesAndNewlines)
                payload = ["answer": .string(text), "evidence": .string(text)]
            }
            if action == "raise_limit", let value = Int(amount) { payload["value"] = .number(Double(value)) }
            try await app.api.meshDecide(decision, action: action, payload: payload, key: key)
            error = ""; onAnswered()
        } catch { self.error = error.localizedDescription }
    }
}
