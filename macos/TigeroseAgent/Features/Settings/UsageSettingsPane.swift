import SwiftUI
import Charts
import AppKit

struct UsageSettingsPane: View {
    @Environment(AppState.self) private var app

    private enum Range: String, CaseIterable, Identifiable {
        case today, week, month, custom
        var id: String { rawValue }
        var label: String {
            switch self {
            case .today: return "今日"
            case .week: return "近7天"
            case .month: return "近30天"
            case .custom: return "自定义"
            }
        }
    }

    @State private var range: Range = .week
    @State private var showSpend = false
    @State private var customStart = Self.shanghaiCalendar.date(byAdding: .day, value: -6, to: Date()) ?? Date()
    @State private var customEnd = Date()
    @State private var model = ""
    @State private var agentId = ""
    @State private var overview: UsageSummary?
    @State private var trend: [UsageTrendItem] = []
    @State private var byModel: [UsageModelItem] = []
    @State private var byAgent: [UsageAgentItem] = []
    @State private var loading = true
    @State private var error = ""
    @State private var hoveredTrendDate = ""
    @State private var trendHoverLocation: CGPoint?

    private static var shanghaiCalendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai") ?? .current
        return calendar
    }

    private static let dateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.calendar = shanghaiCalendar
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()

    private var dates: (Date, Date) {
        let today = Self.shanghaiCalendar.startOfDay(for: Date())
        switch range {
        case .today: return (today, today)
        case .week: return (Self.shanghaiCalendar.date(byAdding: .day, value: -6, to: today) ?? today, today)
        case .month: return (Self.shanghaiCalendar.date(byAdding: .day, value: -29, to: today) ?? today, today)
        case .custom: return (customStart, customEnd)
        }
    }

    private var requestKey: String {
        let dates = dates
        return "\(range.rawValue)-\(Self.dateFormatter.string(from: dates.0))-\(Self.dateFormatter.string(from: dates.1))-\(model)-\(agentId)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
            HStack {
                Text("模型用量").font(TigeroseTheme.titleMd).foregroundStyle(TigeroseTheme.ink)
                Button {
                    Task { await revealPricingFile() }
                } label: {
                    Label("价格表", systemImage: "doc.text")
                }
                .buttonStyle(.borderless)
                .foregroundStyle(TigeroseTheme.primary)
                Spacer()
                if loading { ProgressView().controlSize(.small) }
            }
            .padding(.horizontal, TigeroseTheme.spaceLg)

            filters
            if !error.isEmpty {
                Text(error).font(TigeroseTheme.bodySm).foregroundStyle(TigeroseTheme.error).padding(.horizontal, TigeroseTheme.spaceLg)
            }
            if !loading, overview?.call_count == 0 {
                ContentUnavailableView("暂无上线后的模型用量数据", systemImage: "chart.bar", description: Text("发起一次助理对话后，这里会显示统计结果。"))
                    .foregroundStyle(TigeroseTheme.muted)
            } else if let overview, overview.call_count > 0 {
                ScrollView {
                    VStack(alignment: .leading, spacing: TigeroseTheme.spaceLg) {
                        metrics(overview)
                        Text(usageNote(overview))
                            .font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                        trendChart
                        UsageSummaryTable(title: "按模型汇总", rows: byModel.map { UsageSummaryRow(name: $0.model, spendCNY: $0.spend_cny, priceStatus: $0.price_status, input: $0.uncached_input_tokens, cachedInput: $0.cached_input_tokens, output: $0.output_tokens, total: $0.total_tokens, calls: $0.call_count) })
                        UsageSummaryTable(title: "按助理汇总", rows: byAgent.map { UsageSummaryRow(name: $0.agent_name, spendCNY: $0.spend_cny, unpricedModelCount: $0.unpriced_model_count, input: $0.uncached_input_tokens, cachedInput: $0.cached_input_tokens, output: $0.output_tokens, total: $0.total_tokens, calls: $0.call_count) })
                    }
                    .padding(.horizontal, TigeroseTheme.spaceLg)
                    .padding(.bottom, TigeroseTheme.spaceLg)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .task(id: requestKey) { await reload() }
    }

    private var filters: some View {
        HStack(spacing: TigeroseTheme.spaceSm) {
            TigeroseSegmentedPicker(label: "日期范围", selection: $range, options: Range.allCases.map { ($0, $0.label) })
            .frame(width: 300)
            if range == .custom {
                DatePicker("", selection: $customStart, displayedComponents: .date).labelsHidden()
                Text("至").font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                DatePicker("", selection: $customEnd, displayedComponents: .date).labelsHidden()
            }
            Spacer(minLength: TigeroseTheme.spaceSm)
            Picker("模型", selection: $model) {
                Text("全部模型").tag("")
                ForEach(byModel) { item in Text(item.model).tag(item.model) }
            }
            .frame(width: 180)
            Picker("助理", selection: $agentId) {
                Text("全部助理").tag("")
                ForEach(byAgent.filter { !$0.agent_id.isEmpty }) { item in Text(item.agent_name).tag(item.agent_id) }
            }
            .frame(width: 160)
        }
        .padding(.horizontal, TigeroseTheme.spaceLg)
    }

    private func metrics(_ summary: UsageSummary) -> some View {
        HStack(spacing: TigeroseTheme.spaceMd) {
            UsageMetric(title: "消费金额", value: formatCNY(summary.spend_cny))
            UsageMetric(title: "总 Token", value: formatTokenCount(summary.total_tokens))
            UsageMetric(title: "输入 Token", value: formatTokenCount(summary.uncached_input_tokens))
            UsageMetric(title: "输入（缓存）", value: formatTokenCount(summary.cached_input_tokens))
            UsageMetric(title: "输出 Token", value: formatTokenCount(summary.output_tokens))
        }
    }

    private func usageNote(_ summary: UsageSummary) -> String {
        var parts = [summary.estimated_call_count > 0 ? "含 \(summary.estimated_call_count) 次估算调用" : "全部为 Provider 返回的用量"]
        parts.append("缓存信息仅统计 Provider 返回字段")
        if summary.unpriced_model_count > 0 {
            parts.append("\(summary.unpriced_model_count) 个模型未配置价格，未计入消费金额")
        }
        return parts.joined(separator: " · ")
    }

    private var trendChart: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
            HStack {
                Text(showSpend ? "每日消费金额" : "每日 Token 消耗")
                    .font(TigeroseTheme.titleSm).foregroundStyle(TigeroseTheme.ink)
                Spacer()
                TigeroseSegmentedPicker(label: "趋势指标", selection: $showSpend, options: [(false, "Token"), (true, "金额")])
                .frame(width: 140)
            }
            Chart(trend) { item in
                if !showSpend || item.spend_cny != nil {
                    BarMark(
                        x: .value("日期", item.date),
                        y: .value(showSpend ? "消费金额" : "Token", showSpend ? (item.spend_cny ?? 0) : Double(item.total_tokens))
                    )
                    .foregroundStyle(by: .value("模型", item.model))
                }
            }
            .chartXScale(domain: Array(Set(trend.map(\.date))).sorted())
            .chartForegroundStyleScale(domain: Array(Set(trend.map(\.model))).sorted())
            .chartLegend(position: .bottom, alignment: .leading)
            .chartYAxis {
                AxisMarks(position: .leading) { value in
                    AxisGridLine()
                    AxisTick()
                    AxisValueLabel {
                        if let amount = value.as(Double.self) {
                            if showSpend {
                                Text(amount, format: .currency(code: "CNY").precision(.fractionLength(2...4)))
                            } else {
                                Text(amount, format: .number.precision(.fractionLength(0)))
                            }
                        }
                    }
                }
            }
            .chartOverlay { proxy in
                GeometryReader { geometry in
                    if let plotAnchor = proxy.plotFrame {
                        let plotFrame = geometry[plotAnchor]
                        ZStack(alignment: .topLeading) {
                            Rectangle()
                                .fill(.clear)
                                .contentShape(Rectangle())
                                .onContinuousHover { phase in
                                    switch phase {
                                    case .active(let location):
                                        guard plotFrame.contains(location) else {
                                            hoveredTrendDate = ""
                                            trendHoverLocation = nil
                                            return
                                        }
                                        let x = location.x - plotFrame.origin.x
                                        guard let date: String = proxy.value(atX: x, as: String.self),
                                              !trendDetails(for: date).isEmpty
                                        else {
                                            hoveredTrendDate = ""
                                            trendHoverLocation = nil
                                            return
                                        }
                                        hoveredTrendDate = date
                                        trendHoverLocation = location
                                    case .ended:
                                        hoveredTrendDate = ""
                                        trendHoverLocation = nil
                                    }
                                }

                            if let location = trendHoverLocation, !hoveredTrendDate.isEmpty {
                                UsageTrendTooltip(date: hoveredTrendDate, rows: trendDetails(for: hoveredTrendDate))
                                    .position(tooltipPosition(for: location, in: geometry.size))
                                    .allowsHitTesting(false)
                            }
                        }
                    }
                }
            }
            .frame(height: 220)
            .onChange(of: showSpend) { _, _ in
                hoveredTrendDate = ""
                trendHoverLocation = nil
            }
        }
    }

    private func trendDetails(for date: String) -> [UsageTrendItem] {
        trend
            .filter { $0.date == date }
            .sorted {
                if showSpend {
                    return ($0.spend_cny ?? -1) > ($1.spend_cny ?? -1)
                }
                return $0.total_tokens > $1.total_tokens
            }
    }

    private func tooltipPosition(for location: CGPoint, in size: CGSize) -> CGPoint {
        let rows = trendDetails(for: hoveredTrendDate).count
        let width: CGFloat = 450
        let height = CGFloat(44 + rows * 40)
        return CGPoint(
            x: min(max(location.x + width / 2 + 14, width / 2 + 8), size.width - width / 2 - 8),
            y: min(max(location.y - height / 2, height / 2 + 8), size.height - height / 2 - 8)
        )
    }

    private func reload() async {
        guard app.backendStatus == .running else { return }
        loading = true
        defer { loading = false }
        error = ""
        let dates = dates
        let start = Self.dateFormatter.string(from: dates.0)
        let end = Self.dateFormatter.string(from: dates.1)
        do {
            async let overviewRequest = app.api.usageOverview(startDate: start, endDate: end, model: model, agentId: agentId)
            async let trendRequest = app.api.usageTrend(startDate: start, endDate: end, model: model, agentId: agentId)
            async let modelsRequest = app.api.usageByModel(startDate: start, endDate: end, model: model, agentId: agentId)
            async let agentsRequest = app.api.usageByAgent(startDate: start, endDate: end, model: model, agentId: agentId)
            let (overviewResponse, trendResponse, modelResponse, agentResponse) = try await (overviewRequest, trendRequest, modelsRequest, agentsRequest)
            overview = overviewResponse.data
            trend = trendResponse.items
            byModel = modelResponse.items
            byAgent = agentResponse.items
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func revealPricingFile() async {
        do {
            let response = try await app.api.usagePricingFile()
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: response.path)])
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct UsageMetric: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceXxs) {
            Text(title).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
            Text(value).font(TigeroseTheme.titleSm).foregroundStyle(TigeroseTheme.ink).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.bottom, TigeroseTheme.spaceSm)
        .overlay(alignment: .bottom) { Rectangle().fill(TigeroseTheme.hairline).frame(height: 1) }
    }
}

private struct UsageTrendTooltip: View {
    let date: String
    let rows: [UsageTrendItem]

    var body: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceXs) {
            Text(date).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
            ForEach(rows) { row in
                VStack(alignment: .leading, spacing: 2) {
                    Text(row.model).font(TigeroseTheme.bodySm).foregroundStyle(TigeroseTheme.ink)
                    Text("输入：\(formatTokenCount(row.uncached_input_tokens))  |  输入（缓存）：\(formatTokenCount(row.cached_input_tokens))  |  输出：\(formatTokenCount(row.output_tokens))  |  总：\(formatTokenCount(row.total_tokens))")
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.body)
                        .lineLimit(1)
                    Text("消费金额：\(formatConsumptionAmount(row.spend_cny, priceStatus: row.price_status))")
                        .font(TigeroseTheme.caption)
                        .foregroundStyle(TigeroseTheme.body)
                }
            }
        }
        .frame(width: 430, alignment: .leading)
        .padding(TigeroseTheme.spaceSm)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 6))
        .overlay {
            RoundedRectangle(cornerRadius: 6)
                .stroke(TigeroseTheme.hairline, lineWidth: 1)
        }
        .shadow(color: TigeroseTheme.ink.opacity(0.10), radius: 8, y: 3)
    }
}

private struct UsageSummaryRow: Identifiable {
    let name: String
    let spendCNY: Double?
    let priceStatus: String
    let unpricedModelCount: Int
    let input: Int
    let cachedInput: Int
    let output: Int
    let total: Int
    let calls: Int
    var id: String { name }

    init(
        name: String,
        spendCNY: Double?,
        priceStatus: String = "configured",
        unpricedModelCount: Int = 0,
        input: Int,
        cachedInput: Int,
        output: Int,
        total: Int,
        calls: Int
    ) {
        self.name = name
        self.spendCNY = spendCNY
        self.priceStatus = priceStatus
        self.unpricedModelCount = unpricedModelCount
        self.input = input
        self.cachedInput = cachedInput
        self.output = output
        self.total = total
        self.calls = calls
    }
}

private struct UsageSummaryTable: View {
    let title: String
    let rows: [UsageSummaryRow]

    var body: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
            Text(title).font(TigeroseTheme.titleSm).foregroundStyle(TigeroseTheme.ink)
            Grid(alignment: .leading, horizontalSpacing: TigeroseTheme.spaceMd, verticalSpacing: TigeroseTheme.spaceXs) {
                GridRow {
                    Text(title == "按模型汇总" ? "模型" : "助理")
                    Text("消费金额"); Text("输入"); Text("输入（缓存）"); Text("输出"); Text("总 Token"); Text("调用")
                }
                .font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted)
                ForEach(rows) { row in
                    Divider().gridCellColumns(7)
                    GridRow {
                        Text(row.name).lineLimit(1).hoverFullText(row.name)
                        Text(formatConsumptionAmount(row.spendCNY, priceStatus: row.priceStatus, unpricedModelCount: row.unpricedModelCount))
                        Text(formatTokenCount(row.input)); Text(formatTokenCount(row.cachedInput)); Text(formatTokenCount(row.output)); Text(formatTokenCount(row.total)); Text("\(row.calls)")
                    }
                    .font(TigeroseTheme.bodySm).foregroundStyle(TigeroseTheme.body)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

func formatTokenCount(_ value: Int) -> String {
    if value >= 1_000_000 { return String(format: value >= 10_000_000 ? "%.1fM" : "%.2fM", Double(value) / 1_000_000) }
    if value >= 1_000 { return String(format: value >= 10_000 ? "%.1fK" : "%.2fK", Double(value) / 1_000) }
    return "\(value)"
}

func formatCNY(_ value: Double) -> String {
    String(format: "¥%.2f", value)
}

func formatConsumptionAmount(_ amount: Double?, priceStatus: String = "configured", unpricedModelCount: Int = 0) -> String {
    guard let amount else {
        return priceStatus == "unconfigured" ? "未配置价格" : "汇率不可用"
    }
    let text = formatCNY(amount)
    return unpricedModelCount > 0 ? "\(text)（\(unpricedModelCount) 未配置）" : text
}
