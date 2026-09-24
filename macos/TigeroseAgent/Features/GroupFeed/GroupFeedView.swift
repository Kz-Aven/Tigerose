import SwiftUI
import AppKit

struct GroupFeedView: View {
    @Environment(AppState.self) private var app
    let groupId: String

    @State private var detail: ProjectGroup?
    @State private var events: [FeedEvent] = []
    @State private var draft = ""
    @State private var error = ""
    @State private var showSettings = false
    @State private var attachments: [Attachment] = []
    @State private var sse = SSEClient()
    @State private var mentionFilter = ""
    @State private var columnWidth: CGFloat = 0
    @State private var liveTraces: [String: LiveTrace] = [:]
    @State private var activeRunIds: Set<String> = []
    @State private var webEnabled = false
    @State private var pendingPermission: PermissionRequest?
    @State private var pendingQuestions: [PendingQuestion] = []
    @State private var dismissedFeedProposals: Set<String> = []
    @State private var confirmedFeedProposals: Set<String> = []
    @State private var confirmClear = false
    @State private var confirmDeleteGroup = false

    private struct LiveTrace: Identifiable {
        var id: String { instanceId }
        var instanceId: String
        var displayName: String
        var steps: [ThinkingStep]
        var durationMs: Int
        var running: Bool
    }

    private var group: ProjectGroup? {
        detail ?? app.groups.first { $0.group_id == groupId }
    }

    private var members: [GroupMember] {
        detail?.members ?? []
    }

    private var bubbleMaxWidth: CGFloat {
        // Ignore tiny/zero probes so bubbles never collapse to a 1-glyph column.
        let column = columnWidth >= 320 ? columnWidth : TigeroseTheme.bubbleMaxWidth
        return column * TigeroseTheme.bubbleMaxWidthFraction
    }

    private var queryMarkers: [ChatQueryMarker] {
        events.compactMap { event in
            guard event.speaker_type == "user" else { return nil }
            let query = event.content.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !query.isEmpty else { return nil }
            return ChatQueryMarker(id: event.event_id, query: query)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            ScrollViewReader { proxy in
                ScrollView {
                    // VStack (not LazyVStack): variable-height bubbles need accurate layout.
                    VStack(alignment: .leading, spacing: TigeroseTheme.spaceLg) {
                    // Full-width probe for column size (non-zero height so GeometryReader is reliable).
                    Color.clear
                        .frame(height: 1)
                        .frame(maxWidth: .infinity)
                        .reportChatColumnWidth()

                        ForEach(events) { e in
                            feedRow(e).id(e.event_id)
                        }
                        ForEach(Array(liveTraces.values).sorted(by: { $0.displayName < $1.displayName })) { live in
                            HStack(alignment: .top, spacing: TigeroseTheme.spaceXs) {
                                AvatarView(
                                    name: live.displayName,
                                    colorHex: members.first(where: { $0.instance_id == live.instanceId })?.theme_color ?? "#5db8a6",
                                    avatarID: members.first(where: { $0.instance_id == live.instanceId })?.avatar_id ?? ""
                                )
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(live.displayName)
                                        .font(TigeroseTheme.caption)
                                        .foregroundStyle(TigeroseTheme.muted)
                                    ThinkingChainView(
                                        steps: live.steps,
                                        durationMs: live.durationMs,
                                        isRunning: live.running,
                                        maxWidth: bubbleMaxWidth
                                    )
                                }
                                Spacer(minLength: 48)
                            }
                            .id("live-\(live.instanceId)")
                        }
                        Color.clear
                            .frame(height: 12)
                            .id("scroll-bottom")
                    }
                    .padding(.leading, TigeroseTheme.spaceMd)
                    .padding(.vertical, TigeroseTheme.spaceMd)
                    .padding(.trailing, 68)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .background(TigeroseTheme.canvas)
                .onChatColumnWidthChange { columnWidth = $0 }
                .onChange(of: events.count) { _, _ in
                    scrollChatToBottom(proxy)
                }
                .onChange(of: liveTraces.count) { _, _ in
                    scrollChatToBottom(proxy)
                }
                .overlay(alignment: .trailing) {
                    ChatQueryMarkers(markers: queryMarkers) { eventID in
                        withAnimation(.easeOut(duration: 0.22)) {
                            proxy.scrollTo(eventID, anchor: .center)
                        }
                    }
                    .padding(.trailing, 4)
                }
            }
            mentionBar
            if !error.isEmpty {
                Text(error)
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.error)
                    .padding(.horizontal)
            }
            if let pending = pendingPermission {
                PermissionBanner(
                    request: pending,
                    onDecide: { decision in
                        Task { await resolvePermission(pending, decision: decision) }
                    }
                )
            }
            ForEach(pendingQuestions) { pending in
                AskUserQuestionCard(
                    request: pending,
                    onSubmit: { answers in
                        try await answerQuestion(pending, answers: answers)
                    },
                    onCancel: {
                        try await cancelQuestion(pending)
                    }
                )
            }
            if !attachments.isEmpty {
                pendingAttachmentBar
            }
            ComposerBar(
                text: $draft,
                placeholder: "消息需包含 @成员",
                canSend: draft.contains("@"),
                onSend: { Task { await send() } },
                isRunning: !activeRunIds.isEmpty || liveTraces.values.contains { $0.running },
                onStop: {
                    Task { await cancelActiveRuns() }
                },
                onAttach: { pickFiles() },
                hasAttachments: !attachments.isEmpty,
                onAttachFiles: { urls in
                    Task { await uploadFiles(urls) }
                },
                webEnabled: $webEnabled,
                onWebEnabledChange: { enabled in
                    Task { await persistWebEnabled(enabled) }
                }
            )
            .fixedSize(horizontal: false, vertical: true)
            .layoutPriority(1)
        }
        .background(TigeroseTheme.canvas)
        .task(id: groupId) { await load() }
        .sheet(isPresented: $showSettings) {
            GroupSettingsSheet(groupId: groupId, onChanged: { Task { await load(); try? await app.refreshLists() } })
        }
        .confirmationDialog("清空后无法恢复，确定清空当前对话？", isPresented: $confirmClear, titleVisibility: .visible) {
            Button("清空对话", role: .destructive) { Task { await clearFeed() } }
            Button("取消", role: .cancel) {}
        }
        .confirmationDialog("将删除该项目群及其全部内容，确定删除？", isPresented: $confirmDeleteGroup, titleVisibility: .visible) {
            Button("删除项目群", role: .destructive) { Task { await deleteGroup() } }
            Button("取消", role: .cancel) {}
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 10) {
            GroupAvatarView(
                members: members,
                fallbackName: group?.name ?? "?",
                size: 32
            )
            VStack(alignment: .leading, spacing: 2) {
                Text(group?.name ?? "项目群")
                    .font(TigeroseTheme.titleMd)
                    .foregroundStyle(TigeroseTheme.ink)
                    .lineLimit(1)
                    .hoverFullText(group?.name ?? "项目群")
                Text("\(members.count) 成员")
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(TigeroseTheme.muted)
            }
            .frame(minHeight: 32, alignment: .center)
            Spacer(minLength: 8)
            Menu {
                Button("群设置…") { showSettings = true }
                Divider()
                Button("清空对话…", role: .destructive) { confirmClear = true }
                Button("删除项目群…", role: .destructive) { confirmDeleteGroup = true }
            } label: {
                Image(systemName: "gearshape")
                    .foregroundStyle(TigeroseTheme.muted)
                    .frame(width: 28, height: 28)
            }
            .menuStyle(.borderlessButton)
            .help("群设置 / 清空 / 删除")
        }
        .padding(TigeroseTheme.spaceMd)
        .background(TigeroseTheme.canvas)
        .overlay(alignment: .bottom) {
            Rectangle().fill(TigeroseTheme.hairline).frame(height: 1)
        }
    }

    private var mentionBar: some View {
        HStack(alignment: .center, spacing: TigeroseTheme.spaceSm) {
            Text("收件人")
                .font(TigeroseTheme.caption)
                .foregroundStyle(TigeroseTheme.muted)
            ScrollView(.horizontal) {
                HStack(spacing: TigeroseTheme.spaceXs) {
                ForEach(members) { m in
                    Button("@\(m.display_name)") {
                        toggleMention(m)
                    }
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(draft.contains("@\(m.display_name)") ? TigeroseTheme.primary : TigeroseTheme.ink)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(
                        draft.contains("@\(m.display_name)") ? TigeroseTheme.surfaceCreamStrong : TigeroseTheme.surfaceCard,
                        in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm)
                            .stroke(TigeroseTheme.hairline, lineWidth: 1)
                    )
                    .buttonStyle(.plain)
                }
                }
                .padding(.vertical, 4)
            }
            .padding(.horizontal, TigeroseTheme.spaceMd)
        }
        .padding(.horizontal, TigeroseTheme.spaceMd)
        .padding(.vertical, TigeroseTheme.spaceXs)
        .background(TigeroseTheme.surfaceSoft)
        .overlay(alignment: .bottom) {
            Rectangle().fill(TigeroseTheme.hairlineSoft).frame(height: 1)
        }
    }

    private func toggleMention(_ member: GroupMember) {
        let mention = "@\(member.display_name)"
        if draft.contains(mention) {
            draft = draft
                .replacingOccurrences(of: "\(mention) ", with: "")
                .replacingOccurrences(of: mention, with: "")
                .replacingOccurrences(of: "  ", with: " ")
                .trimmingCharacters(in: .whitespaces)
        } else if draft.isEmpty {
            draft = "\(mention) "
        } else if draft.hasSuffix(" ") {
            draft += "\(mention) "
        } else {
            draft += " \(mention) "
        }
    }

    private var pendingAttachmentBar: some View {
        ScrollView(.horizontal) {
            HStack(spacing: TigeroseTheme.spaceXs) {
                ForEach(Array(attachments.enumerated()), id: \.offset) { index, attachment in
                    HStack(spacing: TigeroseTheme.spaceXs) {
                        Text(attachment.name)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Button {
                            attachments.remove(at: index)
                        } label: {
                            Image(systemName: "xmark")
                                .font(.system(size: 9, weight: .bold))
                                .frame(width: 16, height: 16)
                                .contentShape(Circle())
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(TigeroseTheme.muted)
                        .help("取消上传")
                    }
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(TigeroseTheme.ink)
                    .padding(.leading, 10)
                    .padding(.trailing, 5)
                    .padding(.vertical, 6)
                    .background(TigeroseTheme.surfaceCard, in: Capsule())
                    .overlay(Capsule().stroke(TigeroseTheme.hairline, lineWidth: 1))
                }
            }
            .padding(.horizontal, TigeroseTheme.spaceMd)
            .padding(.vertical, 8)
        }
        .background(TigeroseTheme.surfaceSoft)
    }

    @ViewBuilder
    private func feedRow(_ e: FeedEvent) -> some View {
        let isUser = e.speaker_type == "user"
        let displayedAttachments = isUser ? e.attachments : LocalFileAttachmentCards.includingImages(in: e.content, attachments: e.attachments)
        let member = members.first { $0.instance_id == e.speaker_id }
        let name: String = {
            if isUser { return "你" }
            return member?.display_name ?? e.speaker_id
        }()
        let color = member?.theme_color ?? "#5db8a6"
        let modelLabel: String = {
            guard !isUser else { return "" }
            if let label = member?.model_label, !label.isEmpty { return label }
            if let mid = member?.model_profile_id, !mid.isEmpty { return mid }
            // Fallback: template from app list
            if let tid = member?.template_id,
               let tpl = app.assistants.first(where: { $0.template_id == tid }),
               let mid = tpl.model_profile_id, !mid.isEmpty
            {
                return mid
            }
            return ""
        }()
        HStack(alignment: .top, spacing: TigeroseTheme.spaceXs) {
            if isUser { Spacer(minLength: 48) }
            if !isUser {
                AvatarView(name: name, colorHex: color, avatarID: member?.avatar_id ?? "")
            }
            VStack(alignment: isUser ? .trailing : .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(name)
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.muted)
                    Text(ChatMessageTimestamp.format(e.ts))
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.muted)
                    if !modelLabel.isEmpty {
                        Text("·")
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(TigeroseTheme.mutedSoft)
                        Text(modelLabel)
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(TigeroseTheme.mutedSoft)
                            .lineLimit(1)
                            .hoverFullText(modelLabel)
                    }
                }
                if !isUser, let thinking = ThinkingMeta.from(meta: e.meta) {
                    ThinkingChainView(
                        steps: thinking.steps,
                        durationMs: thinking.duration_ms,
                        isRunning: false,
                        budgetSummary: thinking.budgetSummary,
                        runOutcome: thinking.runOutcome,
                        executionStatus: thinking.executionStatus,
                        evidenceStatus: thinking.evidenceStatus,
                        resultVerdict: thinking.resultVerdict,
                        maxWidth: bubbleMaxWidth
                    )
                }
                if !isUser, let ask = AskUserSnapshot.from(meta: e.meta) {
                    AskUserTranscriptCard(snapshot: ask, maxWidth: bubbleMaxWidth)
                } else if !e.displayContent.isEmpty {
                    MessageBubble(
                        content: e.displayContent,
                        isUser: isUser,
                        renderMarkdown: true,
                        themeHex: color,
                        maxWidth: bubbleMaxWidth
                    )
                }
                if !displayedAttachments.isEmpty {
                    LocalFileAttachmentCards(
                        attachments: displayedAttachments,
                        maxWidth: bubbleMaxWidth
                    )
                }
                if !isUser,
                   let proposal = RememberProposalParser.parse(meta: e.meta),
                   !dismissedFeedProposals.contains(e.event_id)
                {
                    RememberProposalCard(
                        proposal: proposal,
                        maxWidth: bubbleMaxWidth,
                        onIgnore: {
                            Task { await dismissFeedProposal(e) }
                        },
                        onConfirm: {
                            Task { await confirmFeedProposal(e, proposal: proposal) }
                        }
                    )
                } else if !isUser,
                          RememberProposalParser.isConfirmed(meta: e.meta)
                            || confirmedFeedProposals.contains(e.event_id)
                {
                    Text("已写入记忆")
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.muted)
                }
            }
            if !isUser { Spacer(minLength: 48) }
        }
        .frame(maxWidth: .infinity, alignment: isUser ? .trailing : .leading)
    }

    private func templateIdForFeedEvent(_ e: FeedEvent) -> String? {
        if let tid = e.meta?["template_id"]?.stringValue, !tid.isEmpty {
            return tid
        }
        return members.first(where: { $0.instance_id == e.speaker_id })?.template_id
    }

    private func confirmFeedProposal(_ e: FeedEvent, proposal: RememberProposalData) async {
        guard let tid = templateIdForFeedEvent(e) else {
            error = "无法确定助理，无法写入记忆"
            return
        }
        var groups = proposal.sourceGroups
        if groups.isEmpty { groups = [groupId] }
        do {
            try await app.api.confirmMemory(
                tid,
                body: proposal.body,
                sourceGroups: groups,
                source: "confirm",
                eventId: e.event_id,
                groupId: groupId
            )
            confirmedFeedProposals.insert(e.event_id)
            patchLocalFeedMeta(e.event_id, key: "remember_confirmed", value: true)
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func dismissFeedProposal(_ e: FeedEvent) async {
        guard let tid = templateIdForFeedEvent(e) else {
            dismissedFeedProposals.insert(e.event_id)
            return
        }
        do {
            try await app.api.dismissMemory(
                tid,
                eventId: e.event_id,
                groupId: groupId
            )
            dismissedFeedProposals.insert(e.event_id)
            patchLocalFeedMeta(e.event_id, key: "remember_ignored", value: true)
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func patchLocalFeedMeta(_ eventId: String, key: String, value: Bool) {
        guard let idx = events.firstIndex(where: { $0.event_id == eventId }) else { return }
        var meta = events[idx].meta ?? [:]
        meta[key] = .bool(value)
        events[idx].meta = meta
    }

    private func load() async {
        error = ""
        sse.cancel()
        liveTraces = [:]
        pendingPermission = nil
        pendingQuestions = []
        do {
            detail = try await app.api.getGroup(groupId)
            webEnabled = detail?.webEnabled ?? false
            let page = try await app.api.getFeed(groupId, limit: 50)
            events = page.items
        } catch {
            self.error = error.localizedDescription
        }
        do {
            pendingQuestions = try await app.api.listPendingQuestions(
                channel: AppState.groupChannel(groupId)
            )
        } catch {
            self.error = error.localizedDescription
        }
        sse.subscribe(baseURL: app.api.baseURL, channel: "group:\(groupId)") { type, data in
            Task { @MainActor in
                if type == "connected" {
                    try? await refreshPendingPermission()
                }
                if type == "feed.message" {
                    if let ev = try? JSONDecoder().decode(FeedEvent.self, from: data) {
                        if !events.contains(where: { $0.event_id == ev.event_id }) {
                            events.append(ev)
                        } else if let idx = events.firstIndex(where: { $0.event_id == ev.event_id }) {
                            events[idx] = ev
                        }
                        if ev.speaker_type == "agent" {
                            liveTraces.removeValue(forKey: ev.speaker_id)
                        }
                        app.setGroupPreview(groupId, content: ev.content)
                        try? await app.refreshLists()
                    }
                }
                if type == "feed.trace" {
                    handleFeedTrace(data)
                }
                if type == "instance.status" {
                    if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let status = obj["status"] as? String
                    {
                        let busy = status == "running"
                        if busy {
                            app.busyKey = "group:\(groupId)"
                        } else if app.busyKey == "group:\(groupId)" {
                            app.busyKey = nil
                        }
                        if !busy, let runId = obj["run_id"] as? String {
                            activeRunIds.remove(runId)
                        }
                    }
                }
                if type == "error" {
                    if app.busyKey == "group:\(groupId)" {
                        app.busyKey = nil
                    }
                    if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let message = obj["message"] as? String
                    {
                        if let runId = obj["run_id"] as? String {
                            activeRunIds.remove(runId)
                        }
                        error = message
                    }
                }
                if type == "permission_request" {
                    if let req = try? JSONDecoder().decode(PermissionRequest.self, from: data) {
                        pendingPermission = req
                    }
                }
                if type == "permission_resolved" {
                    if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let rid = obj["request_id"] as? String,
                       pendingPermission?.request_id == rid
                    {
                        pendingPermission = nil
                    }
                }
                if type == "ask_user_question" {
                    if let pending = try? JSONDecoder().decode(PendingQuestion.self, from: data),
                       pending.channel == AppState.groupChannel(groupId)
                    {
                        upsertPendingQuestion(pending)
                    } else {
                        try? await refreshPendingQuestions()
                    }
                }
                if type == "ask_user_question_resolved" {
                    if let questionId = resolvedQuestionId(from: data) {
                        pendingQuestions.removeAll { $0.question_id == questionId }
                    } else {
                        try? await refreshPendingQuestions()
                    }
                }
            }
        }
        try? await refreshPendingPermission()
    }

    private func refreshPendingPermission() async throws {
        let requests = try await app.api.listPendingPermissions(
            channel: AppState.groupChannel(groupId)
        )
        pendingPermission = requests.first
    }

    private func refreshPendingQuestions() async throws {
        pendingQuestions = try await app.api.listPendingQuestions(
            channel: AppState.groupChannel(groupId)
        )
    }

    private func upsertPendingQuestion(_ pending: PendingQuestion) {
        if let index = pendingQuestions.firstIndex(where: { $0.question_id == pending.question_id }) {
            pendingQuestions[index] = pending
        } else {
            pendingQuestions.append(pending)
        }
    }

    private func resolvedQuestionId(from data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return (object["question_id"] as? String) ?? (object["id"] as? String)
    }

    private func answerQuestion(
        _ pending: PendingQuestion,
        answers: [AskUserQuestionAnswer]
    ) async throws {
        let response = try await app.api.answerQuestion(
            questionId: pending.question_id,
            answers: answers
        )
        guard response.ok else {
            throw APIError.badStatus(-1, "提交回答失败")
        }
        pendingQuestions.removeAll { $0.question_id == pending.question_id }
        let runIds = response.runs.map(\.run_id).filter { !$0.isEmpty }
        if !runIds.isEmpty {
            activeRunIds.formUnion(runIds)
            app.busyKey = AppState.groupChannel(groupId)
        }
    }

    private func cancelQuestion(_ pending: PendingQuestion) async throws {
        let response = try await app.api.cancelQuestion(questionId: pending.question_id)
        guard response.ok else {
            throw APIError.badStatus(-1, "取消问题失败")
        }
        pendingQuestions.removeAll { $0.question_id == pending.question_id }
    }

    private func resolvePermission(_ request: PermissionRequest, decision: PermissionDecision) async {
        do {
            switch decision {
            case .deny:
                try await app.api.resolvePermission(requestId: request.request_id, approved: false)
            case .allowOnce:
                try await app.api.resolvePermission(requestId: request.request_id, approved: true, mode: "once")
            case .allowAlways:
                try await app.api.resolvePermission(requestId: request.request_id, approved: true, mode: "always")
            }
            if pendingPermission?.request_id == request.request_id {
                pendingPermission = nil
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func clearFeed() async {
        error = ""
        // Do not cancel SSE before the HTTP call — tearing down the stream first
        // can surface spurious "网络连接已中断" on the shared session.
        var clearError: Error?
        do {
            try await app.api.clearGroup(groupId)
        } catch {
            if Self.isTransientNetworkError(error) {
                // Server may already have applied the delete; retry once then verify via reload.
                do {
                    try await app.api.clearGroup(groupId)
                } catch {
                    clearError = error
                }
            } else {
                clearError = error
            }
        }
        events = []
        liveTraces = [:]
        try? await app.refreshLists()
        await load()
        if events.isEmpty {
            error = ""
        } else if let clearError {
            error = clearError.localizedDescription
        }
    }

    private func deleteGroup() async {
        error = ""
        do {
            try await app.api.deleteGroup(groupId)
            sse.cancel()
            app.surface = .empty
            try await app.refreshLists()
        } catch {
            if Self.isTransientNetworkError(error) {
                // Soft-deleted groups disappear from list; treat as success if gone.
                if let groups = try? await app.api.listGroups(),
                   !groups.contains(where: { $0.group_id == groupId })
                {
                    sse.cancel()
                    app.surface = .empty
                    try? await app.refreshLists()
                    return
                }
            }
            self.error = error.localizedDescription
        }
    }

    private static func isTransientNetworkError(_ error: Error) -> Bool {
        let urlErr = error as? URLError
        switch urlErr?.code {
        case .some(.networkConnectionLost), .some(.timedOut), .some(.notConnectedToInternet):
            return true
        default:
            return false
        }
    }

    private func handleFeedTrace(_ data: Data) {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let phase = obj["phase"] as? String,
              let instanceId = obj["instance_id"] as? String
        else { return }
        let display = (obj["display_name"] as? String)
            ?? members.first(where: { $0.instance_id == instanceId })?.display_name
            ?? instanceId
        let runId = (obj["run_id"] as? String) ?? ""
        switch phase {
        case "start":
            if !runId.isEmpty { activeRunIds.insert(runId) }
            liveTraces[instanceId] = LiveTrace(
                instanceId: instanceId,
                displayName: display,
                steps: [],
                durationMs: 0,
                running: true
            )
            app.busyKey = "group:\(groupId)"
        case "step":
            var live = liveTraces[instanceId] ?? LiveTrace(
                instanceId: instanceId,
                displayName: display,
                steps: [],
                durationMs: 0,
                running: true
            )
            live.running = true
            live.displayName = display
            if let stepObj = obj["step"],
               let stepData = try? JSONSerialization.data(withJSONObject: stepObj),
               let step = try? JSONDecoder().decode(ThinkingStep.self, from: stepData)
            {
                live.steps.append(step)
            }
            liveTraces[instanceId] = live
        case "end":
            if !runId.isEmpty { activeRunIds.remove(runId) }
            if var live = liveTraces[instanceId] {
                live.running = false
                if let ms = obj["duration_ms"] as? Int {
                    live.durationMs = ms
                } else if let ms = obj["duration_ms"] as? Double {
                    live.durationMs = Int(ms)
                }
                liveTraces[instanceId] = live
            }
        default:
            break
        }
    }

    private func scrollChatToBottom(_ proxy: ScrollViewProxy) {
        DispatchQueue.main.async {
            withAnimation(.easeOut(duration: 0.2)) {
                proxy.scrollTo("scroll-bottom", anchor: .bottom)
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) {
            withAnimation(.easeOut(duration: 0.15)) {
                proxy.scrollTo("scroll-bottom", anchor: .bottom)
            }
        }
    }

    private func persistWebEnabled(_ enabled: Bool) async {
        do {
            let g = try await app.api.setGroupWebEnabled(groupId, enabled: enabled)
            detail = g
            webEnabled = g.webEnabled
            app.upsertGroup(g)
        } catch {
            webEnabled = !enabled
            self.error = error.localizedDescription
        }
    }

    private func send() async {
        let content = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard content.contains("@") else {
            error = "群消息必须 @ 至少一位成员"
            return
        }
        draft = ""
        let atts = attachments
        attachments = []
        do {
            let ev = try await app.api.postGroupMessage(groupId, content: content, attachments: atts)
            if !events.contains(where: { $0.event_id == ev.event_id }) {
                events.append(ev)
            }
            activeRunIds.formUnion(ev.runIds)
            app.busyKey = "group:\(groupId)"
            app.setGroupPreview(groupId, content: ev.content)
            try? await app.refreshLists()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func cancelActiveRuns() async {
        let ids = activeRunIds
        for runId in ids {
            try? await app.api.cancelRun(runId)
        }
        activeRunIds.removeAll()
        for key in Array(liveTraces.keys) {
            liveTraces[key]?.running = false
        }
        app.busyKey = nil
    }

    private func pickFiles() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        guard panel.runModal() == .OK else { return }
        Task { await uploadFiles(panel.urls) }
    }

    private func uploadFiles(_ urls: [URL]) async {
        for url in urls {
            do {
                attachments.append(try await app.api.uploadFile(url: url))
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

struct GroupSettingsSheet: View {
    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss
    let groupId: String
    var onChanged: () -> Void

    @State private var name = ""
    @State private var members: [GroupMember] = []
    @State private var candidates: [AgentTemplate] = []
    @State private var error = ""

    var body: some View {
        VStack(spacing: 0) {
            TigeroseSheetHeader(title: "项目群设置", onClose: { dismiss() })
            VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
                HStack(spacing: TigeroseTheme.spaceXs) {
                    TextField("群名称", text: $name)
                        .textFieldStyle(TigeroseTextFieldStyle())
                    Button("保存") { Task { await saveName() } }
                        .buttonStyle(TigerosePrimaryButtonStyle())
                }
                Text("成员")
                    .font(TigeroseTheme.titleSm)
                    .foregroundStyle(TigeroseTheme.ink)
                List {
                    ForEach(members) { m in
                        HStack {
                            AvatarView(name: m.display_name, colorHex: m.theme_color, avatarID: m.avatar_id, size: 28)
                            Text(m.display_name)
                                .foregroundStyle(TigeroseTheme.ink)
                            Spacer()
                            Button("移除", role: .destructive) {
                                Task { await remove(m.instance_id) }
                            }
                            .foregroundStyle(TigeroseTheme.error)
                        }
                        .listRowBackground(TigeroseTheme.canvas)
                    }
                }
                .scrollContentBackground(.hidden)
                .frame(height: 160)
                Text("添加成员")
                    .font(TigeroseTheme.titleSm)
                    .foregroundStyle(TigeroseTheme.ink)
                List {
                    ForEach(candidates) { a in
                        HStack {
                            AvatarView(name: a.name, colorHex: a.theme_color, avatarID: a.avatar_id, size: 28)
                            Text(a.name)
                                .foregroundStyle(TigeroseTheme.ink)
                            Spacer()
                            Button {
                                Task { await add(a.template_id) }
                            } label: {
                                Image(systemName: "plus")
                                    .font(.system(size: 12, weight: .semibold))
                                    .foregroundStyle(TigeroseTheme.primary)
                                    .frame(width: 28, height: 28)
                                    .background(TigeroseTheme.canvas, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm))
                                    .overlay(
                                        RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm)
                                            .stroke(TigeroseTheme.hairline, lineWidth: 1)
                                    )
                            }
                            .buttonStyle(.plain)
                            .help("添加 \(a.name)")
                        }
                        .listRowBackground(TigeroseTheme.canvas)
                    }
                }
                .scrollContentBackground(.hidden)
                .frame(height: 120)
                if !error.isEmpty {
                    Text(error)
                        .font(TigeroseTheme.bodySm)
                        .foregroundStyle(TigeroseTheme.error)
                }
            }
            .padding(.horizontal, TigeroseTheme.spaceLg)
            .padding(.bottom, TigeroseTheme.spaceLg)
        }
        .frame(width: 480, height: 520)
        .background(TigeroseTheme.canvas)
        .task { await load() }
    }

    private func load() async {
        do {
            let g = try await app.api.getGroup(groupId)
            name = g.name
            members = g.members ?? []
            let inGroup = Set(members.map(\.template_id))
            candidates = app.assistants.filter { !inGroup.contains($0.template_id) }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func saveName() async {
        do {
            _ = try await app.api.updateGroup(groupId, name: name, workspacePath: nil)
            onChanged()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func remove(_ instanceId: String) async {
        do {
            try await app.api.removeMember(groupId: groupId, instanceId: instanceId)
            await load()
            onChanged()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func add(_ templateId: String) async {
        do {
            _ = try await app.api.addMember(groupId: groupId, templateId: templateId)
            await load()
            onChanged()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
