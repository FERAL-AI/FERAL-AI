import Foundation

// No live speech/provider/account calls. URLProtocol supplies every response.
private final class SpeechSettingsWire: URLProtocol {
    static var calls: [(String, String, [String: Any])] = []
    static var config: [String: Any] = ["audio": ["realtime_providers": ["gemini_live", "openai_realtime"], "chained_fallback": ["stt_provider": "deepgram", "tts_provider": "elevenlabs"], "future_audio": true, "opaque_private": "fixture-secret-sentinel"], "voice": ["chained": ["stt_provider": "groq_whisper", "tts_provider": "elevenlabs", "opaque": ["retain": true, "private": "fixture-secret-sentinel"]]], "unrelated": "keep"]
    static var wake: [String: Any] = ["enabled": false, "supported": true, "phrase": "hey feral", "effective_phrase": "hey jarvis", "detector": "fixture"]
    static var probeOK = false, failWrite = false, hostileVerdict = false, networkFailure = false, toggleDetectorChange = false, foreignResponse = false, hook: (() -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var data = request.httpBody ?? Data()
        if let stream = request.httpBodyStream { stream.open(); defer { stream.close() }; var bytes = [UInt8](repeating: 0, count: 4096); while stream.hasBytesAvailable { let n = stream.read(&bytes, maxLength: bytes.count); if n <= 0 { break }; data.append(bytes, count: n) } }
        let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        let path = request.url!.path, method = request.httpMethod ?? "GET"; Self.calls.append((path, method, body))
        if Self.networkFailure { client?.urlProtocol(self, didFailWithError: NSError(domain: "fixture", code: 7, userInfo: [NSLocalizedDescriptionKey: "Bearer fixture-secret-sentinel"])); return }
        let result: [String: Any]
        switch path {
        case "/api/config": result = Self.config
        case "/api/voice/status": result = ["realtime_available": true, "audio_available": false, "active_realtime_sessions": 0]
        case "/api/ambient/wake_word/status": result = Self.wake
        case "/api/voice/providers": result = ["providers": [["id": "openai_realtime", "kind": "realtime", "name": "OpenAI", "configured": true, "probe_status": "ok", "models": ["fixture-model"]], ["id": "openai_tts", "kind": "tts", "name": "OpenAI TTS", "configured": false, "probe_status": Self.hostileVerdict ? "Bearer fixture-secret-sentinel" : "no_key", "probe_detail": "Authorization fixture-secret-sentinel"], ["id": "groq_whisper", "kind": "stt", "name": "Groq", "configured": false, "probe_status": "no_key"], ["id": "silero_vad", "kind": "vad", "name": "Silero VAD", "configured": true, "probe_status": "ok"]]]
        case "/api/voice/providers/probe": result = ["provider_id": body["provider_id"]!, "ok": Self.probeOK, "reason": Self.hostileVerdict ? "Bearer fixture-secret-sentinel" : (Self.probeOK ? "ok" : "unauthorized"), "detail": "Authorization fixture-secret-sentinel"]
        case "/api/config/update":
            if Self.failWrite { result = ["ok": false, "error": "fixture refusal"] }
            else { let section = body["section"] as! String; var block = Self.config[section] as? [String: Any] ?? [:]; block[body["key"] as! String] = body["value"]; Self.config[section] = block; result = ["ok": true] }
        case "/api/ambient/wake_word/toggle": Self.wake["enabled"] = !(Self.wake["enabled"] as! Bool); if Self.toggleDetectorChange { Self.wake["detector"] = "changed-during-toggle" }; result = ["enabled": Self.wake["enabled"]!]
        default: result = ["error": "unexpected request"]
        }
        Self.hook?(); Self.hook = nil
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: Self.foreignResponse ? URL(string: "http://127.0.0.1:29998/foreign")! : request.url!, statusCode: 200, httpVersion: nil, headerFields: [:])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: result)); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
@main private struct NativeVoiceConfigurationFeatureTests {
    @MainActor static func main() async throws {
        var count = 0
        func check(_ value: Bool, _ text: String) { precondition(value, text); count += 1 }
        func refuses(_ action: () throws -> Void) { do { try action(); preconditionFailure("expected review refusal") } catch { count += 1 } }
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [SpeechSettingsWire.self]; let session = URLSession(configuration: config)
        let model = NativeVoiceConfigurationModel(baseURL: URL(string: "http://127.0.0.1:19998")!, session: session)
        await model.refresh()
        check(SpeechSettingsWire.calls.count == 3 && SpeechSettingsWire.calls.allSatisfy { $0.1 == "GET" && !$0.0.contains("providers") }, "passive saved refresh cannot probe")
        check(model.realtimeOrder == ["gemini_live", "openai_realtime"], "saved chain order retained")
        check(model.effectiveChained("stt_provider") == "groq_whisper" && model.effectiveChained("tts_provider") == "elevenlabs", "voice overrides headless fallback")
        check(model.wake?["effective_phrase"] as? String == "hey jarvis" && model.wake?["phrase"] as? String == "hey feral", "configured and effective phrase stay distinct")
        let order = try model.audioReview(key: "realtime_providers", value: ["openai_realtime", "gemini_live"])
        check(order.scope.contains("not a guarantee") && order.proposed.contains("openai_realtime"), "review discloses surface policy")
        check(await model.perform(order), "exact reordered fallback save")
        check(model.audio["future_audio"] as? Bool == true && SpeechSettingsWire.config["unrelated"] as? String == "keep", "audio key write preserves sibling and unrelated blocks")
        let replayCalls = SpeechSettingsWire.calls.count
        check(await model.perform(order) == false, "review single use")
        check(SpeechSettingsWire.calls.count == replayCalls, "replay cannot reach network")
        refuses { _ = try model.audioReview(key: "realtime_providers", value: ["openai_realtime", "openai_realtime"]) }
        refuses { _ = try model.audioReview(key: "realtime_primary", value: "typo") }
        refuses { _ = try model.audioReview(key: "credentials", value: "unsafe") }
        let chained = try model.chainedReview(key: "tts_provider", value: "openai_tts")
        check(!chained.previous.contains("fixture-secret-sentinel") && !chained.proposed.contains("fixture-secret-sentinel") && !chained.previous.contains("opaque_private") && !chained.proposed.contains("opaque"), "old/new disclosure excludes opaque private blocks")
        check(chained.proposed.contains("tts_provider") && chained.previous.contains("groq_whisper"), "known routing keys remain reviewable")
        check(await model.perform(chained), "catalogue OpenAI TTS alias saved")
        check(model.chained["tts_provider"] as? String == "openai" && model.chained["stt_provider"] as? String == "groq_whisper" && (model.chained["opaque"] as? [String: Any])?["retain"] as? Bool == true, "chained merge preserves other engine and opaque data")
        let voice = try model.chainedReview(key: "tts_voice_id", value: "provider-specific-id")
        check(await model.perform(voice), "real voice ID schema write")
        let sttModel = try model.chainedReview(key: "stt_model", value: "chosen-stt-model")
        check(await model.perform(sttModel), "real STT model schema write")
        let catalogue = try model.catalogueReview()
        check(catalogue.scope.contains("every configured speech provider"), "catalogue GET probing disclosed")
        check(await model.perform(catalogue), "explicit catalogue request")
        check(model.providers?.count == 4 && model.providers?.first?.models == ["fixture-model"], "backend kinds/models/status preserved")
        check(model.providers?.first(where: { $0.id == "openai_tts" })?.detail.isEmpty == true, "catalogue probe detail is never displayable")
        let probe = try model.probeReview("openai_tts")
        check(await model.perform(probe), "negative probe is a valid completed test")
        check(model.receipt?.contains("failed") == true && model.providers?.first(where: { $0.id == "openai_tts" })?.configured == false, "unauthorized not reported available")
        let probeCall = SpeechSettingsWire.calls.last!
        check(probeCall.0 == "/api/voice/providers/probe" && probeCall.2.count == 1 && probeCall.2["provider_id"] as? String == "openai_tts", "single-provider forced probe only")
        SpeechSettingsWire.hostileVerdict = true
        let hostileCatalogue = try model.catalogueReview(); _ = await model.perform(hostileCatalogue)
        check(model.providers?.first(where: { $0.id == "openai_tts" })?.verdict.contains("fixture-secret-sentinel") == false && model.providers?.first(where: { $0.id == "openai_tts" })?.detail.isEmpty == true, "unknown catalogue verdict and raw detail cannot leak secrets")
        check(model.providers?.first(where: { $0.id == "openai_tts" })?.raw["probe_detail"] == nil && model.providers?.first(where: { $0.id == "openai_tts" })?.raw["probe_status"] as? String == "unrecognized verdict (details withheld)", "published provider metadata excludes unsafe fields")
        let hostileProbe = try model.probeReview("openai_tts"); _ = await model.perform(hostileProbe)
        check(model.receipt?.contains("fixture-secret-sentinel") == false && model.receipt?.contains("unrecognized verdict") == true, "unknown forced probe reason is safely rendered")
        SpeechSettingsWire.hostileVerdict = false
        SpeechSettingsWire.networkFailure = true; await model.refresh()
        check(model.errors.values.allSatisfy { !$0.contains("fixture-secret-sentinel") }, "raw exception descriptions cannot reach status errors")
        SpeechSettingsWire.networkFailure = false; await model.refresh()
        let stale = try model.chainedReview(key: "tts_model", value: "reviewed")
        var changed = SpeechSettingsWire.config["audio"] as! [String: Any]; changed["future_audio"] = false; SpeechSettingsWire.config["audio"] = changed
        let writes = SpeechSettingsWire.calls.filter { $0.0 == "/api/config/update" }.count
        check(await model.perform(stale) == false, "changed saved snapshot blocks mutation")
        check(SpeechSettingsWire.calls.filter { $0.0 == "/api/config/update" }.count == writes, "stale preflight emits no config write")
        await model.refresh()
        let wake = try model.wakeReview(true)
        check(wake.scope.contains("not an atomic setter"), "toggle contract disclosed")
        check(await model.perform(wake), "preflight/toggle/readback verifies desired wake state")
        check(SpeechSettingsWire.calls.first(where: { $0.0.hasSuffix("wake_word/toggle") })?.2.isEmpty == true, "no misleading enabled setter body")
        let wakeCalls = SpeechSettingsWire.calls.count
        check(await model.perform(wake) == false, "wake review single-use after confirmed toggle")
        check(SpeechSettingsWire.calls.count == wakeCalls, "single-use wake refusal cannot toggle again")
        let staleWake = try model.wakeReview(false); SpeechSettingsWire.wake["detector"] = "changed"
        let toggleCount = SpeechSettingsWire.calls.filter { $0.0.hasSuffix("wake_word/toggle") }.count
        check(await model.perform(staleWake) == false, "stale detector review blocked")
        check(SpeechSettingsWire.calls.filter { $0.0.hasSuffix("wake_word/toggle") }.count == toggleCount, "no stale toggle")
        await model.refresh()
        let concurrentWake = try model.wakeReview(false); SpeechSettingsWire.toggleDetectorChange = true
        let concurrentWakeResult = await model.perform(concurrentWake)
        check(!concurrentWakeResult && model.receipt == nil && model.errors["wake"] != nil && model.wake?["enabled"] as? Bool == false, "changed detector during toggle retains actual state but refuses success")
        SpeechSettingsWire.toggleDetectorChange = false; await model.refresh()
        let failed = try model.audioReview(key: "tts_voice", value: "fixture-voice"); SpeechSettingsWire.failWrite = true
        check(await model.perform(failed) == false && model.receipt == nil && model.errors["config"] != nil, "write refusal marks state unconfirmed")
        SpeechSettingsWire.failWrite = false; await model.refresh()
        let disconnected = try model.audioReview(key: "realtime_model", value: "fixture"); model.configure(nil)
        let before = SpeechSettingsWire.calls.count
        check(await model.perform(disconnected) == false, "changed runtime generation rejects review")
        check(SpeechSettingsWire.calls.count == before, "disconnected review sends nothing")
        let remote = NativeVoiceConfigurationModel(baseURL: URL(string: "http://remote.invalid")!, session: session); await remote.refresh()
        check(SpeechSettingsWire.calls.count == before, "foreign endpoint refused")
        let guardDelegate = NativeVoiceConfigurationRedirectGuard(); let guardedSession = NativeVoiceConfigurationRedirectGuard.session(); let req = URLRequest(url: URL(string: "http://127.0.0.1:19998/api/config/update")!); let task = guardedSession.dataTask(with: req); var accepted = true
        guardDelegate.urlSession(guardedSession, task: task, willPerformHTTPRedirection: HTTPURLResponse(url: req.url!, statusCode: 307, httpVersion: nil, headerFields: [:])!, newRequest: URLRequest(url: URL(string: "https://remote.invalid")!)) { accepted = $0 != nil }
        check(!accepted, "redirect cannot replay saved settings off origin"); task.cancel(); guardedSession.invalidateAndCancel()
        SpeechSettingsWire.foreignResponse = true
        let foreignReply = NativeVoiceConfigurationModel(baseURL: URL(string: "http://127.0.0.1:19998")!, session: session)
        await foreignReply.refresh()
        check(foreignReply.config == nil && foreignReply.status == nil && foreignReply.wake == nil && foreignReply.errors.count == 3, "responses from another origin cannot establish voice settings or readiness")
        SpeechSettingsWire.foreignResponse = false
        print("NativeVoiceConfigurationFeatureTests PASS \(count) assertions; no live probes, mic, providers or accounts")
    }
}
