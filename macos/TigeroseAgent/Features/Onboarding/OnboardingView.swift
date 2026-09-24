import SwiftUI
import AppKit

struct OnboardingView: View {
    @Environment(AppState.self) private var app
    @State private var step = 0
    @State private var apiKey = ""
    @State private var modelId = "qwen/qwen3.6-27b"
    @State private var baseURL = "http://localhost:1234/v1"
    @State private var workspacePath = ""
    @State private var error = ""

    private var isLocalLMStudio: Bool {
        let u = baseURL.lowercased()
        return u.contains("localhost:1234") || u.contains("127.0.0.1:1234")
    }

    var body: some View {
        VStack(spacing: TigeroseTheme.spaceLg) {
            BrandLogo(size: 56)
            Text("设置 Tigerose 钛钢柔")
                .font(TigeroseTheme.display(30))
                .foregroundStyle(TigeroseTheme.ink)
            Text("完成模型、凭据和工作区设置后即可开始。")
                .font(TigeroseTheme.bodySm)
                .foregroundStyle(TigeroseTheme.muted)

            onboardingProgress

            Group {
                switch step {
                case 0:
                    stepCard(title: "1. 默认模型") {
                        fieldLabel("模型 ID")
                        TextField("例如 qwen/qwen3.6-27b", text: $modelId)
                            .textFieldStyle(TigeroseTextFieldStyle())
                        fieldLabel("Base URL")
                        TextField("例如 http://localhost:1234/v1", text: $baseURL)
                            .textFieldStyle(TigeroseTextFieldStyle())
                        HStack(spacing: TigeroseTheme.spaceXs) {
                            chip("LM Studio 本地") {
                                modelId = "qwen/qwen3.6-27b"
                                baseURL = "http://localhost:1234/v1"
                            }
                            chip("DeepSeek") {
                                modelId = "deepseek-v4-flash"
                                baseURL = "https://api.deepseek.com"
                            }
                            chip("OpenAI") {
                                modelId = "gpt-4.1"
                                baseURL = "https://api.openai.com/v1"
                            }
                        }
                    }
                case 1:
                    stepCard(title: "2. API Key") {
                        fieldLabel("API Key")
                        SecureField(
                            isLocalLMStudio ? "本地 LM Studio 可留空" : "OPENAI_API_KEY / 兼容 Key",
                            text: $apiKey
                        )
                        .textFieldStyle(TigeroseTextFieldStyle())
                        Text(
                            isLocalLMStudio
                                ? "本地 LM Studio 一般不需要 Key；留空将写入占位值 lm-studio。"
                                : "仅保存在本机 Application Support，不会打进 App 包。"
                        )
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.muted)
                    }
                default:
                    stepCard(title: "3. 默认工作区") {
                        HStack {
                            Text(workspacePath.isEmpty ? "未选择" : workspacePath)
                                .font(TigeroseTheme.bodySm)
                                .foregroundStyle(workspacePath.isEmpty ? TigeroseTheme.mutedSoft : TigeroseTheme.body)
                                .lineLimit(2)
                                .hoverFullText(workspacePath.isEmpty ? "未选择" : workspacePath)
                            Spacer()
                            Button("选择文件夹…") { pickFolder() }
                                .buttonStyle(TigeroseSecondaryButtonStyle())
                        }
                        Text("新项目群将默认使用此路径；agent 也可在此工作区读写文件。")
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(TigeroseTheme.muted)
                    }
                }
            }
            .frame(maxWidth: 520)

            if !error.isEmpty {
                Text(error)
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.error)
            }

            HStack {
                if step > 0 {
                    Button("上一步") { step -= 1 }
                        .buttonStyle(TigeroseSecondaryButtonStyle())
                }
                Spacer()
                if step < 2 {
                    Button("下一步") { advance() }
                        .buttonStyle(TigerosePrimaryButtonStyle())
                        .keyboardShortcut(.defaultAction)
                } else {
                    Button("完成并启动") {
                        Task { await finish() }
                    }
                    .buttonStyle(TigerosePrimaryButtonStyle())
                    .keyboardShortcut(.defaultAction)
                }
            }
            .frame(maxWidth: 520)
        }
        .padding(TigeroseTheme.spaceXl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(TigeroseTheme.canvas)
    }

    private var onboardingProgress: some View {
        HStack(spacing: TigeroseTheme.spaceSm) {
            ForEach(Array(["模型", "凭据", "工作区"].enumerated()), id: \.offset) { index, title in
                HStack(spacing: 6) {
                    Image(systemName: index < step ? "checkmark.circle.fill" : "circle")
                        .foregroundStyle(index <= step ? TigeroseTheme.primary : TigeroseTheme.mutedSoft)
                    Text(title)
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(index <= step ? TigeroseTheme.ink : TigeroseTheme.muted)
                }
                if index < 2 {
                    Rectangle()
                        .fill(index < step ? TigeroseTheme.primary : TigeroseTheme.hairline)
                        .frame(width: 28, height: 1)
                }
            }
        }
        .accessibilityLabel("引导进度：第 \(step + 1) 步，共 3 步")
    }

    private func stepCard<Content: View>(title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
            Text(title)
                .font(TigeroseTheme.titleMd)
                .foregroundStyle(TigeroseTheme.ink)
            content()
        }
        .padding(TigeroseTheme.spaceLg)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(TigeroseTheme.surfaceCard, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusLg))
        .overlay(
            RoundedRectangle(cornerRadius: TigeroseTheme.radiusLg)
                .stroke(TigeroseTheme.hairline, lineWidth: 1)
        )
    }

    private func chip(_ title: String, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .font(TigeroseTheme.caption)
            .foregroundStyle(TigeroseTheme.ink)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(TigeroseTheme.surfaceCreamStrong, in: Capsule())
            .buttonStyle(.plain)
    }

    private func fieldLabel(_ title: String) -> some View {
        Text(title)
            .font(TigeroseTheme.caption)
            .foregroundStyle(TigeroseTheme.bodyStrong)
    }

    private func advance() {
        error = ""
        if step == 0 {
            if modelId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                error = "请填写模型 ID"
                return
            }
            if baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                error = "请填写 Base URL"
                return
            }
        }
        if step == 1, !isLocalLMStudio, apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            error = "云端模型请填写 API Key"
            return
        }
        step += 1
    }

    private func pickFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "选择"
        if panel.runModal() == .OK, let url = panel.url {
            workspacePath = url.path
        }
    }

    private func finish() async {
        error = ""
        if workspacePath.isEmpty {
            error = "请选择工作区目录"
            return
        }
        var prefs = AppPreferences()
        prefs.onboardingCompleted = true
        prefs.defaultModelId = modelId.trimmingCharacters(in: .whitespacesAndNewlines)
        prefs.defaultBaseURL = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        prefs.defaultWorkspacePath = workspacePath
        var key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        if key.isEmpty, isLocalLMStudio {
            key = "lm-studio"
        }
        await app.completeOnboarding(prefs, apiKey: key)
    }
}

struct TigeroseTextFieldStyle: TextFieldStyle {
    func _body(configuration: TextField<Self._Label>) -> some View {
        configuration
            .font(TigeroseTheme.bodyMd)
            .foregroundStyle(TigeroseTheme.ink)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(TigeroseTheme.canvas, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
            .overlay(
                RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                    .stroke(TigeroseTheme.hairline, lineWidth: 1)
            )
    }
}
