import Foundation

enum LocalImageReferences {
    private static let imageExtensions: Set<String> = [
        "png", "jpg", "jpeg", "gif", "webp", "heic", "tiff", "bmp", "avif"
    ]

    static func paths(in source: String) -> [String] {
        var paths: [String] = []
        var seen: Set<String> = []
        var fence: String?
        var fenceAllowsPath = false
        var fencedLines: [String] = []

        func append(_ candidate: String) {
            guard let path = imagePath(candidate), seen.insert(path).inserted else { return }
            paths.append(path)
        }

        // Match code spans as whole tokens so code containing a link is not treated as a reference.
        let references = try! NSRegularExpression(
            pattern: #"(`+)(.*?)\1|!?\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\n)]+))\s*\)"#
        )
        for line in source.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if let activeFence = fence {
                if trimmed.count >= activeFence.count && trimmed.allSatisfy({ $0 == activeFence.first! }) {
                    if fenceAllowsPath, fencedLines.count == 1 {
                        append(fencedLines[0])
                    }
                    fence = nil
                    fencedLines = []
                } else if !trimmed.isEmpty {
                    fencedLines.append(trimmed)
                }
                continue
            }
            if trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") {
                let marker = trimmed.first!
                let delimiter = String(trimmed.prefix(while: { $0 == marker }))
                let language = trimmed.dropFirst(delimiter.count).trimmingCharacters(in: .whitespaces)
                fence = delimiter
                fenceAllowsPath = ["", "text", "plaintext"].contains(language.lowercased())
                continue
            }

            append(trimmed)
            let nsLine = line as NSString
            for match in references.matches(in: line, range: NSRange(location: 0, length: nsLine.length)) {
                let candidateRange = (2...4).map { match.range(at: $0) }.first { $0.location != NSNotFound }
                if let candidateRange { append(nsLine.substring(with: candidateRange)) }
            }
        }
        return paths
    }

    private static func imagePath(_ reference: String) -> String? {
        let reference = reference.trimmingCharacters(in: .whitespacesAndNewlines)
        let path: String
        if reference.hasPrefix("file://") {
            guard let url = URL(string: reference), url.isFileURL,
                  url.host == nil || url.host == "" || url.host == "localhost",
                  url.query == nil, url.fragment == nil else { return nil }
            path = url.path
        } else {
            guard reference.hasPrefix("/") else { return nil }
            path = reference
        }
        let url = URL(fileURLWithPath: path).standardizedFileURL
        guard imageExtensions.contains(url.pathExtension.lowercased()),
              let values = try? url.resourceValues(forKeys: [.isRegularFileKey]),
              values.isRegularFile == true else { return nil }
        return url.path
    }
}
