import Foundation

final class IntegrationFixture:URLProtocol {
    static var requests:[URLRequest] = []
    static var vault:[String:[String:Any]] = ["EXISTING_KEY":["stored":true,"fingerprint":"0123456789ab","value":"never-display"]]
    static var enabled = false
    static var command = "fixture-command"
    static var channelConnected = false
    static var channelActive = true
    static var liveNames:[String] = []
    static var savedChannels:[String:Any] = ["telegram_allowed_senders":["owner-old"],"telegram_allowed_chats":["chat-old"],"opaque_future":["token":"opaque-secret","keep":true]]
    static var savedFeatures:[String:Any] = ["telegram":true,"opaque_future":"keep"]
    static var delayPath:String?,delayed = false
    static var handler:(URLRequest) -> (Int,Any) = normal
    static func normal(_ request:URLRequest) -> (Int,Any) {
        let path = request.url!.path,body = (try? JSONSerialization.jsonObject(with:request.httpBody ?? Data())) as? [String:Any] ?? [:]
        switch path {
        case "/api/integrations":return (200,["providers":[["id":"oauth","name":"Fixture OAuth","auth_type":"oauth2","connected":false,"has_client_id":true,"probe_verified":false,"token":"never-display"],["id":"token","name":"Fixture token","auth_type":"token","connected":false,"probe_verified":false],["id":"gmail","name":"Gmail","auth_type":"app_password","connected":false]]])
        case "/api/channels":return (200,["active_channels":channelActive ? ["telegram"] : [],"channel_count":channelActive ? 1 : 0,"details":channelActive ? ["telegram":["running":true,"connected":channelConnected,"access_configured":false,"allowed_sender_count":0,"bot_token":"never-display"]] : [:]])
        case "/api/channels/stop":channelActive = false;return (200,["ok":true,"stopped":true,"channel":body["type"]!,"scope":"runtime_only","saved_configuration_changed":false,"credentials_revoked":false])
        case "/api/mcp/disconnect":let name = body["name"] as! String;liveNames.removeAll { $0 == name };return (200,["success":true,"disconnected":true,"name":name,"scope":"runtime_only","saved_configuration_changed":false,"credentials_revoked":false,"remote_session_closed":"unverified"])
        case "/api/mcp/servers":return (200,["servers":[["id":"fixture","name":"Fixture MCP","command":command,"args":["--fixture","never-display"],"env":["FIXTURE_SECRET":"never-display"],"installed":true,"connected":false,"ready":true,"transport":"stdio","future_configuration":["preserve":true]]]])
        case "/api/config":return (200,["channels":savedChannels,"features":savedFeatures])
        case "/api/config/update":
            let key = body["key"] as! String
            if body["section"] as? String == "channels" { savedChannels[key] = body["value"]! } else { savedFeatures[key] = body["value"]! }
            return (200,["ok":true])
        case "/api/mcp/status":return (200,["server":["tools_exposed":0],"client":["servers_connected":liveNames.count,"server_names":liveNames,"server_states":Dictionary(uniqueKeysWithValues:liveNames.map { ($0,"connected") }),"total_tools":2,"total_resources":0,"degraded_servers":["unsafe":"opaque-secret"]]])
        case "/api/mcp/tools":return (200,["tools":[["name":"fixture.read","description":"Fixture tool","inputSchema":["properties":["path":["type":"string","default":"do-not-display"]],"required":["path"]]]]])
        case "/api/mcp/projection":if request.httpMethod == "POST" { enabled = body["enabled"] as! Bool };return (200,["enabled":enabled,"ready":false,"projected_count":0])
        case "/api/security/vault":return (200,["keys":vault])
        case "/api/security/vault/store":let name = body["key_name"] as! String;let fingerprint = NativeIntegrationWire.hash(body["value"] as! String);vault[name] = ["stored":true,"fingerprint":fingerprint];return (200,["ok":true,"key_name":name,"fingerprint":fingerprint])
        case "/api/security/vault/EXISTING_KEY":vault["EXISTING_KEY"] = nil;return (200,["ok":true])
        case "/api/integrations/token":return (200,["ok":true,"provider":body["provider_id"]!])
        case "/api/integrations/oauth/client":return (200,["ok":true,"provider":body["provider_id"]!,"applied":false])
        case "/api/oauth/authorize/oauth":return (200,["success":true,"provider":"oauth","url":"https://provider.example/consent?state=fixture-state","scopes":["read_profile"]])
        case "/api/integrations/refresh":return (200,["ok":true,"results":[body["provider_id"] as! String:false],"sweeper_running":true])
        case "/api/integrations/disconnect/oauth":return (200,["ok":true,"provider":"oauth"])
        case "/api/config/credentials":return (200,["ok":true,"keys_saved":Array(body.keys),"rejected":[],"persisted_to_vault":Array(body.keys),"persisted_to_credentials_json":false])
        case "/api/mcp/connect":let name = body["name"] as! String;if !liveNames.contains(name) { liveNames.append(name) };return (200,["success":true,"tools":2])
        default:return (404,["error":"Fixture path unavailable"])
        }
    }
    static func body(_ request:URLRequest) -> Data {
        if let data = request.httpBody { return data };guard let stream = request.httpBodyStream else { return Data() };stream.open();defer { stream.close() };var data = Data(),buffer = [UInt8](repeating:0,count:4096)
        while stream.hasBytesAvailable { let length = stream.read(&buffer,maxLength:buffer.count);if length <= 0 { break };data.append(buffer,count:length) };return data
    }
    override class func canInit(with request:URLRequest) -> Bool { true }
    override class func canonicalRequest(for request:URLRequest) -> URLRequest { request }
    override func startLoading() {
        var captured = request;if request.httpMethod == "POST" { captured.httpBody = Self.body(request) };Self.requests.append(captured)
        let (status,value) = Self.handler(captured),response = HTTPURLResponse(url:request.url!,statusCode:status,httpVersion:nil,headerFields:nil)!,data = try! JSONSerialization.data(withJSONObject:value)
        let deliver = { self.client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed);self.client?.urlProtocol(self,didLoad:data);self.client?.urlProtocolDidFinishLoading(self) }
        if Self.delayPath == request.url!.path { Self.delayPath = nil;Self.delayed = true;DispatchQueue.global().asyncAfter(deadline:.now()+0.1,execute:deliver) } else { deliver() }
    }
    override func stopLoading() {}
}
@main struct NativeIntegrationFeatureTests {
    static var count = 0
    @MainActor static func check(_ value:Bool,_ label:String) { if !value { fatalError("FAIL: \(label)") };count += 1 }
    @MainActor static func waitForDelay() async throws { for _ in 0..<1000 { if IntegrationFixture.delayed { return };try await Task.sleep(nanoseconds:1_000_000) };fatalError("Fixture delayed request not reached") }
    @MainActor static func main() async throws {
        let config = URLSessionConfiguration.ephemeral;config.protocolClasses = [IntegrationFixture.self]
        let session = URLSession(configuration:config),base = URL(string:"http://127.0.0.1:9464")!,model = NativeIntegrationModel(session:session)
        await model.configure(baseURL:nil);check(!model.available && IntegrationFixture.requests.isEmpty,"nil readiness causes no calls")
        await model.configure(baseURL:base)
        check(model.errors.isEmpty && model.providers.count == 3 && model.servers.count == 1 && model.keys.count == 1 && model.tools == ["fixture.read"],"actual eight bounded passive resources read")
        check(IntegrationFixture.requests.allSatisfy { $0.httpMethod == "GET" } && IntegrationFixture.requests.filter { $0.url!.path == "/api/integrations" }.allSatisfy { URLComponents(url:$0.url!,resolvingAgainstBaseURL:false)!.queryItems?.first?.value == "0" },"passive integration inventory explicitly suppresses probes and sweeper")
        check(model.providers[0].fields["token"] == nil && model.keys[0].fields["value"] == nil && model.channels[0].fields["bot_token"] == nil,"untrusted secret fields excluded from displayed metadata")
        check(model.toolRows.first?.fields["parameters"] as? [String] == ["path: string"] && model.toolRows.first?.fields["default"] == nil,"tool inspector exposes parameter names/types but excludes secret-bearing defaults/examples")
        check(IntegrationFixture.requests.count == 8,"passive discovery is bounded to exactly eight local requests")
        let start = IntegrationFixture.requests.count,overwrite = try model.review(.vaultStore(name:"EXISTING_KEY",value:"new-secret"))
        check(overwrite.title.contains("Overwrite") && !overwrite.detail.contains("new-secret") && IntegrationFixture.requests.count == start,"credential overwrite review inert and hides new value")
        check(await model.perform(overwrite) && model.receipt?.contains("EXISTING_KEY") == true && model.keys[0].fields["fingerprint"] as? String == NativeIntegrationWire.hash("new-secret"),"confirmed exact key storage refreshes safe fingerprint")
        let reusedCount = IntegrationFixture.requests.count
        check(!(await model.perform(overwrite)) && IntegrationFixture.requests.count == reusedCount,"completed credential approval cannot be replayed")
        let invalidNameCount = IntegrationFixture.requests.count
        do { _ = try model.review(.vaultStore(name:"folder/unsafe",value:"secret"));fatalError("path-shaped key accepted") } catch {}
        check(IntegrationFixture.requests.count == invalidNameCount,"credential name route bounds enforced before dispatch")
        let store = IntegrationFixture.requests.last { $0.url!.path.hasSuffix("vault/store") }!,storeBody = try JSONSerialization.jsonObject(with:store.httpBody!) as! [String:Any]
        check(storeBody["key_name"] as? String == "EXISTING_KEY" && storeBody["value"] as? String == "new-secret","new credential transmitted only to backend store route")
        check(await model.perform(try model.review(.vaultDelete("EXISTING_KEY"))) && model.keys.isEmpty,"explicit permanent delete exact vault entry")
        let missing = IntegrationFixture.requests.count
        do { _ = try model.review(.vaultDelete("MISSING"));fatalError("missing key deletion accepted") } catch {}
        check(IntegrationFixture.requests.count == missing,"unknown key rejected before network")
        let clientReview = try model.review(.oauthClient(provider:"oauth",clientID:"client-fixture",secret:"app-secret"))
        check(!clientReview.detail.contains("app-secret") && clientReview.detail.contains("does not authorize"),"OAuth app review hides secret and separates account authorization")
        check(await model.perform(clientReview) && model.receipt?.contains("higher-priority") == true,"saved-but-not-applied OAuth config is truthful")
        let authorize = try model.review(.authorize("oauth"))
        check(await model.perform(authorize) && model.authorizationURL?.scheme == "https" && model.authorizationScopes == ["read_profile"],"authorization only prepares safe consent URL, no browser request")
        let consentURL = model.authorizationURL!
        check(model.canOpenAuthorization(consentURL) && !model.canOpenAuthorization(URL(string:"https://another.example/consent")!),"browser consent opening requires exact prepared current HTTPS URL")
        check(IntegrationFixture.requests.allSatisfy { $0.url!.host == "127.0.0.1" },"fixtures never send authorization traffic to provider")
        check(await model.perform(try model.review(.probe("oauth"))) && model.receipt?.contains("did not verify") == true,"false probe verdict not marked connected")
        check(await model.perform(try model.review(.disconnect("oauth"))) && model.authorizationURL == nil,"disconnect exact provider clears pending consent link")
        let token = try model.review(.token(provider:"token",token:"account-secret",address:"",url:""))
        check(await model.perform(token) && model.receipt?.contains("not established") == true,"token saved is distinct from connected")
        let oauthTokenCount = IntegrationFixture.requests.count
        do { _ = try model.review(.token(provider:"oauth",token:"secret",address:"",url:""));fatalError("OAuth secret accepted as token") } catch {}
        check(IntegrationFixture.requests.count == oauthTokenCount,"OAuth client secret cannot be pasted as account token")
        let channel = try model.review(.channel(type:"telegram",token:"bot-secret"))
        check(channel.detail.contains("automatically requests") && !channel.detail.contains("bot-secret"),"channel activation review discloses background listener")
        check(await model.perform(channel) && model.receipt?.contains("startup requested") == true && NativeIntegrationWire.bool(model.channels[0].fields["connected"]) == false,"saved bot token never implies connected listener")
        let channelReq = IntegrationFixture.requests.last { $0.url!.path == "/api/config/credentials" }!,channelBody = try JSONSerialization.jsonObject(with:channelReq.httpBody!) as! [String:Any]
        check(channelBody.count == 1 && channelBody["FERAL_TELEGRAM_BOT_TOKEN"] as? String == "bot-secret" && !IntegrationFixture.requests.contains { $0.url!.path == "/api/channels/start" },"canonical credential route auto-start avoids duplicate listener request")
        let mcp = try model.review(.mcp(id:"fixture"))
        check(!mcp.detail.contains("never-display") && mcp.detail.contains("download code"),"MCP launch review hides existing values and discloses executable effects")
        check(await model.perform(mcp) && model.receipt?.contains("2 tools") == true,"MCP connection receipt actual tool count no execution claim")
        let mcpReq = IntegrationFixture.requests.last { $0.url!.path == "/api/mcp/connect" }!,mcpBody = try JSONSerialization.jsonObject(with:mcpReq.httpBody!) as! [String:Any]
        check(mcpBody["name"] as? String == "fixture" && (mcpBody["env"] as? [String:String])?["FIXTURE_SECRET"] == "never-display" && mcpBody["future_configuration"] == nil,"canonical MCP connect preserves needed env but excludes unrelated metadata")
        let changed = try model.review(.mcp(id:"fixture"));IntegrationFixture.command = "different-command";await model.refresh();let changedCount = IntegrationFixture.requests.count
        check(!(await model.perform(changed)) && IntegrationFixture.requests.count == changedCount && model.actionError?.contains("changed") == true,"changed MCP configuration invalidates consent before launch")
        check(await model.perform(try model.review(.projection(true))) && NativeIntegrationWire.bool(model.projection["enabled"]) == true && model.receipt?.contains("Ready: No") == true,"projection enabled-but-not-ready state retained")
        let normal = IntegrationFixture.normal
        let customChanges:[String:Any] = ["transport":"stdio","command":"/fixture/local-agent","args":["--directory","/fixture/reviewed-workspace"],"env":["NEW_SECRET":"new-secure-value"]]
        let customReview = try model.review(.mcpConfigured(id:"fixture",changes:customChanges,scope:"Read/write /fixture/reviewed-workspace only"))
        check(customReview.detail.contains("replaced") || customReview.detail.contains("replacement"),"edited MCP review discloses replacing existing same-ID process")
        check(!customReview.detail.contains("never-display") && !customReview.detail.contains("new-secure-value") && customReview.detail.contains("reviewed-workspace"),"explicit declared scope with all old/new credential values hidden")
        check(await model.perform(customReview) && model.receipt?.contains("not persisted") == true,"edited canonical MCP connection is live not a fake persisted config save")
        let configured = IntegrationFixture.requests.last { $0.url!.path == "/api/mcp/connect" }!,configuredBody = try JSONSerialization.jsonObject(with:configured.httpBody!) as! [String:Any]
        check(configuredBody["command"] as? String == "/fixture/local-agent" && configuredBody["args"] as? [String] == ["--directory","/fixture/reviewed-workspace"] && (configuredBody["env"] as? [String:String])?["FIXTURE_SECRET"] == "never-display" && (configuredBody["env"] as? [String:String])?["NEW_SECRET"] == "new-secure-value","edited server keeps existing credential siblings and exact argv while applying reviewed scope changes")
        let remoteReview = try model.review(.mcpConfigured(id:"new-http",changes:["transport":"http","url":"http://service.fixture/mcp?token=hidden-url-value","headers":["Authorization":"hidden-header-value"]],scope:"Fixture remote account, read-only tools"))
        check(remoteReview.detail.contains("service.fixture") && remoteReview.detail.contains("without transport encryption") && !remoteReview.detail.contains("hidden-url-value") && !remoteReview.detail.contains("hidden-header-value"),"remote scope review names host and plaintext HTTP risk without echoing credentials")
        check(await model.perform(remoteReview) && (model.mcpStatus["names"] as? [String])?.contains("new-http") == true,"new custom HTTP server routed through genuine canonical connect and live inventory")
        let activeCount = IntegrationFixture.requests.count
        do { _ = try model.review(.mcpConfigured(id:"new-http",changes:["transport":"http","url":"https://replacement.fixture/mcp"],scope:"Replacement"));fatalError("opaque active config replaced") } catch {}
        check(IntegrationFixture.requests.count == activeCount,"cannot overwrite live custom server lacking readable canonical configuration")
        let bounded = IntegrationFixture.requests.count
        do { _ = try model.review(.mcpConfigured(id:"bounded",changes:["command":"fixture","args":Array(repeating:"x",count:101)],scope:"Scope"));fatalError("unbounded args accepted") } catch {}
        do { _ = try model.review(.mcpConfigured(id:"bounded",changes:["transport":"http","url":"https://user:secret@service.fixture/mcp"],scope:"Scope"));fatalError("embedded URL credentials accepted") } catch {}
        check(IntegrationFixture.requests.count == bounded,"unbounded argv and embedded endpoint credentials rejected before transport")
        IntegrationFixture.savedFeatures["discord"] = "false";await model.refresh()
        check(!model.channelBootEnabled("discord") && model.channelBootEnabled("slack"),"saved legacy string and absent boot gates match actual backend enable resolution")
        let savedReview = try model.review(.channelSetting(type:"telegram",key:"allowed_senders",value:["owner-new"]))
        check(savedReview.detail.contains("union") && savedReview.detail.contains("no live") && !savedReview.detail.contains("opaque-secret"),"channel review explains saved-only access semantics without opaque sibling disclosure")
        check(await model.perform(savedReview) && model.channelList(type:"telegram",key:"allowed_senders") == ["owner-new"] && IntegrationFixture.savedChannels["opaque_future"] != nil && IntegrationFixture.savedChannels["telegram_allowed_chats"] as? [String] == ["chat-old"],"narrow verified allow-list write preserves unrelated channel and opaque siblings")
        let policyWire = IntegrationFixture.requests.last { $0.url!.path == "/api/config/update" }!,policyBody = try JSONSerialization.jsonObject(with:policyWire.httpBody!) as! [String:Any]
        check(policyBody.count == 3 && policyBody["section"] as? String == "channels" && policyBody["key"] as? String == "telegram_allowed_senders" && policyBody["value"] as? [String] == ["owner-new"],"actual canonical saved allow-list key only, no whole-block clobber")
        check(await model.perform(try model.review(.channelSetting(type:"telegram",key:"boot_enabled",value:false))) && IntegrationFixture.savedFeatures["telegram"] as? Bool == false && model.receipt?.contains("no live") == true,"saved startup disable explicitly does not claim stopping active listener")
        let policyStale = try model.review(.channelSetting(type:"telegram",key:"allowed_chats",value:["new-chat"]))
        IntegrationFixture.savedChannels["opaque_future"] = ["keep":true,"changed":true]
        let writesBefore = IntegrationFixture.requests.filter { $0.url!.path == "/api/config/update" }.count
        check(!(await model.perform(policyStale)) && IntegrationFixture.requests.filter { $0.url!.path == "/api/config/update" }.count == writesBefore,"backend sibling change invalidates reviewed allow-list before write")
        await model.refresh()
        IntegrationFixture.handler = { request in request.url!.path == "/api/config/update" ? (200,["ok":true]) : normal(request) }
        check(!(await model.perform(try model.review(.channelSetting(type:"telegram",key:"allowed_chats",value:["lying-change"])))) && model.actionError?.contains("readback") == true,"partial setting acknowledgement cannot fabricate saved access change")
        IntegrationFixture.handler = { request in request.url!.path == "/api/mcp/connect" ? (200,["success":true,"error":"old-or-new-secret-should-not-display"]) : normal(request) }
        check(!(await model.perform(try model.review(.mcpConfigured(id:"another-http",changes:["transport":"http","url":"https://service.fixture/mcp","headers":["Authorization":"new-secret"]],scope:"Fixture scope")))) && model.actionError?.contains("old-or-new-secret") == false,"custom MCP errors cannot echo old or new credentials")
        IntegrationFixture.handler = normal
        let staleScope = try model.review(.mcpConfigured(id:"stale-http",changes:["transport":"http","url":"https://service.fixture/mcp"],scope:"Fixture scope"))
        IntegrationFixture.liveNames.append("stale-http")
        let connectsBefore = IntegrationFixture.requests.filter { $0.url!.path == "/api/mcp/connect" }.count
        check(!(await model.perform(staleScope)) && IntegrationFixture.requests.filter { $0.url!.path == "/api/mcp/connect" }.count == connectsBefore,"new server became live after review, cannot silently replace unreadable scope")
        await model.refresh()
        IntegrationFixture.handler = { request in request.url!.path == "/api/config/credentials" ? (200,["ok":true,"keys_saved":["FERAL_TELEGRAM_BOT_TOKEN"],"rejected":["FERAL_TELEGRAM_BOT_TOKEN"],"persisted_to_vault":true]) : normal(request) }
        check(!(await model.perform(try model.review(.channel(type:"telegram",token:"rejected-secret")))) && model.receipt == nil,"rejected channel credential not accepted despite HTTP200 ok")
        IntegrationFixture.handler = { request in request.url!.path.hasSuffix("vault/store") ? (200,["error":"Bad secret sensitive-fixture-value"]) : normal(request) }
        check(!(await model.perform(try model.review(.vaultStore(name:"SAFE",value:"sensitive-fixture-value")))) && model.actionError?.contains("sensitive-fixture-value") == false && model.actionError?.contains("backend reported") == true,"error cannot echo submitted secret")
        IntegrationFixture.handler = { request in request.url!.path.hasPrefix("/api/oauth/authorize/") ? (200,["success":true,"provider":"oauth","url":"http://provider.example/consent"]) : normal(request) }
        check(!(await model.perform(try model.review(.authorize("oauth")))) && model.authorizationURL == nil,"insecure OAuth URL refused without browser launch")
        IntegrationFixture.handler = { request in request.url!.path == "/api/security/vault" ? (200,["error":"existing-secret-must-stay-private"]) : normal(request) };await model.refresh()
        check(model.errors["vault"]?.contains("existing-secret") == false,"passive backend errors cannot disclose unknown existing secrets")
        IntegrationFixture.handler = { request in request.url!.path == "/api/security/vault" ? (200,["keys":["UNSAFE":["stored":true,"fingerprint":"raw-secret-value"]]]) : normal(request) };await model.refresh()
        check(model.keys.isEmpty && model.errors["vault"] != nil,"fingerprints must match canonical twelve-hex metadata rather than arbitrary secret strings")
        IntegrationFixture.handler = normal;IntegrationFixture.vault["ROTATED"] = ["stored":true,"fingerprint":"0123456789ab"];await model.refresh()
        let rotatedDelete = try model.review(.vaultDelete("ROTATED"));IntegrationFixture.vault["ROTATED"] = ["stored":true,"fingerprint":"abcdef123456"]
        let deleteCount = IntegrationFixture.requests.filter { $0.httpMethod == "DELETE" }.count
        check(!(await model.perform(rotatedDelete)) && IntegrationFixture.requests.filter { $0.httpMethod == "DELETE" }.count == deleteCount,"vault key rotation after review detected by safe preflight before deletion")
        await model.refresh()
        IntegrationFixture.handler = { request in request.url!.path == "/api/security/vault/ROTATED" ? (200,["ok":true]) : normal(request) }
        check(!(await model.perform(try model.review(.vaultDelete("ROTATED")))) && model.actionError?.contains("still listed") == true,"lying vault deletion receipt rejected by exact safe readback")
        IntegrationFixture.handler = { request in request.url!.path.hasSuffix("vault/store") ? (200,["ok":true,"key_name":"WRONG","fingerprint":"0123456789ab"]) : normal(request) }
        check(!(await model.perform(try model.review(.vaultStore(name:"WRONG",value:"some-secret")))) && model.receipt == nil,"vault store fingerprint must match submitted secret hash not just key name")
        IntegrationFixture.handler = { request in request.url!.path == "/api/mcp/connect" ? (200,["success":true,"tools":2.5]) : normal(request) }
        check(!(await model.perform(try model.review(.mcp(id:"fixture")))) && model.receipt == nil,"fractional MCP tool count not accepted as exact connection receipt")
        IntegrationFixture.handler = { request in request.url!.path == "/api/integrations" ? (200,["providers":[["id":"same","auth_type":"token"],["id":"same","auth_type":"oauth2"]]]) : normal(request) };await model.refresh()
        check(model.providers.isEmpty && model.errors["accounts"] != nil,"duplicate account identifiers cannot select ambiguous credential target")
        IntegrationFixture.handler = { request in request.url!.path == "/api/security/vault" ? (200,["keys":["BROKEN":"raw-secret"]]) : normal(request) };await model.refresh()
        check(model.keys.isEmpty && model.errors["vault"] != nil && model.providers.count == 3,"malformed vault metadata unavailable without breaking independent account inventory")
        IntegrationFixture.handler = normal;IntegrationFixture.channelActive = true;IntegrationFixture.liveNames = ["fixture","foreign"];await model.refresh()
        let inert = IntegrationFixture.requests.count,stopReview = try model.review(.channelStop("telegram")),disconnectReview = try model.review(.mcpDisconnect("fixture"))
        check(IntegrationFixture.requests.count == inert && stopReview.detail.contains("credentials") && disconnectReview.detail.contains("not undone"),"runtime stop/disconnect reviews inert and disclose retained credentials and completed actions")
        check(await model.perform(stopReview) && model.channels.isEmpty && model.receipt?.contains("not revoked") == true,"runtime channel stop exact receipt plus absent-ID readback without revocation claim")
        let stopWire = IntegrationFixture.requests.last { $0.url!.path == "/api/channels/stop" }!,stopBody = try JSONSerialization.jsonObject(with:stopWire.httpBody!) as! [String:Any]
        check(stopBody.count == 1 && stopBody["type"] as? String == "telegram","runtime stop sends only reviewed channel type")
        check(await model.perform(disconnectReview) && IntegrationFixture.liveNames == ["foreign"] && model.receipt?.contains("unverified") == true,"exact MCP disconnect leaves foreign connections intact and remote outcome unverified")
        let disconnectWire = IntegrationFixture.requests.last { $0.url!.path == "/api/mcp/disconnect" }!,disconnectBody = try JSONSerialization.jsonObject(with:disconnectWire.httpBody!) as! [String:Any]
        check(disconnectBody.count == 1 && disconnectBody["name"] as? String == "fixture","MCP disconnect sends only reviewed server name")
        let replayCount = IntegrationFixture.requests.count
        check(!(await model.perform(stopReview)) && IntegrationFixture.requests.count == replayCount,"runtime approval cannot be replayed")
        IntegrationFixture.channelActive = true;await model.refresh()
        let staleStop = try model.review(.channelStop("telegram"));IntegrationFixture.channelConnected = true
        let stopsBefore = IntegrationFixture.requests.filter { $0.url!.path == "/api/channels/stop" }.count
        check(!(await model.perform(staleStop)) && IntegrationFixture.requests.filter { $0.url!.path == "/api/channels/stop" }.count == stopsBefore,"per-ID fresh listener status change prevents stale stop POST")
        await model.refresh()
        IntegrationFixture.handler = { request in request.url!.path == "/api/channels/stop" ? (200,["ok":true,"stopped":true,"channel":"foreign","scope":"runtime_only","saved_configuration_changed":false,"credentials_revoked":false]) : normal(request) }
        check(!(await model.perform(try model.review(.channelStop("telegram")))) && model.receipt == nil && model.actionError?.contains("uncertain") == true,"foreign channel receipt cannot establish runtime stop")
        IntegrationFixture.handler = { request in request.url!.path == "/api/channels/stop" ? (200,["ok":true,"stopped":true,"channel":"telegram","scope":"runtime_only","saved_configuration_changed":false,"credentials_revoked":false]) : normal(request) }
        check(!(await model.perform(try model.review(.channelStop("telegram")))) && model.actionError?.contains("readback") == true,"lying stop receipt rejected by active-ID readback")
        IntegrationFixture.handler = normal;IntegrationFixture.liveNames = ["fixture","foreign"];await model.refresh()
        let staleDisconnect = try model.review(.mcpDisconnect("fixture"));IntegrationFixture.liveNames.removeAll { $0 == "fixture" }
        let disconnectsBefore = IntegrationFixture.requests.filter { $0.url!.path == "/api/mcp/disconnect" }.count
        check(!(await model.perform(staleDisconnect)) && IntegrationFixture.requests.filter { $0.url!.path == "/api/mcp/disconnect" }.count == disconnectsBefore,"missing fresh MCP ID prevents stop POST")
        IntegrationFixture.liveNames = ["fixture","foreign"];await model.refresh()
        IntegrationFixture.handler = { request in request.url!.path == "/api/mcp/disconnect" ? (200,["success":true,"disconnected":true,"name":"foreign","scope":"runtime_only","saved_configuration_changed":false,"credentials_revoked":false,"remote_session_closed":"unverified"]) : normal(request) }
        check(!(await model.perform(try model.review(.mcpDisconnect("fixture")))) && model.receipt == nil,"foreign MCP receipt cannot establish reviewed disconnect")
        IntegrationFixture.handler = { request in request.url!.path == "/api/mcp/disconnect" ? (200,["success":true]) : normal(request) }
        check(!(await model.perform(try model.review(.mcpDisconnect("fixture")))) && model.receipt == nil,"generic success does not fabricate disconnect proof")
        IntegrationFixture.handler = { request in request.url!.path == "/api/mcp/disconnect" ? (200,["success":true,"disconnected":true,"name":"fixture","scope":"runtime_only","saved_configuration_changed":false,"credentials_revoked":false,"remote_session_closed":"unverified"]) : normal(request) }
        check(!(await model.perform(try model.review(.mcpDisconnect("fixture")))) && model.actionError?.contains("readback") == true,"MCP replaced/reconnected ID readback rejects fabricated absence")
        IntegrationFixture.handler = { request in request.url!.path == "/api/mcp/disconnect" ? (409,["reason":"connection_replaced","opaque":"secret-must-not-display"]) : normal(request) }
        let uncertainReview = try model.review(.mcpDisconnect("fixture"))
        check(!(await model.perform(uncertainReview)) && model.actionError?.contains("uncertain") == true && model.actionError?.contains("secret-must") == false,"concurrent replacement failure uncertain and redacted")
        let uncertainWrites = IntegrationFixture.requests.count
        check(!(await model.perform(uncertainReview)) && IntegrationFixture.requests.count == uncertainWrites,"uncertain disconnect approval never blindly retried")
        IntegrationFixture.handler = normal;await model.refresh();let stale = try model.review(.vaultStore(name:"STALE",value:"secret"));await model.configure(baseURL:base);let staleCount = IntegrationFixture.requests.count
        check(!(await model.perform(stale)) && IntegrationFixture.requests.count == staleCount,"connection generation invalidates reviewed credential action")
        IntegrationFixture.delayPath = "/api/integrations/oauth/client";IntegrationFixture.delayed = false
        let lateReview = try model.review(.oauthClient(provider:"oauth",clientID:"client-fixture",secret:"late-secret")),late = Task { await model.perform(lateReview) }
        try await waitForDelay();await model.configure(baseURL:URL(string:"http://127.0.0.1:9465")!)
        check(!(await late.value) && model.receipt == nil && model.authorizationURL == nil && !model.busy,"late old-backend mutation receipt discarded and busy reset")
        IntegrationFixture.delayPath = "/api/security/vault";IntegrationFixture.delayed = false
        let preflightReview = try model.review(.vaultStore(name:"NEW",value:"stale-secret")),preflight = Task { await model.perform(preflightReview) }
        try await waitForDelay();let mutationsBeforeDisconnect = IntegrationFixture.requests.filter { $0.httpMethod != "GET" }.count
        await model.configure(baseURL:nil)
        check(!(await preflight.value) && IntegrationFixture.requests.filter { $0.httpMethod != "GET" }.count == mutationsBeforeDisconnect && !model.busy,"disconnect during safe vault preflight prevents later mutation dispatch")
        check(!model.canOpenAuthorization(consentURL),"prepared old OAuth browser URL cannot open after agent disconnect")
        let refusedCount = IntegrationFixture.requests.count
        for unsafe in ["http://localhost:9464","https://provider.example","http://user:secret@127.0.0.1:9464"] { do { _ = try await NativeIntegrationClient(baseURL:URL(string:unsafe)!,session:session).request("/api/security/vault");fatalError("unsafe base accepted") } catch {} }
        check(IntegrationFixture.requests.count == refusedCount,"literal loopback no credentials before transport")
        let delegate = NativeIntegrationRedirectGuard(),task = session.dataTask(with:base),response = HTTPURLResponse(url:base,statusCode:307,httpVersion:nil,headerFields:nil)!
        var forwarded = true;delegate.urlSession(session,task:task,willPerformHTTPRedirection:response,newRequest:URLRequest(url:URL(string:"https://provider.example")!)) { forwarded = $0 != nil }
        check(!forwarded,"redirect cannot forward credentials to another origin")
        await model.configure(baseURL:nil);check(model.providers.isEmpty && model.servers.isEmpty && model.keys.isEmpty && model.authorizationURL == nil && model.receipt == nil,"disconnect clears account and credential metadata")
        task.cancel();session.invalidateAndCancel()
        print("PASS: \(count) native Integration fixture assertions; no real providers, OAuth browser, MCP processes, channels or Keychain calls")
    }
}
