import Foundation

/// URLSession-backed SSE client. Assistant DMs may retain multiple instances via AppState while busy.
@MainActor
final class SSEClient: NSObject, URLSessionDataDelegate {
    private var task: URLSessionDataTask?
    private var session: URLSession!
    private var buffer = Data()
    private var onEvent: ((String, Data) -> Void)?

    override init() {
        super.init()
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = .infinity
        config.timeoutIntervalForResource = .infinity
        session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }

    func subscribe(baseURL: URL, channel: String, onEvent: @escaping (String, Data) -> Void) {
        cancel()
        self.onEvent = onEvent
        var comps = URLComponents(url: baseURL.appendingPathComponent("api/sse"), resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "channel", value: channel)]
        guard let url = comps.url else { return }
        var req = URLRequest(url: url)
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        task = session.dataTask(with: req)
        task?.resume()
    }

    func cancel() {
        task?.cancel()
        task = nil
        buffer.removeAll()
        onEvent = nil
    }

    nonisolated func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        Task { @MainActor in
            buffer.append(data)
            while let range = buffer.range(of: Data("\n\n".utf8)) {
                let chunk = buffer.subdata(in: buffer.startIndex ..< range.lowerBound)
                buffer.removeSubrange(buffer.startIndex ..< range.upperBound)
                handleSSEChunk(chunk)
            }
        }
    }

    private func handleSSEChunk(_ chunk: Data) {
        guard let text = String(data: chunk, encoding: .utf8) else { return }
        var dataLines: [String] = []
        for line in text.split(separator: "\n", omittingEmptySubsequences: false) {
            if line.hasPrefix("data:") {
                dataLines.append(String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces))
            }
        }
        let payload = dataLines.joined(separator: "\n")
        guard let raw = payload.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: raw) as? [String: Any],
              let type = obj["type"] as? String
        else { return }
        let dataObj = obj["data"]
        let dataJSON = (try? JSONSerialization.data(withJSONObject: dataObj ?? NSNull())) ?? Data()
        onEvent?(type, dataJSON)
    }
}
