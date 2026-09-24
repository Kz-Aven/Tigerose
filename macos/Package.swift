// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "TigeroseAgent",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "TigeroseAgent", targets: ["TigeroseAgent"]),
    ],
    dependencies: [
        .package(url: "https://github.com/gonzalezreal/swift-markdown-ui", from: "2.4.1"),
    ],
    targets: [
        .executableTarget(
            name: "TigeroseAgent",
            dependencies: [
                .product(name: "MarkdownUI", package: "swift-markdown-ui"),
            ],
            path: "TigeroseAgent",
            exclude: [
                "Resources/AppIcon/AppIcon.iconset",
            ],
            resources: [
                .copy("Resources/RuntimePlaceholder"),
                .copy("Resources/AppIcon/AppIcon.icns"),
                .copy("Resources/AppIcon/logo.png"),
                .copy("Resources/AppIcon/logo.svg"),
                .copy("Resources/Avatars"),
                .copy("Resources/Themes"),
            ]
        ),
    ]
)
