import SwiftUI
import AppKit
import UniformTypeIdentifiers

struct AssistantDMView: View {
    @Environment(AppState.self) private var app
    let templateId: String

    @State private var messages: [AssistantMessage] = []
    @State private var draft = ""
    @State private var error = ""
    @State private var showConfig = false
    @State private var profiles: [ManagedModelProfile] = []
    @State private var attachments: [Attachment] = []
    @State private var columnWidth: CGFloat = 0
    @State private var webEnabled = false
    @State private var dingtalkEnabled = false
    @State private var dingtalkConnected = false
    @State private var larkEnabled = false
    @State private var larkConnected = false
    @State private var pendingPermission: PermissionRequest?
    @State private var pendingQuestions: [PendingQuestion] = []
    @State private var dismissedProposals: Set<String> = []
    @State private var confirmedProposals: Set<String> = []
    @State private var copiedMessageID: String?

    @State private var activeSessionId: String?
    @State private var sessionTitle: String = "新对话"
    @State private var sessionMessageCount: Int = 0
    @State private var sessionItems: [AssistantSession] = []
    @State private var confirmClear = false
    @State private var confirmDeleteAssistant = false
    @State private var pendingDeleteSessionId: String?
    @State private var showRename = false
    @State private var renameDraft = ""
    @State private var sessionBusy = false
    @State private var showTasks = false
    @State private var taskPanelWidth: CGFloat = 360
    @GestureState private var taskPanelDrag: CGFloat = 0

    private var channel: String { AppState.assistantChannel(templateId) }

    private var live: LiveRunState { app.liveRun(for: channel, sessionId: activeSessionId) }

    private var visibleMessages: [AssistantMessage] {
        messages.filter {
            !($0.content == "运行已取消。" && $0.meta?["origin"]?.stringValue == "im")
        }
    }

    private var queryMarkers: [ChatQueryMarker] {
        visibleMessages.compactMap { message in
            guard messageIsFromUser(message) else { return nil }
            let query = message.content.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !query.isEmpty else { return nil }
            return ChatQueryMarker(id: message.message_id, query: query)
        }
    }

    private var assistant: AgentTemplate? {
        app.assistants.first { $0.template_id == templateId }
    }

    private var bubbleMaxWidth: CGFloat {
        // Ignore tiny/zero probes so bubbles never collapse to a 1-glyph column.
        let column = columnWidth >= 320 ? columnWidth : TigeroseTheme.bubbleMaxWidth
        return column * TigeroseTheme.bubbleMaxWidthFraction
    }

    var body: some View {
        GeometryReader { geometry in
            HStack(spacing: 0) {
                chatContent
                if showTasks, geometry.size.width >= 780 {
                    Divider()
                        .frame(width: 5)
                        .contentShape(Rectangle())
                        .gesture(DragGesture()
                            .updating($taskPanelDrag) { value, state, _ in state = -value.translation.width }
                            .onEnded { value in taskPanelWidth = min(480, max(320, taskPanelWidth - value.translation.width)) })
                    MeshAssistantPanel(assistantID: templateId, onClose: { showTasks = false })
                        .frame(width: min(480, max(320, taskPanelWidth + taskPanelDrag)))
                }
            }
            .overlay(alignment: .trailing) {
                if showTasks, geometry.size.width < 780 {
                    MeshAssistantPanel(assistantID: templateId, onClose: { showTasks = false })
                        .frame(width: min(360, max(280, geometry.size.width - 100)))
                        .background(TigeroseTheme.canvas)
                        .shadow(radius: 8, x: -3)
                }
            }
        }
    }

    private var chatContent: some View {
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

                        ForEach(visibleMessages) { m in
                            messageRow(m)
                                .id(m.message_id)
                        }
                        if live.showsTypingChrome {
                            VStack(alignment: .leading, spacing: 8) {
                                if !live.steps.isEmpty || live.thinking {
                                    ThinkingChainView(
                                        steps: live.steps,
                                        durationMs: live.durationMs,
                                        isRunning: live.running || live.thinking,
                                        maxWidth: bubbleMaxWidth
                                    )
                                } else if live.running {
                                    HStack(spacing: TigeroseTheme.spaceXs) {
                                        ProgressView().controlSize(.small).tint(TigeroseTheme.primary)
                                        Text("正在回复…")
                                            .font(TigeroseTheme.caption)
                                            .foregroundStyle(TigeroseTheme.muted)
                                    }
                                }
                            }
                            .padding(.vertical, 4)
                            .id("typing")
                        }
                        // Stable end marker so scroll always reaches past the last bubble / thinking row.
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
                .onChange(of: messages.count) { _, _ in
                    scrollChatToBottom(proxy)
                }
                .onChange(of: live.running) { _, on in
                    if on { scrollChatToBottom(proxy) }
                }
                .onChange(of: live.thinking) { _, on in
                    if on { scrollChatToBottom(proxy) }
                }
                .onChange(of: live.steps.count) { _, _ in
                    if live.showsTypingChrome { scrollChatToBottom(proxy) }
                }
                .overlay(alignment: .trailing) {
                    ChatQueryMarkers(markers: queryMarkers) { messageID in
                        withAnimation(.easeOut(duration: 0.22)) {
                            proxy.scrollTo(messageID, anchor: .center)
                        }
                    }
                    .padding(.trailing, 4)
                }
            }
            if !error.isEmpty {
                Text(error)
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.error)
                    .padding(.horizontal)
            }
            if !attachments.isEmpty {
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
            ComposerBar(
                text: $draft,
                placeholder: "给 \(assistant?.name ?? "助理") 发消息",
                canSend: true,
                onSend: { Task { await send() } },
                isRunning: live.running,
                onStop: {
                    Task { await app.cancelLiveRun(channel, sessionId: activeSessionId) }
                },
                onAttach: { pickFiles() },
                hasAttachments: !attachments.isEmpty,
                onAttachFiles: { urls in
                    Task { await uploadFiles(urls) }
                },
                webEnabled: $webEnabled,
                onWebEnabledChange: { enabled in
                    Task { await persistWebEnabled(enabled) }
                },
                dingtalkEnabled: dingtalkEnabled,
                dingtalkConnected: dingtalkConnected,
                onDingtalkEnabledChange: { enabled in
                    Task { await persistDingTalkEnabled(enabled) }
                },
                larkEnabled: larkEnabled,
                larkConnected: larkConnected,
                onLarkEnabledChange: { enabled in
                    Task { await persistLarkEnabled(enabled) }
                },
                onConnectorMenuOpened: {
                    Task {
                        dingtalkConnected = (try? await app.api.getDingTalkConnector())?.connected ?? false
                        larkConnected = (try? await app.api.getLarkConnector())?.authenticated ?? false
                    }
                }
            )
            .fixedSize(horizontal: false, vertical: true)
            .layoutPriority(1)
        }
        .background(TigeroseTheme.canvas)
        .task(id: templateId) {
            await load()
        }
        .onDisappear {
            app.detachAssistantSSE(templateId: templateId)
        }
        .sheet(isPresented: $showConfig) {
            if let a = assistant {
                CapabilitiesSheet(template: a)
            }
        }
        .sheet(isPresented: $showRename) {
            renameSheet
        }
        .confirmationDialog("清空后无法恢复，确定清空当前对话？", isPresented: $confirmClear, titleVisibility: .visible) {
            Button("清空对话", role: .destructive) { Task { await clearConversation() } }
            Button("取消", role: .cancel) {}
        }
        .confirmationDialog("将删除该助理及其全部对话，确定删除？", isPresented: $confirmDeleteAssistant, titleVisibility: .visible) {
            Button("删除助理", role: .destructive) { Task { await deleteAssistant() } }
            Button("取消", role: .cancel) {}
        }
        .confirmationDialog("删除后无法恢复，确定删除该对话？", isPresented: Binding(
            get: { pendingDeleteSessionId != nil },
            set: { if !$0 { pendingDeleteSessionId = nil } }
        ), titleVisibility: .visible) {
            Button("删除对话", role: .destructive) {
                if let sid = pendingDeleteSessionId {
                    Task { await deleteSession(sid) }
                }
            }
            Button("取消", role: .cancel) { pendingDeleteSessionId = nil }
        }
    }

    private var renameSheet: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("编辑对话名称")
                    .font(TigeroseTheme.titleMd)
                Spacer()
                Button {
                    showRename = false
                } label: {
                    Image(systemName: "xmark")
                        .foregroundStyle(TigeroseTheme.muted)
                }
                .buttonStyle(.plain)
            }
            TextField("对话名称", text: $renameDraft)
                .textFieldStyle(.roundedBorder)
            HStack {
                Spacer()
                Button("取消") { showRename = false }
                Button("确定") {
                    Task { await renameCurrentSession() }
                }
                .buttonStyle(TigerosePrimaryButtonStyle(disabled: renameDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty))
                .disabled(renameDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(20)
        .frame(width: 360)
    }

    private var header: some View {
        HStack(spacing: 8) {
            HStack(alignment: .center, spacing: 10) {
                AvatarView(
                    name: assistant?.name ?? "?",
                    colorHex: assistant?.theme_color ?? "#888",
                    avatarID: assistant?.avatar_id ?? "",
                    size: 32
                )
                VStack(alignment: .leading, spacing: 2) {
                    Text(assistant?.name ?? "助理")
                        .font(TigeroseTheme.titleMd)
                        .foregroundStyle(TigeroseTheme.ink)
                        .lineLimit(1)
                        .hoverFullText(assistant?.name ?? "助理")
                    Text(assistant?.role ?? "")
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.muted)
                        .lineLimit(1)
                        .hoverFullText(assistant?.role ?? "")
                }
                .frame(minHeight: 32, alignment: .center)
                Spacer(minLength: 8)
                sessionHeader
                    .frame(maxWidth: 280)
                    .layoutPriority(-1)
                Spacer(minLength: 8)
                HStack(alignment: .center, spacing: 8) {
                    Button { showTasks.toggle() } label: {
                        Label("助理任务", systemImage: "checklist")
                            .font(TigeroseTheme.caption)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(TigeroseTheme.primary)
                    .fixedSize()
                    Menu {
                        ForEach(profiles) { p in
                            Button(p.label) {
                                Task { await switchModel(p.id) }
                            }
                        }
                    } label: {
                        Label(assistant?.model_profile_id ?? "模型", systemImage: "cpu")
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(TigeroseTheme.ink)
                            .padding(.horizontal, 8)
                            .frame(height: 28)
                            .background(TigeroseTheme.secondary, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm))
                    }
                    .menuStyle(.borderlessButton)
                    .frame(height: 32)
                    .fixedSize(horizontal: true, vertical: false)
                    Menu {
                        Button("能力配置…") { showConfig = true }
                        Divider()
                        Button("清空对话…", role: .destructive) { confirmClear = true }
                        Button("删除助理…", role: .destructive) { confirmDeleteAssistant = true }
                    } label: {
                        Image(systemName: "gearshape")
                            .foregroundStyle(TigeroseTheme.muted)
                            .frame(width: 28, height: 28)
                    }
                    .menuStyle(.borderlessButton)
                    .help("能力配置 / 清空 / 删除")
                }
            }

        }
        .padding(TigeroseTheme.spaceMd)
        .background(TigeroseTheme.canvas)
        .overlay(alignment: .bottom) {
            Rectangle().fill(TigeroseTheme.hairline).frame(height: 1)
        }
    }

    private var sessionHeader: some View {
        SessionSwitcher(
                title: sessionTitle,
                canRename: sessionMessageCount > 0 && activeSessionId != nil,
                unreadCount: assistant?.unread_count ?? 0,
                items: sessionItems,
                onNew: { Task { await startNewSession() } },
                onSelect: { sid in Task { await activateSession(sid) } },
                onDelete: { sid in pendingDeleteSessionId = sid },
                onRename: { _ in
                    renameDraft = sessionTitle
                    showRename = true
                }
            )
    }

    @ViewBuilder
    private func messageRow(_ m: AssistantMessage) -> some View {
        let isUser = messageIsFromUser(m)
        let displayedAttachments = isUser ? m.attachments : LocalFileAttachmentCards.includingImages(in: m.content, attachments: m.attachments)
        let theme = assistant?.theme_color ?? "#5db8a6"
        HStack(alignment: .top, spacing: 0) {
            if isUser { Spacer(minLength: 48) }
            VStack(alignment: isUser ? .trailing : .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(isUser ? "你" : (assistant?.name ?? "助理"))
                    Text(ChatMessageTimestamp.format(m.ts))
                }
                .font(TigeroseTheme.caption)
                .foregroundStyle(TigeroseTheme.muted)
                if !isUser, let thinking = ThinkingMeta.from(meta: m.meta) {
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
                if !isUser, let ask = AskUserSnapshot.from(meta: m.meta) {
                    AskUserTranscriptCard(snapshot: ask, maxWidth: bubbleMaxWidth)
                } else {
                    MessageBubble(
                        content: m.content,
                        isUser: isUser,
                        renderMarkdown: true,
                        themeHex: theme,
                        maxWidth: bubbleMaxWidth
                    )
                }
                if !isUser {
                    HStack(spacing: 4) {
                        if let usage = UsageSummary.from(meta: m.meta) {
                            Text("本轮消耗 \(formatTokenCount(usage.total_tokens)) Token · 输入 \(formatTokenCount(usage.input_tokens)) · 输出 \(formatTokenCount(usage.output_tokens))\(usage.usage_source == "provider" ? "" : " · 含估算值")")
                                .font(TigeroseTheme.caption)
                                .foregroundStyle(TigeroseTheme.muted)
                        }
                        Button { copyMessage(m) } label: {
                            Image(systemName: copiedMessageID == m.message_id ? "checkmark" : "doc.on.doc")
                                .font(.system(size: 12, weight: .medium))
                                .frame(width: 24, height: 18)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(copiedMessageID == m.message_id ? TigeroseTheme.primary : TigeroseTheme.muted)
                        .help(copiedMessageID == m.message_id ? "已复制" : "复制回复")
                    }
                }
                if !displayedAttachments.isEmpty {
                    LocalFileAttachmentCards(
                        attachments: displayedAttachments,
                        maxWidth: bubbleMaxWidth
                    )
                }
                if let proposal = RememberProposalParser.parse(meta: m.meta),
                   !dismissedProposals.contains(m.message_id)
                {
                    RememberProposalCard(
                        proposal: proposal,
                        maxWidth: bubbleMaxWidth,
                        confirmed: false,
                        onIgnore: {
                            Task { await dismissProposal(m) }
                        },
                        onConfirm: {
                            Task { await confirmProposal(m, proposal: proposal) }
                        }
                    )
                } else if RememberProposalParser.isConfirmed(meta: m.meta)
                            || confirmedProposals.contains(m.message_id)
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

    private func confirmProposal(_ m: AssistantMessage, proposal: RememberProposalData) async {
        do {
            try await app.api.confirmMemory(
                templateId,
                body: proposal.body,
                sourceGroups: proposal.sourceGroups,
                source: "confirm",
                messageId: m.message_id
            )
            confirmedProposals.insert(m.message_id)
            patchLocalMessageMeta(m.message_id, key: "remember_confirmed", value: true)
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func dismissProposal(_ m: AssistantMessage) async {
        do {
            try await app.api.dismissMemory(templateId, messageId: m.message_id)
            dismissedProposals.insert(m.message_id)
            patchLocalMessageMeta(m.message_id, key: "remember_ignored", value: true)
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func patchLocalMessageMeta(_ messageId: String, key: String, value: Bool) {
        guard let idx = messages.firstIndex(where: { $0.message_id == messageId }) else { return }
        var meta = messages[idx].meta ?? [:]
        meta[key] = .bool(value)
        messages[idx].meta = meta
    }

    private func applySessions(_ resp: SessionsResponse) {
        if let cur = resp.current {
            activeSessionId = cur.session_id
            sessionTitle = cur.title.isEmpty ? "新对话" : cur.title
            sessionMessageCount = cur.message_count
        } else {
            activeSessionId = nil
            sessionTitle = "新对话"
            sessionMessageCount = 0
        }
        sessionItems = resp.items
    }

    private func load() async {
        error = ""
        pendingPermission = nil
        pendingQuestions = []
        do {
            let sessions = try await app.api.listSessions(templateId)
            applySessions(sessions)
            if let sid = activeSessionId {
                let page = try await app.api.listAssistantMessages(templateId, sessionId: sid, limit: 50)
                messages = page.items
            } else {
                messages = []
            }
            let listed = try await app.api.listManagedProfiles()
            profiles = listed.profiles
            if let tpl = try? await app.api.getAssistant(templateId) {
                webEnabled = tpl.webEnabled
                dingtalkEnabled = tpl.dingtalkEnabled
                larkEnabled = tpl.larkEnabled
                app.upsertAssistant(tpl)
            } else {
                webEnabled = assistant?.webEnabled ?? false
                dingtalkEnabled = assistant?.dingtalkEnabled ?? false
                larkEnabled = assistant?.larkEnabled ?? false
            }
            dingtalkConnected = (try? await app.api.getDingTalkConnector())?.connected ?? false
            larkConnected = (try? await app.api.getLarkConnector())?.authenticated ?? false
        } catch {
            self.error = error.localizedDescription
        }
        do {
            pendingQuestions = try await app.api.listPendingQuestions(channel: channel)
        } catch {
            self.error = error.localizedDescription
        }
        app.attachAssistantSSE(templateId: templateId) { type, data in
            Task { @MainActor in
                if type == "connected" {
                    try? await refreshPendingPermission()
                }
                if type == "assistant.message" {
                        if let msg = try? JSONDecoder().decode(AssistantMessage.self, from: data) {
                        // Ignore messages from other sessions while viewing one
                        if let cur = activeSessionId, let msid = msg.session_id, !msid.isEmpty, msid != cur {
                            try? await app.refreshLists()
                            if let sessions = try? await app.api.listSessions(templateId) { applySessions(sessions) }
                            return
                        }
                        if !messages.contains(where: { $0.message_id == msg.message_id }) {
                            messages.append(msg)
                        } else if let idx = messages.firstIndex(where: { $0.message_id == msg.message_id }) {
                            messages[idx] = msg
                        }
                        if let sid = msg.session_id, !sid.isEmpty, activeSessionId == nil {
                            activeSessionId = sid
                        }
                        if let sessions = try? await app.api.listSessions(templateId) {
                            applySessions(sessions)
                        }
                        try? await app.refreshLists()
                    }
                }
                if type == "assistant.status" {
                    if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let status = obj["status"] as? String,
                       status == "idle"
                    {
                        if let sid = activeSessionId,
                           let page = try? await app.api.listAssistantMessages(templateId, sessionId: sid, limit: 50)
                        {
                            messages = page.items
                        }
                        if let sessions = try? await app.api.listSessions(templateId) {
                            applySessions(sessions)
                        }
                    }
                }
                if type == "error" {
                    if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let message = obj["message"] as? String
                    {
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
                       pending.channel == channel
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
        let requests = try await app.api.listPendingPermissions(channel: channel)
        pendingPermission = requests.first
    }

    private func refreshPendingQuestions() async throws {
        pendingQuestions = try await app.api.listPendingQuestions(channel: channel)
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
            app.beginAssistantRun(templateId, sessionId: activeSessionId, runIds: runIds)
        }
    }

    private func cancelQuestion(_ pending: PendingQuestion) async throws {
        let response = try await app.api.cancelQuestion(questionId: pending.question_id)
        guard response.ok else {
            throw APIError.badStatus(-1, "取消问题失败")
        }
        pendingQuestions.removeAll { $0.question_id == pending.question_id }
    }

    private func startNewSession() async {
        guard !sessionBusy else { return }
        sessionBusy = true
        defer { sessionBusy = false }
        app.detachAssistantSSE(templateId: templateId)
        do {
            _ = try await app.api.newSession(templateId)
            activeSessionId = nil
            sessionTitle = "新对话"
            sessionMessageCount = 0
            messages = []
            let sessions = try await app.api.listSessions(templateId)
            applySessions(sessions)
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func activateSession(_ sid: String) async {
        guard !sessionBusy else { return }
        sessionBusy = true
        defer { sessionBusy = false }
        app.detachAssistantSSE(templateId: templateId)
        do {
            let resp = try await app.api.activateSession(templateId, sessionId: sid)
            if let cur = resp.current {
                activeSessionId = cur.session_id
                sessionTitle = cur.title.isEmpty ? "新对话" : cur.title
                sessionMessageCount = cur.message_count
            }
            messages = resp.messages?.items ?? []
            let sessions = try await app.api.listSessions(templateId)
            applySessions(sessions)
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func deleteSession(_ sid: String) async {
        pendingDeleteSessionId = nil
        app.detachAssistantSSE(templateId: templateId)
        do {
            try await app.api.deleteSession(templateId, sessionId: sid)
            if activeSessionId == sid {
                activeSessionId = nil
                sessionTitle = "新对话"
                sessionMessageCount = 0
                messages = []
            }
            let sessions = try await app.api.listSessions(templateId)
            applySessions(sessions)
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func clearConversation() async {
        app.detachAssistantSSE(templateId: templateId)
        do {
            if let cur = try await app.api.clearSession(templateId) {
                activeSessionId = cur.session_id
                sessionTitle = cur.title.isEmpty ? "新对话" : cur.title
                sessionMessageCount = cur.message_count
            } else {
                activeSessionId = nil
                sessionTitle = "新对话"
                sessionMessageCount = 0
            }
            messages = []
            let sessions = try await app.api.listSessions(templateId)
            applySessions(sessions)
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func deleteAssistant() async {
        app.detachAssistantSSE(templateId: templateId)
        do {
            try await app.api.deleteAssistant(templateId)
            app.surface = .empty
            try await app.refreshLists()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func renameCurrentSession() async {
        let title = renameDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let sid = activeSessionId, !title.isEmpty else { return }
        do {
            let cur = try await app.api.renameSession(templateId, sessionId: sid, title: title)
            sessionTitle = cur.title
            showRename = false
            let sessions = try await app.api.listSessions(templateId)
            applySessions(sessions)
        } catch {
            self.error = error.localizedDescription
        }
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

    private func scrollChatToBottom(_ proxy: ScrollViewProxy) {
        // Wait for SwiftUI layout (new bubble / thinking row) before scrolling.
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
            let tpl = try await app.api.setAssistantWebEnabled(templateId, enabled: enabled)
            webEnabled = tpl.webEnabled
            app.upsertAssistant(tpl)
        } catch {
            webEnabled = !enabled
            self.error = error.localizedDescription
        }
    }

    private func persistDingTalkEnabled(_ enabled: Bool) async {
        do {
            let tpl = try await app.api.setAssistantDingTalkEnabled(templateId, enabled: enabled)
            dingtalkEnabled = tpl.dingtalkEnabled
            app.upsertAssistant(tpl)
        } catch {
            dingtalkEnabled = !enabled
            self.error = error.localizedDescription
        }
    }

    private func persistLarkEnabled(_ enabled: Bool) async {
        do {
            let tpl = try await app.api.setAssistantLarkEnabled(templateId, enabled: enabled)
            larkEnabled = tpl.larkEnabled
            app.upsertAssistant(tpl)
        } catch {
            larkEnabled = !enabled
            self.error = error.localizedDescription
        }
    }

    private func send() async {
        let content = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !content.isEmpty || !attachments.isEmpty else { return }
        draft = ""
        let atts = attachments
        attachments = []
        do {
            let msg = try await app.api.postAssistantMessage(
                templateId,
                content: content,
                sessionId: activeSessionId,
                attachments: atts
            )
            if let sid = msg.session_id, !sid.isEmpty {
                activeSessionId = sid
            }
            if !messages.contains(where: { $0.message_id == msg.message_id }) {
                messages.append(msg)
            }
            app.beginAssistantRun(templateId, sessionId: activeSessionId, runIds: msg.runIds)
            app.setAssistantPreview(templateId, content: msg.content)
            if let sessions = try? await app.api.listSessions(templateId) {
                applySessions(sessions)
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func switchModel(_ profileId: String) async {
        do {
            _ = try await app.api.switchModel(profileId: profileId, templateId: templateId)
            try await app.refreshLists()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func messageIsFromUser(_ m: AssistantMessage) -> Bool {
        if m.role == "user" { return true }
        // Pre-v0.1.6: /goal commands were stored as role=system and rendered as assistant.
        if m.role == "system", let action = m.meta?["goal_command"]?.stringValue {
            return action == "set" || action == "resume"
        }
        return false
    }

    private func copyMessage(_ message: AssistantMessage) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(message.content, forType: .string)
        copiedMessageID = message.message_id
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            if copiedMessageID == message.message_id {
                copiedMessageID = nil
            }
        }
    }

    private func pickFiles() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
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
