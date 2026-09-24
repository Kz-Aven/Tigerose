import SwiftUI

struct AskUserQuestionCard: View {
    let request: PendingQuestion
    var onSubmit: ([AskUserQuestionAnswer]) async throws -> Void
    var onCancel: () async throws -> Void

    @State private var selections: [String: Set<String>] = [:]
    @State private var otherText: [String: String] = [:]
    @State private var isWorking = false
    @State private var error = ""
    @FocusState private var focusedQuestionId: String?

    private var isComplete: Bool {
        request.questions.allSatisfy { question in
            !(selections[question.id] ?? []).isEmpty
                || !(otherText[question.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceSm) {
            HStack(spacing: TigeroseTheme.spaceXs) {
                Image(systemName: "questionmark.bubble.fill")
                    .foregroundStyle(TigeroseTheme.primary)
                Text("需要你的回答")
                    .font(TigeroseTheme.titleSm)
                    .foregroundStyle(TigeroseTheme.ink)
                Spacer()
                Text("\(request.questions.count) 个问题")
                    .font(TigeroseTheme.caption)
                    .foregroundStyle(TigeroseTheme.muted)
            }

            ScrollView {
                VStack(alignment: .leading, spacing: TigeroseTheme.spaceMd) {
                    ForEach(request.questions) { question in
                        questionView(question)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 320)

            if !error.isEmpty {
                Text(error)
                    .font(TigeroseTheme.bodySm)
                    .foregroundStyle(TigeroseTheme.error)
                    .accessibilityLabel("错误：\(error)")
            }

            HStack(spacing: TigeroseTheme.spaceXs) {
                Button("取消") {
                    Task { await cancel() }
                }
                .buttonStyle(TigeroseSecondaryButtonStyle())
                .disabled(isWorking)

                Spacer()

                if isWorking {
                    ProgressView()
                        .controlSize(.small)
                }
                Button("提交回答") {
                    Task { await submit() }
                }
                .buttonStyle(TigerosePrimaryButtonStyle(disabled: !isComplete || isWorking))
                .disabled(!isComplete || isWorking)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(TigeroseTheme.spaceMd)
        .background(TigeroseTheme.surfaceSoft)
        .tigeroseHairline(radius: TigeroseTheme.radiusLg)
        .padding(.horizontal, TigeroseTheme.spaceMd)
        .padding(.vertical, TigeroseTheme.spaceXs)
    }

    @ViewBuilder
    private func questionView(_ question: AskUserQuestion) -> some View {
        VStack(alignment: .leading, spacing: TigeroseTheme.spaceXs) {
            Text(question.header)
                .font(TigeroseTheme.caption)
                .foregroundStyle(TigeroseTheme.primary)
            Text(question.question)
                .font(TigeroseTheme.bodyMd)
                .foregroundStyle(TigeroseTheme.ink)
                .fixedSize(horizontal: false, vertical: true)

            ForEach(question.options, id: \.label) { option in
                optionButton(option, for: question)
            }

            HStack(alignment: .center, spacing: TigeroseTheme.spaceXs) {
                Button {
                    let current = (otherText[question.id] ?? "")
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    if current.isEmpty {
                        focusedQuestionId = question.id
                    } else {
                        otherText[question.id] = ""
                    }
                } label: {
                    Image(systemName: otherSymbol(for: question))
                        .foregroundStyle(hasOtherText(question) ? TigeroseTheme.primary : TigeroseTheme.muted)
                        .frame(width: 18)
                }
                .buttonStyle(.plain)
                .help("填写其他答案")

                TextField("其他（请填写）", text: otherBinding(for: question))
                    .textFieldStyle(.roundedBorder)
                    .focused($focusedQuestionId, equals: question.id)
                    .accessibilityLabel("\(question.header)的其他答案")
            }
        }
        .padding(TigeroseTheme.spaceSm)
        .background(TigeroseTheme.canvas, in: RoundedRectangle(cornerRadius: TigeroseTheme.radiusMd))
        .tigeroseHairline(radius: TigeroseTheme.radiusMd)
    }

    private func optionButton(
        _ option: AskUserQuestionOption,
        for question: AskUserQuestion
    ) -> some View {
        let selected = selections[question.id]?.contains(option.label) ?? false
        return Button {
            select(option.label, for: question)
        } label: {
            HStack(alignment: .top, spacing: TigeroseTheme.spaceXs) {
                Image(systemName: selectionSymbol(selected: selected, multiSelect: question.multi_select))
                    .foregroundStyle(selected ? TigeroseTheme.primary : TigeroseTheme.muted)
                    .frame(width: 18)
                VStack(alignment: .leading, spacing: 2) {
                    Text(option.label)
                        .font(TigeroseTheme.bodySm)
                        .foregroundStyle(TigeroseTheme.ink)
                    if !option.description.isEmpty {
                        Text(option.description)
                            .font(TigeroseTheme.caption)
                            .foregroundStyle(TigeroseTheme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(option.label)，\(option.description)")
        .accessibilityValue(selected ? "已选择" : "未选择")
    }

    private func select(_ label: String, for question: AskUserQuestion) {
        if question.multi_select {
            var current = selections[question.id] ?? []
            if current.contains(label) {
                current.remove(label)
            } else {
                current.insert(label)
            }
            selections[question.id] = current
        } else {
            selections[question.id] = [label]
            otherText[question.id] = ""
        }
        error = ""
    }

    private func otherBinding(for question: AskUserQuestion) -> Binding<String> {
        Binding(
            get: { otherText[question.id] ?? "" },
            set: { value in
                otherText[question.id] = value
                if !question.multi_select,
                   !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                {
                    selections[question.id] = []
                }
                error = ""
            }
        )
    }

    private func hasOtherText(_ question: AskUserQuestion) -> Bool {
        !(otherText[question.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func otherSymbol(for question: AskUserQuestion) -> String {
        selectionSymbol(selected: hasOtherText(question), multiSelect: question.multi_select)
    }

    private func selectionSymbol(selected: Bool, multiSelect: Bool) -> String {
        if multiSelect {
            return selected ? "checkmark.square.fill" : "square"
        }
        return selected ? "largecircle.fill.circle" : "circle"
    }

    @MainActor
    private func submit() async {
        guard isComplete else {
            error = "请回答每一个问题后再提交。"
            return
        }
        isWorking = true
        error = ""
        defer { isWorking = false }
        let answers = request.questions.map { question in
            AskUserQuestionAnswer(
                question_id: question.id,
                selected_labels: Array(selections[question.id] ?? []).sorted(),
                other_text: (otherText[question.id] ?? "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
            )
        }
        do {
            try await onSubmit(answers)
        } catch {
            self.error = error.localizedDescription
        }
    }

    @MainActor
    private func cancel() async {
        isWorking = true
        error = ""
        defer { isWorking = false }
        do {
            try await onCancel()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
