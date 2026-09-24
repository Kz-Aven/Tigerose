import SwiftUI
import AppKit

/// Tigerose wordmark / mark loaded from packaged resources.
struct BrandLogo: View {
    /// Longest edge in points. Wordmark is taller than wide.
    var size: CGFloat = 36
    /// Use the upper graphic portion when the wordmark would be unreadable.
    var markOnly = false

    var body: some View {
        Group {
            if let url = AppResources.url(forResource: "logo", withExtension: "png")
                ?? AppResources.url(forResource: "logo", withExtension: "svg"),
               let nsImage = NSImage(contentsOf: url) {
                Image(nsImage: nsImage)
                    .resizable()
                    .interpolation(.high)
                    .aspectRatio(contentMode: markOnly ? .fill : .fit)
                    .frame(width: markOnly ? size : nil, height: markOnly ? size : nil)
                    .frame(maxWidth: markOnly ? nil : size, maxHeight: markOnly ? nil : size)
                    .clipped()
            } else if let icon = NSApp.applicationIconImage {
                Image(nsImage: icon)
                    .resizable()
                    .interpolation(.high)
                    .aspectRatio(contentMode: markOnly ? .fill : .fit)
                    .frame(width: size, height: size)
                    .clipped()
            } else {
                Image(systemName: "sparkles")
                    .font(.system(size: size * 0.72, weight: .medium))
                    .foregroundStyle(TigeroseTheme.primary)
                    .frame(width: size, height: size)
            }
        }
        .accessibilityLabel("Tigerose 钛钢柔")
    }
}
