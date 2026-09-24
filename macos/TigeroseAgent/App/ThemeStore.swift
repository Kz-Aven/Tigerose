import Foundation
import Observation
import SwiftUI

/// Loads and switches skins. Token data comes from JSON (with embedded fallback).
/// Future skin UI should call `setSkin(_:)` — views already read through `TigeroseTheme` / Environment.
@Observable
final class ThemeStore {
    static let shared = ThemeStore()

    private static let skinIDKey = "tigerose.theme.skinID"
    private static let defaultSkinID = "pinterest"

    private(set) var skinID: String
    private(set) var tokens: ThemeTokens

    /// Registered built-in fallbacks (id → tokens). JSON overrides these when present.
    private var builtins: [String: ThemeTokens] = [
        ThemeTokens.pinterest.id: .pinterest,
    ]

    init(defaults: UserDefaults = .standard) {
        let saved = defaults.string(forKey: Self.skinIDKey) ?? Self.defaultSkinID
        self.skinID = saved
        self.tokens = Self.loadTokens(id: saved, builtins: [
            ThemeTokens.pinterest.id: .pinterest,
        ]) ?? .pinterest
        if self.tokens.id != saved {
            self.skinID = self.tokens.id
        }
    }

    /// Switch skin by id. Persists selection. Unknown ids fall back to Pinterest.
    func setSkin(_ id: String, defaults: UserDefaults = .standard) {
        let resolved = Self.loadTokens(id: id, builtins: builtins) ?? .pinterest
        skinID = resolved.id
        tokens = resolved
        defaults.set(resolved.id, forKey: Self.skinIDKey)
    }

    /// Register additional embedded skins before JSON discovery (plugin / future packs).
    func registerBuiltin(_ tokens: ThemeTokens) {
        builtins[tokens.id] = tokens
    }

    /// Skin ids available from builtins + Themes/*.json in the resource bundle.
    func availableSkinIDs() -> [String] {
        var ids = Set(builtins.keys)
        ids.formUnion(Self.discoveredJSONSkinIDs())
        return ids.sorted()
    }

    // MARK: - Loading

    private static func loadTokens(id: String, builtins: [String: ThemeTokens]) -> ThemeTokens? {
        if let fromJSON = decodeJSONSkin(id: id) {
            return fromJSON
        }
        return builtins[id] ?? builtins[defaultSkinID]
    }

    private static func decodeJSONSkin(id: String) -> ThemeTokens? {
        guard let url = themeJSONURL(id: id) else { return nil }
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(ThemeTokens.self, from: data)
    }

    private static func themeJSONURL(id: String) -> URL? {
        if let url = AppResources.url(forResource: id, withExtension: "json", subdirectory: "Themes") {
            return url
        }
        // Flattened packaging: pinterest.json at resource root.
        return AppResources.url(forResource: id, withExtension: "json")
    }

    private static func discoveredJSONSkinIDs() -> Set<String> {
        var ids = Set<String>()
        for bundle in AppResources.resourceBundles() {
            let roots: [URL] = [
                bundle.resourceURL?.appendingPathComponent("Themes", isDirectory: true),
                bundle.resourceURL,
            ].compactMap { $0 }
            for root in roots {
                guard let files = try? FileManager.default.contentsOfDirectory(
                    at: root,
                    includingPropertiesForKeys: nil
                ) else { continue }
                for file in files where file.pathExtension == "json" {
                    ids.insert(file.deletingPathExtension().lastPathComponent)
                }
            }
        }
        return ids
    }
}

// MARK: - Environment

private struct TigeroseThemeKey: EnvironmentKey {
    static let defaultValue: ThemeTokens = .pinterest
}

extension EnvironmentValues {
    var tigeroseTheme: ThemeTokens {
        get { self[TigeroseThemeKey.self] }
        set { self[TigeroseThemeKey.self] = newValue }
    }
}
