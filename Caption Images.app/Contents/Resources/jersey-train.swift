// jersey-train: teaches a jersey-number model from the jerseys you labelled in
// "Teach numbers". Runs on this Mac with Apple's Create ML; nothing is sent anywhere.
//
// Usage: jersey-train <labels dir> <model dir>
//   <labels dir> has one folder per number ("23", "none"...) of jersey crops.
//   Writes <model dir>/numbers.mlmodelc (replacing the old one only when training worked)
//   and prints one JSON line: {"ok":true, "numbers":{"23":12,...}, "checked":40, "right":0.9, "seconds":12}
import CreateML
import CoreML
import Foundation

func finish(_ o: [String: Any]) -> Never {
    let d = try! JSONSerialization.data(withJSONObject: o)
    print(String(data: d, encoding: .utf8)!)
    exit(o["ok"] as? Bool == true ? 0 : 1)
}

let args = CommandLine.arguments
guard args.count >= 3 else { finish(["ok": false, "error": "usage: jersey-train <labels dir> <model dir>"]) }
let labelsDir = URL(fileURLWithPath: args[1]), modelDir = URL(fileURLWithPath: args[2])
let fm = FileManager.default
let minPer = 3

// one list of crops per number; numbers with too few examples wait until there are more
var byLabel: [String: [URL]] = [:]
for label in (try? fm.contentsOfDirectory(atPath: labelsDir.path)) ?? [] where !label.hasPrefix(".") {
    let dir = labelsDir.appendingPathComponent(label)
    let files = ((try? fm.contentsOfDirectory(atPath: dir.path)) ?? [])
        .filter { $0.lowercased().hasSuffix(".jpg") }.map { dir.appendingPathComponent($0) }
    if files.count >= minPer { byLabel[label] = files }
}
guard byLabel.count >= 2 else { finish(["ok": false, "error": "Label at least \(minPer) jerseys each for two or more numbers first."]) }

// hold back a fifth of each number to check the model on jerseys it hasn't seen. Crops are named
// by photo, so a block in the middle of the sorted list is mostly other moments, not the same burst.
var train: [String: [URL]] = [:], check: [String: [URL]] = [:]
for (label, files) in byLabel {
    let sorted = files.sorted { $0.lastPathComponent < $1.lastPathComponent }
    let lo = Int(Double(sorted.count) * 0.4), hi = Int(Double(sorted.count) * 0.6)
    for (i, f) in sorted.enumerated() {
        if sorted.count >= 5 && i >= lo && i < max(hi, lo + 1) { check[label, default: []].append(f) } else { train[label, default: []].append(f) }
    }
}

let start = Date()
var params = MLImageClassifier.ModelParameters(validation: .none, maxIterations: 40,
                                               augmentation: [.crop, .blur, .exposure, .rotation])
params.algorithm = .transferLearning(featureExtractor: .scenePrint(revision: 2), classifier: .logisticRegressor)
do {
    let checked = check.values.reduce(0) { $0 + $1.count }
    var right: Double? = nil
    if checked > 0 {
        let held = try MLImageClassifier(trainingData: .filesByLabel(train), parameters: params)
        let m = held.evaluation(on: .filesByLabel(check))
        right = 1 - m.classificationError
    }
    // the model you use learns from everything
    let model = try MLImageClassifier(trainingData: .filesByLabel(byLabel), parameters: params)
    try fm.createDirectory(at: modelDir, withIntermediateDirectories: true)
    let raw = modelDir.appendingPathComponent("numbers-new.mlmodel")
    try? fm.removeItem(at: raw)
    try model.write(to: raw, metadata: MLModelMetadata(author: "Caption Images", shortDescription: "Jersey numbers", version: "1"))
    let compiled = try MLModel.compileModel(at: raw)
    try? fm.removeItem(at: raw)
    let dest = modelDir.appendingPathComponent("numbers.mlmodelc")
    if fm.fileExists(atPath: dest.path) { _ = try fm.replaceItemAt(dest, withItemAt: compiled) } else { try fm.moveItem(at: compiled, to: dest) }
    var out: [String: Any] = ["ok": true, "numbers": byLabel.mapValues { $0.count }, "checked": checked,
                              "seconds": Int(Date().timeIntervalSince(start))]
    if let r = right { out["right"] = (r * 1000).rounded() / 1000 }
    finish(out)
} catch {
    finish(["ok": false, "error": error.localizedDescription])
}
