import Foundation

enum OnboardingWriter {
    static func write(prefs: AppPreferences, apiKey: String) throws {
        try TigerosePaths.ensureHomeLayout()

        // .env — restrictive permissions
        let envBody = "OPENAI_API_KEY=\(apiKey)\n"
        try envBody.write(to: TigerosePaths.envFile, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: TigerosePaths.envFile.path
        )

        let modelId = prefs.defaultModelId.isEmpty ? "default" : prefs.defaultModelId
        let baseURL = prefs.defaultBaseURL.isEmpty ? "https://api.deepseek.com" : prefs.defaultBaseURL
        let label = modelId

        let yaml = """
        _config_version: 1
        model:
          default: \(yamlQuote(modelId))
          provider: custom
          base_url: \(yamlQuote(baseURL))
          api_key: ""
          profiles:
            - id: \(yamlQuote(modelId))
              label: \(yamlQuote(label))
              provider: custom
              base_url: \(yamlQuote(baseURL))
              api_key: ""
              reasoning_effort: none
        agent:
          reasoning_effort: none
        terminal:
          cwd: \(yamlQuote(prefs.defaultWorkspacePath.isEmpty ? "." : prefs.defaultWorkspacePath))
          timeout: 180
        session:
          persist_dir: .sessions
        avent:
          onboarding_completed: true
          default_workspace: \(yamlQuote(prefs.defaultWorkspacePath))
        """
        try yaml.write(to: TigerosePaths.configYAML, atomically: true, encoding: .utf8)
    }

    private static func yamlQuote(_ s: String) -> String {
        let escaped = s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        return "\"\(escaped)\""
    }
}
