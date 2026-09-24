import SwiftUI

struct WorkflowDAGView: View {
    let graph: MeshGraph
    let assistantNames: [String: String]
    let onSelect: (String) -> Void

    private let cardSize = CGSize(width: 190, height: 104)
    private let columnGap: CGFloat = 72
    private let rowGap: CGFloat = 28

    var body: some View {
        let layout = DAGLayout(graph: graph, cardSize: cardSize, columnGap: columnGap, rowGap: rowGap)
        ScrollView([.horizontal, .vertical]) {
            ZStack(alignment: .topLeading) {
                Canvas { context, _ in
                    for edge in graph.edges {
                        guard let from = layout.positions[edge.source], let to = layout.positions[edge.target] else { continue }
                        drawEdge(edge, from: from, to: to, in: &context)
                    }
                }
                .frame(width: layout.size.width, height: layout.size.height)

                ForEach(graph.nodes) { node in
                    DAGNodeCard(node: node, assistantNames: assistantNames) {
                        if let taskID = node.task_id { onSelect(taskID) }
                    }
                    .frame(width: cardSize.width, height: cardSize.height)
                    .position(layout.positions[node.node_id] ?? .zero)
                }
            }
            .frame(width: layout.size.width, height: layout.size.height, alignment: .topLeading)
            .padding(16)
        }
        .background(TigeroseTheme.surfaceSoft)
    }

    private func drawEdge(_ edge: MeshEdge, from: CGPoint, to: CGPoint, in context: inout GraphicsContext) {
        let start = CGPoint(x: from.x + cardSize.width / 2, y: from.y)
        let end = CGPoint(x: to.x - cardSize.width / 2, y: to.y)
        let middle = (start.x + end.x) / 2
        var path = Path()
        path.move(to: start)
        path.addCurve(to: end, control1: CGPoint(x: middle, y: start.y), control2: CGPoint(x: middle, y: end.y))
        let color: Color = edge.kind == "dependency" ? .orange : edge.kind == "aggregation" ? .purple : TigeroseTheme.primary
        var style = StrokeStyle(lineWidth: 1.5, lineCap: .round)
        if edge.kind == "dependency" { style.dash = [5, 4] }
        context.stroke(path, with: .color(color.opacity(0.72)), style: style)

        var arrow = Path()
        arrow.move(to: CGPoint(x: end.x - 8, y: end.y - 5))
        arrow.addLine(to: end)
        arrow.addLine(to: CGPoint(x: end.x - 8, y: end.y + 5))
        context.stroke(arrow, with: .color(color.opacity(0.72)), style: StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))
    }
}

private struct DAGNodeCard: View {
    let node: MeshGraphNode
    let assistantNames: [String: String]
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 5) {
                    Image(systemName: icon).font(.system(size: 11, weight: .semibold))
                    Text(kindTitle).font(TigeroseTheme.caption.weight(.semibold))
                    Spacer()
                }
                .foregroundStyle(accent)
                Text(title).font(TigeroseTheme.bodySm.weight(.semibold)).lineLimit(2).multilineTextAlignment(.leading)
                Text(detail).font(TigeroseTheme.caption).foregroundStyle(TigeroseTheme.muted).lineLimit(1)
                Text(MeshTask.statusTitle(node.status)).font(TigeroseTheme.caption.weight(.semibold)).foregroundStyle(statusColor)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
            .padding(10)
            .background(background, in: RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(accent.opacity(0.35), lineWidth: 1))
        }
        .buttonStyle(.plain)
        .disabled(node.task_id == nil)
    }

    private var title: String { assistantNames[node.assistant_id ?? ""] ?? node.title }
    private var detail: String {
        node.task_title ?? node.assistant_name ?? ""
    }
    private var kindTitle: String { node.kind == "summary" ? "最终汇总" : (node.assistant_name ?? "助理") }
    private var icon: String { node.kind == "summary" ? "flag.checkered" : "person.crop.circle" }
    private var accent: Color { node.kind == "summary" ? .purple : .blue }
    private var background: Color { node.kind == "summary" ? .purple.opacity(0.06) : .blue.opacity(0.05) }
    private var statusColor: Color { node.status == "completed" ? TigeroseTheme.success : node.status == "failed" ? .red : TigeroseTheme.muted }
}

private struct DAGLayout {
    let positions: [String: CGPoint]
    let size: CGSize

    init(graph: MeshGraph, cardSize: CGSize, columnGap: CGFloat, rowGap: CGFloat) {
        let ids = Set(graph.nodes.map(\.node_id))
        var incoming = Dictionary(uniqueKeysWithValues: graph.nodes.map { ($0.node_id, 0) })
        var outgoing = Dictionary(uniqueKeysWithValues: graph.nodes.map { ($0.node_id, [String]()) })
        for edge in graph.edges where ids.contains(edge.source) && ids.contains(edge.target) {
            incoming[edge.target, default: 0] += 1
            outgoing[edge.source, default: []].append(edge.target)
        }
        var rank: [String: Int] = [:]
        var queue = graph.nodes.filter { incoming[$0.node_id] == 0 }.map(\.node_id)
        for id in queue { rank[id] = 0 }
        var index = 0
        while index < queue.count {
            let id = queue[index]
            index += 1
            for target in outgoing[id] ?? [] {
                rank[target] = max(rank[target] ?? 0, (rank[id] ?? 0) + 1)
                incoming[target, default: 1] -= 1
                if incoming[target] == 0 { queue.append(target) }
            }
        }
        for node in graph.nodes where rank[node.node_id] == nil { rank[node.node_id] = 0 }
        var columns: [Int: [MeshGraphNode]] = [:]
        for node in graph.nodes { columns[rank[node.node_id] ?? 0, default: []].append(node) }
        for key in columns.keys { columns[key]?.sort { $0.title < $1.title } }
        let maxRank = columns.keys.max() ?? 0
        var result: [String: CGPoint] = [:]
        var height: CGFloat = cardSize.height + 32
        for column in 0...maxRank {
            let nodes = columns[column] ?? []
            height = max(height, CGFloat(nodes.count) * cardSize.height + CGFloat(max(0, nodes.count - 1)) * rowGap + 32)
            for (row, node) in nodes.enumerated() {
                result[node.node_id] = CGPoint(x: 16 + cardSize.width / 2 + CGFloat(column) * (cardSize.width + columnGap), y: 16 + cardSize.height / 2 + CGFloat(row) * (cardSize.height + rowGap))
            }
        }
        positions = result
        size = CGSize(width: CGFloat(maxRank + 1) * cardSize.width + CGFloat(maxRank) * columnGap + 32, height: height)
    }
}
