import Foundation
import UniformTypeIdentifiers

enum APIError: LocalizedError {
    case badStatus(Int, String)
    case decode(Error)

    var errorDescription: String? {
        switch self {
        case .badStatus(_, let detail): return detail
        case .decode(let err): return err.localizedDescription
        }
    }
}

@MainActor
final class APIClient {
    var meshAdminToken = ""
    var baseURL = URL(string: "http://127.0.0.1:8765")!

    private func request<T: Decodable>(
        _ path: String,
        method: String = "GET",
        body: (any Encodable)? = nil,
        headers: [String: String] = [:]
    ) async throws -> T {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw APIError.badStatus(-1, "无效 URL: \(path)")
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if path.hasPrefix("/api/agent-mesh") || path.hasPrefix("/api/hooks") {
            req.setValue("Bearer \(meshAdminToken)", forHTTPHeaderField: "Authorization")
        }
        for (key, value) in headers {
            req.setValue(value, forHTTPHeaderField: key)
        }
        if let body {
            req.httpBody = try JSONEncoder().encode(AnyEncodable(body))
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse else {
            throw APIError.badStatus(-1, "无响应")
        }
        if http.statusCode == 204 {
            if let empty = Empty() as? T { return empty }
        }
        guard (200 ..< 300).contains(http.statusCode) else {
            let detail = Self.parseDetail(data) ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
            throw APIError.badStatus(http.statusCode, detail)
        }
        if T.self == Empty.self {
            return Empty() as! T
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.decode(error)
        }
    }

    private static func parseDetail(_ data: Data) -> String? {
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let s = obj["detail"] as? String { return s }
            if let arr = obj["detail"] as? [[String: Any]] {
                return arr.compactMap { $0["msg"] as? String }.joined(separator: "; ")
            }
            if let any = obj["detail"] {
                return String(describing: any)
            }
        }
        return String(data: data, encoding: .utf8)
    }

    func health() async throws -> Bool {
        struct Ok: Decodable { var ok: Bool }
        let r: Ok = try await request("/api/health")
        return r.ok
    }

    func meshTasks(assistantID: String = "", role: String = "all", status: String = "", groupID: String = "", cursor: String = "") async throws -> MeshTaskPage {
        var components = URLComponents()
        components.path = "/api/agent-mesh/tasks"
        components.queryItems = ["assistant_id": assistantID, "role": role, "status": status, "group_id": groupID, "cursor": cursor].map { URLQueryItem(name: $0.key, value: $0.value) }
        return try await request(components.string!)
    }

    func meshTask(_ id: String) async throws -> MeshTask {
        try await request("/api/agent-mesh/tasks/\(id)")
    }

    func pendingMeshPermissions() async throws -> [MeshDecision] {
        let page: MeshDecisionPage = try await request("/api/agent-mesh/decisions/pending")
        return page.items
    }

    func meshMessages(_ id: String, after: Int = 0) async throws -> MeshMessagePage {
        try await request("/api/agent-mesh/tasks/\(id)/messages?after_sequence=\(after)")
    }

    func meshGraph(_ workflowID: String) async throws -> MeshGraph {
        try await request("/api/agent-mesh/workflows/\(workflowID)/graph")
    }

    func cancelMeshTask(_ id: String, workflow: Bool = false) async throws {
        let _: Empty = try await request("/api/agent-mesh/\(workflow ? "workflows" : "tasks")/\(id)/cancel", method: "POST", body: ["reason": "用户取消"])
    }

    func meshCollaboration(_ assistantID: String) async throws -> MeshCollaboration {
        try await request("/api/agent-mesh/assistants/\(assistantID)/collaboration")
    }

    func saveMeshCollaboration(_ assistantID: String, outgoing: [String], accepting: Bool, revision: Int) async throws -> MeshCollaboration {
        struct Body: Encodable { var outgoing: [String]; var accepting_tasks: Bool; var expected_revision: Int }
        return try await request("/api/agent-mesh/assistants/\(assistantID)/collaboration", method: "PUT", body: Body(outgoing: outgoing, accepting_tasks: accepting, expected_revision: revision))
    }

    func meshDecide(_ decision: MeshDecision, action: String, payload: [String: JSONValue], key: String) async throws {
        struct Body: Encodable { var action: String; var expected_task_revision: Int; var payload: [String: JSONValue]; var idempotency_key: String }
        let _: Empty = try await request("/api/agent-mesh/decisions/\(decision.decision_id)", method: "POST", body: Body(action: action, expected_task_revision: decision.expected_task_revision, payload: payload, idempotency_key: key))
    }

    func meshArtifact(_ id: String) async throws -> URL {
        var req = URLRequest(url: baseURL.appendingPathComponent("api/agent-mesh/artifacts/\(id)"))
        req.setValue("Bearer \(meshAdminToken)", forHTTPHeaderField: "Authorization")
        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw APIError.badStatus((response as? HTTPURLResponse)?.statusCode ?? -1, Self.parseDetail(data) ?? "无法读取交付物")
        }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("tigerose-mesh-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let filename = URL(fileURLWithPath: response.suggestedFilename ?? "artifact").lastPathComponent
        let target = directory.appendingPathComponent(filename)
        try data.write(to: target)
        return target
    }

    func listAssistants() async throws -> [AgentTemplate] {
        try await request("/api/assistants")
    }

    func createAssistant(
        name: String,
        role: String = "",
        systemPrompt: String = "",
        avatarId: String = ""
    ) async throws -> AgentTemplate {
        struct Body: Encodable {
            var name: String
            var role: String
            var system_prompt: String
            var avatar_id: String
        }
        return try await request(
            "/api/assistants",
            method: "POST",
            body: Body(name: name, role: role, system_prompt: systemPrompt, avatar_id: avatarId)
        )
    }

    func getAssistant(_ id: String) async throws -> AgentTemplate {
        try await request("/api/assistants/\(id)")
    }

    func updateAssistant(_ id: String, body: [String: String]) async throws -> AgentTemplate {
        try await request("/api/assistants/\(id)", method: "PATCH", body: body)
    }

    func deleteAssistant(_ id: String) async throws {
        let _: Empty = try await request("/api/assistants/\(id)", method: "DELETE")
    }

    func listSessions(_ id: String) async throws -> SessionsResponse {
        try await request("/api/assistants/\(id)/sessions")
    }

    func newSession(_ id: String) async throws -> SessionsResponse {
        // Server returns { current: null }; normalize to SessionsResponse
        struct Body: Decodable {
            var current: SessionsCurrent?
        }
        let _: Body = try await request("/api/assistants/\(id)/sessions", method: "POST")
        return SessionsResponse(current: nil, items: [])
    }

    func clearSession(_ id: String) async throws -> SessionsCurrent? {
        struct Resp: Decodable { var current: SessionsCurrent? }
        let r: Resp = try await request(
            "/api/assistants/\(id)/sessions/clear",
            method: "POST",
            headers: ["X-Avent-Actor": "user_ui"]
        )
        return r.current
    }

    func activateSession(_ id: String, sessionId: String) async throws -> SessionActivateResponse {
        try await request(
            "/api/assistants/\(id)/sessions/\(sessionId)/activate",
            method: "POST"
        )
    }

    func renameSession(_ id: String, sessionId: String, title: String) async throws -> SessionsCurrent {
        struct Body: Encodable { var title: String }
        return try await request(
            "/api/assistants/\(id)/sessions/\(sessionId)",
            method: "PATCH",
            body: Body(title: title)
        )
    }

    func deleteSession(_ id: String, sessionId: String) async throws {
        let _: Empty = try await request(
            "/api/assistants/\(id)/sessions/\(sessionId)",
            method: "DELETE",
            headers: ["X-Avent-Actor": "user_ui"]
        )
    }

    func setAssistantWebEnabled(_ id: String, enabled: Bool) async throws -> AgentTemplate {
        struct Body: Encodable { var web_enabled: Bool }
        return try await request(
            "/api/assistants/\(id)",
            method: "PATCH",
            body: Body(web_enabled: enabled)
        )
    }

    func setAssistantPermissionPolicy(
        _ id: String,
        fileAccess: String,
        dangerPolicy: String,
        dangerRules: [String]
    ) async throws -> AgentTemplate {
        struct Body: Encodable {
            var file_access: String
            var danger_policy: String
            var danger_rules: [String]
        }
        return try await request(
            "/api/assistants/\(id)",
            method: "PATCH",
            body: Body(
                file_access: fileAccess,
                danger_policy: dangerPolicy,
                danger_rules: dangerRules
            )
        )
    }

    func listAssistantMessages(
        _ id: String,
        sessionId: String,
        limit: Int = 30,
        beforeTs: Double? = nil
    ) async throws -> Page<AssistantMessage> {
        var q = "limit=\(limit)&session_id=\(sessionId.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? sessionId)"
        if let beforeTs { q += "&before_ts=\(beforeTs)" }
        return try await request("/api/assistants/\(id)/messages?\(q)")
    }

    func postAssistantMessage(
        _ id: String,
        content: String,
        sessionId: String? = nil,
        aggregateGroupIds: [String] = [],
        attachments: [Attachment] = []
    ) async throws -> AssistantMessage {
        struct Body: Encodable {
            var content: String
            var aggregate_group_ids: [String]
            var attachments: [[String: String]]
            var session_id: String?
        }
        let atts = attachments.map { ["path": $0.path, "name": $0.name, "mime": $0.mime] }
        return try await request(
            "/api/assistants/\(id)/messages",
            method: "POST",
            body: Body(
                content: content,
                aggregate_group_ids: aggregateGroupIds,
                attachments: atts,
                session_id: sessionId
            )
        )
    }

    func cancelRun(_ runId: String) async throws {
        struct Resp: Decodable {
            var accepted: Bool
            var run_id: String
        }
        let encoded = runId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? runId
        let _: Resp = try await request(
            "/api/runs/\(encoded)/cancel",
            method: "POST"
        )
    }

    func confirmMemory(
        _ id: String,
        body: String,
        sourceGroups: [String] = [],
        source: String? = nil,
        messageId: String? = nil,
        eventId: String? = nil,
        groupId: String? = nil,
        type: String? = nil,
        summary: String? = nil,
        scopeKind: String? = nil,
        scopeId: String? = nil
    ) async throws {
        struct Body: Encodable {
            var body: String
            var source_groups: [String]
            var source: String?
            var message_id: String?
            var event_id: String?
            var group_id: String?
            var type: String?
            var summary: String?
            var scope_kind: String?
            var scope_id: String?
        }
        let _: Empty = try await request(
            "/api/assistants/\(id)/memories/confirm",
            method: "POST",
            body: Body(
                body: body,
                source_groups: sourceGroups,
                source: source,
                message_id: messageId,
                event_id: eventId,
                group_id: groupId,
                type: type,
                summary: summary,
                scope_kind: scopeKind,
                scope_id: scopeId
            )
        )
    }

    func dismissMemory(
        _ id: String,
        messageId: String? = nil,
        eventId: String? = nil,
        groupId: String? = nil
    ) async throws {
        struct Body: Encodable {
            var message_id: String?
            var event_id: String?
            var group_id: String?
        }
        let _: Empty = try await request(
            "/api/assistants/\(id)/memories/dismiss",
            method: "POST",
            body: Body(message_id: messageId, event_id: eventId, group_id: groupId)
        )
    }

    func listMemories(_ id: String, status: String = "active") async throws -> [AssistantMemory] {
        try await request("/api/assistants/\(id)/memories?status=\(status)")
    }

    func updateMemory(_ id: String, memoryId: String, body: String, expectedVersion: Int? = nil,
                      type: String? = nil, summary: String? = nil) async throws -> AssistantMemory {
        struct Body: Encodable { var body: String; var expected_version: Int?; var type: String?; var summary: String? }
        return try await request(
            "/api/assistants/\(id)/memories/\(memoryId)",
            method: "PATCH",
            body: Body(body: body, expected_version: expectedVersion, type: type, summary: summary)
        )
    }

    func deleteMemory(_ id: String, memoryId: String, expectedVersion: Int? = nil) async throws {
        let query = expectedVersion.map { "?expected_version=\($0)" } ?? ""
        let _: Empty = try await request(
            "/api/assistants/\(id)/memories/\(memoryId)\(query)",
            method: "DELETE"
        )
    }

    func memoryRevisions(_ id: String, memoryId: String) async throws -> [MemoryRevision] {
        try await request("/api/assistants/\(id)/memories/\(memoryId)/revisions")
    }

    func restoreMemory(_ id: String, memoryId: String, revision: Int, expectedVersion: Int) async throws {
        struct Body: Encodable { var revision_version: Int; var expected_version: Int }
        let _: Empty = try await request("/api/assistants/\(id)/memories/\(memoryId)/restore", method: "POST",
                                        body: Body(revision_version: revision, expected_version: expectedVersion))
    }

    func memoryCandidates(_ id: String) async throws -> [MemoryCandidate] {
        try await request("/api/assistants/\(id)/memory-candidates")
    }

    func resolveMemoryCandidate(_ id: String, candidateId: String, confirm: Bool) async throws {
        let action = confirm ? "confirm" : "dismiss"
        let _: Empty = try await request("/api/assistants/\(id)/memory-candidates/\(candidateId)/\(action)", method: "POST")
    }

    func memoryJobs(_ id: String) async throws -> [MemoryJob] {
        try await request("/api/assistants/\(id)/memory-jobs")
    }

    func retryMemoryJob(_ id: String, jobId: String) async throws {
        let _: Empty = try await request("/api/assistants/\(id)/memory-jobs/\(jobId)/retry", method: "POST")
    }

    func organizeMemories(_ id: String, scopeKind: String, scopeId: String, memoryIds: [String]) async throws {
        struct Body: Encodable { var scope_kind: String; var scope_id: String; var memory_ids: [String] }
        let _: Empty = try await request("/api/assistants/\(id)/memories/organize", method: "POST",
                                        body: Body(scope_kind: scopeKind, scope_id: scopeId, memory_ids: memoryIds))
    }

    func previewMemoryImport(_ id: String, path: String, scopeKind: String, scopeId: String) async throws -> MemoryImportPreview {
        struct Body: Encodable { var path: String; var scope_kind: String; var scope_id: String }
        return try await request("/api/assistants/\(id)/memories/import-preview", method: "POST",
                                 body: Body(path: path, scope_kind: scopeKind, scope_id: scopeId))
    }

    func confirmMemoryImport(_ id: String, previewId: String) async throws {
        struct Body: Encodable { var preview_id: String }
        let _: Empty = try await request("/api/assistants/\(id)/memories/import-confirm", method: "POST", body: Body(preview_id: previewId))
    }

    func listGroups() async throws -> [ProjectGroup] {
        try await request("/api/groups")
    }

    func createGroup(name: String, workspacePath: String, memberTemplateIds: [String]) async throws -> ProjectGroup {
        struct Body: Encodable {
            var name: String
            var workspace_path: String
            var member_template_ids: [String]
        }
        return try await request(
            "/api/groups",
            method: "POST",
            body: Body(name: name, workspace_path: workspacePath, member_template_ids: memberTemplateIds)
        )
    }

    func getGroup(_ id: String) async throws -> ProjectGroup {
        try await request("/api/groups/\(id)")
    }

    func updateGroup(_ id: String, name: String?, workspacePath: String?) async throws -> ProjectGroup {
        struct Body: Encodable {
            var name: String?
            var workspace_path: String?
        }
        return try await request("/api/groups/\(id)", method: "PATCH", body: Body(name: name, workspace_path: workspacePath))
    }

    func clearGroup(_ id: String) async throws {
        struct Resp: Decodable { var ok: Bool?; var cleared: Int? }
        let _: Resp = try await request("/api/groups/\(id)/clear", method: "POST")
    }

    func deleteGroup(_ id: String) async throws {
        struct Resp: Decodable { var ok: Bool? }
        let _: Resp = try await request("/api/groups/\(id)", method: "DELETE")
    }

    func setGroupWebEnabled(_ id: String, enabled: Bool) async throws -> ProjectGroup {
        struct Body: Encodable { var web_enabled: Bool }
        return try await request(
            "/api/groups/\(id)",
            method: "PATCH",
            body: Body(web_enabled: enabled)
        )
    }

    func addMember(groupId: String, templateId: String, isCoordinator: Bool = false) async throws -> GroupMember {
        struct Body: Encodable { var template_id: String; var is_coordinator: Bool }
        return try await request(
            "/api/groups/\(groupId)/members",
            method: "POST",
            body: Body(template_id: templateId, is_coordinator: isCoordinator)
        )
    }

    func removeMember(groupId: String, instanceId: String) async throws {
        let _: Empty = try await request(
            "/api/groups/\(groupId)/members/\(instanceId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? instanceId)",
            method: "DELETE"
        )
    }

    func getFeed(_ groupId: String, limit: Int = 30, beforeTs: Double? = nil) async throws -> Page<FeedEvent> {
        var q = "limit=\(limit)"
        if let beforeTs { q += "&before_ts=\(beforeTs)" }
        return try await request("/api/groups/\(groupId)/feed?\(q)")
    }

    func postGroupMessage(_ groupId: String, content: String, attachments: [Attachment] = []) async throws -> FeedEvent {
        struct Body: Encodable {
            var content: String
            var attachments: [[String: String]]
        }
        let atts = attachments.map { ["path": $0.path, "name": $0.name, "mime": $0.mime] }
        return try await request(
            "/api/groups/\(groupId)/messages",
            method: "POST",
            body: Body(content: content, attachments: atts)
        )
    }

    func listManagedProfiles() async throws -> (defaultId: String, profiles: [ManagedModelProfile]) {
        struct Resp: Decodable {
            var default_id: String
            var profiles: [ManagedModelProfile]
        }
        let r: Resp = try await request("/api/models/profiles")
        return (r.default_id, r.profiles)
    }

    func getGoalEvaluatorModel() async throws -> GoalEvaluatorModelSettings {
        try await request("/api/models/goal-evaluator")
    }

    func setGoalEvaluatorModel(profileId: String) async throws -> GoalEvaluatorModelSettings {
        struct Body: Encodable { var profile_id: String }
        return try await request(
            "/api/models/goal-evaluator",
            method: "PUT",
            body: Body(profile_id: profileId)
        )
    }

    func createManagedProfile(model: String, baseURL: String, apiKey: String?, label: String?, provider: String?) async throws -> ManagedModelProfile {
        struct Body: Encodable {
            var model: String
            var base_url: String
            var api_key: String?
            var label: String?
            var provider: String?
        }
        return try await request(
            "/api/models/profiles",
            method: "POST",
            body: Body(model: model, base_url: baseURL, api_key: apiKey, label: label, provider: provider)
        )
    }

    func updateManagedProfile(id: String, model: String?, baseURL: String?, apiKey: String?, label: String?, provider: String?) async throws -> ManagedModelProfile {
        struct Body: Encodable {
            var model: String?
            var base_url: String?
            var api_key: String?
            var label: String?
            var provider: String?
        }
        return try await request(
            "/api/models/profiles/\(id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id)",
            method: "PUT",
            body: Body(model: model, base_url: baseURL, api_key: apiKey, label: label, provider: provider)
        )
    }

    func deleteManagedProfile(id: String) async throws {
        struct Resp: Decodable { var ok: Bool }
        let _: Resp = try await request(
            "/api/models/profiles/\(id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id)",
            method: "DELETE"
        )
    }

    func usageOverview(startDate: String, endDate: String, model: String = "", agentId: String = "") async throws -> UsageOverviewResponse {
        try await request(usagePath("/api/usage/overview", startDate: startDate, endDate: endDate, model: model, agentId: agentId))
    }

    func usageTrend(startDate: String, endDate: String, model: String = "", agentId: String = "") async throws -> UsageTrendResponse {
        try await request(usagePath("/api/usage/trend", startDate: startDate, endDate: endDate, model: model, agentId: agentId))
    }

    func usageByModel(startDate: String, endDate: String, model: String = "", agentId: String = "") async throws -> UsageModelResponse {
        try await request(usagePath("/api/usage/by-model", startDate: startDate, endDate: endDate, model: model, agentId: agentId))
    }

    func usageByAgent(startDate: String, endDate: String, model: String = "", agentId: String = "") async throws -> UsageAgentResponse {
        try await request(usagePath("/api/usage/by-agent", startDate: startDate, endDate: endDate, model: model, agentId: agentId))
    }

    func usagePricingFile() async throws -> UsagePricingFileResponse {
        try await request("/api/usage/pricing-file")
    }

    private func usagePath(_ path: String, startDate: String, endDate: String, model: String, agentId: String) -> String {
        var parts = ["start_date=\(startDate)", "end_date=\(endDate)"]
        if !model.isEmpty { parts.append("model=\(model.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? model)") }
        if !agentId.isEmpty { parts.append("agent_id=\(agentId.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? agentId)") }
        return "\(path)?\(parts.joined(separator: "&"))"
    }

    func switchModel(profileId: String, templateId: String) async throws -> AgentTemplate {
        struct Body: Encodable { var profile_id: String; var template_id: String }
        struct Resp: Decodable {
            var template: AgentTemplate
        }
        let r: Resp = try await request(
            "/api/models/switch",
            method: "POST",
            body: Body(profile_id: profileId, template_id: templateId)
        )
        return r.template
    }

    func listCapabilities(kind: String? = nil, workspace: String? = nil) async throws -> [CapabilityItem] {
        var parts: [String] = []
        if let kind {
            parts.append("kind=\(kind.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? kind)")
        }
        if let workspace, !workspace.isEmpty {
            parts.append("workspace=\(workspace.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? workspace)")
        }
        let q = parts.isEmpty ? "" : "?\(parts.joined(separator: "&"))"
        return try await request("/api/capabilities\(q)")
    }

    func importCapability(kind: String, path: String) async throws -> CapabilityItem {
        struct Body: Encodable { var kind: String; var path: String }
        return try await request("/api/capabilities/import", method: "POST", body: Body(kind: kind, path: path))
    }

    func deleteCapability(_ id: String) async throws {
        let enc = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        let _: OkFlag = try await request("/api/capabilities/\(enc)", method: "DELETE")
    }

    func patchCapabilityRegistry(
        _ id: String,
        displayName: String? = nil,
        description: String? = nil
    ) async throws {
        struct Body: Encodable {
            var display_name: String?
            var description: String?
        }
        struct Resp: Decodable {
            var capability_id: String?
        }
        let enc = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        let _: Resp = try await request(
            "/api/capability-registry/\(enc)",
            method: "PATCH",
            body: Body(display_name: displayName, description: description)
        )
    }

    func listMcpServers(workspace: String? = nil) async throws -> [McpServerConfig] {
        let q: String
        if let workspace, !workspace.isEmpty {
            let enc = workspace.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? workspace
            q = "?workspace=\(enc)"
        } else {
            q = ""
        }
        return try await request("/api/mcp-servers\(q)")
    }

    func upsertMcpServer(
        name: String,
        command: String?,
        args: [String],
        env: [String: String],
        description: String,
        type: String? = nil,
        url: String? = nil
    ) async throws {
        struct Body: Encodable {
            var name: String
            var command: String?
            var args: [String]
            var env: [String: String]
            var description: String
            var type: String?
            var url: String?
        }
        struct Resp: Decodable { var name: String? }
        let enc = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        let _: Resp = try await request(
            "/api/mcp-servers/\(enc)",
            method: "PUT",
            body: Body(
                name: name,
                command: command,
                args: args,
                env: env,
                description: description,
                type: type,
                url: url
            )
        )
    }

    func deleteMcpServer(_ name: String) async throws {
        let enc = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        let _: OkFlag = try await request("/api/mcp-servers/\(enc)", method: "DELETE")
    }

    func testMcpServer(_ name: String, workspace: String? = nil) async throws -> McpTestResult {
        struct Body: Encodable { var workspace: String? }
        let enc = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        return try await request(
            "/api/mcp-servers/\(enc)/test",
            method: "POST",
            body: Body(workspace: workspace)
        )
    }

    func listHooks(workspace: String? = nil) async throws -> [HookDefinition] {
        let query = workspace.flatMap { $0.isEmpty ? nil : $0.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) }
        return try await request("/api/hooks" + (query.map { "?workspace=\($0)" } ?? ""))
    }

    func saveHook(_ hook: HookDefinition, scope: String, workspace: String? = nil) async throws -> HookDefinition {
        var parts = ["scope=\(scope)"]
        if let workspace, !workspace.isEmpty { parts.append("workspace=\(workspace.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? workspace)") }
        let id = hook.hook_id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? hook.hook_id
        return try await request("/api/hooks/\(id)?\(parts.joined(separator: "&"))", method: "PUT", body: hook)
    }

    func deleteHook(_ id: String, scope: String, workspace: String? = nil) async throws {
        var parts = ["scope=\(scope)"]
        if let workspace, !workspace.isEmpty { parts.append("workspace=\(workspace.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? workspace)") }
        let encoded = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        let _: OkFlag = try await request("/api/hooks/\(encoded)?\(parts.joined(separator: "&"))", method: "DELETE")
    }

    func testHook(_ hook: HookDefinition) async throws -> HookTestResult {
        struct Body: Encodable { var hook: HookDefinition; var payload: [String: JSONValue] = [:] }
        return try await request("/api/hooks/test", method: "POST", body: Body(hook: hook))
    }

    func listImChannels(platform: String? = nil) async throws -> [ImChannelApplication] {
        let suffix = platform.map { "?platform=\($0)" } ?? ""
        return try await request("/api/im-channels\(suffix)")
    }

    func startImRegistration(platform: String) async throws -> ImRegistration {
        try await request("/api/im-channels/\(platform)/registrations", method: "POST")
    }

    func getImRegistration(_ id: String) async throws -> ImRegistration {
        try await request("/api/im-channels/registrations/\(id)")
    }

    func refreshImRegistration(_ id: String) async throws -> ImRegistration {
        try await request("/api/im-channels/registrations/\(id)/refresh", method: "POST")
    }

    func cancelImRegistration(_ id: String) async throws {
        let _: Empty = try await request("/api/im-channels/registrations/\(id)", method: "DELETE")
    }

    func listAssistantImBots(_ templateId: String) async throws -> ImAssistantBots {
        try await request("/api/im-channels/assistants/\(templateId)")
    }

    func bindImBot(_ botId: String, templateId: String?) async throws -> ImBot {
        struct Body: Encodable { var template_id: String? }
        return try await request(
            "/api/im-channels/bots/\(botId)/binding", method: "PUT", body: Body(template_id: templateId)
        )
    }

    func deleteImBot(_ botId: String) async throws {
        let _: Empty = try await request("/api/im-channels/bots/\(botId)", method: "DELETE")
    }

    func getTavilyWeb() async throws -> TavilyWebStatus {
        try await request("/api/web/tavily")
    }

    func setTavilyWebKey(_ apiKey: String) async throws -> TavilyWebStatus {
        struct Body: Encodable { var api_key: String }
        return try await request(
            "/api/web/tavily",
            method: "PUT",
            body: Body(api_key: apiKey)
        )
    }

    func getDingTalkConnector() async throws -> DingTalkConnectorStatus {
        try await request("/api/connectors/dingtalk")
    }

    func connectDingTalk() async throws -> DingTalkConnectorStatus {
        try await request("/api/connectors/dingtalk/connect", method: "POST")
    }

    func disconnectDingTalk() async throws -> DingTalkConnectorStatus {
        try await request("/api/connectors/dingtalk/disconnect", method: "POST")
    }

    func getLarkConnector() async throws -> ManagedConnectorStatus {
        try await request("/api/connectors/lark")
    }

    func installLarkConnector() async throws -> ManagedConnectorStatus {
        try await request("/api/connectors/lark/install", method: "POST")
    }

    func connectLarkConnector() async throws -> ManagedConnectorStatus {
        try await request("/api/connectors/lark/connect", method: "POST")
    }

    func reconnectLarkConnector() async throws -> ManagedConnectorStatus {
        try await request("/api/connectors/lark/reconnect", method: "POST")
    }

    func disconnectLarkConnector() async throws -> ManagedConnectorStatus {
        try await request("/api/connectors/lark/disconnect", method: "POST")
    }

    func uninstallLarkConnector() async throws -> ManagedConnectorStatus {
        try await request("/api/connectors/lark/uninstall", method: "POST")
    }

    func setAssistantDingTalkEnabled(_ id: String, enabled: Bool) async throws -> AgentTemplate {
        struct Body: Encodable { var enabled: Bool }
        return try await request(
            "/api/connectors/assistants/\(id)/dingtalk",
            method: "PUT",
            body: Body(enabled: enabled)
        )
    }

    func setAssistantLarkEnabled(_ id: String, enabled: Bool) async throws -> AgentTemplate {
        struct Body: Encodable { var enabled: Bool }
        return try await request(
            "/api/connectors/assistants/\(id)/lark",
            method: "PUT",
            body: Body(enabled: enabled)
        )
    }

    func listPendingQuestions(channel: String) async throws -> [PendingQuestion] {
        let encoded = channel.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? channel
        let response: PendingQuestionsResponse = try await request("/api/questions/pending?channel=\(encoded)")
        return response.items
    }

    func listPendingPermissions(channel: String) async throws -> [PermissionRequest] {
        struct Response: Decodable { var items: [PermissionRequest] }
        let encoded = channel.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? channel
        let response: Response = try await request("/api/permissions?channel=\(encoded)")
        return response.items
    }

    func answerQuestion(
        questionId: String,
        answers: [AskUserQuestionAnswer]
    ) async throws -> QuestionResolutionResponse {
        struct Body: Encodable {
            var answers: [AskUserQuestionAnswer]
        }
        let encoded = questionId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? questionId
        return try await request(
            "/api/questions/\(encoded)/answer",
            method: "POST",
            body: Body(answers: answers)
        )
    }

    func cancelQuestion(questionId: String) async throws -> QuestionResolutionResponse {
        let encoded = questionId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? questionId
        return try await request(
            "/api/questions/\(encoded)/cancel",
            method: "POST"
        )
    }

    func resolvePermission(requestId: String, approved: Bool, mode: String? = nil) async throws {
        struct Body: Encodable {
            var approved: Bool
            var mode: String?
        }
        struct Resp: Decodable {
            var request_id: String
            var approved: Bool
            var tool: String?
            var mode: String?
        }
        let _: Resp = try await request(
            "/api/permissions/\(requestId)/resolve",
            method: "POST",
            body: Body(approved: approved, mode: mode)
        )
    }

    func getAssistantCapabilities(_ id: String) async throws -> (capabilities: AssistantCapabilities, resolved: ResolvedCapabilities) {
        struct Resp: Decodable {
            var capabilities: AssistantCapabilities
            var resolved: ResolvedCapabilities
        }
        let r: Resp = try await request("/api/assistants/\(id)/capabilities")
        return (r.capabilities, r.resolved)
    }

    func putAssistantCapabilities(_ id: String, caps: AssistantCapabilities) async throws -> AgentTemplate {
        struct Resp: Decodable { var template: AgentTemplate }
        let r: Resp = try await request("/api/assistants/\(id)/capabilities", method: "PUT", body: caps)
        return r.template
    }

    func uploadFile(url fileURL: URL) async throws -> Attachment {
        let boundary = "Boundary-\(UUID().uuidString)"
        var req = URLRequest(url: URL(string: "/api/uploads", relativeTo: baseURL)!.absoluteURL)
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

        var data = Data()
        let filename = fileURL.lastPathComponent
        let fileData = try Data(contentsOf: fileURL)
        let mimeType = UTType(filenameExtension: fileURL.pathExtension)?.preferredMIMEType
            ?? "application/octet-stream"
        data.append("--\(boundary)\r\n".data(using: .utf8)!)
        data.append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n".data(using: .utf8)!)
        data.append("Content-Type: \(mimeType)\r\n\r\n".data(using: .utf8)!)
        data.append(fileData)
        data.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        req.httpBody = data

        let (respData, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse, (200 ..< 300).contains(http.statusCode) else {
            let detail = String(data: respData, encoding: .utf8) ?? "upload failed"
            throw APIError.badStatus((resp as? HTTPURLResponse)?.statusCode ?? -1, detail)
        }
        return try JSONDecoder().decode(Attachment.self, from: respData)
    }
}

struct McpTestResult: Codable {
    var ok: Bool
    var tools: [McpToolInfo]?
    var tool_count: Int?
    var error: String?
}

struct McpToolInfo: Codable, Hashable {
    var name: String?
    var description: String?
}

struct ResolvedCapabilities: Codable {
    var skills: [CapabilityItem]
    var plugins: [CapabilityItem]
    var tools: [CapabilityItem]
    var mcp_servers: [CapabilityItem]
}

private struct Empty: Decodable {}

private struct OkFlag: Decodable {
    var ok: Bool?
}

private struct AnyEncodable: Encodable {
    private let encodeFunc: (Encoder) throws -> Void
    init(_ value: any Encodable) {
        encodeFunc = { encoder in try value.encode(to: encoder) }
    }
    func encode(to encoder: Encoder) throws { try encodeFunc(encoder) }
}

private extension String {
    func trimmingPrefix(_ prefix: String) -> String {
        hasPrefix(prefix) ? String(dropFirst(prefix.count)) : self
    }
}
