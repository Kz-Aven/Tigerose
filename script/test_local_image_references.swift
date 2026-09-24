import Foundation

@main
struct LocalImageReferencesTests {
    static func main() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let first = root.appendingPathComponent("first.png")
        let second = root.appendingPathComponent("image with spaces.JPG")
        let third = root.appendingPathComponent("\u{56FE}\u{7247}.webp")
        for file in [first, second, third] {
            try Data([0]).write(to: file)
        }
        func check(_ source: String, _ expected: [URL]) {
            let result = LocalImageReferences.paths(in: source)
            precondition(result == expected.map(\.path), "Unexpected references: \(result), source: \(source)")
        }
        check(first.path, [first])
        check("Generated: `\(first.path)`", [first])
        check("```\n\(second.path)\n```", [second])
        check("```text\n\(third.path)\n```", [third])
        check("![image](\(second.path)) [image](<\(third.path)>)", [second, third])
        check("![image](\(second.absoluteString))", [second])
        check("`\(third.path)` [first](\(first.path))\n\(third.path)", [third, first])
        check("https://example.com/image.png\n![remote](https://example.com/image.png)", [])
        check(root.appendingPathComponent("missing.png").path, [])
        check("```swift\n\(first.path)\n```", [])
        check("```\nlet image = \"\(first.path)\"\n```", [])
        check("`let image = \"\(first.path)\"`", [])
        check("`![not a reference](\(first.path))`", [])
        check("let image = \"\(first.path)\"", [])
        check("[remote file](file://otherhost\(first.path))", [])
        let directory = root.appendingPathComponent("directory.png")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        check(directory.path, [])
        print("LocalImageReferences: all tests passed")
    }
}
