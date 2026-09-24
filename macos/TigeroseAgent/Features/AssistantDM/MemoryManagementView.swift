import SwiftUI

struct MemoryManagementView: View {
    @Environment(AppState.self) private var app
    let templateId: String
    @State private var memories: [AssistantMemory] = []
    @State private var candidates: [MemoryCandidate] = []
    @State private var jobs: [MemoryJob] = []
    @State private var groups: [ProjectGroup] = []
    @State private var search = ""
    @State private var typeFilter = "all"
    @State private var scopeFilter = "all"
    @State private var section = "active"
    @State private var busy = false
    @State private var error = ""
    @State private var editor: AssistantMemory?
    @State private var adding = false
    @State private var editBody = ""
    @State private var editSummary = ""
    @State private var editType = "project"
    @State private var editScope = "assistant"
    @State private var editScopeId = ""
    @State private var revisionMemory: AssistantMemory?
    @State private var revisions: [MemoryRevision] = []
    @State private var importOpen = false
    @State private var importPath = ""
    @State private var importPreview: MemoryImportPreview?
    @State private var organizeOpen = false
    @State private var pendingDelete: AssistantMemory?

    private let types = ["user", "feedback", "project", "reference", "unknown"]
    private func typeName(_ value: String) -> String {
        ["user": "用户", "feedback": "反馈", "project": "项目", "reference": "资源引用", "unknown": "未分类"][value] ?? value
    }
    private func scopeName(_ kind: String?, _ id: String?) -> String {
        guard kind == "group" else { return "此助理" }
        return groups.first(where: { $0.group_id == id })?.name ?? "群 \(id ?? "")"
    }
    private func dateName(_ ts: Double?) -> String {
        guard let ts else { return "" }
        return Date(timeIntervalSince1970: ts).formatted(date: .abbreviated, time: .shortened)
    }
    private var filtered: [AssistantMemory] {
        memories.filter {
            (typeFilter == "all" || $0.type == typeFilter)
            && (scopeFilter == "all" || $0.scope_kind == scopeFilter)
            && (search.isEmpty || $0.body.localizedCaseInsensitiveContains(search)
                || ($0.summary ?? "").localizedCaseInsensitiveContains(search))
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                TigeroseSegmentedPicker(label: "视图", selection: $section, options: [
                    ("active", "记忆"),
                    ("candidates", "待确认 \(candidates.filter { $0.status == "pending" }.count)"),
                    ("jobs", "后台任务"),
                    ("deleted", "已删除")
                ])
                Button { Task { await reload() } } label: { Image(systemName: "arrow.clockwise") }.help("刷新记忆")
                if busy { ProgressView().controlSize(.small) }
            }
            if section == "active" || section == "deleted" {
                HStack {
                    TextField("搜索记忆", text: $search).textFieldStyle(TigeroseTextFieldStyle())
                    Picker("类型", selection: $typeFilter) {
                        Text("全部类型").tag("all")
                        ForEach(types, id: \.self) { Text(typeName($0)).tag($0) }
                    }.labelsHidden().frame(width: 100)
                    Picker("范围", selection: $scopeFilter) {
                        Text("全部范围").tag("all")
                        Text("此助理").tag("assistant")
                        Text("群").tag("group")
                    }.labelsHidden().frame(width: 90)
                    if section == "active" {
                        Button { beginAdd() } label: { Image(systemName: "plus") }.help("添加记忆")
                        Menu {
                            Button("整理记忆…") { editScope = "assistant"; editScopeId = templateId; organizeOpen = true }
                            Button("从 CLI 导入…") {
                                importPreview = nil; importPath = ""; editScope = "assistant"; editScopeId = templateId; importOpen = true
                            }
                        } label: { Image(systemName: "ellipsis") }.menuStyle(.borderlessButton).frame(width: 22).help("记忆操作")
                    }
                }
                if filtered.isEmpty {
                    ContentUnavailableView("暂无记忆", systemImage: "brain")
                } else {
                    List(filtered) { memory in memoryRow(memory).listRowBackground(TigeroseTheme.canvas) }
                        .scrollContentBackground(.hidden)
                }
            } else if section == "candidates" {
                if candidates.isEmpty { ContentUnavailableView("暂无候选", systemImage: "checkmark.bubble") }
                else {
                    List(candidates) { candidate in candidateRow(candidate).listRowBackground(TigeroseTheme.canvas) }
                        .scrollContentBackground(.hidden)
                }
            } else {
                if jobs.isEmpty { ContentUnavailableView("暂无后台任务", systemImage: "clock") }
                else { List(jobs) { job in jobRow(job).listRowBackground(TigeroseTheme.canvas) }.scrollContentBackground(.hidden) }
            }
            if !error.isEmpty { Text(error).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.error).textSelection(.enabled) }
        }
        .padding(TigeroseTheme.spaceMd)
        .disabled(busy)
        .task { await reload() }
        .onChange(of: section) { _, _ in Task { await reload() } }
        .sheet(item: $editor) { _ in editorSheet }
        .sheet(isPresented: $adding) { editorSheet }
        .sheet(item: $revisionMemory) { memory in revisionSheet(memory) }
        .sheet(isPresented: $importOpen) { importSheet }
        .sheet(isPresented: $organizeOpen) { organizeSheet }
        .confirmationDialog("删除这条记忆？", isPresented: Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } })) {
            if let memory = pendingDelete {
                Button("删除记忆", role: .destructive) {
                    Task { await perform { try await app.api.deleteMemory(templateId, memoryId: memory.id, expectedVersion: memory.version) } }
                }
            }
        }
    }

    private func memoryRow(_ memory: AssistantMemory) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if let summary = memory.summary, !summary.isEmpty { Text(summary).fontWeight(.medium) }
            Text(memory.body).textSelection(.enabled)
            HStack(spacing: 8) {
                Text(typeName(memory.type ?? "project"))
                Text(scopeName(memory.scope_kind, memory.scope_id))
                Text(dateName(memory.updated_at ?? memory.ts))
                Text("v\(memory.version ?? 1)")
                Spacer()
                if section == "active" {
                    Button { beginEdit(memory) } label: { Image(systemName: "pencil") }.help("编辑记忆")
                    Button(role: .destructive) { pendingDelete = memory } label: { Image(systemName: "trash") }.help("删除记忆")
                }
                Button {
                    Task {
                        await perform {
                            revisions = try await app.api.memoryRevisions(templateId, memoryId: memory.id)
                            revisionMemory = memory
                        }
                    }
                } label: { Image(systemName: "clock.arrow.circlepath") }.help("查看修订与恢复")
            }.font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
            DisclosureGroup("来源 · \(memory.sourceLabel)") { sources(memory.source_refs) }
                .font(TigeroseTheme.caption)
        }.padding(.vertical, 4)
    }

    private func sources(_ refs: [[String: JSONValue]]?) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array((refs ?? []).enumerated()), id: \.offset) { _, ref in
                ForEach(ref.keys.sorted(), id: \.self) { key in
                    Text("\(key): \(jsonLabel(ref[key]))").textSelection(.enabled)
                }
            }
            if (refs ?? []).isEmpty { Text("无来源记录").foregroundStyle(TigeroseTheme.muted) }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    private func jsonLabel(_ value: JSONValue?) -> String {
        guard let value else { return "" }
        if let text = value.stringValue { return text }
        guard let data = try? JSONEncoder().encode(value) else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }

    private func candidateRow(_ candidate: MemoryCandidate) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(["add": "新增", "update": "更新", "archive": "归档"][candidate.intent ?? "add"] ?? "建议").fontWeight(.medium)
                Text(typeName(candidate.type ?? "project"))
                Text(scopeName(candidate.scope_kind, candidate.scope_id))
                Spacer()
                Text(statusName(candidate.status))
            }.font(TigeroseTheme.caption)
            if let before = candidate.before_body, !before.isEmpty {
                Text("当前内容 · v\(candidate.target_version ?? 1)").font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                Text(before).textSelection(.enabled)
                Divider()
            }
            Text("建议内容").font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
            Text(candidate.body).textSelection(.enabled)
            DisclosureGroup("来源") { sources(candidate.source_refs) }.font(TigeroseTheme.caption)
            if candidate.status == "pending" {
                HStack {
                    Spacer()
                    Button("拒绝") { resolve(candidate, confirm: false) }
                    Button("确认\(candidate.intent == "archive" ? "归档" : "保存")") { resolve(candidate, confirm: true) }
                        .buttonStyle(TigerosePrimaryButtonStyle())
                }
            }
        }.padding(.vertical, 6)
    }

    private func statusName(_ status: String) -> String {
        ["pending": "待确认", "queued": "排队中", "running": "处理中", "completed": "已完成", "succeeded": "已完成",
         "failed": "失败", "conflict": "内容冲突", "cancelled": "已取消", "canceled": "已取消",
         "confirmed": "已确认", "dismissed": "已拒绝", "stale": "已失效"][status] ?? status
    }

    private func jobRow(_ job: MemoryJob) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(["extract": "记忆提取", "extraction": "记忆提取", "organize": "记忆整理"][job.kind] ?? job.kind)
                Spacer()
                Text(job.status == "pending" ? "排队中" : statusName(job.status)).foregroundStyle(TigeroseTheme.muted)
                if job.status == "failed" {
                    Button { Task { await perform { try await app.api.retryMemoryJob(templateId, jobId: job.id) } } }
                    label: { Image(systemName: "arrow.clockwise") }.help("重试任务")
                }
            }
            if let reason = job.error, !reason.isEmpty { Text(reason).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.error) }
        }.padding(.vertical, 4)
    }

    private var scopePicker: some View {
        HStack {
            Picker("范围", selection: $editScope) {
                Text("此助理").tag("assistant")
                Text("群").tag("group")
            }.frame(width: 180)
            if editScope == "group" {
                Picker("群", selection: $editScopeId) {
                    Text("选择群").tag("")
                    ForEach(groups) { Text($0.name).tag($0.group_id) }
                }
            }
        }.onChange(of: editScope) { _, value in editScopeId = value == "assistant" ? templateId : "" }
    }

    private var editorSheet: some View {
        VStack(alignment: .leading, spacing: 12) {
            TigeroseSheetHeader(title: adding ? "添加记忆" : "编辑记忆", onClose: { editor = nil; adding = false })
            Picker("类型", selection: $editType) { ForEach(types, id: \.self) { Text(typeName($0)).tag($0) } }
            if adding { scopePicker }
            else { Text(scopeName(editor?.scope_kind, editor?.scope_id)).foregroundStyle(TigeroseTheme.muted) }
            TextField("简述", text: $editSummary).textFieldStyle(TigeroseTextFieldStyle())
            TextEditor(text: $editBody).font(TigeroseTheme.bodyMd).frame(minHeight: 170)
            if !error.isEmpty { Text(error).foregroundStyle(TigeroseTheme.error).font(TigeroseTheme.caption) }
            HStack {
                Spacer()
                Button("取消") { editor = nil; adding = false }
                Button("保存") { Task { await saveEditor() } }.buttonStyle(TigerosePrimaryButtonStyle())
                    .disabled(busy || editBody.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || (adding && editScopeId.isEmpty))
            }
        }.padding(16).frame(width: 540, height: 410)
    }

    private func revisionSheet(_ memory: AssistantMemory) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            TigeroseSheetHeader(title: "修订记录 · 当前 v\(memory.version ?? 1)", onClose: { revisionMemory = nil })
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(revisions.sorted { $0.version > $1.version }) { revision in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text("v\(revision.version) · \(dateName(revision.ts))")
                                Text(revision.actor ?? "").foregroundStyle(TigeroseTheme.muted)
                                Spacer()
                            }.font(TigeroseTheme.caption)
                            Text(revision.after.summary ?? "").fontWeight(.medium)
                            Text(revision.after.body).textSelection(.enabled)
                            Text("\(typeName(revision.after.type ?? "project")) · \(scopeName(revision.after.scope_kind, revision.after.scope_id))")
                                .font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                            if revision.after.status != "deleted" {
                                Button("恢复此版本") {
                                    Task {
                                        await perform {
                                            try await app.api.restoreMemory(templateId, memoryId: memory.id, revision: revision.version, expectedVersion: memory.version ?? 1)
                                            revisionMemory = nil
                                        }
                                    }
                                }.disabled(busy)
                            } else { Text("已删除").foregroundStyle(TigeroseTheme.muted) }
                        }
                        Divider()
                    }
                }
            }
            if !error.isEmpty { Text(error).foregroundStyle(TigeroseTheme.error).font(TigeroseTheme.caption) }
        }.padding(16).frame(width: 560, height: 460)
    }

    private var importSheet: some View {
        VStack(alignment: .leading, spacing: 12) {
            TigeroseSheetHeader(title: "从 CLI 导入记忆", onClose: { importOpen = false })
            if let preview = importPreview {
                Text("待导入 \(preview.entries.count) 条 · \(scopeName(editScope, editScopeId))")
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(Array(preview.entries.enumerated()), id: \.offset) { _, entry in
                            Text(entry.summary ?? typeName(entry.type ?? "project")).fontWeight(.medium)
                            Text(entry.body).textSelection(.enabled)
                            Text(entry.source_path ?? "").font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                            Divider()
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                HStack {
                    Button("返回") { importPreview = nil }
                    Spacer()
                    Button("确认导入") {
                        Task {
                            await perform {
                                try await app.api.confirmMemoryImport(templateId, previewId: preview.id)
                                importOpen = false
                            }
                        }
                    }.buttonStyle(TigerosePrimaryButtonStyle()).disabled(busy || preview.entries.isEmpty)
                }
            } else {
                TextField("CLI 记忆目录绝对路径", text: $importPath).textFieldStyle(TigeroseTextFieldStyle())
                scopePicker
                Spacer()
                HStack {
                    Spacer()
                    Button("预览") {
                        Task {
                            await perform {
                                importPreview = try await app.api.previewMemoryImport(templateId, path: importPath, scopeKind: editScope, scopeId: editScopeId)
                            }
                        }
                    }.buttonStyle(TigerosePrimaryButtonStyle()).disabled(busy || importPath.isEmpty || editScopeId.isEmpty)
                }
            }
            if !error.isEmpty { Text(error).foregroundStyle(TigeroseTheme.error).font(TigeroseTheme.caption) }
        }.padding(16).frame(width: 560, height: 440)
    }

    private var organizeSheet: some View {
        VStack(alignment: .leading, spacing: 12) {
            TigeroseSheetHeader(title: "整理记忆", onClose: { organizeOpen = false })
            scopePicker
            let selected = memories.filter { $0.status != "deleted" && ($0.scope_kind ?? "assistant") == editScope && ($0.scope_id ?? templateId) == editScopeId }
            Text("选定范围：\(selected.count) 条记忆")
            List(selected) { memory in Text(memory.summary?.isEmpty == false ? memory.summary! : memory.body).lineLimit(3) }
            if !error.isEmpty { Text(error).foregroundStyle(TigeroseTheme.error).font(TigeroseTheme.caption) }
            HStack {
                Spacer()
                Button("生成整理建议") {
                    Task {
                        await perform {
                            try await app.api.organizeMemories(templateId, scopeKind: editScope, scopeId: editScopeId, memoryIds: selected.map(\.id))
                            organizeOpen = false
                            section = "jobs"
                        }
                    }
                }.buttonStyle(TigerosePrimaryButtonStyle()).disabled(busy || selected.isEmpty)
            }
        }.padding(16).frame(width: 540, height: 380)
    }

    private func beginAdd() {
        error = ""; editBody = ""; editSummary = ""; editType = "project"
        editScope = "assistant"; editScopeId = templateId; adding = true
    }
    private func beginEdit(_ memory: AssistantMemory) {
        error = ""; editBody = memory.body; editSummary = memory.summary ?? ""
        editType = memory.type ?? "project"; editor = memory
    }
    private func saveEditor() async {
        let body = editBody.trimmingCharacters(in: .whitespacesAndNewlines)
        await perform {
            if let memory = editor {
                _ = try await app.api.updateMemory(templateId, memoryId: memory.id, body: body,
                                                  expectedVersion: memory.version, type: editType, summary: editSummary)
            } else {
                try await app.api.confirmMemory(templateId, body: body, source: "manual", type: editType,
                                                summary: editSummary, scopeKind: editScope, scopeId: editScopeId)
            }
            editor = nil; adding = false
        }
    }
    private func resolve(_ candidate: MemoryCandidate, confirm: Bool) {
        Task { await perform { try await app.api.resolveMemoryCandidate(templateId, candidateId: candidate.id, confirm: confirm) } }
    }
    private func perform(_ operation: () async throws -> Void) async {
        guard !busy else { return }
        busy = true; error = ""
        do { try await operation() }
        catch APIError.badStatus(409, _) { error = "记忆已更新或候选已失效。请刷新后核对最新内容，再重试。" }
        catch { self.error = error.localizedDescription }
        busy = false
        await reload(clearError: false)
    }
    private func reload(clearError: Bool = true) async {
        guard !busy else { return }
        busy = true
        if clearError { error = "" }
        defer { busy = false }
        do {
            memories = try await app.api.listMemories(templateId, status: section == "deleted" ? "deleted" : "active")
            candidates = try await app.api.memoryCandidates(templateId)
            jobs = try await app.api.memoryJobs(templateId)
            groups = try await app.api.listGroups()
        } catch { self.error = error.localizedDescription }
    }
}
