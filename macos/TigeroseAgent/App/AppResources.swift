import Foundation

/// Resource lookup that is safe inside a manually packaged `.app`.
///
/// SwiftPM's generated `Bundle.module` looks for resources at the `.app` root
/// and at a hard-coded build-machine `.build/...` path. Accessing it when
/// neither exists triggers `fatalError` / `EXC_BREAKPOINT` — which is what
/// colleagues hit after onboarding when the sidebar logo loads.
enum AppResources {
    static func url(forResource name: String, withExtension ext: String) -> URL? {
        for bundle in resourceBundles() {
            if let url = bundle.url(forResource: name, withExtension: ext) {
                return url
            }
            // Some packaging layouts flatten the file next to Info.plist resources.
            if let root = bundle.resourceURL {
                let flat = root.appendingPathComponent("\(name).\(ext)")
                if FileManager.default.fileExists(atPath: flat.path) {
                    return flat
                }
            }
        }
        return nil
    }

    static func url(forResource name: String, withExtension ext: String, subdirectory: String) -> URL? {
        for bundle in resourceBundles() {
            if let url = bundle.url(forResource: name, withExtension: ext, subdirectory: subdirectory) {
                return url
            }
            if let root = bundle.resourceURL {
                let nested = root
                    .appendingPathComponent(subdirectory, isDirectory: true)
                    .appendingPathComponent("\(name).\(ext)")
                if FileManager.default.fileExists(atPath: nested.path) {
                    return nested
                }
            }
        }
        return nil
    }

    static func resourceBundles() -> [Bundle] {
        var bundles: [Bundle] = []
        var seen = Set<URL>()

        func append(_ bundle: Bundle) {
            let url = bundle.bundleURL.standardizedFileURL
            guard !seen.contains(url) else { return }
            seen.insert(url)
            bundles.append(bundle)
        }

        if let resourceURL = Bundle.main.resourceURL {
            let preferred = resourceURL.appendingPathComponent("TigeroseAgent_TigeroseAgent.bundle")
            if let nested = Bundle(url: preferred) {
                append(nested)
            }
            if let kids = try? FileManager.default.contentsOfDirectory(
                at: resourceURL,
                includingPropertiesForKeys: nil
            ) {
                for url in kids where url.pathExtension == "bundle" {
                    if let nested = Bundle(url: url) {
                        append(nested)
                    }
                }
            }
        }

        append(.main)

        // Dev / `swift run` only: Bundle.module is valid when `.build` exists.
        // Never evaluate it inside a shipped `.app` — that alone can fatalError.
        if Bundle.main.bundleURL.pathExtension != "app" {
            append(.module)
        }

        return bundles
    }
}
