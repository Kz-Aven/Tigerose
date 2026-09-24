import Foundation

enum TigerosePaths {
    /// Mutable data home: `~/Library/Application Support/Tigerose`,
    /// or `TIGEROSE_HOME` / `AVENT_HOME` when set.
    static var home: URL {
        let env = ProcessInfo.processInfo.environment
        if let raw = env["TIGEROSE_HOME"], !raw.isEmpty {
            return URL(fileURLWithPath: raw, isDirectory: true)
        }
        if let raw = env["AVENT_HOME"], !raw.isEmpty {
            return URL(fileURLWithPath: raw, isDirectory: true)
        }
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let tigerose = base.appendingPathComponent("Tigerose", isDirectory: true)
        migrateLegacyHomeIfNeeded(to: tigerose)
        return tigerose
    }

    static var configYAML: URL { home.appendingPathComponent("config.yaml") }
    static var envFile: URL { home.appendingPathComponent(".env") }
    static var appStateFile: URL { home.appendingPathComponent("app.json") }
    static var backendLog: URL { home.appendingPathComponent("logs/backend.log") }
    static var logsDir: URL { home.appendingPathComponent("logs", isDirectory: true) }

    /// Repo root when running from a SwiftPM build inside the checkout.
    static var repoRoot: URL? {
        // macos/TigeroseAgent/App/TigerosePaths.swift → repo root
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // App/
            .deletingLastPathComponent() // TigeroseAgent/
            .deletingLastPathComponent() // macos/
            .deletingLastPathComponent() // repo
        let marker = packageRoot.appendingPathComponent("server/app.py")
        if FileManager.default.fileExists(atPath: marker.path) {
            return packageRoot
        }
        // A packaged development build lives at repo/dist/Tigerose.app. Swift may
        // compile #filePath as a module-relative value, so also resolve from the bundle.
        let bundleRoot = Bundle.main.bundleURL
            .deletingLastPathComponent() // dist/
            .deletingLastPathComponent() // repo/
        if FileManager.default.fileExists(atPath: bundleRoot.appendingPathComponent("server/app.py").path) {
            return bundleRoot
        }
        // Fallback: TIGEROSE_DEV_ROOT, then AVENT_DEV_ROOT
        let env = ProcessInfo.processInfo.environment
        for key in ["TIGEROSE_DEV_ROOT", "AVENT_DEV_ROOT"] {
            if let raw = env[key], !raw.isEmpty {
                let url = URL(fileURLWithPath: raw)
                if FileManager.default.fileExists(atPath: url.appendingPathComponent("server/app.py").path) {
                    return url
                }
            }
        }
        return nil
    }

    /// If Application Support/Tigerose is missing but Avent-Agent exists, move it once.
    private static func migrateLegacyHomeIfNeeded(to tigerose: URL) {
        let fm = FileManager.default
        if fm.fileExists(atPath: tigerose.path) {
            return
        }
        let legacy = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            .appendingPathComponent("Avent-Agent", isDirectory: true)
        guard fm.fileExists(atPath: legacy.path) else { return }
        do {
            try fm.moveItem(at: legacy, to: tigerose)
        } catch {
            // Best-effort; ensureHomeLayout will create a fresh tree if move fails.
        }
    }

    static func ensureHomeLayout() throws {
        let fm = FileManager.default
        try fm.createDirectory(at: home, withIntermediateDirectories: true)
        for rel in ["data", "data/uploads", ".sessions", ".memory", ".worktrees", "logs"] {
            try fm.createDirectory(
                at: home.appendingPathComponent(rel, isDirectory: true),
                withIntermediateDirectories: true
            )
        }
    }
}

struct AppPreferences: Codable, Equatable {
    var onboardingCompleted: Bool = false
    var defaultWorkspacePath: String = ""
    var defaultModelId: String = ""
    var defaultBaseURL: String = "https://api.deepseek.com"
}

enum AppPreferencesStore {
    static func load() -> AppPreferences {
        let url = TigerosePaths.appStateFile
        guard let data = try? Data(contentsOf: url),
              let prefs = try? JSONDecoder().decode(AppPreferences.self, from: data)
        else {
            return AppPreferences()
        }
        return prefs
    }

    static func save(_ prefs: AppPreferences) throws {
        try TigerosePaths.ensureHomeLayout()
        let data = try JSONEncoder().encode(prefs)
        try data.write(to: TigerosePaths.appStateFile, options: .atomic)
    }
}
