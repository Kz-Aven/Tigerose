import AppKit
import SwiftUI

/// Runtime access to the active skin.
///
/// Prefer `@Environment(\.tigeroseTheme)` for new code. Static members below
/// forward to `ThemeStore.shared` so existing call sites keep working; pair with
/// `.environment(\.tigeroseTheme, store.tokens).id(store.skinID)` at the root
/// so a future skin switch refreshes the tree.
enum TigeroseTheme {
    private static var t: ThemeTokens { ThemeStore.shared.tokens }

    // MARK: - Colors

    static var primary: Color { color(t.primary) }
    static let controlFill = Color(red: 0, green: 0, blue: 0)
    static var primaryActive: Color { color(t.primaryActive) }
    static var primaryDisabled: Color { primary.opacity(0.36) }
    static var brandRose: Color { Color(hex: "#C83F50") ?? primary }
    static var ink: Color { Color(nsColor: .labelColor) }
    static var body: Color { Color(nsColor: .labelColor) }
    static var bodyStrong: Color { Color(nsColor: .labelColor) }
    static var muted: Color { Color(nsColor: .secondaryLabelColor) }
    static var mutedSoft: Color { Color(nsColor: .tertiaryLabelColor) }
    static var hairline: Color { Color(nsColor: .separatorColor) }
    static var hairlineSoft: Color { Color(nsColor: .separatorColor).opacity(0.55) }
    static var canvas: Color { adaptiveSurface(light: 0xFCFBFC, dark: .windowBackgroundColor) }
    static var surfaceSoft: Color { adaptiveSurface(light: 0xF7F7F7, dark: .underPageBackgroundColor) }
    static var surfaceCard: Color { adaptiveSurface(light: 0xF8F8F8, dark: .controlBackgroundColor) }
    static var surfaceCreamStrong: Color { primary.opacity(0.12) }
    static var surfaceDark: Color { Color(nsColor: .underPageBackgroundColor) }
    static var surfaceDarkElevated: Color { Color(nsColor: .controlBackgroundColor) }
    static var onPrimary: Color { .white }
    static var onDark: Color { Color(nsColor: .labelColor) }
    static var onDarkSoft: Color { Color(nsColor: .secondaryLabelColor) }
    static var secondary: Color { adaptiveSurface(light: 0xF8F9FA, dark: .controlBackgroundColor) }
    static var secondaryPressed: Color { primary.opacity(0.12) }
    static var success: Color { Color(nsColor: .systemGreen) }
    static var warning: Color { Color(nsColor: .systemOrange) }
    static var error: Color { Color(nsColor: .systemRed) }

    // MARK: - Radii

    static var radiusXs: CGFloat { CGFloat(t.radiusXs) }
    static var radiusSm: CGFloat { CGFloat(t.radiusSm) }
    static var radiusMd: CGFloat { CGFloat(t.radiusMd) }
    static var radiusLg: CGFloat { CGFloat(t.radiusLg) }
    static var radiusXl: CGFloat { CGFloat(t.radiusXl) }

    // MARK: - Spacing

    static var spaceXxs: CGFloat { CGFloat(t.spaceXxs) }
    static var spaceXs: CGFloat { CGFloat(t.spaceXs) }
    static var spaceSm: CGFloat { CGFloat(t.spaceSm) }
    static var spaceMd: CGFloat { CGFloat(t.spaceMd) }
    static var spaceLg: CGFloat { CGFloat(t.spaceLg) }
    static var spaceXl: CGFloat { CGFloat(t.spaceXl) }

    // MARK: - Typography (system geometric sans — Pin Sans substitute)

    static func display(_ size: CGFloat, weight: Font.Weight = .semibold) -> Font {
        Font.system(size: size, weight: weight, design: .default)
    }

    static func sans(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        Font.system(size: size, weight: weight, design: .default)
    }

    static func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        Font.system(size: size, weight: weight, design: .monospaced)
    }

    static let titleDisplay = display(28, weight: .bold)
    static let titleLg = sans(22, weight: .semibold)
    static let titleMd = sans(18, weight: .semibold)
    static let titleSm = sans(16, weight: .semibold)
    static let bodyMd = sans(16)
    static let bodySm = sans(14)
    static let caption = sans(12, weight: .medium)
    static let button = sans(14, weight: .bold)

    /// Soft chat bubble fill from a design-token / theme hex.
    static func bubbleFill(hex: String, isUser: Bool) -> Color {
        if isUser {
            return primary.opacity(0.16)
        }
        return (Color(hex: hex) ?? primary).opacity(0.18)
    }

    static func bubbleStroke(hex: String, isUser: Bool) -> Color {
        if isUser {
            return primary.opacity(0.28)
        }
        return (Color(hex: hex) ?? primary).opacity(0.28)
    }

    /// Fallback when column width not yet measured.
    static let bubbleMaxWidth: CGFloat = 480
    /// Bubbles grow with text until this fraction of the chat column, then wrap.
    static let bubbleMaxWidthFraction: CGFloat = 0.70

    private static func color(_ hex: String) -> Color {
        Color(hex: hex) ?? .primary
    }

    private static func adaptiveSurface(light: Int, dark: NSColor) -> Color {
        let lightColor = NSColor(
            calibratedRed: CGFloat((light >> 16) & 0xFF) / 255,
            green: CGFloat((light >> 8) & 0xFF) / 255,
            blue: CGFloat(light & 0xFF) / 255,
            alpha: 1
        )
        let color = NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : lightColor
        }
        return Color(nsColor: color)
    }
}

// MARK: - Shared chrome

struct TigeroseSegmentedPicker<Value: Hashable>: View {
    let label: String
    @Binding var selection: Value
    let options: [(Value, String)]

    var body: some View {
        HStack(spacing: 2) {
            ForEach(options, id: \.0) { value, title in
                Button { selection = value } label: {
                    Text(title)
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(selection == value ? TigeroseTheme.onPrimary : TigeroseTheme.ink)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 4)
                        .frame(maxWidth: .infinity)
                        .background(
                            selection == value ? TigeroseTheme.controlFill : .clear,
                            in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusXs)
                        )
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(selection == value ? .isSelected : [])
            }
        }
        .padding(2)
        .background(Color(nsColor: .quaternaryLabelColor).opacity(0.35), in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusXs + 2))
        .accessibilityElement(children: .contain)
        .accessibilityLabel(label)
    }
}

struct TigerosePrimaryButtonStyle: ButtonStyle {
    var disabled: Bool = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(TigeroseTheme.button)
            .foregroundStyle(TigeroseTheme.onPrimary)
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .frame(minHeight: 40)
            .background(
                disabled
                    ? TigeroseTheme.primaryDisabled
                    : TigeroseTheme.controlFill,
                in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
            )
            .opacity(disabled ? 0.72 : 1)
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct TigeroseSecondaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(TigeroseTheme.button)
            .foregroundStyle(TigeroseTheme.ink)
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .frame(minHeight: 40)
            .background(
                configuration.isPressed ? TigeroseTheme.secondaryPressed : TigeroseTheme.secondary,
                in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
            )
            .overlay(
                RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd)
                    .stroke(TigeroseTheme.hairline, lineWidth: 1)
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct TigeroseCardBackground: ViewModifier {
    func body(content: Content) -> some View {
        content
            .background(TigeroseTheme.surfaceCard, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusLg))
    }
}

struct TigeroseHairlineBorder: ViewModifier {
    var radius: CGFloat = TigeroseTheme.radiusMd

    func body(content: Content) -> some View {
        content.overlay(
            RoundedRectangle(cornerRadius: radius)
                .stroke(TigeroseTheme.hairline, lineWidth: 1)
        )
    }
}

extension View {
    func tigeroseCard() -> some View { modifier(TigeroseCardBackground()) }
    func tigeroseHairline(radius: CGFloat = TigeroseTheme.radiusMd) -> some View {
        modifier(TigeroseHairlineBorder(radius: radius))
    }
}

/// Unified sheet chrome: title on the left, × close on the right.
struct TigeroseSheetHeader: View {
    let title: String
    var onClose: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: TigeroseTheme.spaceSm) {
            Text(title)
                .font(TigeroseTheme.titleLg)
                .foregroundStyle(TigeroseTheme.ink)
                .lineLimit(1)
                .hoverFullText(title)
            Spacer(minLength: 8)
            Button(action: onClose) {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(TigeroseTheme.muted)
                    .frame(width: 28, height: 28)
                    .background(TigeroseTheme.surfaceCreamStrong, in: Circle())
            }
            .buttonStyle(.plain)
            .help("关闭")
            .keyboardShortcut(.cancelAction)
        }
        .padding(.horizontal, TigeroseTheme.spaceLg)
        .padding(.top, TigeroseTheme.spaceLg)
        .padding(.bottom, TigeroseTheme.spaceSm)
    }
}

extension View {
    /// Hover tooltip with full text (for truncated / ellipsis labels).
    @ViewBuilder
    func hoverFullText(_ text: String) -> some View {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            self
        } else {
            self.help(trimmed)
        }
    }
}
