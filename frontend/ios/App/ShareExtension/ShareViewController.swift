import UIKit
import Social
import UniformTypeIdentifiers

/// Receives image(s) from the iOS share sheet, copies each into the
/// shared App Group container, records their file URLs in the App
/// Group's UserDefaults (the format `SendIntentPlugin` reads in the
/// host app's AppDelegate), then re-opens the host app via the
/// `betterclaude://` scheme.
///
/// App Group `group.com.betterclaude.app` must be enabled on BOTH this
/// extension target and the main app target.
class ShareViewController: UIViewController {
    private let appGroup = "group.com.betterclaude.app"
    private let urlScheme = "betterclaude"

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        guard let items = extensionContext?.inputItems as? [NSExtensionItem] else {
            return finish()
        }

        var shareItems: [[String: String]] = []
        let group = DispatchGroup()
        let imageType = UTType.image.identifier

        for item in items {
            for attachment in item.attachments ?? [] {
                guard attachment.hasItemConformingToTypeIdentifier(imageType) else { continue }
                group.enter()
                attachment.loadItem(forTypeIdentifier: imageType, options: nil) { data, _ in
                    defer { group.leave() }
                    let saved = self.persist(data)
                    if let url = saved {
                        shareItems.append([
                            "title": url.lastPathComponent,
                            "description": "",
                            "type": "image/" + (url.pathExtension.isEmpty ? "png" : url.pathExtension),
                            "url": url.absoluteString,
                        ])
                    }
                }
            }
        }

        group.notify(queue: .main) {
            let prefs = UserDefaults(suiteName: self.appGroup)
            prefs?.set(shareItems, forKey: "shareItems")
            prefs?.synchronize()
            self.redirectToHost()
        }
    }

    /// Copy/write the shared payload into the App Group container.
    private func persist(_ data: NSSecureCoding?) -> URL? {
        guard let dir = FileManager.default
            .containerURL(forSecurityApplicationGroupIdentifier: appGroup) else { return nil }

        if let src = data as? URL {
            let ext = src.pathExtension.isEmpty ? "png" : src.pathExtension
            let dest = dir.appendingPathComponent(UUID().uuidString + "." + ext)
            try? FileManager.default.copyItem(at: src, to: dest)
            return dest
        }
        if let image = data as? UIImage, let png = image.pngData() {
            return write(png, ext: "png", in: dir)
        }
        if let raw = data as? Data {
            return write(raw, ext: "png", in: dir)
        }
        return nil
    }

    private func write(_ data: Data, ext: String, in dir: URL) -> URL? {
        let dest = dir.appendingPathComponent(UUID().uuidString + "." + ext)
        try? data.write(to: dest)
        return dest
    }

    private func finish() {
        extensionContext?.completeRequest(returningItems: [], completionHandler: nil)
    }

    /// Open the host app from within the extension via the custom scheme.
    private func redirectToHost() {
        guard let url = URL(string: urlScheme + "://shared") else { return finish() }
        var responder: UIResponder? = self
        let selector = sel_registerName("openURL:")
        while let r = responder {
            if r.responds(to: selector) && !(r is ShareViewController) {
                r.perform(selector, with: url)
                break
            }
            responder = r.next
        }
        finish()
    }
}
