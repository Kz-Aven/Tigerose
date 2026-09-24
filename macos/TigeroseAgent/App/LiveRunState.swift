import Foundation

/// Per-channel live reply / thinking state (keyed by `assistant:{id}` / `group:{id}`).
struct LiveRunState: Equatable {
    var running = false
    var thinking = false
    var steps: [ThinkingStep] = []
    var durationMs = 0
    var runIds: [String] = []

    var showsTypingChrome: Bool { running || thinking }
}
