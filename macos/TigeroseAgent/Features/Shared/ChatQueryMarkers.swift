import SwiftUI

struct ChatQueryMarker: Identifiable {
    let id: String
    let query: String
}

struct ChatQueryMarkers: View {
    let markers: [ChatQueryMarker]
    let onSelect: (String) -> Void

    @State private var selectedID: String?
    @State private var hoveredIndex: Int?

    var body: some View {
        GeometryReader { geometry in
            let visibleMarkers = Array(markers.prefix(max(0, Int((geometry.size.height - 32) / 18))))

            VStack(spacing: 7) {
                ForEach(Array(visibleMarkers.enumerated()), id: \.element.id) { index, marker in
                    markerButton(marker, index: index)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .trailing)
            .padding(.trailing, 10)
        }
        .allowsHitTesting(!markers.isEmpty)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("用户提问快捷跳转")
    }

    @ViewBuilder
    private func markerButton(_ marker: ChatQueryMarker, index: Int) -> some View {
        let distance = hoveredIndex.map { abs($0 - index) }
        let width: CGFloat = switch distance {
        case 0: 44
        case 1: 32
        case 2: 22
        default: 10
        }
        let opacity: Double = switch distance {
        case 0: 1
        case 1: 0.78
        case 2: 0.56
        default: 0.24
        }

        Button {
            selectedID = marker.id
            onSelect(marker.id)
        } label: {
            Rectangle()
                .fill(hoveredIndex == index ? TigeroseTheme.ink : (selectedID == marker.id ? TigeroseTheme.primary : TigeroseTheme.muted))
                .frame(width: width, height: 5)
                .opacity(opacity)
                .frame(width: 44, height: 11, alignment: .trailing)
                .contentShape(Rectangle())
                .animation(.easeOut(duration: 0.16), value: hoveredIndex)
        }
        .buttonStyle(.plain)
        .onHover { hovering in
            hoveredIndex = hovering ? index : nil
        }
        .overlay(alignment: .leading) {
            if hoveredIndex == index {
                Text(marker.query)
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.ink)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(width: 300, alignment: .leading)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 7)
                    .background(TigeroseTheme.canvas, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm))
                    .overlay {
                        RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm)
                            .stroke(TigeroseTheme.hairline, lineWidth: 1)
                    }
                    .offset(x: -312)
                    .transition(.opacity)
                    .zIndex(1)
            }
        }
        .accessibilityLabel(marker.query)
        .accessibilityHint("跳转到这条用户提问")
    }
}
