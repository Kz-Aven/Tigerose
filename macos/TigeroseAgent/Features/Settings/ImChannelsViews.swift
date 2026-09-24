import SwiftUI
import AppKit

struct ImChannelsSettingsPane: View {
    @Environment(AppState.self) private var app
    @State private var platform = "feishu"
    @State private var applications: [ImChannelApplication] = []
    @State private var registration: ImRegistration?
    @State private var loading = false
    @State private var error = ""

    private let platforms = [("feishu", "飞书"), ("dingtalk", "钉钉"), ("wecom", "企微")]

    var body: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
            HStack {
                Text("IM频道").font(TigeroseTheme.titleMd).foregroundStyle(TigeroseTheme.ink)
                Spacer()
                Button("新增配置") { Task { await startRegistration() } }
                    .buttonStyle(TigerosePrimaryButtonStyle(disabled: loading))
                    .disabled(loading)
            }
            .padding(.horizontal, TigeroseTheme.spaceLg)

            TigeroseSegmentedPicker(label: "IM平台", selection: $platform, options: platforms)
            .fixedSize()
            .padding(.horizontal, TigeroseTheme.spaceLg)
            .onChange(of: platform) { _, _ in Task { await load() } }

            if !error.isEmpty {
                Text(error).font(TigeroseTheme.bodySm).foregroundStyle(TigeroseTheme.error)
                    .padding(.horizontal, TigeroseTheme.spaceLg)
            }
            if applications.isEmpty {
                ContentUnavailableView("暂无机器人", systemImage: "bubble.left.and.bubble.right", description: Text("新增配置后，通过扫码关联机器人。"))
                    .foregroundStyle(TigeroseTheme.muted)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
                        ForEach(applications) { application in applicationRow(application) }
                    }
                    .padding(.horizontal, TigeroseTheme.spaceLg)
                }
            }
        }
        .padding(.vertical, TigeroseTheme.spaceMd)
        .task { await load() }
        .sheet(item: $registration) { value in registrationSheet(value) }
    }

    private func applicationRow(_ application: ImChannelApplication) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label(application.display_name.isEmpty ? platformTitle(application.platform) : application.display_name, systemImage: platformIcon(application.platform))
                    .font(TigeroseTheme.titleSm).foregroundStyle(TigeroseTheme.ink)
                Spacer()
                Text(application.status == "connected" ? "已连接" : application.status)
                    .font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
            }
            Text(mask(application.provider_application_id)).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
            ForEach(application.bots) { bot in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(bot.display_name.isEmpty ? "机器人" : bot.display_name).font(TigeroseTheme.bodySm).foregroundStyle(TigeroseTheme.ink)
                        Text(mask(bot.provider_bot_id)).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                    }
                    Spacer()
                    Text(bot.template_name ?? "未绑定助理").font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                    Button("删除", role: .destructive) { Task { await delete(bot) } }
                        .buttonStyle(TigeroseSecondaryButtonStyle())
                }
            }
        }
        .padding(TigeroseTheme.spaceMd)
        .background(TigeroseTheme.surfaceCard, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
        .overlay(RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd).stroke(TigeroseTheme.hairline, lineWidth: 1))
    }

    private func registrationSheet(_ value: ImRegistration) -> some View {
        VStack(spacing: TigeroseTheme.spaceMd) {
            TigeroseSheetHeader(title: "关联\(platformTitle(value.platform))机器人", onClose: { Task { await closeRegistration() } })
            if let image = imageFromDataURL(value.qr_image) {
                Image(nsImage: image).interpolation(.none).resizable().scaledToFit().frame(width: 260, height: 260)
            } else if value.state == "starting" {
                ProgressView().frame(width: 260, height: 260)
            } else if let error = value.error {
                Text(error).font(TigeroseTheme.bodySm).foregroundStyle(TigeroseTheme.error).multilineTextAlignment(.center).padding()
            }
            Text(registrationText(value)).font(TigeroseTheme.bodySm).foregroundStyle(TigeroseTheme.muted).multilineTextAlignment(.center)
            HStack {
                Button("刷新二维码") { Task { await refreshRegistration() } }.buttonStyle(TigeroseSecondaryButtonStyle())
                if value.state == "success" { Button("完成") { registration = nil }.buttonStyle(TigerosePrimaryButtonStyle()) }
            }
        }
        .frame(width: 380, height: 440)
        .background(TigeroseTheme.canvas)
    }

    private func load() async {
        do { applications = try await app.api.listImChannels(platform: platform); error = "" }
        catch { self.error = error.localizedDescription }
    }

    private func startRegistration() async {
        loading = true; defer { loading = false }
        do { registration = try await app.api.startImRegistration(platform: platform); await pollRegistration() }
        catch { self.error = error.localizedDescription }
    }

    private func refreshRegistration() async {
        guard let registration else { return }
        do { self.registration = try await app.api.refreshImRegistration(registration.id); await pollRegistration() }
        catch { self.error = error.localizedDescription }
    }

    private func pollRegistration() async {
        while let current = registration, ["starting", "waiting"].contains(current.state) {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard registration?.id == current.id else { return }
            if let next = try? await app.api.getImRegistration(current.id) { registration = next }
        }
        if registration?.state == "success" { await load() }
    }

    private func closeRegistration() async {
        if let registration, ["starting", "waiting"].contains(registration.state) { try? await app.api.cancelImRegistration(registration.id) }
        registration = nil
    }

    private func delete(_ bot: ImBot) async { try? await app.api.deleteImBot(bot.bot_id); await load() }
    private func platformTitle(_ id: String) -> String { platforms.first(where: { $0.0 == id })?.1 ?? id }
    private func platformIcon(_ id: String) -> String { id == "feishu" ? "paperplane" : "bubble.left.and.bubble.right" }
    private func mask(_ id: String) -> String { id.count <= 8 ? id : "\(id.prefix(4))********\(id.suffix(4))" }
    private func registrationText(_ registration: ImRegistration) -> String {
        if let message = registration.message, !message.isEmpty { return message }
        if registration.state == "success" { return "机器人已安全关联到本机，请到助理配置中绑定。" }
        if registration.state == "waiting" { return "请使用\(platformTitle(registration.platform))扫描二维码并在手机上确认。" }
        return "正在准备扫码关联。"
    }
    private func imageFromDataURL(_ value: String?) -> NSImage? {
        guard let value, let comma = value.firstIndex(of: ","), let data = Data(base64Encoded: String(value[value.index(after: comma)...])) else { return nil }
        return NSImage(data: data)
    }
}

struct ImAssistantBotsPane: View {
    @Environment(AppState.self) private var app
    let templateId: String
    @State private var bots = ImAssistantBots(bound: [], available: [])
    @State private var error = ""

    var body: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
            Text("IM频道").font(TigeroseTheme.titleMd).foregroundStyle(TigeroseTheme.ink)
            if !error.isEmpty { Text(error).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.error) }
            if bots.bound.isEmpty && bots.available.isEmpty {
                ContentUnavailableView("暂无可用机器人", systemImage: "bubble.left.and.bubble.right", description: Text("请先在设置中的 IM频道扫码关联机器人。"))
                    .foregroundStyle(TigeroseTheme.muted)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ForEach(bots.bound) { bot in botRow(bot, action: "解绑") { await bind(bot, templateId: nil) } }
                if !bots.available.isEmpty {
                    Divider()
                    Text("可绑定机器人").font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                    ForEach(bots.available) { bot in botRow(bot, action: "绑定") { await bind(bot, templateId: templateId) } }
                }
                Spacer()
            }
        }
        .padding(TigeroseTheme.spaceLg)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .task { await load() }
    }

    private func botRow(_ bot: ImBot, action: String, work: @escaping () async -> Void) -> some View {
        HStack {
            Image(systemName: bot.platform == "feishu" ? "paperplane" : "bubble.left.and.bubble.right").foregroundStyle(TigeroseTheme.muted)
            VStack(alignment: .leading, spacing: 2) {
                Text(bot.display_name.isEmpty ? "机器人" : bot.display_name).font(TigeroseTheme.bodySm).foregroundStyle(TigeroseTheme.ink)
                Text(bot.platform ?? "").font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
            }
            Spacer()
            Text(bot.health == "connected" ? "已连接" : bot.health).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
            Button(action) { Task { await work() } }.buttonStyle(TigeroseSecondaryButtonStyle())
        }
        .padding(.vertical, 6)
    }

    private func load() async { do { bots = try await app.api.listAssistantImBots(templateId); error = "" } catch { self.error = error.localizedDescription } }
    private func bind(_ bot: ImBot, templateId: String?) async { do { _ = try await app.api.bindImBot(bot.bot_id, templateId: templateId); await load() } catch { self.error = error.localizedDescription } }
}
