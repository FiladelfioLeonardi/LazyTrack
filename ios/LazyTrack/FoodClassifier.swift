import UIKit
import Vision

struct FoodResult {
    let label: String
    let confidence: Float
    let junk: Bool
    let isFood: Bool

    var prettyLabel: String { label.replacingOccurrences(of: "_", with: " ") }
}

/// Uses Apple's built-in on-device image classifier (no internet, no model download).
/// The photo is never saved or uploaded.
enum FoodClassifier {
    static let junkWords = [
        "pizza", "burger", "french_fries", "fries", "hotdog", "hot_dog", "donut", "doughnut", "ice_cream",
        "cake", "cookie", "candy", "chocolate", "burrito", "taco", "nacho", "fried_chicken", "chips",
        "pretzel", "waffle", "milkshake", "popcorn", "kebab", "doner", "soda", "cola", "fast_food",
        "brownie", "croissant", "churro", "onion_rings", "nugget", "bacon",
    ]
    static let foodWords = [
        "food", "dish", "meal", "salad", "vegetable", "fruit", "bread", "sandwich", "soup", "rice",
        "pasta", "meat", "fish", "egg", "snack", "dessert", "drink", "beverage", "apple", "banana",
        "broccoli", "chicken", "sushi", "noodle", "yogurt", "oatmeal", "cereal", "curry", "steak",
    ]

    static func classify(_ image: UIImage) async -> [VNClassificationObservation] {
        guard let cg = image.cgImage else { return [] }
        let orientation = CGImagePropertyOrientation(image.imageOrientation)
        return await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let request = VNClassifyImageRequest()
                let handler = VNImageRequestHandler(cgImage: cg, orientation: orientation)
                do {
                    try handler.perform([request])
                    let results = (request.results ?? []).filter { $0.confidence > 0.03 }
                    cont.resume(returning: results.sorted { $0.confidence > $1.confidence })
                } catch {
                    cont.resume(returning: [])
                }
            }
        }
    }

    static func judge(_ obs: [VNClassificationObservation]) -> FoodResult? {
        func matches(_ o: VNClassificationObservation, _ words: [String]) -> Bool {
            words.contains { o.identifier.lowercased().contains($0) }
        }
        if let j = obs.first(where: { $0.confidence > 0.15 && matches($0, junkWords) }) {
            return FoodResult(label: j.identifier, confidence: j.confidence, junk: true, isFood: true)
        }
        if let f = obs.first(where: { $0.confidence > 0.1 && matches($0, foodWords) }) {
            return FoodResult(label: f.identifier, confidence: f.confidence, junk: false, isFood: true)
        }
        return obs.first.map { FoodResult(label: $0.identifier, confidence: $0.confidence, junk: false, isFood: false) }
    }
}

extension CGImagePropertyOrientation {
    init(_ o: UIImage.Orientation) {
        switch o {
        case .up: self = .up
        case .down: self = .down
        case .left: self = .left
        case .right: self = .right
        case .upMirrored: self = .upMirrored
        case .downMirrored: self = .downMirrored
        case .leftMirrored: self = .leftMirrored
        case .rightMirrored: self = .rightMirrored
        @unknown default: self = .up
        }
    }
}
