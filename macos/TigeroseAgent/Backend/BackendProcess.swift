import Foundation

enum BackendError: LocalizedError {
    case noPythonRuntime
    case healthTimeout
    case spawnFailed(String)

    var errorDescription: String? {
        switch self {
        case .noPythonRuntime:
            return "未找到 Python 运行时（开发模式需要本机 python3，或先执行打包脚本）"
        case .healthTimeout:
            return "引擎启动超时，请查看 logs/backend.log"
        case .spawnFailed(let msg):
            return "启动引擎失败：\(msg)"
        }
    }
}

final class BackendProcess: @unchecked Sendable {
    let meshAdminToken = UUID().uuidString + UUID().uuidString
    var onStateChange: ((BackendStatus, Int?, String) -> Void)?

    private var process: Process?
    private var port: Int?

    func start() async throws {
        stop()
        let port = try Self.pickFreePort()
        self.port = port
        onStateChange?(.starting, port, "")

        try TigerosePaths.ensureHomeLayout()
        let logURL = TigerosePaths.backendLog
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }

        let launch = try Self.resolveLaunch(port: port)
        let proc = Process()
        proc.executableURL = launch.executable
        proc.arguments = launch.arguments
        proc.currentDirectoryURL = TigerosePaths.home
        proc.environment = launch.environment
        proc.environment?["TIGEROSE_MESH_ADMIN_TOKEN"] = meshAdminToken

        let out = try FileHandle(forWritingTo: logURL)
        try out.seekToEnd()
        proc.standardOutput = out
        proc.standardError = out

        do {
            try proc.run()
        } catch {
            throw BackendError.spawnFailed(error.localizedDescription)
        }

        process = proc

        try await waitHealthy(port: port)
        onStateChange?(.running, port, "")
    }

    func stop() {
        let proc = process
        process = nil
        guard let proc else {
            onStateChange?(.stopped, nil, "")
            return
        }
        if proc.isRunning {
            proc.terminate()
            let deadline = Date().addingTimeInterval(3)
            while proc.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.05)
            }
            if proc.isRunning {
                kill(proc.processIdentifier, SIGKILL)
            }
        }
        onStateChange?(.stopped, nil, "")
    }

    private struct Launch {
        var executable: URL
        var arguments: [String]
        var environment: [String: String]
    }

    private static func resolveLaunch(port: Int) throws -> Launch {
        // Do not inherit the full parent env (Cursor/IDE sandbox vars can hang
        // Python 3.14 codec init on os.listdir of broken cache mounts).
        var env = Self.cleanLaunchEnvironment()
        env["TIGEROSE_HOME"] = TigerosePaths.home.path
        env["AVENT_HOME"] = TigerosePaths.home.path  // compat
        env["PYTHONUNBUFFERED"] = "1"
        env["AGENT_NONINTERACTIVE"] = "1"
        // GUI apps often lack shell PATH (nvm/homebrew); keep MCP binaries resolvable.
        env["PATH"] = Self.augmentedPATH(env["PATH"])

        if let runtime = bundledRuntimeRoot(),
           let python = bundledPython(runtime: runtime)
        {
            let site = runtime.appendingPathComponent("site-packages")
            env["PYTHONPATH"] = [runtime.path, site.path].joined(separator: ":")
            env["PATH"] = "\(python.deletingLastPathComponent().path):\(env["PATH"] ?? "")"
            return Launch(
                executable: python,
                arguments: ["-m", "uvicorn", "server.app:app", "--host", "127.0.0.1", "--port", "\(port)"],
                environment: env
            )
        }

        if let repo = TigerosePaths.repoRoot {
            env["PYTHONPATH"] = repo.path
            return Launch(
                executable: URL(fileURLWithPath: "/usr/bin/env"),
                arguments: ["python3", "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1", "--port", "\(port)"],
                environment: env
            )
        }

        throw BackendError.noPythonRuntime
    }

    /// Minimal env for the backend child. Keeps shell PATH/HOME/proxy secrets,
    /// drops IDE sandbox paths that stall Python startup.
    private static func cleanLaunchEnvironment() -> [String: String] {
        let inherited = ProcessInfo.processInfo.environment
        var env: [String: String] = [:]
        let exactKeys: Set<String> = [
            "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP",
            "LANG", "LC_ALL", "LC_CTYPE",
            "SSH_AUTH_SOCK", "PATH",
            "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
            "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
            "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        ]
        for (key, value) in inherited {
            if key.hasPrefix("CURSOR_") || key.hasPrefix("VSCODE_") || key.hasPrefix("ELECTRON_") {
                continue
            }
            if value.contains("cursor-sandbox-cache") {
                continue
            }
            if exactKeys.contains(key) || key.hasPrefix("LC_") {
                env[key] = value
            }
        }
        if env["HOME"] == nil {
            env["HOME"] = FileManager.default.homeDirectoryForCurrentUser.path
        }
        return env
    }

    private static func augmentedPATH(_ current: String?) -> String {
        var parts: [String] = []
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let extras = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "\(home)/.nvm/versions/node/v24.12.0/bin",
            "\(home)/.local/bin",
        ]
        // Prefer newest nvm node bin if present
        let nvmDir = "\(home)/.nvm/versions/node"
        if let versions = try? FileManager.default.contentsOfDirectory(atPath: nvmDir) {
            for v in versions.sorted().reversed() {
                parts.append("\(nvmDir)/\(v)/bin")
            }
        }
        parts.append(contentsOf: extras)
        if let current, !current.isEmpty {
            parts.append(current)
        }
        // dedupe preserving order
        var seen = Set<String>()
        return parts.filter { seen.insert($0).inserted }.joined(separator: ":")
    }

    private func waitHealthy(port: Int) async throws {
        let url = URL(string: "http://127.0.0.1:\(port)/api/health")!
        let started = Date()
        while Date().timeIntervalSince(started) < 30 {
            let running = process?.isRunning ?? false
            if !running {
                throw BackendError.spawnFailed("进程已退出，见 backend.log")
            }
            do {
                let (_, resp) = try await URLSession.shared.data(from: url)
                if let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                    return
                }
            } catch {
                // retry
            }
            try await Task.sleep(nanoseconds: 300_000_000)
        }
        throw BackendError.healthTimeout
    }

    private static func pickFreePort() throws -> Int {
        let sock = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(sock) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let bindResult = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(sock, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else { throw BackendError.spawnFailed("无法绑定本地端口") }
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let got = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(sock, $0, &len)
            }
        }
        guard got == 0 else { throw BackendError.spawnFailed("无法读取端口") }
        return Int(UInt16(bigEndian: addr.sin_port))
    }

    private static func bundledRuntimeRoot() -> URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let runtime = resources.appendingPathComponent("runtime", isDirectory: true)
        let marker = runtime.appendingPathComponent("server/app.py")
        return FileManager.default.fileExists(atPath: marker.path) ? runtime : nil
    }

    private static func bundledPython(runtime: URL) -> URL? {
        let candidates = [
            runtime.appendingPathComponent("python/bin/python3"),
            runtime.appendingPathComponent("bin/python3"),
        ]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0.path) }
    }
}
