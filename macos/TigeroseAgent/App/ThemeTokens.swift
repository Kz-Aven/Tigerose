import Foundation

/// Serializable skin tokens. Views should not hardcode these values —
/// load via `ThemeStore` / `TigeroseTheme` / `Environment(\.tigeroseTheme)`.
struct ThemeTokens: Codable, Equatable, Sendable {
    var id: String
    var name: String

    // MARK: Colors (hex, e.g. "#167A7A")
    var primary: String
    var primaryActive: String
    var primaryDisabled: String
    var ink: String
    var body: String
    var bodyStrong: String
    var muted: String
    var mutedSoft: String
    var hairline: String
    var hairlineSoft: String
    var canvas: String
    var surfaceSoft: String
    var surfaceCard: String
    var surfaceCreamStrong: String
    var surfaceDark: String
    var surfaceDarkElevated: String
    var onPrimary: String
    var onDark: String
    var onDarkSoft: String
    var secondary: String
    var secondaryPressed: String
    var success: String
    var warning: String
    var error: String

    // MARK: Radii (pt)
    var radiusXs: Double
    var radiusSm: Double
    var radiusMd: Double
    var radiusLg: Double
    var radiusXl: Double

    // MARK: Spacing (pt)
    var spaceXxs: Double
    var spaceXs: Double
    var spaceSm: Double
    var spaceMd: Double
    var spaceLg: Double
    var spaceXl: Double
}

extension ThemeTokens {
    /// Built-in Tigerose skin. Used when the bundled JSON is unavailable.
    static let pinterest = ThemeTokens(
        id: "pinterest",
        name: "Tigerose Native",
        primary: "#167A7A",
        primaryActive: "#0F5F5C",
        primaryDisabled: "#A7D5D2",
        ink: "#1D1D1F",
        body: "#1D1D1F",
        bodyStrong: "#1D1D1F",
        muted: "#6E6E73",
        mutedSoft: "#8E8E93",
        hairline: "#E7E7E7",
        hairlineSoft: "#EFEFEF",
        canvas: "#FCFBFC",
        surfaceSoft: "#F7F7F7",
        surfaceCard: "#F8F8F8",
        surfaceCreamStrong: "#D6EDEC",
        surfaceDark: "#1C1C1E",
        surfaceDarkElevated: "#2C2C2E",
        onPrimary: "#ffffff",
        onDark: "#ffffff",
        onDarkSoft: "#91918c",
        secondary: "#F8F9FA",
        secondaryPressed: "#D6EDEC",
        success: "#248A3D",
        warning: "#C93400",
        error: "#D70015",
        radiusXs: 4,
        radiusSm: 6,
        radiusMd: 8,
        radiusLg: 12,
        radiusXl: 16,
        spaceXxs: 4,
        spaceXs: 8,
        spaceSm: 12,
        spaceMd: 16,
        spaceLg: 24,
        spaceXl: 32
    )
}
