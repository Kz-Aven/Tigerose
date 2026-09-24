import Foundation

struct MeshTaskPage: Decodable { var items: [MeshTask]; var next_cursor: String? }
struct MeshMessagePage: Decodable { var items: [MeshMessage]; var next_sequence: Int }

struct MeshTask: Decodable, Identifiable {
    var task_id: String
    var workflow_id: String
    var title: String
    var objective: String?
    var status: String
    var caller_id: String?
    var target_id: String?
    var parent_task_id: String?
    var caller_snapshot: [String: JSONValue]?
    var target_snapshot: [String: JSONValue]?
    var wait_reason: String?
    var last_error: String?
    var acceptance_criteria: JSONValue?
    var submissions: [MeshSubmission]?
    var reviews: [MeshReview]?
    var decisions: [MeshDecision]?
    var artifacts: [MeshArtifact]?
    var executions_stopping: Int?
    var usage: [String: JSONValue]?
    var coordination_status: String?
    var coordination_wait_reason: String?
    var id: String { task_id }
    var isTerminal: Bool { ["completed", "failed", "cancelled", "timeout"].contains(status) }
    var coordinationTitle: String? {
        guard let coordination_status else { return nil }
        switch coordination_status {
        case "completed": return "整体协作：汇总回传已完成"
        case "waiting": return ["user_input", "permission", "budget", "limit_reached", "reconciliation"].contains(coordination_wait_reason ?? "") ? "整体协作：汇总回传等待你的决定" : "整体协作：汇总回传等待处理"
        case "running": return "整体协作：正在汇总回传"
        case "cancelled": return "整体协作：已取消"
        case "failed", "timeout": return "整体协作：汇总回传未完成"
        default: return "整体协作：等待汇总回传"
        }
    }
    static let statuses = ["queued", "running", "waiting", "submitted", "revision_requested", "completed", "failed", "cancelled", "timeout"]
    static func statusTitle(_ status: String) -> String {
        ["queued": "排队中", "running": "执行中", "waiting": "等待中", "submitted": "待助理验收", "revision_requested": "待修改", "completed": "任务已验收", "failed": "失败", "cancelled": "已取消", "timeout": "超时", "active": "协作中"][status] ?? status
    }
}

struct MeshMessage: Decodable, Identifiable {
    var message_id: String
    var sequence: Int
    var sender_id: String?
    var kind: String
    var body: JSONValue
    var created_at: Double?
    var id: String { message_id }
    var kindTitle: String {
        ["message": "消息", "clarification_request": "请求澄清", "information_request": "请求补充", "decision_request": "请求决策", "progress": "进度", "artifact": "交付物", "task_created": "创建任务", "submitted": "提交结果", "changes_requested": "退回修改", "accepted": "验收通过", "failed": "失败", "cancelled": "取消", "timed_out": "超时", "user_decision": "用户决定"][kind] ?? kind
    }
}

struct MeshSubmission: Decodable {
    var revision: Int
    var result: JSONValue
    var summary: String { result.objectValue?["summary"]?.meshText ?? result.meshText }
}
struct MeshReview: Decodable { var decision: String; var reason: String? }
struct MeshArtifact: Decodable, Identifiable {
    var artifact_id: String
    var kind: String?
    var metadata: [String: JSONValue]?
    var id: String { artifact_id }
    var displayName: String { metadata?["display_name"]?.stringValue ?? metadata?["name"]?.stringValue ?? kind ?? artifact_id }
}
struct MeshDecision: Decodable, Identifiable {
    var task_title: String?
    var requester_name: String?
    var decision_id: String
    var reason: String
    var allowed_actions: [String]
    var expected_task_revision: Int
    var status: String
    var payload: [String: JSONValue]?
    var id: String { decision_id }
    var isInterruptedTurn: Bool {
        reason == "user_decision" && payload?["reason"]?.stringValue == "Execution ended without submit or explicit wait"
    }
    var explanation: String {
        if isInterruptedTurn {
            return payload?["previous_status"]?.stringValue == "submitted"
                ? "助理已结束本轮，但尚未验收已提交的结果。继续处理后将恢复验收与汇总。"
                : "助理已结束本轮，但尚未完成提交或明确等待。继续处理后将恢复此任务。"
        }
        return payload?["question"]?.stringValue ?? payload?["detail"]?.stringValue
            ?? payload?["reason"]?.stringValue ?? "请补充你的决定或处理要求，助理将据此继续此任务。"
    }
    var questionRequest: PendingQuestion? {
        guard reason == "user_input", let questions = payload?["questions"]?.arrayValue, !questions.isEmpty else { return nil }
        let value = JSONValue.object([
            "question_id": .string(decision_id), "tool_call_id": .string(decision_id),
            "channel": .string("mesh"), "questions": .array(questions)
        ])
        guard let data = try? JSONEncoder().encode(value) else { return nil }
        return try? JSONDecoder().decode(PendingQuestion.self, from: data)
    }
}
struct MeshDecisionPage: Decodable { var items: [MeshDecision] }
struct MeshGraph: Codable {
    var workflow: [String: JSONValue]?
    var workflow_id: String
    var nodes: [MeshGraphNode]
    var edges: [MeshEdge]
    var dependencies: [MeshEdge] { edges.filter { $0.kind == "dependency" } }
    var isTerminal: Bool { ["completed", "cancelled", "failed", "timeout"].contains(workflow?["status"]?.stringValue ?? "") }
}
struct MeshGraphNode: Codable, Identifiable {
    var node_id: String
    var kind: String
    var task_id: String?
    var assistant_id: String?
    var assistant_name: String?
    var caller_id: String?
    var target_id: String?
    var parent_task_id: String?
    var task_title: String?
    var title: String
    var status: String
    var id: String { node_id }
    var isTerminal: Bool { ["completed", "cancelled", "failed", "timeout"].contains(status) }
}
struct MeshEdge: Codable, Identifiable {
    var source: String
    var target: String
    var kind: String
    var from: String { source }
    var to: String { target }
    var id: String { "\(kind):\(source):\(target)" }
}
struct MeshCollaboration: Decodable {
    var outgoing: [String]
    var incoming: [String]
    var accepting_tasks: Bool
    var revision: Int
}

extension JSONValue {
    var meshText: String {
        switch self {
        case .string(let value): return value
        case .array(let values): return values.map(\.meshText).joined(separator: "\n")
        case .object(let values): return values.keys.sorted().map { "\($0)：\(values[$0]!.meshText)" }.joined(separator: "\n")
        case .number(let value):
            if value.rounded() == value, value >= Double(Int64.min), value <= Double(Int64.max) {
                return String(Int64(value))
            }
            return String(format: "%.12f", value)
                .replacingOccurrences(of: #"(\\.\\d*?[1-9])0+$"#, with: "$1", options: .regularExpression)
                .replacingOccurrences(of: #"\\.0+$"#, with: "", options: .regularExpression)
        case .bool(let value): return value ? "是" : "否"
        case .null: return ""
        }
    }
}
