import SwiftUI

struct PermissionRequest: Codable, Identifiable, Hashable {
    var request_id: String
    var tool: String
    var reason: String
    var detail: String
    var args_preview: [String: String]?
    var timeout_s: Int?
    var domain: String?
    var approval_choices: [String]?

    var id: String { request_id }

    var offersCapabilityGrant: Bool {
        if let choices = approval_choices, !choices.isEmpty {
            return choices.contains("once") && choices.contains("always")
        }
        switch reason {
        case "scope_capability_domain",
             "scope_unknown_mcp",
             "scope_domain",
             "scope_runtime_edit",
             "scope_runtime_domain",
             "scope_bash",
             "scope_bash_unknown",
             "scope_bash_mutation":
            return true
        default:
            return false
        }
    }

    var isHighRisk: Bool {
        reason == "dangerous_bash" || reason == "scope_bash_high_risk"
    }

    var command: String {
        args_preview?["command"]?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    private var fileAction: String {
        switch tool {
        case "read_file", "excel_read": return "读取"
        case "write_file", "edit_file", "excel_write": return "修改"
        default: return "访问"
        }
    }

    private var externalService: String {
        switch domain ?? "" {
        case "dingtalk.message": return "钉钉消息"
        case "dingtalk.calendar": return "钉钉日历"
        case "dingtalk.todo": return "钉钉待办"
        default: return domain?.replacingOccurrences(of: "mcp.", with: "") ?? "外部服务"
        }
    }

    private var bashIsReadOnly: Bool {
        let trimmed = command.lowercased()
        return trimmed.hasPrefix("git status")
            || trimmed.hasPrefix("git diff")
            || trimmed.hasPrefix("git log")
            || trimmed.hasPrefix("git branch")
            || trimmed.hasPrefix("ls")
            || trimmed.hasPrefix("pwd")
    }

    var title: String {
        switch reason {
        case "external_path":
            return fileAction == "读取" ? "访问项目外文件" : "修改项目外文件"
        case "dangerous_bash":
            return "运行可能影响电脑的命令"
        case "session_clear":
            return "需要清空当前对话"
        case "scope_capability_domain":
            return "使用未启用的服务"
        case "scope_runtime_edit", "scope_runtime_domain":
            return reason == "scope_runtime_edit" ? "修改助理运行代码" : "修改助理运行配置"
        case "scope_bash":
            return bashIsReadOnly ? "运行项目检查命令" : "运行终端命令"
        case "scope_bash_unknown":
            return "运行未验证的命令计划"
        case "scope_bash_mutation":
            return "执行工作区写入计划"
        case "scope_bash_high_risk":
            return "运行高风险终端命令"
        case "scope_unknown_mcp":
            return "使用未验证的外部工具"
        default:
            return "需要你的确认"
        }
    }

    var userDetail: String {
        switch reason {
        case "external_path":
            return fileAction == "读取"
                ? "助理想读取项目目录以外的文件，继续完成你的请求。"
                : "助理想在项目目录以外创建或修改文件，请确认目标位置正确。"
        case "dangerous_bash":
            return "这条命令可能修改文件、删除内容或访问网络。请确认后再执行。"
        case "scope_bash":
            return bashIsReadOnly
                ? "助理想检查当前项目状态，不会修改文件。"
                : "助理想在当前项目中运行命令，请确认该命令符合你的预期。"
        case "scope_bash_unknown":
            return "系统无法确认这些命令只读或只访问当前项目。你可以确认本次计划，或允许本任务内同类命令。"
        case "scope_bash_mutation":
            return "这些命令会修改当前项目中的内容。你可以确认本次计划，或允许本任务内同类写入。"
        case "scope_bash_high_risk":
            return "这条命令可能造成高影响操作。每次新增或变化都需要单独确认。"
        case "session_clear":
            return "当前聊天消息将从界面中清除。系统会先保留审计归档，但无法在聊天界面直接撤销。"
        case "scope_capability_domain":
            return "助理想使用未为此助理启用的 \(externalService)，继续完成你的请求。"
        case "scope_unknown_mcp":
            return "系统无法判断这个外部工具会产生什么影响，需要你先确认。"
        case "scope_runtime_edit", "scope_runtime_domain":
            return reason == "scope_runtime_edit"
                ? "助理想修改影响自身行为的代码或测试文件，而不只是处理业务内容。"
                : "助理想修改运行时配置、状态或内部行为。"
        default:
            return "助理需要额外权限才能继续完成你的请求。"
        }
    }

    var detailLabel: String {
        switch reason {
        case "external_path": return "目标位置与原因"
        case "dangerous_bash", "scope_bash", "scope_bash_unknown", "scope_bash_mutation", "scope_bash_high_risk": return "命令与影响"
        case "scope_capability_domain", "scope_unknown_mcp": return "服务与操作详情"
        default: return "操作详情"
        }
    }
}

enum PermissionDecision {
    case deny
    case allowOnce
    case allowAlways
}

/// Docked strip above the composer for HITL tool permission.
struct PermissionBanner: View {
    let request: PermissionRequest
    var onDecide: (PermissionDecision) -> Void
    @State private var busy = false
    @State private var isDetailExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "lock.shield")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(TigeroseTheme.warning)
                Text(request.title)
                    .font(TigeroseTheme.bodySm.weight(.semibold))
                    .foregroundStyle(TigeroseTheme.ink)
                Spacer(minLength: 0)
                Text(request.tool)
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(TigeroseTheme.muted)
            }

            Text(request.userDetail)
                .font(TigeroseTheme.caption)
                .foregroundStyle(TigeroseTheme.ink)
                .lineLimit(8)
                .frame(maxWidth: .infinity, alignment: .leading)

            DisclosureGroup(request.detailLabel, isExpanded: $isDetailExpanded) {
                ScrollView(.vertical) {
                    VStack(alignment: .leading, spacing: 6) {
                        if !request.command.isEmpty {
                            Text(request.command)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(TigeroseTheme.ink)
                                .textSelection(.enabled)
                        }
                        Text(request.detail)
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(TigeroseTheme.muted)
                            .textSelection(.enabled)
                        if let domain = request.domain, !domain.isEmpty {
                            Text("权限范围：\(domain)")
                                .font(TigeroseTheme.caption)
                                .foregroundStyle(TigeroseTheme.muted)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 2)
                }
                .frame(maxHeight: 128, alignment: .top)
            }
            .font(TigeroseTheme.caption)
            .foregroundStyle(TigeroseTheme.muted)

            HStack(spacing: 8) {
                Spacer(minLength: 0)
                Button {
                    guard !busy else { return }
                    busy = true
                    onDecide(.deny)
                } label: {
                    Text("拒绝")
                        .font(TigeroseTheme.bodySm.weight(.medium))
                        .foregroundStyle(TigeroseTheme.ink)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 7)
                        .background(TigeroseTheme.surfaceCard, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
                        .overlay(
                            RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                                .stroke(TigeroseTheme.hairline, lineWidth: 1)
                        )
                }
                .buttonStyle(.plain)
                .disabled(busy)

                if request.offersCapabilityGrant {
                    Button {
                        guard !busy else { return }
                        busy = true
                        onDecide(.allowOnce)
                    } label: {
                        Text("允许一次")
                            .font(TigeroseTheme.bodySm.weight(.semibold))
                            .foregroundStyle(TigeroseTheme.ink)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 7)
                            .background(TigeroseTheme.surfaceCard, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
                            .overlay(
                                RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                                    .stroke(TigeroseTheme.primary.opacity(0.55), lineWidth: 1)
                            )
                    }
                    .buttonStyle(.plain)
                    .disabled(busy)

                    Button {
                        guard !busy else { return }
                        busy = true
                        onDecide(.allowAlways)
                    } label: {
                        Text(request.reason.hasPrefix("scope_bash_") ? "本任务允许" : "本轮允许")
                            .font(TigeroseTheme.bodySm.weight(.semibold))
                            .foregroundStyle(TigeroseTheme.onPrimary)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 7)
                            .background(
                                request.isHighRisk ? TigeroseTheme.error : TigeroseTheme.controlFill,
                                in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                            )
                    }
                    .buttonStyle(.plain)
                    .disabled(busy)
                } else {
                    Button {
                        guard !busy else { return }
                        busy = true
                        onDecide(.allowOnce)
                    } label: {
                        Text(request.reason == "session_clear" ? "确认清空" : "仅本次允许")
                            .font(TigeroseTheme.bodySm.weight(.semibold))
                            .foregroundStyle(TigeroseTheme.onPrimary)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 7)
                            .background(
                                request.isHighRisk ? TigeroseTheme.error : TigeroseTheme.controlFill,
                                in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                            )
                    }
                    .buttonStyle(.plain)
                    .disabled(busy)
                }
            }
        }
        .padding(.horizontal, TigeroseTheme.spaceMd)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(TigeroseTheme.surfaceSoft)
        .overlay(alignment: .top) {
            Rectangle()
                .fill(TigeroseTheme.warning.opacity(0.55))
                .frame(height: 2)
        }
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(TigeroseTheme.hairline)
                .frame(height: 1)
        }
        .onChange(of: request.request_id) { _, _ in
            busy = false
            isDetailExpanded = false
        }
    }
}
