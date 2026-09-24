import AppKit
import ImageIO
import QuickLook
import SwiftUI
import UniformTypeIdentifiers

struct LocalFileAttachmentCards: View {
    let attachments: [Attachment]
    let maxWidth: CGFloat

    static func includingImages(in content: String, attachments: [Attachment]) -> [Attachment] {
        var result = attachments
        var seen = Set(attachments.map { URL(fileURLWithPath: $0.path).standardizedFileURL.path })
        for path in LocalImageReferences.paths(in: content) where seen.insert(path).inserted {
            result.append(Attachment(path: path, name: URL(fileURLWithPath: path).lastPathComponent,
                                     mime: "image/*", is_image: true))
        }
        return result
    }

    var body: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceXs) {
            ForEach(Array(attachments.enumerated()), id: \.offset) { _, attachment in
                if attachment.is_image == true || attachment.mime.hasPrefix("image/")
                    || UTType(filenameExtension: URL(fileURLWithPath: attachment.path).pathExtension)?.conforms(to: .image) == true {
                    LocalImagePreview(attachment: attachment, maxWidth: maxWidth)
                } else {
                    LocalFileAttachmentCard(attachment: attachment, maxWidth: maxWidth)
                }
            }
        }
    }
}

private struct LocalImagePreview: View {
    let attachment: Attachment
    let maxWidth: CGFloat
    @State private var thumbnail: NSImage?
    @State private var loading = true
    @State private var hovered = false
    @State private var previewURL: URL?

    private var fileURL: URL { URL(fileURLWithPath: attachment.path) }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ZStack(alignment: .topTrailing) {
                Button {
                    previewURL = fileURL
                } label: {
                    Color.clear
                        .aspectRatio(4.0 / 3.0, contentMode: .fit)
                        .overlay {
                            if let thumbnail {
                                Image(nsImage: thumbnail)
                                    .resizable()
                                    .scaledToFit()
                            } else if loading {
                                ProgressView().controlSize(.small)
                            } else {
                                Label("图片无法预览", systemImage: "photo")
                                    .foregroundStyle(TigeroseTheme.muted)
                            }
                        }
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(thumbnail == nil)
                .help("预览图片")
                .accessibilityLabel("预览图片：\(fileURL.lastPathComponent)")

                Button {
                    NSWorkspace.shared.activateFileViewerSelecting([fileURL])
                } label: {
                    Label("在 Finder 中显示", systemImage: "folder")
                        .font(TigeroseTheme.caption.weight(.medium))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 7)
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
                .help("在 Finder 中显示")
                .padding(8)
                .opacity(hovered ? 1 : 0)
                .allowsHitTesting(hovered)
                .accessibilityHidden(!hovered)
            }
            .background(TigeroseTheme.surfaceSoft)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .onHover { hovered = $0 }

            Text(attachment.name.isEmpty ? fileURL.lastPathComponent : attachment.name)
                .font(TigeroseTheme.caption)
                .foregroundStyle(TigeroseTheme.muted)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .frame(maxWidth: min(maxWidth, 400), alignment: .leading)
        .quickLookPreview($previewURL)
        .task(id: attachment.path) {
            loading = true
            thumbnail = nil
            let url = fileURL
            let image = await Task.detached(priority: .userInitiated) { () -> CGImage? in
                guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
                return CGImageSourceCreateThumbnailAtIndex(source, 0, [
                    kCGImageSourceCreateThumbnailFromImageAlways: true,
                    kCGImageSourceCreateThumbnailWithTransform: true,
                    kCGImageSourceThumbnailMaxPixelSize: 800,
                ] as CFDictionary)
            }.value
            guard !Task.isCancelled else { return }
            thumbnail = image.map { NSImage(cgImage: $0, size: .zero) }
            loading = false
        }
    }
}

private struct LocalFileAttachmentCard: View {
    let attachment: Attachment
    let maxWidth: CGFloat

    private var filename: String {
        attachment.name.isEmpty ? URL(fileURLWithPath: attachment.path).lastPathComponent : attachment.name
    }

    var body: some View {
        HStack(spacing: TigeroseTheme.spaceSm) {
            Image(systemName: "doc.fill")
                .font(.system(size: 16, weight: .medium))
                .foregroundStyle(TigeroseTheme.muted)
                .frame(width: 36, height: 36)
                .background(TigeroseTheme.surfaceSoft, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm))

            VStack(alignment: .leading, spacing: 3) {
                Text(filename)
                    .font(TigeroseTheme.bodySm.weight(.semibold))
                    .foregroundStyle(TigeroseTheme.ink)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(attachment.path)
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(TigeroseTheme.muted)
                    .textSelection(.enabled)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Button("Finder 中打开") {
                revealInFinder()
            }
            .font(TigeroseTheme.caption.weight(.semibold))
            .foregroundStyle(TigeroseTheme.canvas)
            .padding(.horizontal, TigeroseTheme.spaceSm)
            .padding(.vertical, 6)
            .background(TigeroseTheme.ink, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusSm))
            .buttonStyle(.plain)
        }
        .padding(.horizontal, TigeroseTheme.spaceMd)
        .padding(.vertical, TigeroseTheme.spaceSm)
        .frame(maxWidth: maxWidth, alignment: .leading)
        .background(TigeroseTheme.surfaceCard, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
        .tigeroseHairline(radius: TigeroseTheme.radiusMd)
    }

    private func revealInFinder() {
        let fileURL = URL(fileURLWithPath: attachment.path)
        if FileManager.default.fileExists(atPath: fileURL.path) {
            NSWorkspace.shared.activateFileViewerSelecting([fileURL])
        } else {
            NSWorkspace.shared.open(fileURL.deletingLastPathComponent())
        }
    }
}
