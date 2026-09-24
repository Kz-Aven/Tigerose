import Foundation
import Observation

enum Surface: Hashable, Equatable {
    case empty
    case assistant(String)
    case group(String)
    case tasks
}

@MainActor
@Observable
final class AppState {
    var preferences: AppPreferences
    var surface: Surface = .empty
    var meshReturnSurface: Surface = .empty
    var meshSelectedTaskID: String?
    var contentSurface: Surface { surface == .tasks ? meshReturnSurface : surface }

    func openMeshTasks(taskID: String? = nil) {
        if surface != .tasks { meshReturnSurface = surface }
        meshSelectedTaskID = taskID
        surface = .tasks
    }
    var backendPort: Int?
    var backendStatus: BackendStatus = .stopped
    var backendError: String = ""
    var globalError: String = ""

    var assistants: [AgentTemplate] = []
    var groups: [ProjectGroup] = []
    /// Sidebar avatar pulse — derived from `liveRuns` when possible.
    var busyKey: String?
    /// Live reply/thinking per SSE channel (`assistant:{id}` / `group:{id}`).
    var liveRuns: [String: LiveRunState] = [:]
    /// Assistant DM runs are isolated by session so an IM conversation cannot
    /// take over the currently displayed local conversation.
    private var assistantSessionRuns: [String: LiveRunState] = [:]

    /// Retained SSE clients so a busy assistant keeps receiving traces after leaving its DM.
    private var retainedSSE: [String: SSEClient] = [:]
    /// UI handlers for the currently visible surface (nil while channel is background-retained).
    private var surfaceSSEHandlers: [String: (String, Data) -> Void] = [:]
    /// Channel whose DM is currently on screen (at most one assistant surface).
    private var activeAssistantSurface: String?

    let backend = BackendProcess()
    let api = APIClient()

    var baseURL: URL {
        if let port = backendPort {
            return URL(string: "http://127.0.0.1:\(port)")!
        }
        return URL(string: "http://127.0.0.1:8765")!
    }

    var needsOnboarding: Bool { !preferences.onboardingCompleted }

    init() {
        preferences = AppPreferencesStore.load()
        backend.onStateChange = { [weak self] status, port, error in
            Task { @MainActor in
                guard let self else { return }
                self.backendStatus = status
                self.backendPort = port
                self.backendError = error
                if let port {
                    self.api.baseURL = URL(string: "http://127.0.0.1:\(port)")!
                }
            }
        }
    }

    func bootstrap() async {
        do {
            try TigerosePaths.ensureHomeLayout()
        } catch {
            globalError = error.localizedDescription
            return
        }
        guard !needsOnboarding else { return }
        await startBackendAndLoad()
    }

    func completeOnboarding(_ prefs: AppPreferences, apiKey: String) async {
        do {
            try TigerosePaths.ensureHomeLayout()
            try OnboardingWriter.write(prefs: prefs, apiKey: apiKey)
            preferences = prefs
            try AppPreferencesStore.save(prefs)
            await startBackendAndLoad()
        } catch {
            globalError = error.localizedDescription
        }
    }

    func startBackendAndLoad() async {
        backendStatus = .starting
        api.meshAdminToken = backend.meshAdminToken
        do {
            try await backend.start()
            try await refreshLists()
        } catch {
            backendStatus = .failed
            backendError = error.localizedDescription
        }
    }

    func refreshLists() async throws {
        assistants = try await api.listAssistants()
        groups = try await api.listGroups()
    }

    /// Merge GET/PATCH assistant payloads into the sidebar list without dropping previews.
    func upsertAssistant(_ tpl: AgentTemplate) {
        if let idx = assistants.firstIndex(where: { $0.template_id == tpl.template_id }) {
            var merged = tpl
            let incoming = (tpl.last_message_preview ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if incoming.isEmpty {
                merged.last_message_preview = assistants[idx].last_message_preview
            }
            assistants[idx] = merged
        } else {
            assistants.append(tpl)
        }
    }

    func setAssistantPreview(_ templateId: String, content: String) {
        guard let idx = assistants.firstIndex(where: { $0.template_id == templateId }) else { return }
        let snippet = Self.previewSnippet(content)
        assistants[idx].last_message_preview = snippet.isEmpty ? nil : snippet
    }

    func upsertGroup(_ group: ProjectGroup) {
        if let idx = groups.firstIndex(where: { $0.group_id == group.group_id }) {
            var merged = group
            let incoming = (group.last_message_preview ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if incoming.isEmpty {
                merged.last_message_preview = groups[idx].last_message_preview
            }
            if merged.member_count == nil {
                merged.member_count = groups[idx].member_count
            }
            groups[idx] = merged
        } else {
            groups.append(group)
        }
    }

    func setGroupPreview(_ groupId: String, content: String) {
        guard let idx = groups.firstIndex(where: { $0.group_id == groupId }) else { return }
        let snippet = Self.previewSnippet(content)
        groups[idx].last_message_preview = snippet.isEmpty ? nil : snippet
    }

    private static func previewSnippet(_ text: String, maxLen: Int = 40) -> String {
        let t = text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        guard !t.isEmpty else { return "" }
        if t.count <= maxLen { return t }
        return String(t.prefix(maxLen - 1)) + "…"
    }

    // MARK: - Live runs / retained SSE

    static func assistantChannel(_ templateId: String) -> String { "assistant:\(templateId)" }
    static func groupChannel(_ groupId: String) -> String { "group:\(groupId)" }

    func liveRun(for channel: String) -> LiveRunState {
        liveRuns[channel] ?? LiveRunState()
    }

    func liveRun(for channel: String, sessionId: String?) -> LiveRunState {
        guard let sessionId, !sessionId.isEmpty else { return LiveRunState() }
        return assistantSessionRuns[assistantSessionKey(channel, sessionId)] ?? LiveRunState()
    }

    func beginAssistantRun(_ templateId: String, sessionId: String? = nil, runIds: [String] = []) {
        let channel = Self.assistantChannel(templateId)
        let state = LiveRunState(
            running: true,
            thinking: true,
            steps: [],
            durationMs: 0,
            runIds: runIds
        )
        if let sessionId, !sessionId.isEmpty {
            assistantSessionRuns[assistantSessionKey(channel, sessionId)] = state
            refreshAssistantChannel(channel)
        } else {
            liveRuns[channel] = state
            refreshBusyKey()
        }
    }

    func cancelLiveRun(_ channel: String, sessionId: String? = nil) async {
        let key = sessionId.map { assistantSessionKey(channel, $0) }
        let ids = key.flatMap { assistantSessionRuns[$0]?.runIds } ?? liveRuns[channel]?.runIds ?? []
        for runId in ids {
            try? await api.cancelRun(runId)
        }
        if let key {
            assistantSessionRuns.removeValue(forKey: key)
            refreshAssistantChannel(channel)
        } else {
            clearLiveRun(channel)
        }
    }

    /// Attach (or re-attach) SSE for an assistant DM. Keeps the socket alive while `running`.
    func attachAssistantSSE(
        templateId: String,
        onEvent: @escaping @MainActor (String, Data) -> Void
    ) {
        let channel = Self.assistantChannel(templateId)
        if let prev = activeAssistantSurface, prev != channel {
            surfaceSSEHandlers[prev] = nil
            if !(liveRuns[prev]?.running ?? false) {
                retainedSSE[prev]?.cancel()
                retainedSSE.removeValue(forKey: prev)
            }
        }
        activeAssistantSurface = channel
        surfaceSSEHandlers[channel] = onEvent
        if retainedSSE[channel] == nil {
            let client = SSEClient()
            retainedSSE[channel] = client
            client.subscribe(baseURL: api.baseURL, channel: channel) { [weak self] type, data in
                Task { @MainActor in
                    guard let self else { return }
                    self.ingestAssistantSSE(channel: channel, templateId: templateId, type: type, data: data)
                    self.surfaceSSEHandlers[channel]?(type, data)
                }
            }
        }
    }

    /// Drop the UI handler when leaving a DM. Cancel the socket only if that assistant is idle.
    func detachAssistantSSE(templateId: String) {
        let channel = Self.assistantChannel(templateId)
        if activeAssistantSurface == channel {
            activeAssistantSurface = nil
        }
        surfaceSSEHandlers[channel] = nil
        guard !(liveRuns[channel]?.running ?? false) else { return }
        retainedSSE[channel]?.cancel()
        retainedSSE.removeValue(forKey: channel)
    }

    private func ingestAssistantSSE(channel: String, templateId: String, type: String, data: Data) {
        switch type {
        case "assistant.trace":
            applyAssistantTrace(channel: channel, data: data)
        case "assistant.status":
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let status = obj["status"] as? String
            {
                applyAssistantStatus(
                    channel: channel,
                    sessionId: (obj["session_id"] as? String) ?? "",
                    status: status
                )
            }
        case "assistant.message":
            if let msg = try? JSONDecoder().decode(AssistantMessage.self, from: data) {
                setAssistantPreview(templateId, content: msg.content)
                let sessionId = msg.session_id ?? ""
                if msg.isIntermediate {
                    var state = assistantSessionState(channel, sessionId)
                    state.running = true
                    state.thinking = true
                    state.steps = []
                    setAssistantSessionState(channel, sessionId, state)
                    return
                }
                clearAssistantSessionRun(channel, sessionId)
                return
            }
            clearLiveRun(channel)
        case "error":
            clearLiveRun(channel)
        default:
            break
        }
    }

    private func applyAssistantTrace(channel: String, data: Data) {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let phase = obj["phase"] as? String
        else { return }
        let sessionId = (obj["session_id"] as? String) ?? ""
        var state = assistantSessionState(channel, sessionId)
        if let runId = obj["run_id"] as? String,
           !runId.isEmpty,
           !state.runIds.contains(runId)
        {
            state.runIds.append(runId)
        }
        switch phase {
        case "start":
            state.running = true
            state.thinking = true
            state.steps = []
            state.durationMs = 0
        case "step":
            state.running = true
            state.thinking = true
            if let stepObj = obj["step"],
               let stepData = try? JSONSerialization.data(withJSONObject: stepObj),
               let step = try? JSONDecoder().decode(ThinkingStep.self, from: stepData)
            {
                state.steps.append(step)
            }
        case "end":
            if let ms = obj["duration_ms"] as? Int {
                state.durationMs = ms
            } else if let ms = obj["duration_ms"] as? Double {
                state.durationMs = Int(ms)
            }
            state.thinking = false
        default:
            break
        }
        setAssistantSessionState(channel, sessionId, state)
    }

    private func applyAssistantStatus(channel: String, sessionId: String, status: String) {
        var state = assistantSessionState(channel, sessionId)
        if status == "running" {
            state.running = true
            if !state.thinking, state.steps.isEmpty {
                state.thinking = true
            }
            setAssistantSessionState(channel, sessionId, state)
        } else if status == "idle" {
            clearAssistantSessionRun(channel, sessionId)
        }
    }

    private func assistantSessionKey(_ channel: String, _ sessionId: String) -> String {
        "\(channel)|\(sessionId)"
    }

    private func assistantSessionState(_ channel: String, _ sessionId: String) -> LiveRunState {
        guard !sessionId.isEmpty else { return liveRuns[channel] ?? LiveRunState() }
        return assistantSessionRuns[assistantSessionKey(channel, sessionId)] ?? LiveRunState()
    }

    private func setAssistantSessionState(_ channel: String, _ sessionId: String, _ state: LiveRunState) {
        guard !sessionId.isEmpty else {
            liveRuns[channel] = state
            refreshBusyKey()
            return
        }
        assistantSessionRuns[assistantSessionKey(channel, sessionId)] = state
        refreshAssistantChannel(channel)
    }

    private func clearAssistantSessionRun(_ channel: String, _ sessionId: String) {
        guard !sessionId.isEmpty else {
            clearLiveRun(channel)
            return
        }
        assistantSessionRuns.removeValue(forKey: assistantSessionKey(channel, sessionId))
        refreshAssistantChannel(channel)
    }

    private func refreshAssistantChannel(_ channel: String) {
        let prefix = "\(channel)|"
        if let state = assistantSessionRuns.first(where: { $0.key.hasPrefix(prefix) })?.value {
            liveRuns[channel] = state
        } else {
            liveRuns[channel] = nil
        }
        refreshBusyKey()
        if liveRuns[channel] == nil, surfaceSSEHandlers[channel] == nil {
            retainedSSE[channel]?.cancel()
            retainedSSE.removeValue(forKey: channel)
        }
    }

    func clearLiveRun(_ channel: String) {
        liveRuns[channel] = nil
        let prefix = "\(channel)|"
        assistantSessionRuns = assistantSessionRuns.filter { !$0.key.hasPrefix(prefix) }
        refreshBusyKey()
        // If nothing is watching this channel, drop the retained socket.
        if surfaceSSEHandlers[channel] == nil {
            retainedSSE[channel]?.cancel()
            retainedSSE.removeValue(forKey: channel)
        }
    }

    private func refreshBusyKey() {
        if let busy = liveRuns.first(where: { $0.value.running })?.key {
            busyKey = busy
        } else {
            busyKey = nil
        }
    }

    func shutdown() {
        for client in retainedSSE.values {
            client.cancel()
        }
        retainedSSE.removeAll()
        surfaceSSEHandlers.removeAll()
        liveRuns.removeAll()
        assistantSessionRuns.removeAll()
        busyKey = nil
        backend.stop()
    }
}

enum BackendStatus: String {
    case stopped
    case starting
    case running
    case failed
}
