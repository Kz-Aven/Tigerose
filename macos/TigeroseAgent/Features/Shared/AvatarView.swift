import SwiftUI
import AppKit

enum AvatarCatalog {
    static let ids = [
        "animal-avatars-01", "animal-avatars-02", "animal-avatars-03",
        "animal-avatars-04", "animal-avatars-05", "animal-avatars-06",
        "animal-avatars-07", "animal-avatars-08", "animal-avatars-09",
        "human-avatars-01", "human-avatars-02", "human-avatars-03",
        "human-avatars-04", "human-avatars-05", "human-avatars-06",
        "human-avatars-07", "human-avatars-08", "human-avatars-09",
        "plant-avatars-01", "plant-avatars-02", "plant-avatars-03",
        "plant-avatars-04", "plant-avatars-05", "plant-avatars-06",
        "plant-avatars-07", "plant-avatars-08", "plant-avatars-09",
    ]

    static var randomID: String { ids.randomElement() ?? "" }
}

struct AvatarView: View {
    var name: String
    var colorHex: String
    var avatarID: String = ""
    var square: Bool = false
    var busy: Bool = false
    /// Outer size in points. Defaults match a single title line; use ~36–40 for title+subtitle.
    var size: CGFloat = 22

    var body: some View {
        Group {
            if square {
                avatarContent
                    .clipShape(RoundedRectangle(cornerRadius: max(TigeroseTheme.radiusSm, size * 0.22), style: .continuous))
            } else {
                avatarContent.clipShape(Circle())
            }
        }
        .overlay {
            if busy {
                AvatarActivityRing(size: size, square: square)
            }
        }
    }

    private var avatarContent: some View {
        ZStack {
            if let image = image {
                Image(nsImage: image)
                    .resizable()
                    .scaledToFill()
            } else {
                (Color(hex: colorHex) ?? TigeroseTheme.primary)
                Text(initials)
                    .font(.system(size: max(10, size * 0.38), weight: .semibold))
                    .foregroundStyle(.white)
            }
        }
        .frame(width: size, height: size)
    }

    private var image: NSImage? {
        guard !avatarID.isEmpty,
              let url = AppResources.url(forResource: avatarID, withExtension: "webp", subdirectory: "Avatars")
        else { return nil }
        return NSImage(contentsOf: url)
    }

    private var initials: String {
        let t = name.trimmingCharacters(in: .whitespaces)
        guard let first = t.first else { return "?" }
        if first.unicodeScalars.contains(where: { $0.value >= 0x4E00 && $0.value <= 0x9FFF }) {
            return String(first)
        }
        return String(first).uppercased()
    }
}

private struct AvatarActivityRing: View {
    var size: CGFloat
    var square: Bool

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var outline: Path {
        let rect = CGRect(x: 2, y: 2, width: size + 4, height: size + 4)
        if square {
            return RoundedRectangle(
                cornerRadius: max(TigeroseTheme.radiusSm, size * 0.22) + 2,
                style: .continuous
            ).path(in: rect)
        }
        return Circle().path(in: rect)
    }

    var body: some View {
        Group {
            if reduceMotion {
                outline.stroke(TigeroseTheme.primary, lineWidth: 2)
            } else {
                TimelineView(.animation(minimumInterval: 1.0 / 30)) { timeline in
                    Canvas { context, _ in
                        let path = outline
                        let phase = timeline.date.timeIntervalSinceReferenceDate
                            .truncatingRemainder(dividingBy: 2) / 2
                        context.stroke(path, with: .color(TigeroseTheme.primary.opacity(0.16)), lineWidth: 1)

                        // Increasing width and opacity form a tapered tail; split at the path seam.
                        for index in 0..<48 {
                            let strength = Double(index + 1) / 48
                            let start = (phase - 0.24 + Double(index) * 0.005 + 1)
                                .truncatingRemainder(dividingBy: 1)
                            let end = start + 0.005
                            let style = StrokeStyle(lineWidth: 0.3 + 2.3 * strength, lineCap: .round)
                            let shading = GraphicsContext.Shading.color(
                                TigeroseTheme.primary.opacity(0.08 + 0.92 * strength)
                            )
                            context.stroke(path.trimmedPath(from: start, to: min(end, 1)), with: shading, style: style)
                            if end > 1 {
                                context.stroke(path.trimmedPath(from: 0, to: end - 1), with: shading, style: style)
                            }
                        }
                        if let head = path.trimmedPath(from: 0, to: max(phase, 0.000001)).currentPoint {
                            context.fill(
                                Path(ellipseIn: CGRect(x: head.x - 1.5, y: head.y - 1.5, width: 3, height: 3)),
                                with: .color(TigeroseTheme.primary)
                            )
                        }
                    }
                }
            }
        }
        .frame(width: size + 8, height: size + 8)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

struct AvatarPicker: View {
    @Binding var selection: String
    var name: String = ""
    var colorHex: String = "#5db8a6"

    @State private var showChoices = false

    private let columns = Array(repeating: GridItem(.fixed(40), spacing: 8), count: 6)

    var body: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceXs) {
            Text("头像")
                .font(TigeroseTheme.titleSm)
                .foregroundStyle(TigeroseTheme.ink)
            Button { showChoices = true } label: {
                ZStack(alignment: .bottomTrailing) {
                    AvatarView(name: name, colorHex: colorHex, avatarID: selection, size: 64)
                        .overlay(Circle().stroke(TigeroseTheme.hairline, lineWidth: 1))
                    Image(systemName: "plus")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: 20, height: 20)
                        .background(TigeroseTheme.controlFill, in: Circle())
                        .overlay(Circle().stroke(TigeroseTheme.canvas, lineWidth: 2))
                        .offset(x: 2, y: 2)
                }
                .frame(width: 68, height: 68, alignment: .topLeading)
            }
            .buttonStyle(.plain)
            .help("更换头像")
            .popover(isPresented: $showChoices, arrowEdge: .bottom) {
                avatarChoices
            }
        }
    }

    private var avatarChoices: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
            Text("选择头像")
                .font(TigeroseTheme.titleSm)
                .foregroundStyle(TigeroseTheme.ink)
            LazyVGrid(columns: columns, spacing: 8) {
                ForEach(AvatarCatalog.ids, id: \.self) { id in
                    Button {
                        selection = id
                        showChoices = false
                    } label: {
                        AvatarView(name: "", colorHex: colorHex, avatarID: id, size: 40)
                            .overlay {
                                Circle()
                                    .stroke(
                                        selection == id ? TigeroseTheme.primary : Color.clear,
                                        lineWidth: 3
                                    )
                            }
                    }
                    .buttonStyle(.plain)
                    .help("选择头像")
                }
            }
        }
        .padding(TigeroseTheme.spaceMd)
        .frame(width: 320, alignment: .leading)
    }
}

struct GroupAvatarView: View {
    var members: [GroupMember]
    var fallbackName: String
    var fallbackColor: String = "#5B8DEF"
    var busy: Bool = false
    var size: CGFloat = 36

    var body: some View {
        Group {
            if members.isEmpty {
                AvatarView(name: fallbackName, colorHex: fallbackColor, square: true, busy: busy, size: size)
            } else {
                LazyVGrid(
                    columns: Array(repeating: GridItem(.flexible(), spacing: 1), count: 3),
                    spacing: 1
                ) {
                    ForEach(Array(members.prefix(9).enumerated()), id: \.offset) { _, member in
                        AvatarView(
                            name: member.display_name,
                            colorHex: member.theme_color,
                            avatarID: member.avatar_id,
                            square: true,
                            size: (size - 2) / 3
                        )
                    }
                }
                .padding(1)
                .frame(width: size, height: size, alignment: .topLeading)
                .background(TigeroseTheme.surfaceSoft)
                .clipShape(RoundedRectangle(cornerRadius: max(TigeroseTheme.radiusSm, size * 0.22), style: .continuous))
            }
        }
        .overlay {
            if busy && !members.isEmpty {
                AvatarActivityRing(size: size, square: true)
            }
        }
    }
}

extension Color {
    init?(hex: String) {
        var s = hex.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt64(s, radix: 16) else { return nil }
        self.init(
            red: Double((v >> 16) & 0xFF) / 255,
            green: Double((v >> 8) & 0xFF) / 255,
            blue: Double(v & 0xFF) / 255
        )
    }
}
