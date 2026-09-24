import SwiftUI
import AppKit

@main
struct TigeroseAgentApp: App {
    @State private var appState = AppState()
    @State private var themeStore = ThemeStore.shared
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(appState)
                .environment(themeStore)
                .environment(\.tigeroseTheme, themeStore.tokens)
                .id(themeStore.skinID)
                .task {
                    appDelegate.appState = appState
                    await appState.bootstrap()
                }
                .onAppear { NSApp.setActivationPolicy(.regular) }
        }
        .defaultSize(width: 1100, height: 720)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("Tigerose") {
                Button("新建助理…") {
                    NotificationCenter.default.post(name: .tigeroseNewAssistant, object: nil)
                }
                .keyboardShortcut("n", modifiers: [.command])
                Button("新建项目群…") {
                    NotificationCenter.default.post(name: .tigeroseNewGroup, object: nil)
                }
                .keyboardShortcut("n", modifiers: [.command, .shift])
                Button("设置…") {
                    NotificationCenter.default.post(name: .tigeroseOpenSettings, object: nil)
                }
                .keyboardShortcut(",", modifiers: [.command])
                Divider()
                Button("打开引擎日志") {
                    NSWorkspace.shared.open(TigerosePaths.backendLog)
                }
                Button("重试启动引擎") {
                    Task { await appState.startBackendAndLoad() }
                }
            }
        }

        Settings {
            SettingsView()
                .environment(appState)
                .environment(themeStore)
                .environment(\.tigeroseTheme, themeStore.tokens)
                .id(themeStore.skinID)
                .frame(minWidth: 520, minHeight: 360)
        }
    }
}

extension Notification.Name {
    static let tigeroseNewAssistant = Notification.Name("tigeroseNewAssistant")
    static let tigeroseNewGroup = Notification.Name("tigeroseNewGroup")
    static let tigeroseOpenSettings = Notification.Name("tigeroseOpenSettings")
}

struct RootView: View {
    @Environment(AppState.self) private var app
    @State private var meshPermission: MeshDecision?

    var body: some View {
        Group {
            if app.needsOnboarding {
                OnboardingView()
            } else if app.backendStatus == .starting {
                ProgressView("正在启动引擎…")
                    .tint(TigeroseTheme.primary)
                    .foregroundStyle(TigeroseTheme.body)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if app.backendStatus == .failed {
                VStack(spacing: TigeroseTheme.spaceMd) {
                    Text("引擎未启动")
                        .font(TigeroseTheme.titleDisplay)
                        .foregroundStyle(TigeroseTheme.ink)
                    Text(app.backendError)
                        .font(TigeroseTheme.bodySm)
                        .foregroundStyle(TigeroseTheme.muted)
                        .multilineTextAlignment(.center)
                    HStack(spacing: TigeroseTheme.spaceSm) {
                        Button("打开日志") { NSWorkspace.shared.open(TigerosePaths.backendLog) }
                            .buttonStyle(TigeroseSecondaryButtonStyle())
                        Button("重试") { Task { await app.startBackendAndLoad() } }
                            .buttonStyle(TigerosePrimaryButtonStyle())
                    }
                }
                .padding(TigeroseTheme.spaceXl)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ContentView()
            }
        }
        .background(TigeroseTheme.canvas)
        .tint(TigeroseTheme.primary)
        .sheet(item: $meshPermission) { decision in
            MeshDecisionCard(decision: decision, permissionDialog: true) { meshPermission = nil }
                .id(decision.id)
            .frame(width: 520, height: 520)
            .interactiveDismissDisabled()
        }
        .task(id: app.backendStatus) {
            guard app.backendStatus == .running, !app.needsOnboarding else { return }
            while !Task.isCancelled {
                do {
                    let pending = try await app.api.pendingMeshPermissions()
                    guard !Task.isCancelled else { return }
                    if let current = meshPermission {
                        meshPermission = pending.first { $0.id == current.id }
                    } else {
                        meshPermission = pending.first
                    }
                } catch { /* Keep a pending prompt visible during transient reconnects. */ }
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
            }
        }
        .overlay(alignment: .bottom) {
            if !app.globalError.isEmpty {
                Text(app.globalError)
                    .font(TigeroseTheme.caption)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(TigeroseTheme.error.opacity(0.92), in: Capsule())
                    .foregroundStyle(TigeroseTheme.onPrimary)
                    .padding()
            }
        }
    }
}
