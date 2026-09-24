import Foundation

struct AssistantMemory: Codable, Identifiable, Hashable {
    var memory_id: String
    var template_id: String?
    var body: String
    var source_groups: [String]?
    /// tool | confirm | manual
    var source: String?
    var ts: Double?
    var type: String?
    var summary: String?
    var scope_kind: String?
    var scope_id: String?
    var version: Int?
    var updated_at: Double?
    var source_refs: [[String: JSONValue]]?
    var status: String?

    var id: String { memory_id }

    var sourceLabel: String {
        switch (source ?? "manual").lowercased() {
        case "tool": return "工具写入"
        case "confirm": return "已确认提案"
        case "extraction", "maintenance": return "后台提取"
        case "import", "cli_import": return "CLI 导入"
        default: return "手动添加"
        }
    }
}

struct MemoryRevision: Codable, Identifiable {
    var memory_id: String
    var version: Int
    var before: AssistantMemory?
    var after: AssistantMemory
    var actor: String?
    var ts: Double?
    var id: Int { version }
}

struct MemoryCandidate: Codable, Identifiable {
    var candidate_id: String
    var job_id: String?
    var body: String
    var type: String?
    var summary: String?
    var scope_kind: String?
    var scope_id: String?
    var intent: String?
    var target_memory_id: String?
    var target_version: Int?
    var status: String
    var before_body: String?
    var source_refs: [[String: JSONValue]]?
    var id: String { candidate_id }
}

struct MemoryJob: Codable, Identifiable {
    var job_id: String
    var kind: String
    var status: String
    var error: String?
    var attempts: Int?
    var id: String { job_id }
}

struct MemoryImportPreview: Codable, Identifiable {
    struct Entry: Codable {
        var body: String
        var type: String?
        var summary: String?
        var source_path: String?
        var source_scope: String?
        var content_hash: String?
    }
    var preview_id: String
    var entries: [Entry]
    var id: String { preview_id }
}

struct AssistantCapabilities: Codable, Hashable {
    var skills: [String] = []
    var plugins: [String] = []
    var tools: [String] = []
    var mcp_servers: [String] = []
}

struct HookHandler: Codable, Hashable {
    var type: String = "command"
    var command: String = ""
    var args: [String] = []
    var mode: String = "observe"
    var timeout_seconds: Double = 10
    var payload_profile: String = "tigerose-v1"
}

struct HookDefinition: Codable, Identifiable, Hashable {
    var hook_id: String
    var display_name: String
    var enabled: Bool = true
    var event: String
    var priority: Int = 100
    var matcher: [String: [String]] = [:]
    var handler: HookHandler
    var failure_policy: String = "allow"
    var description: String = ""
    var source: String?
    var overrides_global: Bool?
    var readonly: Bool?
    var id: String { hook_id }
}

struct HookTestResult: Codable {
    var execution: String
    var effects: [[String: JSONValue]]
    var control: [String: JSONValue]
    var failure_kind: String?
    var duration_ms: Int
}

struct AgentTemplate: Codable, Identifiable, Hashable {
    var template_id: String
    var name: String
    var role: String
    var system_prompt: String
    var tools_allowlist: [String]?
    var capabilities: AssistantCapabilities?
    var theme_color: String
    var avatar_id: String
    var model_profile_id: String?
    var config_meta: [String: JSONValue]?
    /// Latest DM snippet for sidebar (server-truncated).
    var last_message_preview: String?
    var unread_count: Int?

    var id: String { template_id }
}

struct ProjectGroup: Codable, Identifiable, Hashable {
    var group_id: String
    var name: String
    var workspace_path: String
    var status: String
    var settings: [String: JSONValue]?
    var member_count: Int?
    var members: [GroupMember]?
    /// Latest feed snippet for sidebar (server-truncated).
    var last_message_preview: String?

    var id: String { group_id }

    var webEnabled: Bool {
        settings?["web_enabled"]?.boolValue ?? false
    }
}

extension AgentTemplate {
    var webEnabled: Bool {
        config_meta?["web_enabled"]?.boolValue ?? false
    }

    var fileAccess: String {
        config_meta?["file_access"]?.stringValue ?? "ask"
    }

    var dangerPolicy: String {
        config_meta?["danger_policy"]?.stringValue ?? "deny"
    }

    var dangerRules: [String] {
        if let arr = config_meta?["danger_rules"]?.arrayValue {
            return arr.compactMap(\.stringValue)
        }
        return ["dangerous_bash"]
    }

    var dingtalkEnabled: Bool {
        let value = config_meta?["connectors"]?.objectValue?["dingtalk"]
        return value?.boolValue ?? value?.objectValue?["enabled"]?.boolValue ?? false
    }

    var larkEnabled: Bool {
        let value = config_meta?["connectors"]?.objectValue?["lark"]
        return value?.boolValue ?? value?.objectValue?["enabled"]?.boolValue ?? false
    }
}

struct GroupMember: Codable, Identifiable, Hashable {
    var instance_id: String
    var group_id: String
    var template_id: String
    var display_name: String
    var theme_color: String
    var avatar_id: String
    var is_coordinator: Int
    var runtime_status: String
    var model_profile_id: String?
    var model_label: String?

    var id: String { instance_id }
}

struct FeedEvent: Codable, Identifiable, Hashable {
    var event_id: String
    var group_id: String
    var speaker_type: String
    var speaker_id: String
    var visibility: String
    var content: String
    var ts: Double
    var meta: [String: JSONValue]?

    var id: String { event_id }

    var runIds: [String] { Self.extractRunIds(meta) }

    var attachments: [Attachment] {
        (meta?["attachments"]?.arrayValue ?? []).compactMap { item in
            guard let value = item.objectValue,
                  let path = value["path"]?.stringValue,
                  !path.isEmpty
            else { return nil }
            let name = value["name"]?.stringValue ?? URL(fileURLWithPath: path).lastPathComponent
            return Attachment(
                path: path,
                name: name,
                mime: value["mime"]?.stringValue ?? "application/octet-stream",
                size: value["size"]?.numberValue.map { Int($0) },
                is_image: value["kind"]?.stringValue == "image"
            )
        }
    }

    var displayContent: String {
        guard !attachments.isEmpty else { return content }
        let summary = "[附件]\n" + attachments.map { "- \($0.name): \($0.path)" }.joined(separator: "\n")
        let suffix = "\n\n" + summary
        guard content.hasSuffix(suffix) else { return content }
        return String(content.dropLast(suffix.count))
    }

    fileprivate static func extractRunIds(_ meta: [String: JSONValue]?) -> [String] {
        guard let runs = meta?["runs"]?.arrayValue else { return [] }
        return runs.compactMap { item in
            item.objectValue?["run_id"]?.stringValue
        }.filter { !$0.isEmpty }
    }
}

struct AssistantMessage: Codable, Identifiable, Hashable {
    var message_id: String
    var template_id: String
    var role: String
    var content: String
    var ts: Double
    var meta: [String: JSONValue]?
    var session_id: String?

    var id: String { message_id }

    var runIds: [String] { FeedEvent.extractRunIds(meta) }

    var isIntermediate: Bool {
        meta?["intermediate"]?.boolValue ?? false
    }

    var attachments: [Attachment] {
        (meta?["attachments"]?.arrayValue ?? []).compactMap { item in
            guard let value = item.objectValue,
                  let path = value["path"]?.stringValue,
                  !path.isEmpty
            else { return nil }
            let name = value["name"]?.stringValue ?? URL(fileURLWithPath: path).lastPathComponent
            return Attachment(
                path: path,
                name: name,
                mime: value["mime"]?.stringValue ?? "application/octet-stream",
                size: value["size"]?.numberValue.map { Int($0) },
                is_image: value["kind"]?.stringValue == "image"
            )
        }
    }
}

struct AssistantSession: Codable, Identifiable, Hashable {
    var session_id: String
    var title: String
    var last_message_at: Double?
    var message_count: Int?
    var unread_count: Int?
    var im_channel: ImConversationSummary?

    var id: String { session_id }
}

struct UsageSummary: Codable, Hashable {
    var input_tokens: Int
    var cached_input_tokens: Int
    var uncached_input_tokens: Int
    var output_tokens: Int
    var total_tokens: Int
    var call_count: Int
    var provider_call_count: Int
    var estimated_call_count: Int
    var cache_available_call_count: Int?
    var usage_source: String?
    var spend_cny: Double
    var unpriced_model_count: Int

    static func from(meta: [String: JSONValue]?) -> UsageSummary? {
        guard let object = meta?["usage_summary"]?.objectValue,
              let input = object["input_tokens"]?.numberValue,
              let cached = object["cached_input_tokens"]?.numberValue,
              let uncached = object["uncached_input_tokens"]?.numberValue,
              let output = object["output_tokens"]?.numberValue,
              let total = object["total_tokens"]?.numberValue,
              let calls = object["call_count"]?.numberValue,
              let providerCalls = object["provider_call_count"]?.numberValue,
              let estimatedCalls = object["estimated_call_count"]?.numberValue
        else { return nil }
        return UsageSummary(
            input_tokens: Int(input), cached_input_tokens: Int(cached),
            uncached_input_tokens: Int(uncached), output_tokens: Int(output),
            total_tokens: Int(total), call_count: Int(calls),
            provider_call_count: Int(providerCalls), estimated_call_count: Int(estimatedCalls),
            cache_available_call_count: object["cache_available_call_count"]?.numberValue.map(Int.init),
            usage_source: object["usage_source"]?.stringValue,
            spend_cny: 0,
            unpriced_model_count: 0
        )
    }
}

struct UsageOverviewResponse: Codable, Hashable {
    var start_date: String
    var end_date: String
    var data: UsageSummary
}

struct UsageTrendItem: Codable, Hashable, Identifiable {
    var date: String
    var provider: String
    var model: String
    var input_tokens: Int
    var cached_input_tokens: Int
    var uncached_input_tokens: Int
    var output_tokens: Int
    var total_tokens: Int
    var call_count: Int
    var spend_cny: Double?
    var price_status: String

    var id: String { "\(date)-\(provider)-\(model)" }
}

struct UsageTrendResponse: Codable, Hashable {
    var start_date: String
    var end_date: String
    var items: [UsageTrendItem]
}

struct UsageModelItem: Codable, Hashable, Identifiable {
    var provider: String
    var model: String
    var input_tokens: Int
    var cached_input_tokens: Int
    var uncached_input_tokens: Int
    var output_tokens: Int
    var total_tokens: Int
    var call_count: Int
    var estimated_call_count: Int
    var spend_cny: Double?
    var price_status: String

    var id: String { "\(provider)-\(model)" }
}

struct UsageModelResponse: Codable, Hashable {
    var start_date: String
    var end_date: String
    var items: [UsageModelItem]
}

struct UsageAgentItem: Codable, Hashable, Identifiable {
    var agent_id: String
    var agent_name: String
    var input_tokens: Int
    var cached_input_tokens: Int
    var uncached_input_tokens: Int
    var output_tokens: Int
    var total_tokens: Int
    var call_count: Int
    var estimated_call_count: Int
    var spend_cny: Double
    var unpriced_model_count: Int

    var id: String { agent_id.isEmpty ? "platform" : agent_id }
}

struct UsageAgentResponse: Codable, Hashable {
    var start_date: String
    var end_date: String
    var items: [UsageAgentItem]
}

struct UsagePricingFileResponse: Codable, Hashable {
    var path: String
}

struct ImConversationSummary: Codable, Hashable {
    var platform: String
    var kind: String
    var display_name: String
}

struct ImBot: Codable, Identifiable, Hashable {
    var bot_id: String
    var template_id: String?
    var provider_bot_id: String
    var display_name: String
    var health: String
    var last_error: String
    var platform: String?
    var provider_application_id: String?
    var application_name: String?
    var template_name: String?

    var id: String { bot_id }
}

struct ImChannelApplication: Codable, Identifiable, Hashable {
    var application_id: String
    var platform: String
    var provider_application_id: String
    var display_name: String
    var status: String
    var bots: [ImBot]

    var id: String { application_id }
}

struct ImAssistantBots: Codable, Hashable {
    var bound: [ImBot]
    var available: [ImBot]
}

struct ImRegistration: Codable, Hashable, Identifiable {
    var id: String
    var platform: String
    var state: String
    var qr_image: String?
    var expires_in: Int?
    var bot_id: String?
    var display_name: String?
    var error: String?
    var message: String?
}

struct SessionsCurrent: Codable, Hashable {
    var session_id: String
    var title: String
    var message_count: Int
}

struct SessionsResponse: Codable, Hashable {
    var current: SessionsCurrent?
    var items: [AssistantSession]
}

struct SessionActivateResponse: Codable {
    var current: SessionsCurrent?
    var messages: Page<AssistantMessage>?
}

struct PendingQuestionsResponse: Codable {
    var items: [PendingQuestion]
}

struct PendingQuestion: Codable, Identifiable, Hashable {
    var question_id: String
    var tool_call_id: String
    var goal_id: String
    var run_id: String
    var channel: String
    var surface: String
    var template_id: String?
    var group_id: String?
    var instance_id: String?
    var questions: [AskUserQuestion]
    var status: String
    var created_at: Double

    var id: String { question_id }

    private enum CodingKeys: String, CodingKey {
        case question_id, tool_call_id, goal_id, run_id, channel, surface
        case template_id, group_id, instance_id, questions, status, created_at, payload
    }

    private struct PayloadBox: Codable {
        var questions: [AskUserQuestion]?
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        question_id = try container.decode(String.self, forKey: .question_id)
        tool_call_id = try container.decode(String.self, forKey: .tool_call_id)
        goal_id = try container.decodeIfPresent(String.self, forKey: .goal_id) ?? ""
        run_id = try container.decodeIfPresent(String.self, forKey: .run_id) ?? ""
        channel = try container.decode(String.self, forKey: .channel)
        surface = try container.decodeIfPresent(String.self, forKey: .surface) ?? ""
        template_id = try container.decodeIfPresent(String.self, forKey: .template_id)
        group_id = try container.decodeIfPresent(String.self, forKey: .group_id)
        instance_id = try container.decodeIfPresent(String.self, forKey: .instance_id)
        status = try container.decodeIfPresent(String.self, forKey: .status) ?? "pending"
        created_at = try container.decodeIfPresent(Double.self, forKey: .created_at) ?? 0
        if let flat = try container.decodeIfPresent([AskUserQuestion].self, forKey: .questions),
           !flat.isEmpty
        {
            questions = flat
        } else if let payload = try container.decodeIfPresent(PayloadBox.self, forKey: .payload) {
            questions = payload.questions ?? []
        } else {
            questions = []
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(question_id, forKey: .question_id)
        try container.encode(tool_call_id, forKey: .tool_call_id)
        try container.encode(goal_id, forKey: .goal_id)
        try container.encode(run_id, forKey: .run_id)
        try container.encode(channel, forKey: .channel)
        try container.encode(surface, forKey: .surface)
        try container.encodeIfPresent(template_id, forKey: .template_id)
        try container.encodeIfPresent(group_id, forKey: .group_id)
        try container.encodeIfPresent(instance_id, forKey: .instance_id)
        try container.encode(questions, forKey: .questions)
        try container.encode(status, forKey: .status)
        try container.encode(created_at, forKey: .created_at)
    }
}

struct AskUserQuestion: Codable, Identifiable, Hashable {
    var id: String
    var header: String
    var question: String
    var options: [AskUserQuestionOption]
    var multi_select: Bool
}

struct AskUserQuestionOption: Codable, Hashable {
    var label: String
    var description: String
}

struct AskUserQuestionAnswer: Codable, Hashable {
    var question_id: String
    var selected_labels: [String]
    var other_text: String
}

struct AskUserSnapshot: Hashable {
    var questionId: String
    var status: String
    var questions: [AskUserQuestion]
    var answers: [AskUserQuestionAnswer]

    var isPending: Bool { status == "pending" || status.isEmpty }
    var isAnswered: Bool { status == "answered" }
    var isCancelled: Bool { status == "cancelled" }

    static func from(meta: [String: JSONValue]?) -> AskUserSnapshot? {
        guard let ask = meta?["ask_user"]?.objectValue else { return nil }
        let questionId = ask["question_id"]?.stringValue ?? ""
        guard !questionId.isEmpty else { return nil }
        var questions: [AskUserQuestion] = []
        if let arr = ask["questions"]?.arrayValue {
            for item in arr {
                guard let obj = item.objectValue else { continue }
                let id = obj["id"]?.stringValue ?? ""
                let header = obj["header"]?.stringValue ?? ""
                let question = obj["question"]?.stringValue ?? ""
                guard !id.isEmpty, !question.isEmpty else { continue }
                var options: [AskUserQuestionOption] = []
                if let opts = obj["options"]?.arrayValue {
                    for opt in opts {
                        guard let o = opt.objectValue else { continue }
                        let label = o["label"]?.stringValue ?? ""
                        guard !label.isEmpty else { continue }
                        options.append(
                            AskUserQuestionOption(
                                label: label,
                                description: o["description"]?.stringValue ?? ""
                            )
                        )
                    }
                }
                questions.append(
                    AskUserQuestion(
                        id: id,
                        header: header,
                        question: question,
                        options: options,
                        multi_select: obj["multi_select"]?.boolValue ?? false
                    )
                )
            }
        }
        var answers: [AskUserQuestionAnswer] = []
        if let arr = ask["answers"]?.arrayValue {
            for item in arr {
                guard let obj = item.objectValue else { continue }
                let qid = obj["question_id"]?.stringValue ?? ""
                guard !qid.isEmpty else { continue }
                let labels = (obj["selected_labels"]?.arrayValue ?? []).compactMap(\.stringValue)
                answers.append(
                    AskUserQuestionAnswer(
                        question_id: qid,
                        selected_labels: labels,
                        other_text: obj["other_text"]?.stringValue ?? ""
                    )
                )
            }
        }
        return AskUserSnapshot(
            questionId: questionId,
            status: ask["status"]?.stringValue ?? "pending",
            questions: questions,
            answers: answers
        )
    }

    func answer(for questionId: String) -> AskUserQuestionAnswer? {
        answers.first { $0.question_id == questionId }
    }
}

struct RunRef: Codable, Hashable {
    var run_id: String
    var agent_id: String?
}

struct QuestionResolutionResponse: Codable {
    var ok: Bool
    var runs: [RunRef]
}

struct Attachment: Codable, Hashable {
    var path: String
    var name: String
    var mime: String
    var size: Int?
    var is_image: Bool?
}

struct ManagedModelProfile: Codable, Identifiable, Hashable {
    var id: String
    var label: String
    var provider: String
    var base_url: String
    var api_key_masked: String
    var has_api_key: Bool
    var reasoning_effort: String?
}

struct GoalEvaluatorModelSettings: Codable, Hashable {
    var profile_id: String
    var configured_profile_id: String
    var effective_profile_id: String
    var fallback: Bool
}

struct CapabilityItem: Codable, Identifiable, Hashable {
    var id: String
    var kind: String
    var name: String
    var display_name: String
    var description: String
    var source_path: String?
    var source: String?
    var missing: Bool?
    var from_plugin: String?
    var dangerous: Bool?
    var nested_skills: [String]?
    var runtime_required: Bool?
    var deletable: Bool?
    var editable: Bool?
    var meta: [String: JSONValue]?
}

struct McpServerConfig: Codable, Identifiable, Hashable {
    var id: String
    var name: String
    var type: String?
    var command: String?
    var args: [String]?
    var env: [String: String]?
    var url: String?
    var description: String?
    var source: String?
    var editable: Bool?
    var deletable: Bool?
}

struct TavilyWebStatus: Codable, Hashable {
    var configured: Bool
    var api_key_masked: String?
    var has_api_key: Bool?
    var search_backend: String?
    var extract_backend: String?
}

struct DingTalkConnectorStatus: Codable, Hashable {
    var installed: Bool
    var connected: Bool
    var state: String
    var account_name: String?
    var corp_name: String?
    var message: String?
}

struct ManagedConnectorStatus: Codable, Hashable {
    var connector_id: String
    var display_name: String
    var installed: Bool
    var authenticated: Bool
    var health: String
    var active_version: String?
    var app_id: String? = nil
    var app_name: String? = nil
    var account_name: String?
    var corp_name: String?
    var message: String?
}

struct Page<T: Codable>: Codable {
    var items: [T]
    var has_more: Bool
    var limit: Int
}

/// Minimal JSON value for loosely-typed meta dictionaries.
enum JSONValue: Codable, Hashable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let v = try? c.decode(Bool.self) { self = .bool(v); return }
        if let v = try? c.decode(Double.self) { self = .number(v); return }
        if let v = try? c.decode(String.self) { self = .string(v); return }
        if let v = try? c.decode([String: JSONValue].self) { self = .object(v); return }
        if let v = try? c.decode([JSONValue].self) { self = .array(v); return }
        self = .null
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }

    var stringValue: String? {
        if case .string(let s) = self { return s }
        return nil
    }

    var numberValue: Double? {
        if case .number(let n) = self { return n }
        return nil
    }

    var objectValue: [String: JSONValue]? {
        if case .object(let o) = self { return o }
        return nil
    }

    var boolValue: Bool? {
        if case .bool(let b) = self { return b }
        return nil
    }

    var arrayValue: [JSONValue]? {
        if case .array(let a) = self { return a }
        return nil
    }
}

struct ThinkingStep: Codable, Hashable, Identifiable {
    var kind: String
    var name: String
    var detail: String
    var ts: Double

    var id: String { "\(kind)-\(name)-\(ts)" }

    var label: String {
        switch kind {
        case "query": return "[query]"
        case "task_state": return "[task_state]"
        case "capabilities": return "[capabilities]"
        case "tool_call": return "[tool_call]"
        case "tool_result": return "[tool_result]"
        case "hook": return "[hook]"
        case "final_output": return "[final_output]"
        default: return "[\(kind)]"
        }
    }
}

struct ThinkingMeta: Codable, Hashable {
    var duration_ms: Int
    var steps: [ThinkingStep]
    var intent: String?
    var budgetSummary: String?
    var runOutcome: String?
    var executionStatus: String?
    var evidenceStatus: String?
    var resultVerdict: String?
    var classificationStatus: String?

    static func from(meta: [String: JSONValue]?) -> ThinkingMeta? {
        let executionChain = meta?["execution_chain"]?.objectValue
        guard let source = executionChain ?? meta?["thinking"]?.objectValue else { return nil }
        let duration = Int(meta?["thinking"]?.objectValue?["duration_ms"]?.numberValue ?? 0)
        let arr = source["steps"]?.arrayValue ?? []
        var steps: [ThinkingStep] = []
        for item in arr {
            guard let obj = item.objectValue else { continue }
            let kind = obj["kind"]?.stringValue ?? ""
            let name = obj["name"]?.stringValue ?? ""
            guard !kind.isEmpty, !name.isEmpty else { continue }
            steps.append(
                ThinkingStep(
                    kind: kind,
                    name: name,
                    detail: obj["detail"]?.stringValue ?? "",
                    ts: obj["ts"]?.numberValue ?? 0
                )
            )
        }
        let thinking = meta?["thinking"]?.objectValue
        if let recall = thinking?["memory_recall"]?.objectValue {
            let status = recall["selection_status"]?.stringValue ?? "failed"
            let statusLabel = ["no_match": "无相关记忆", "text": "文本召回", "model": "模型召回",
                               "degraded": "已降级召回", "failed": "召回失败"][status] ?? status
            let elapsed = Int(recall["elapsed_ms"]?.numberValue ?? 0)
            let items = recall["items"]?.arrayValue ?? []
            let details = items.compactMap { item -> String? in
                guard let row = item.objectValue, let id = row["memory_id"]?.stringValue else { return nil }
                let version = Int(row["version"]?.numberValue ?? 1)
                let scope = row["scope_kind"]?.stringValue == "group" ? "群" : "助理"
                return "\(id) · v\(version) · \(scope) \(row["scope_id"]?.stringValue ?? "")"
            }
            steps.insert(ThinkingStep(kind: "memory", name: "记忆召回",
                                      detail: (["\(statusLabel) · \(items.count) 条 · \(elapsed) ms"] + details).joined(separator: "\n"), ts: 0), at: 0)
        }
        guard !steps.isEmpty else { return nil }
        let intent = thinking?["intent"]?.stringValue ?? meta?["intent"]?.stringValue
        let runOutcome = thinking?["run_outcome"]?.stringValue ?? meta?["run_outcome_status"]?.stringValue
        let executionStatus = thinking?["execution_status"]?.stringValue ?? meta?["execution_status"]?.stringValue
        let evidenceStatus = thinking?["evidence_status"]?.stringValue ?? meta?["evidence_status"]?.stringValue
        let resultVerdict = thinking?["result_verdict"]?.stringValue ?? meta?["result_verdict"]?.stringValue
        let classificationStatus = thinking?["classification_status"]?.stringValue ?? meta?["classification_status"]?.stringValue
        var budgetSummary: String?
        if let budget = thinking?["budget"]?.objectValue {
            let tools = Int(budget["tool_calls"]?.numberValue ?? 0)
            let toolLimit = Int(budget["tool_limit"]?.numberValue ?? 0)
            let timeout = Int(budget["timeout_s"]?.numberValue ?? 0)
            let profile = budget["profile"]?.stringValue ?? ""
            let intentLabel: String = {
                switch intent {
                case "one_shot_action": return "一次性任务"
                case "durable_goal": return "长期目标"
                case "read_only": return "只读查询"
                case "conversation": return "对话"
                default: return profile.isEmpty ? "任务" : profile
                }
            }()
            if toolLimit > 0 {
                budgetSummary = "\(intentLabel) · 工具 \(tools)/\(toolLimit)"
                if timeout > 0 {
                    let secs = max(1, Int((Double(duration) / 1000.0).rounded()))
                    budgetSummary = "\(budgetSummary!) · \(secs)s/\(timeout)s"
                }
            } else if intent != nil {
                budgetSummary = intentLabel
            }
        } else if classificationStatus == "unavailable" {
            budgetSummary = "任务类型识别中"
        } else if let intent {
            switch intent {
            case "one_shot_action": budgetSummary = "一次性任务"
            case "durable_goal": budgetSummary = "长期目标"
            case "read_only": budgetSummary = "只读查询"
            default: break
            }
        }
        return ThinkingMeta(
            duration_ms: duration,
            steps: steps,
            intent: intent,
            budgetSummary: budgetSummary,
            runOutcome: runOutcome,
            executionStatus: executionStatus,
            evidenceStatus: evidenceStatus,
            resultVerdict: resultVerdict,
            classificationStatus: classificationStatus
        )
    }

    static func from(stepJSON data: Data) -> ThinkingStep? {
        try? JSONDecoder().decode(ThinkingStep.self, from: data)
    }
}
