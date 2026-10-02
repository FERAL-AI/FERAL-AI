import SwiftUI
import Foundation
import AppKit
import CoreFoundation
import CryptoKit

struct NativeIntegrationError: LocalizedError {
    let message:String
    init(_ message:String) { self.message = message }
    var errorDescription:String? { message }
}
enum NativeIntegrationWire {
    static func bool(_ value:Any?) -> Bool? { guard let n = value as? NSNumber,CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil };return n.boolValue }
    static func identifier(_ value:String) -> Bool { !value.isEmpty && value.count <= 200 && value.unicodeScalars.allSatisfy { CharacterSet(charactersIn:"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:").contains($0) } }
    static func fingerprint(_ value:Any?) -> String? { guard let text = value as? String,text.count == 12,text.unicodeScalars.allSatisfy({ CharacterSet(charactersIn:"0123456789abcdef").contains($0) }) else { return nil };return text }
    static func hash(_ value:String) -> String { SHA256.hash(data:Data(value.utf8)).map { String(format:"%02x",$0) }.joined().prefix(12).description }
    static func segment(_ text:String) -> String { text.addingPercentEncoding(withAllowedCharacters:CharacterSet(charactersIn:"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? "" }
    static func safe(_ raw:[String:Any],fields:[String]) -> [String:Any] { raw.filter { fields.contains($0.key) } }
    static func redact(_ text:String,secrets:[String]) -> String { secrets.filter { !$0.isEmpty }.reduce(text) { $0.replacingOccurrences(of:$1,with:"[redacted]") } }
}
final class NativeIntegrationRedirectGuard:NSObject,URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping(URLRequest?) -> Void) { completionHandler(nil) }
    static func session() -> URLSession { let config = URLSessionConfiguration.ephemeral;config.timeoutIntervalForResource = 180;return URLSession(configuration:config,delegate:NativeIntegrationRedirectGuard(),delegateQueue:nil) }
}
struct NativeIntegrationClient {
    let baseURL:URL
    let session:URLSession
    func request(_ path:String,method:String = "GET",body:[String:Any]? = nil,query:[URLQueryItem] = [],secrets:[String] = []) async throws -> [String:Any] {
        guard baseURL.scheme == "http",["127.0.0.1","::1","[::1]"].contains(baseURL.host ?? ""),baseURL.user == nil,baseURL.password == nil,var parts = URLComponents(url:baseURL,resolvingAgainstBaseURL:false) else { throw NativeIntegrationError("Integrations require the app’s local service.") }
        parts.percentEncodedPath = path;parts.queryItems = query.isEmpty ? nil : query;parts.fragment = nil
        guard let url = parts.url else { throw NativeIntegrationError("Invalid integration request.") }
        var request = URLRequest(url:url);request.httpMethod = method
        if let body = body { request.httpBody = try JSONSerialization.data(withJSONObject:body);request.setValue("application/json",forHTTPHeaderField:"Content-Type") }
        let data:Data,response:URLResponse
        do { (data,response) = try await session.data(for:request) } catch { throw NativeIntegrationError("The local integration request could not finish. Check the service and retry.") }
        guard let http = response as? HTTPURLResponse,(200..<300).contains(http.statusCode) else { throw NativeIntegrationError("Integration request failed (\((response as? HTTPURLResponse)?.statusCode ?? 0)). No successful change is established.") }
        guard let value = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] else { throw NativeIntegrationError("Unreadable integration response.") }
        if let error = value["error"] as? String,!error.isEmpty { throw NativeIntegrationError("The backend reported an integration failure. No successful change is established.") }
        if NativeIntegrationWire.bool(value["ok"]) == false || NativeIntegrationWire.bool(value["success"]) == false { throw NativeIntegrationError("The service refused this change.") }
        return value
    }
}
struct NativeIntegrationRow:Identifiable {
    let id:String
    let fields:[String:Any]
    var title:String { fields["name"] as? String ?? id }
}
enum NativeIntegrationAction {
    case vaultStore(name:String,value:String),vaultDelete(String)
    case token(provider:String,token:String,address:String,url:String)
    case oauthClient(provider:String,clientID:String,secret:String),authorize(String),probe(String),disconnect(String)
    case channel(type:String,token:String)
    case mcp(id:String),mcpConfigured(id:String,changes:[String:Any],scope:String),projection(Bool)
    case channelSetting(type:String,key:String,value:Any)
    case channelStop(String),mcpDisconnect(String)
}
struct NativeIntegrationReview:Identifiable {
    let id = UUID()
    let generation:UUID
    let action:NativeIntegrationAction
    let title:String
    let detail:String
    let configuration:[String:Any]?
}
@MainActor final class NativeIntegrationModel:ObservableObject {
    @Published private(set) var providers:[NativeIntegrationRow] = []
    @Published private(set) var servers:[NativeIntegrationRow] = []
    @Published private(set) var channels:[NativeIntegrationRow] = []
    @Published private(set) var keys:[NativeIntegrationRow] = []
    @Published private(set) var tools:[String] = []
    @Published private(set) var toolRows:[NativeIntegrationRow] = []
    @Published private(set) var mcpStatus:[String:Any] = [:]
    @Published private(set) var savedChannels:[String:Any] = [:]
    @Published private(set) var savedFeatures:[String:Any] = [:]
    @Published private(set) var projection:[String:Any] = [:]
    @Published private(set) var errors:[String:String] = [:]
    @Published private(set) var loading = false
    @Published private(set) var busy = false
    @Published private(set) var receipt:String?
    @Published private(set) var actionError:String?
    @Published private(set) var authorizationURL:URL?
    @Published private(set) var authorizationProvider:String?
    @Published private(set) var authorizationScopes:[String] = []
    private var generation = UUID(),reading = UUID(),operation = UUID()
    private var client:NativeIntegrationClient?
    private let session:URLSession
    private var configurations:[String:[String:Any]] = [:]
    private var issuedReviews = Set<UUID>()
    init(session:URLSession? = nil) { self.session = session ?? NativeIntegrationRedirectGuard.session() }
    var available:Bool { client != nil }
    func configure(baseURL:URL?) async {
        generation = UUID();reading = UUID();operation = UUID();issuedReviews = [];client = baseURL.map { NativeIntegrationClient(baseURL:$0,session:session) }
        providers = [];servers = [];channels = [];keys = [];tools = [];toolRows = [];mcpStatus = [:];savedChannels = [:];savedFeatures = [:];projection = [:];configurations = [:];errors = [:];receipt = nil;actionError = nil;authorizationURL = nil;authorizationProvider = nil;authorizationScopes = [];loading = false;busy = false
        if available { await refresh() }
    }
    func refresh() async {
        guard let client = client else { return };reading = UUID();let read = reading,connection = generation;loading = true
        defer { if connection == generation && read == reading { loading = false } }
        let paths = ["accounts":"/api/integrations","channels":"/api/channels","mcp":"/api/mcp/servers","tools":"/api/mcp/tools","projection":"/api/mcp/projection","vault":"/api/security/vault","status":"/api/mcp/status","config":"/api/config"]
        for name in paths.keys.sorted() {
            guard connection == generation && read == reading else { return }
            do {
                let value = try await client.request(paths[name]!,query:name == "accounts" ? [URLQueryItem(name:"refresh",value:"0")] : [])
                guard connection == generation && read == reading else { return }
                try apply(name,value);errors[name] = nil
            } catch { if connection == generation && read == reading { clear(name);errors[name] = error.localizedDescription } }
        }
    }
    private func clear(_ name:String) { switch name { case "status":mcpStatus = [:];case "config":savedChannels = [:];savedFeatures = [:];case "accounts":providers = [];case "channels":channels = [];case "mcp":servers = [];configurations = [:];case "tools":tools = [];toolRows = [];case "projection":projection = [:];case "vault":keys = [];default:break } }
    private func apply(_ name:String,_ value:[String:Any]) throws {
        switch name {
        case "config":
            guard value["channels"] == nil || value["channels"] is [String:Any],value["features"] == nil || value["features"] is [String:Any] else { throw NativeIntegrationError("Saved channel settings are unreadable. No fields can safely be changed.") }
            savedChannels = value["channels"] as? [String:Any] ?? [:];savedFeatures = value["features"] as? [String:Any] ?? [:]
        case "status":
            guard let client = value["client"] as? [String:Any],let server = value["server"] as? [String:Any],let names = client["server_names"] as? [String],names.count <= 500,Set(names).count == names.count,let states = client["server_states"] as? [String:String],names.allSatisfy({ NativeIntegrationWire.identifier($0) }) else { throw NativeIntegrationError("Live MCP status is unreadable.") }
            mcpStatus = ["client":NativeIntegrationWire.safe(client,fields:["servers_connected","total_tools","total_resources","server_names","server_states"]),"server":NativeIntegrationWire.safe(server,fields:["tools_exposed"])];mcpStatus["names"] = names;mcpStatus["states"] = states.filter { ["connected","degraded"].contains($0.value) }
        case "accounts":
            guard let rows = value["providers"] as? [[String:Any]],rows.allSatisfy({ NativeIntegrationWire.identifier($0["id"] as? String ?? "") }),Set(rows.compactMap { $0["id"] as? String }).count == rows.count else { throw NativeIntegrationError("Unreadable account inventory.") }
            providers = rows.map { NativeIntegrationRow(id:$0["id"] as! String,fields:NativeIntegrationWire.safe($0,fields:["name","auth_type","connected","has_client_id","probe_verified","probe_age_s","setup_status"])) }
        case "channels":
            let rows:[String:Any]
            if let details = value["details"] as? [String:Any] { rows = details }
            else if let empty = value["channels"] as? [Any],empty.isEmpty { rows = [:] }
            else { throw NativeIntegrationError("Unreadable channel inventory.") }
            channels = try rows.keys.sorted().map { id in guard let row = rows[id] as? [String:Any] else { throw NativeIntegrationError("Unreadable channel status.") };return NativeIntegrationRow(id:id,fields:NativeIntegrationWire.safe(row,fields:["connected","running","degraded","failure_count","access_configured","allowed_sender_count","allowed_chat_count","pairing_window_open","known_chats"])) }
        case "mcp":
            guard let rows = value["servers"] as? [[String:Any]],rows.allSatisfy({ NativeIntegrationWire.identifier($0["id"] as? String ?? "") }),Set(rows.compactMap { $0["id"] as? String }).count == rows.count else { throw NativeIntegrationError("Unreadable MCP catalogue.") }
            configurations = Dictionary(rows.map { ($0["id"] as! String,$0) },uniquingKeysWith:{ _,new in new })
            servers = rows.map { NativeIntegrationRow(id:$0["id"] as! String,fields:NativeIntegrationWire.safe($0,fields:["name","description","installed","connected","configured","ready","install_state"])) }
        case "tools":
            guard let rows = value["tools"] as? [[String:Any]],rows.count <= 5000,rows.allSatisfy({ $0["name"] is String }) else { throw NativeIntegrationError("Unreadable MCP tools.") };tools = rows.map { $0["name"] as! String }
            toolRows = rows.enumerated().map { offset,row in
                let schema = row["inputSchema"] as? [String:Any] ?? [:],properties = schema["properties"] as? [String:[String:Any]] ?? [:]
                let fields = properties.keys.sorted().prefix(100).map { name in name + ": " + (properties[name]?["type"] as? String ?? "unspecified") }
                return NativeIntegrationRow(id:String(offset),fields:["name":row["name"]!,"description":row["description"] as? String ?? "","parameters":fields,"required":schema["required"] as? [String] ?? []])
            }
        case "projection": guard NativeIntegrationWire.bool(value["enabled"]) != nil else { throw NativeIntegrationError("MCP skill projection is unavailable.") };projection = NativeIntegrationWire.safe(value,fields:["enabled","ready","projected_count","registry_wired","executor_wired"])
        case "vault":
            guard let rows = value["keys"] as? [String:[String:Any]],rows.keys.allSatisfy({ NativeIntegrationWire.identifier($0) }),rows.values.allSatisfy({ NativeIntegrationWire.bool($0["stored"]) == true && NativeIntegrationWire.fingerprint($0["fingerprint"]) != nil }) else { throw NativeIntegrationError("Unreadable credential metadata.") }
            keys = rows.keys.sorted().map { NativeIntegrationRow(id:$0,fields:NativeIntegrationWire.safe(rows[$0]!,fields:["stored","fingerprint"])) }
        default:break
        }
    }
    func channelList(type:String,key:String) -> [String] {
        let value = savedChannels[type + "_" + key]
        if let rows = value as? [String] { return rows }
        if let text = value as? String { return text.split(separator:",").map { $0.trimmingCharacters(in:.whitespacesAndNewlines) }.filter { !$0.isEmpty } }
        return []
    }
    func channelBootEnabled(_ type:String) -> Bool {
        guard let value = savedFeatures[type],!(value is NSNull) else { return true }
        if let boolean = NativeIntegrationWire.bool(value) { return boolean }
        if let text = value as? String { return ["true","1","yes","on"].contains(text.lowercased()) }
        if let number = value as? NSNumber { return number.doubleValue != 0 }
        return false
    }
    func mcpDraft(_ id:String) -> [String:Any] { NativeIntegrationWire.safe(configurations[id] ?? [:],fields:["command","args","transport","url"]) }
    private func configuredBody(id:String,changes:[String:Any]) throws -> [String:Any] {
        guard NativeIntegrationWire.identifier(id),changes.keys.allSatisfy({ ["command","args","transport","url","env","headers"].contains($0) }) else { throw NativeIntegrationError("Enter a bounded server ID and canonical MCP configuration fields.") }
        var body = NativeIntegrationWire.safe(configurations[id] ?? [:],fields:["command","args","transport","url","env","headers"])
        for name in ["env","headers"] { if body[name] != nil,!(body[name] is [String:String]) { throw NativeIntegrationError("Existing MCP credential fields are unreadable; no sibling values can safely be merged.") } }
        for (key,value) in changes {
            if key == "env" || key == "headers" {
                guard let added = value as? [String:String],added.count <= 50,added.keys.allSatisfy({ NativeIntegrationWire.identifier($0) }),added.values.allSatisfy({ $0.count <= 8192 }) else { throw NativeIntegrationError("Environment/header entries require bounded names and hidden string values.") }
                var merged = body[key] as? [String:String] ?? [:];for (name,secret) in added { merged[name] = secret };body[key] = merged
            } else { body[key] = value }
        }
        let transport = body["transport"] as? String ?? "stdio";body["transport"] = transport;body["name"] = id
        guard ["stdio","http"].contains(transport),(body["args"] == nil || body["args"] is [String]) else { throw NativeIntegrationError("Select stdio/HTTP and an argument list.") };let arguments = body["args"] as? [String] ?? [];guard arguments.count <= 100,arguments.allSatisfy({ $0.count <= 4096 }) else { throw NativeIntegrationError("Select stdio/HTTP and at most 100 bounded argument strings.") }
        body["args"] = arguments
        if transport == "stdio" { guard let command = body["command"] as? String,!command.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty,command.count <= 1000,!command.contains("\n") else { throw NativeIntegrationError("Enter the executable to launch. Arguments are supplied separately, without a shell.") } }
        else { guard let raw = body["url"] as? String,let url = URL(string:raw),["http","https"].contains(url.scheme ?? ""),url.host != nil,url.user == nil,url.password == nil,url.fragment == nil else { throw NativeIntegrationError("Enter an HTTP(S) MCP endpoint without embedded credentials or fragment.") } }
        return body
    }
    func review(_ action:NativeIntegrationAction) throws -> NativeIntegrationReview {
        guard available,!loading,!busy else { throw NativeIntegrationError("Wait for the local integration service.") }
        let title:String,detail:String
        var configuration:[String:Any]?
        switch action {
        case .channelStop(let type):
            guard type.range(of:"^[a-z][a-z0-9_-]{0,63}$",options:.regularExpression) != nil,errors["channels"] == nil,let row = channels.first(where:{$0.id == type}) else { throw NativeIntegrationError("Refresh this active channel before reviewing a stop.") }
            configuration = row.fields
            title = "Stop the live \(type) listener?"
            detail = "Stops this runtime listener and its owned background tasks. Saved credentials, sender/chat access rules and startup enablement remain unchanged; restarting or saving credentials can start it again. Already delivered messages cannot be recalled. This does not revoke provider credentials."
        case .mcpDisconnect(let id):
            guard id.range(of:"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",options:.regularExpression) != nil,errors["status"] == nil,(mcpStatus["names"] as? [String] ?? []).contains(id),let states = mcpStatus["states"] as? [String:String],let status = states[id] else { throw NativeIntegrationError("Refresh this live MCP connection before reviewing a disconnect.") }
            configuration = ["state":status]
            title = "Disconnect live MCP server \(id)?"
            detail = "Closes this runtime connection and its owned local process or HTTP client. Saved server configuration and credentials are retained; provider-side session deletion is unverified. Tools from this connection become unavailable. Already completed filesystem/account actions are not undone."
        case .vaultStore(let name,let value):
            guard NativeIntegrationWire.identifier(name),!value.isEmpty else { throw NativeIntegrationError("Enter a key name and new credential.") }
            configuration = keys.first(where:{$0.id == name})?.fields ?? [:]
            title = keys.contains(where:{$0.id == name}) ? "Overwrite credential \(name)?" : "Store credential \(name)?"
            detail = "Stores the new secret in the backend blind vault. Existing values are never shown. Replacing a key can affect connected services and future agent requests. No system Keychain reset is performed."
        case .vaultDelete(let name):guard NativeIntegrationWire.identifier(name),let existing = keys.first(where:{$0.id == name}) else { throw NativeIntegrationError("Refresh this key before deleting it.") };configuration = existing.fields;title = "Permanently delete \(name)?";detail = "Removes this backend vault entry. It may break services that depend on it. It does not revoke the credential at its provider or stop an existing connection. There is no undo."
        case .token(let provider,let token,let address,let url):
            guard let row = providers.first(where:{$0.id == provider}),["token","api_token","app_password"].contains(row.fields["auth_type"] as? String ?? ""),!token.isEmpty else { throw NativeIntegrationError("Choose a token-based account and enter its new credential.") }
            if provider == "gmail", !address.contains("@") { throw NativeIntegrationError("Enter the Gmail address for this App Password.") }
            if provider == "home_assistant", !url.isEmpty { guard let endpoint = URL(string:url),["http","https"].contains(endpoint.scheme ?? ""),endpoint.host != nil,endpoint.user == nil,endpoint.password == nil else { throw NativeIntegrationError("Enter an HTTP(S) Home Assistant URL without embedded credentials.") } }
            title = "Save or replace \(row.title) credential?";detail = "Overwrites this account’s stored credential. Gmail and Home Assistant also contact the specified service to test it immediately. This can transmit your address/credential to the account service. Saving is distinct from verified connection."
        case .oauthClient(let provider,let id,let secret):
            guard providers.contains(where:{$0.id == provider && $0.fields["auth_type"] as? String == "oauth2"}),!id.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty else { throw NativeIntegrationError("Choose an OAuth account and enter its app Client ID.") }
            title = "Save OAuth app settings for \(provider)?";detail = "Replaces your registered app’s Client ID and supplied Client Secret (\(secret.isEmpty ? "none supplied" : "hidden")). It does not authorize your personal account. Higher-priority server configuration may prevent these settings from becoming active."
        case .authorize(let provider):guard providers.contains(where:{$0.id == provider && $0.fields["auth_type"] as? String == "oauth2"}) else { throw NativeIntegrationError("Refresh this OAuth account first.") };title = "Prepare account authorization for \(provider)?";detail = "Creates a short-lived OAuth authorization request. You will separately open the provider’s browser consent page and review its permissions. Preparing a URL is not proof of account connection."
        case .probe(let provider):guard providers.contains(where:{$0.id == provider}) else { throw NativeIntegrationError("Refresh this account first.") };title = "Contact \(provider) to verify status?";detail = "Sends a real authenticated probe using the stored credential. This endpoint also enables the backend’s recurring integration probe sweeper. No account action beyond status checking is requested."
        case .disconnect(let provider):guard providers.contains(where:{$0.id == provider}) else { throw NativeIntegrationError("Refresh this account first.") };title = "Disconnect \(provider)?";detail = "Removes this account’s backend tokens. This is not a guarantee of vendor-side revocation or stopping already executing operations. Reconnection requires authorization or a replacement token."
        case .channel(let type,let token):guard ["telegram","discord","slack"].contains(type),!token.isEmpty else { throw NativeIntegrationError("Choose a supported bot channel and enter a new bot token.") };title = "Save and enable \(type) messaging?";detail = "Replaces its stored bot token and automatically requests a live background channel start. The bot can receive messages and send agent replies under the existing inbound access policy. Saving does not prove it connected; inspect reported access and connection status afterwards."
        case .mcp(let id):guard let config = configurations[id],config["command"] is String,config["args"] is [String],servers.contains(where:{$0.id == id}) else { throw NativeIntegrationError("Refresh this MCP launch configuration.") };configuration = config;title = "Launch and connect MCP server \(id)?";detail = "Launches the catalogue configuration for \(id) with \((config["args"] as! [String]).count) arguments. Existing argument and environment values are hidden because they can contain credentials. Review this server’s account and filesystem scope in its configuration before proceeding. Package launchers may download code. The server receives configured environment credentials and can expose tools to the agent, including filesystem or remote account access. This is a live connection, not a saved configuration."
        case .mcpConfigured(let id,let changes,let scope):
            let body = try configuredBody(id:id,changes:changes)
            guard !scope.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty,scope.count <= 2000,errors["mcp"] == nil,errors["status"] == nil else { throw NativeIntegrationError("Reload MCP catalogue/live status and describe the intended file/account scope before connecting.") }
            if configurations[id] == nil,(mcpStatus["names"] as? [String] ?? []).contains(id) { throw NativeIntegrationError("This live server has no readable catalogue configuration. Reusing its ID could replace opaque settings; use a new server ID.") }
            configuration = configurations[id] ?? [:]
            let transport = body["transport"] as! String
            let destination = transport == "http" ? (URL(string:body["url"] as! String)?.host ?? "unreported host") : (body["command"] as? String ?? "")
            let credentialNames = (body["env"] as? [String:String] ?? [:]).keys.sorted() + (body["headers"] as? [String:String] ?? [:]).keys.sorted()
            title = "Connect reviewed MCP server \(id)?"
            detail = "Live \(transport) connection to \(destination). Declared scope: \(scope). \((body["args"] as? [String] ?? []).count) exact argument strings; environment/header names: \(credentialNames.joined(separator:", ")). Values are hidden. Existing same-ID connections are disconnected before replacement; failure can leave them offline. Local commands may download code, and remote endpoints receive configured headers. \(transport == "http" && URL(string:body["url"] as! String)?.scheme == "http" ? "HTTP sends credentials without transport encryption." : "") This does not save mcp_servers.json or change sandbox policy. The declared scope is a user description, not an enforced filesystem sandbox."
        case .channelSetting(let type,let key,let value):
            guard ["telegram","discord","slack"].contains(type),["allowed_senders","allowed_chats","boot_enabled"].contains(key),errors["config"] == nil else { throw NativeIntegrationError("Refresh readable channel settings before changing a supported policy field.") }
            if key == "boot_enabled" { guard NativeIntegrationWire.bool(value) != nil else { throw NativeIntegrationError("Boot enablement must be a Boolean.") } }
            else { guard let ids = value as? [String],ids.count <= 100,Set(ids).count == ids.count,ids.allSatisfy({ NativeIntegrationWire.identifier($0) || ($0.count <= 200 && !$0.isEmpty && !$0.contains("\n") && !$0.contains(",")) }) else { throw NativeIntegrationError("Use at most 100 unique bounded sender/chat IDs.") } }
            configuration = ["channels":savedChannels,"features":savedFeatures]
            title = "Save \(type) \(key.replacingOccurrences(of:"_",with:" "))?"
            detail = "Writes only the reviewed saved field. Proposed: \(String(describing:value)). Other fields, including opaque siblings, are preserved. Backend startup unions saved sender/chat IDs with environment/credential rules; removing a saved ID does not guarantee access revocation. Adoption requires channel/app restart and no live listener is stopped or started by this setting write. Live stopping is a separate reviewed action; pairing APIs are unavailable."
        case .projection(let enabled):guard NativeIntegrationWire.bool(projection["enabled"]) != nil else { throw NativeIntegrationError("Load MCP projection status first.") };title = enabled ? "Expose FERAL skills through MCP?" : "Disable MCP skill projection?";detail = enabled ? "Permits connected external MCP clients to discover and invoke projected FERAL skills under existing core policy. This changes live tool access; it does not change network bind/authentication." : "Disables skill projection for future MCP calls. It does not undo completed tool actions."
        }
        let issued = NativeIntegrationReview(generation:generation,action:action,title:title,detail:detail,configuration:configuration);if issuedReviews.count >= 50 { issuedReviews = [] };issuedReviews.insert(issued.id);return issued
    }
    func canOpenAuthorization(_ url:URL) -> Bool { available && !busy && !loading && url == authorizationURL && url.scheme == "https" && url.host != nil && url.user == nil && url.password == nil }
    func perform(_ reviewed:NativeIntegrationReview) async -> Bool {
        guard reviewed.generation == generation,issuedReviews.remove(reviewed.id) != nil,let client = client else { actionError = "Review expired, was already used, or the connection changed. Review again.";return false }
        do { let fresh = try review(reviewed.action);issuedReviews.remove(fresh.id);guard fresh.title == reviewed.title, fresh.detail == reviewed.detail else { throw NativeIntegrationError("The reviewed action changed. Review again.") };if let original = reviewed.configuration { guard let current = fresh.configuration,NSDictionary(dictionary:original).isEqual(to:current) else { throw NativeIntegrationError("Reviewed configuration changed. Review again.") } } } catch { actionError = error.localizedDescription;return false }
        let configuredConnection:[String:Any]?
        switch reviewed.action {
        case .mcpConfigured(let id,let changes,_):do { configuredConnection = try configuredBody(id:id,changes:changes) } catch { actionError = error.localizedDescription;return false }
        case .mcp(let id):guard let config = configurations[id] else { actionError = "MCP configuration changed. Review again.";return false };var body = NativeIntegrationWire.safe(config,fields:["command","args","env","transport","url","headers"]);body["name"] = id;configuredConnection = body
        default:configuredConnection = nil
        }
        let connection = generation;operation = UUID();let op = operation;busy = true;receipt = nil;actionError = nil
        var runtimeWriteDispatched = false
        defer { if generation == connection && operation == op { busy = false } }
        do {
            if let expected = reviewed.configuration {
                let current:[String:Any]
                switch reviewed.action {
                case .channelStop(let type):current = try runtimeState(try await client.request("/api/channels"),id:type,mcp:false) ?? [:]
                case .mcpDisconnect(let id):current = try runtimeState(try await client.request("/api/mcp/status"),id:id,mcp:true) ?? [:]
                case .vaultStore(let name,_),.vaultDelete(let name):
                    let state = try await client.request("/api/security/vault")
                    guard let entries = state["keys"] as? [String:[String:Any]] else { throw NativeIntegrationError("Credential metadata could not be verified before changing it.") }
                    if let entry = entries[name] { guard NativeIntegrationWire.bool(entry["stored"]) == true,NativeIntegrationWire.fingerprint(entry["fingerprint"]) != nil else { throw NativeIntegrationError("Credential metadata could not be verified before changing it.") };current = NativeIntegrationWire.safe(entry,fields:["stored","fingerprint"]) } else { current = [:] }
                case .mcp(let id),.mcpConfigured(let id,_,_):
                    let state = try await client.request("/api/mcp/servers")
                    guard let entries = state["servers"] as? [[String:Any]],entries.filter({$0["id"] as? String == id}).count <= 1 else { throw NativeIntegrationError("MCP configuration could not be verified before launch.") };current = entries.first(where:{$0["id"] as? String == id}) ?? [:]
                    if current.isEmpty { let live = try await client.request("/api/mcp/status");guard let statuses = live["client"] as? [String:Any],let names = statuses["server_names"] as? [String],!names.contains(id) else { throw NativeIntegrationError("The new server ID is now live or its status is unreadable. Choose a new ID and review again.") } }
                case .channelSetting:
                    let state = try await client.request("/api/config")
                    guard state["channels"] == nil || state["channels"] is [String:Any],state["features"] == nil || state["features"] is [String:Any] else { throw NativeIntegrationError("Saved channel settings could not be verified.") }
                    current = ["channels":state["channels"] ?? [:],"features":state["features"] ?? [:]]
                default:current = expected
                }
                guard generation == connection && operation == op else { return false }
                guard NSDictionary(dictionary:expected).isEqual(to:current) else { throw NativeIntegrationError("Reviewed configuration changed on the backend. Refresh and review again.") }
            }
            let response:[String:Any],text:String
            var authorize:URL?,scopes:[String] = [],provider:String?
            switch reviewed.action {
            case .channelStop(let type):
                runtimeWriteDispatched = true
                response = try await client.request("/api/channels/stop",method:"POST",body:["type":type])
                guard NativeIntegrationWire.bool(response["ok"]) == true,NativeIntegrationWire.bool(response["stopped"]) == true,response["channel"] as? String == type,response["scope"] as? String == "runtime_only",NativeIntegrationWire.bool(response["saved_configuration_changed"]) == false,NativeIntegrationWire.bool(response["credentials_revoked"]) == false else { throw NativeIntegrationError("Exact channel stop receipt was not confirmed.") }
                let after = try await client.request("/api/channels")
                guard try runtimeState(after,id:type,mcp:false) == nil else { throw NativeIntegrationError("Channel stop readback still lists this ID; it may have restarted or been replaced.") }
                text = "\(type) runtime listener stopped and its ID is absent from live inventory. Saved startup settings and credentials remain; provider credentials were not revoked."
            case .mcpDisconnect(let id):
                runtimeWriteDispatched = true
                response = try await client.request("/api/mcp/disconnect",method:"POST",body:["name":id])
                guard NativeIntegrationWire.bool(response["success"]) == true,NativeIntegrationWire.bool(response["disconnected"]) == true,response["name"] as? String == id,response["scope"] as? String == "runtime_only",NativeIntegrationWire.bool(response["saved_configuration_changed"]) == false,NativeIntegrationWire.bool(response["credentials_revoked"]) == false,response["remote_session_closed"] as? String == "unverified" else { throw NativeIntegrationError("Exact MCP disconnect receipt was not confirmed.") }
                let after = try await client.request("/api/mcp/status")
                guard try runtimeState(after,id:id,mcp:true) == nil else { throw NativeIntegrationError("MCP disconnect readback still lists this ID; it may have reconnected or been replaced.") }
                text = "\(id) runtime connection disconnected and its ID is absent from live inventory. Saved configuration and credentials remain; remote session deletion is unverified."
            case .vaultStore(let name,let secret):response = try await client.request("/api/security/vault/store",method:"POST",body:["key_name":name,"value":secret],secrets:[secret]);guard NativeIntegrationWire.bool(response["ok"]) == true,response["key_name"] as? String == name,NativeIntegrationWire.fingerprint(response["fingerprint"]) == NativeIntegrationWire.hash(secret) else { throw NativeIntegrationError("Credential storage was not confirmed for that key.") };text = "Credential \(name) stored in the backend vault."
            case .vaultDelete(let name):response = try await client.request("/api/security/vault/" + NativeIntegrationWire.segment(name),method:"DELETE");guard NativeIntegrationWire.bool(response["ok"]) == true else { throw NativeIntegrationError("Credential deletion was not confirmed.") };let after = try await client.request("/api/security/vault");guard let remaining = after["keys"] as? [String:[String:Any]],remaining[name] == nil else { throw NativeIntegrationError("The backend acknowledged deletion but the exact key is still listed.") };text = "Backend credential \(name) deleted; provider-side revocation is not established."
            case .token(let id,let secret,let address,let url):var body:[String:Any] = ["provider_id":id,"token":secret];if id == "gmail" { body["address"] = address;body["app_password"] = secret };if id == "home_assistant" { body["url"] = url };response = try await client.request("/api/integrations/token",method:"POST",body:body,secrets:[secret]);guard NativeIntegrationWire.bool(response["ok"]) == true,response["provider"] as? String == id else { throw NativeIntegrationError("Account credential storage was not confirmed.") };text = "Backend acknowledged \(id) credential save. " + (NativeIntegrationWire.bool(response["connected"]) == true ? "Backend reports connected." : "Verified connection is not established by this receipt.")
            case .oauthClient(let id,let clientID,let secret):response = try await client.request("/api/integrations/oauth/client",method:"POST",body:["provider_id":id,"client_id":clientID,"client_secret":secret],secrets:[secret]);guard NativeIntegrationWire.bool(response["ok"]) == true,response["provider"] as? String == id,let applied = NativeIntegrationWire.bool(response["applied"]) else { throw NativeIntegrationError("OAuth app storage was not confirmed.") };text = "\(id) OAuth app saved. " + (applied ? "App settings applied; account authorization remains separate." : "A higher-priority configuration prevents these settings becoming active.")
            case .authorize(let id):response = try await client.request("/api/oauth/authorize/" + NativeIntegrationWire.segment(id));guard NativeIntegrationWire.bool(response["success"]) == true,response["provider"] as? String == id,let value = response["url"] as? String,let url = URL(string:value),url.scheme == "https",url.host != nil,url.user == nil,url.password == nil else { throw NativeIntegrationError("No safe HTTPS account authorization URL was returned.") };authorize = url;provider = id;scopes = response["scopes"] as? [String] ?? [];text = "Authorization prepared for \(id). Open its consent page separately; connection is not yet confirmed."
            case .probe(let id):response = try await client.request("/api/integrations/refresh",method:"POST",body:["provider_id":id]);guard NativeIntegrationWire.bool(response["ok"]) == true,let results = response["results"] as? [String:Any],results.keys.contains(id) else { throw NativeIntegrationError("This account probe was not acknowledged.") };text = "\(id): " + (NativeIntegrationWire.bool(results[id]).map { $0 ? "probe verified connection" : "probe did not verify connection" } ?? "no registered verdict; connection remains unverified")
            case .disconnect(let id):response = try await client.request("/api/integrations/disconnect/" + NativeIntegrationWire.segment(id),method:"POST");guard NativeIntegrationWire.bool(response["ok"]) == true,response["provider"] as? String == id else { throw NativeIntegrationError("Account disconnect was not acknowledged.") };text = "Backend acknowledged disconnect for \(id)."
            case .channel(let type,let secret):let key = "FERAL_\(type.uppercased())_BOT_TOKEN";response = try await client.request("/api/config/credentials",method:"POST",body:[key:secret],secrets:[secret]);guard NativeIntegrationWire.bool(response["ok"]) == true,(response["keys_saved"] as? [String])?.contains(key) == true,!(response["rejected"] as? [String] ?? []).contains(key),(response["persisted_to_vault"] as? [String])?.contains(key) == true || NativeIntegrationWire.bool(response["persisted_to_credentials_json"]) == true else { throw NativeIntegrationError("The channel credential was not confirmed persisted. No connected status is established.") };text = "\(type) token persisted and automatic listener startup requested. Check live status; no connection is assumed."
            case .mcp(let id):
                guard let body = configuredConnection else { throw NativeIntegrationError("Reviewed MCP configuration is no longer available.") }
                response = try await client.request("/api/mcp/connect",method:"POST",body:body)
                guard NativeIntegrationWire.bool(response["success"]) == true,let count = response["tools"] as? NSNumber,CFGetTypeID(count) != CFBooleanGetTypeID(),count.doubleValue >= 0,count.doubleValue.rounded(.towardZero) == count.doubleValue else { throw NativeIntegrationError("MCP connection was not confirmed.") }
                text = "\(id) connected; backend reports \(count.intValue) tools. Tool execution was not tested."
            case .mcpConfigured(let id,_,_):
                guard let body = configuredConnection else { throw NativeIntegrationError("Reviewed MCP configuration is no longer available.") }
                response = try await client.request("/api/mcp/connect",method:"POST",body:body)
                guard NativeIntegrationWire.bool(response["success"]) == true,let count = response["tools"] as? NSNumber,CFGetTypeID(count) != CFBooleanGetTypeID(),count.doubleValue >= 0,count.doubleValue.rounded(.towardZero) == count.doubleValue else { throw NativeIntegrationError("Custom MCP connection was not confirmed.") }
                text = "Backend reports \(id) connected with \(count.intValue) tools. Configuration was applied to this live connection, not persisted; tool execution is untested."
            case .channelSetting(let type,let key,let value):
                let section = key == "boot_enabled" ? "features" : "channels",setting = key == "boot_enabled" ? type : type + "_" + key
                response = try await client.request("/api/config/update",method:"POST",body:["section":section,"key":setting,"value":value])
                guard NativeIntegrationWire.bool(response["ok"]) == true else { throw NativeIntegrationError("Channel setting write was not acknowledged.") }
                let after = try await client.request("/api/config")
                guard let saved = after[section] as? [String:Any],let actual = saved[setting],NSDictionary(dictionary:["value":actual]).isEqual(to:["value":value]) else { throw NativeIntegrationError("Channel setting readback does not match the reviewed value.") }
                text = "Exact saved \(type) \(key) confirmed. Existing live listeners and environment/credential rules are unchanged; no live revocation or stop is claimed."
            case .projection(let enabled):response = try await client.request("/api/mcp/projection",method:"POST",body:["enabled":enabled]);guard NativeIntegrationWire.bool(response["enabled"]) == enabled else { throw NativeIntegrationError("MCP projection change was not confirmed.") };text = enabled ? "MCP skill projection enabled. Ready: \(NativeIntegrationWire.bool(response["ready"]) == true ? "Yes" : "No")." : "MCP skill projection disabled."
            }
            guard generation == connection && operation == op else { return false }
            if let url = authorize { authorizationURL = url;authorizationScopes = scopes;authorizationProvider = provider }
            if case .disconnect(let id) = reviewed.action,id == authorizationProvider { authorizationURL = nil;authorizationScopes = [];authorizationProvider = nil }
            await refresh();guard generation == connection && operation == op else { return false };receipt = text;return true
        } catch { if generation == connection && operation == op { actionError = error.localizedDescription + (runtimeWriteDispatched ? " Runtime outcome is uncertain. Refresh live status before reviewing any further action; this approval will not be retried." : "") };return false }
    }
    private func runtimeState(_ value:[String:Any],id:String,mcp:Bool) throws -> [String:Any]? {
        if mcp {
            guard let client = value["client"] as? [String:Any],let names = client["server_names"] as? [String],names.count <= 500,Set(names).count == names.count,names.allSatisfy({ NativeIntegrationWire.identifier($0) }),let states = client["server_states"] as? [String:String] else { throw NativeIntegrationError("Live MCP status could not be verified.") }
            guard names.contains(id) else { return nil }
            guard let status = states[id],["connected","degraded"].contains(status) else { throw NativeIntegrationError("Live MCP state could not be verified.") }
            return ["state":status]
        }
        let details:[String:Any]
        if let rows = value["details"] as? [String:Any] { details = rows }
        else if let rows = value["channels"] as? [Any],rows.isEmpty { details = [:] }
        else { throw NativeIntegrationError("Live channel status could not be verified.") }
        guard let raw = details[id] else { return nil }
        guard let row = raw as? [String:Any],NativeIntegrationWire.bool(row["running"]) != nil,NativeIntegrationWire.bool(row["connected"]) != nil else { throw NativeIntegrationError("Live channel state could not be verified.") }
        return NativeIntegrationWire.safe(row,fields:["connected","running","degraded","failure_count","access_configured","allowed_sender_count","allowed_chat_count","pairing_window_open","known_chats"])
    }
}

struct NativeIntegrationSecretDraft:Identifiable { let id = UUID();var name = "";var value = "" }

struct NativeIntegrationFeatureView:View {
    let baseURL:URL?
    @StateObject private var model = NativeIntegrationModel()
    @State private var tab = "Accounts"
    @State private var review:NativeIntegrationReview?
    @State private var error:String?
    @State private var selected:NativeIntegrationRow?
    @State private var browserReview:URL?
    @State private var secret = ""
    @State private var identifier = ""
    @State private var address = ""
    @State private var endpoint = ""
    @State private var keyName = ""
    @State private var channel = "telegram"
    @State private var senderIDs = ""
    @State private var chatIDs = ""
    @State private var editingMCP = false
    @State private var serverID = ""
    @State private var transport = "stdio"
    @State private var executable = ""
    @State private var remoteURL = ""
    @State private var argumentText = ""
    @State private var replaceArguments = false
    @State private var declaredScope = ""
    @State private var environmentDrafts = [NativeIntegrationSecretDraft()]
    @State private var headerDrafts = [NativeIntegrationSecretDraft()]
    init(baseURL:URL?) { self.baseURL = baseURL }
    private var busy:Bool { model.loading || model.busy }
    var body:some View {
        VStack(alignment:.leading,spacing:16) {
            HStack { VStack(alignment:.leading) { Text("Integrations").font(.largeTitle.bold());Text("Connect accounts and tools with explicit permission.").foregroundStyle(.secondary) };Spacer();Button("Refresh saved status") { Task { await model.refresh() } }.disabled(busy || baseURL == nil) }
            Picker("Integration section",selection:$tab) { ForEach(["Accounts","Channels","MCP","Vault"],id:\.self) { Text($0).tag($0) } }.pickerStyle(.segmented).disabled(model.busy)
            if baseURL == nil { Text("The local integration service is not ready.").foregroundStyle(.secondary) }
            if let message = error ?? model.actionError { Text(message).foregroundStyle(.red).textSelection(.enabled) }
            if let message = model.receipt { Text(message).foregroundStyle(.secondary) }
            if model.loading { ProgressView("Reading saved status…") }
            ScrollView { VStack(alignment:.leading,spacing:16) { if tab == "Accounts" { accounts };if tab == "Channels" { channelSection };if tab == "MCP" { mcp };if tab == "Vault" { vault } }.frame(maxWidth:.infinity,alignment:.leading) }
        }.padding(24)
        .task(id:baseURL?.absoluteString ?? "") { review = nil;selected = nil;browserReview = nil;editingMCP = false;environmentDrafts = [];headerDrafts = [];secret = "";await model.configure(baseURL:baseURL) }
        .sheet(item:$review) { item in VStack(alignment:.leading,spacing:16) { Text(item.title).font(.title2.bold());ScrollView { Text(item.detail).textSelection(.enabled) }.frame(maxHeight:300);if let error = model.actionError { Text(error).foregroundStyle(.red) };HStack { Spacer();Button("Cancel") { secret = "";review = nil }.disabled(model.busy);Button(model.busy ? "Applying…" : "Confirm") { Task { if await model.perform(item) { secret = "";review = nil } } }.disabled(busy) } }.padding(24).frame(width:560).interactiveDismissDisabled(model.busy) }
        .sheet(item:$selected) { row in accountForm(row) }
        .sheet(isPresented:$editingMCP,onDismiss:{ environmentDrafts = [];headerDrafts = [];argumentText = "" }) { mcpForm }
        .onChange(of:tab) { _ in secret = "";browserReview = nil }
        .onChange(of:channel) { _ in bindChannelPolicy() }
        .alert("Open account consent in your browser?",isPresented:Binding(get:{ browserReview != nil },set:{ if !$0 { browserReview = nil } })) {
            Button("Cancel",role:.cancel) { browserReview = nil }
            Button("Open browser") { if let url = browserReview,model.canOpenAuthorization(url) { NSWorkspace.shared.open(url) };browserReview = nil }
        } message: { Text("Visit \(browserReview?.host ?? "") for \(model.authorizationProvider ?? "this account"). Requested scopes: \(model.authorizationScopes.isEmpty ? "not supplied" : model.authorizationScopes.joined(separator:", ")). The provider’s browser page will receive this authorization request. Review its consent permissions before continuing.") }
    }
    private func request(_ action:NativeIntegrationAction) { do { error = nil;review = try model.review(action) } catch { self.error = error.localizedDescription } }
    private func card<C:View>(_ title:String,@ViewBuilder content:() -> C) -> some View { VStack(alignment:.leading,spacing:12) { Text(title).font(.headline);content() }.padding(16).frame(maxWidth:.infinity,alignment:.leading).background(Color.secondary.opacity(0.07),in:RoundedRectangle(cornerRadius:12)) }
    @ViewBuilder private func failure(_ key:String) -> some View { if let error = model.errors[key] { Text("Unavailable: " + error).foregroundStyle(.red) } }
    @ViewBuilder private var accounts:some View {
        failure("accounts")
        Text("Saved/cached status only. No automatic account probes or authorization.").font(.caption).foregroundStyle(.secondary)
        ForEach(model.providers) { row in card(row.title) { Text("\(row.fields["auth_type"] as? String ?? "Unknown authentication") · \(NativeIntegrationWire.bool(row.fields["connected"]) == true ? "Reports connected" : "Not connected")");Text(NativeIntegrationWire.bool(row.fields["probe_verified"]) == true ? "Cached probe verified; it may be stale." : "Not verified by a current probe.").font(.caption).foregroundStyle(.secondary);HStack { Button("Configure credential…") { secret = "";identifier = "";address = "";endpoint = "";selected = row };if row.fields["auth_type"] as? String == "oauth2" { Button("Authorize account…") { request(.authorize(row.id)) } };Button("Verify status…") { request(.probe(row.id)) };Button("Disconnect…",role:.destructive) { request(.disconnect(row.id)) } }.disabled(busy) } }
        if let url = model.authorizationURL { card("Account authorization prepared") { Text("Provider: \(model.authorizationProvider ?? "") · \(url.host ?? "")");Text("Requested scopes: " + (model.authorizationScopes.isEmpty ? "Not supplied" : model.authorizationScopes.joined(separator:", "))).font(.caption);Button("Review opening provider consent page…") { browserReview = url }.disabled(busy);Text("Return here and explicitly verify status after completing browser consent.").font(.caption).foregroundStyle(.secondary) } }
    }
    @ViewBuilder private var channelSection:some View {
        failure("channels")
        ForEach(model.channels) { row in card(row.title) {
            Text("\(NativeIntegrationWire.bool(row.fields["connected"]) == true ? "Connected" : "Not connected") · \(NativeIntegrationWire.bool(row.fields["running"]) == true ? "Running" : "Stopped")")
            Text("Inbound access configured: \(NativeIntegrationWire.bool(row.fields["access_configured"]) == true ? "Yes" : "No") · Allowed senders: \(String(describing:row.fields["allowed_sender_count"] ?? "Unavailable"))").font(.caption)
            if NativeIntegrationWire.bool(row.fields["degraded"]) == true { Text("Channel is degraded.").foregroundStyle(.orange) }
            Button("Review stop live listener…",role:.destructive) { request(.channelStop(row.id)) }.disabled(busy)
        } }
        card("Enable a bot channel") { Picker("Channel",selection:$channel) { ForEach(["telegram","discord","slack"],id:\.self) { Text($0.capitalized).tag($0) } };SecureField("New bot token",text:$secret);Button("Review save and enable…") { request(.channel(type:channel,token:secret)) }.disabled(busy || secret.isEmpty);Text("This replaces credentials and requests live messaging. Inbound sender/chat access remains governed by the existing backend policy. Live stopping is reviewed separately; pairing APIs are unavailable.").font(.caption).foregroundStyle(.secondary) }
        card("Saved inbound access policy") {
            failure("config")
            Text("Saved IDs take effect on backend startup and are unioned with environment/credential allowlists. They are not a live revocation control.").font(.caption).foregroundStyle(.secondary)
            Button("Load saved IDs for \(channel)") { bindChannelPolicy() }.disabled(busy)
            TextField("Allowed sender IDs, comma separated",text:$senderIDs)
            Button("Review saved senders…") { request(.channelSetting(type:channel,key:"allowed_senders",value:parsedIDs(senderIDs))) }.disabled(busy)
            TextField("Allowed chat IDs, comma separated",text:$chatIDs)
            Button("Review saved chats…") { request(.channelSetting(type:channel,key:"allowed_chats",value:parsedIDs(chatIDs))) }.disabled(busy)
            let boot = model.channelBootEnabled(channel)
            Text("Saved startup gate: \(boot ? "Enabled / default enabled" : "Disabled")")
            Button(boot ? "Review disable on next startup…" : "Review enable on next startup…") { request(.channelSetting(type:channel,key:"boot_enabled",value:!boot)) }.disabled(busy)
            Text("Disabling startup does not stop the current listener. Use its separate live stop action above.").font(.caption).foregroundStyle(.orange)
        }
    }
    @ViewBuilder private var mcp:some View {
        failure("mcp");failure("projection");failure("tools");failure("status")
        Button("Configure a custom live connection…") { beginMCP("") }.disabled(busy)
        if let names = model.mcpStatus["names"] as? [String],let states = model.mcpStatus["states"] as? [String:String] {
            card("Live MCP connections") {
                if names.isEmpty { Text("No live server connections reported.") }
                ForEach(names,id:\.self) { name in
                    Text("\(name): \(states[name] ?? "unreported")")
                    Button("Review disconnect \(name)…",role:.destructive) { request(.mcpDisconnect(name)) }.disabled(busy)
                }
                Text("Disconnect closes a runtime connection; configuration and credentials remain. Same-ID Connect replaces a live connection; it is not a save operation.").font(.caption).foregroundStyle(.secondary)
            }
        }
        if let enabled = NativeIntegrationWire.bool(model.projection["enabled"]) { card("Expose FERAL skills") { Text(enabled ? "Projection enabled" : "Projection disabled");Text("Ready: \(NativeIntegrationWire.bool(model.projection["ready"]) == true ? "Yes" : "No")");Button(enabled ? "Disable projection…" : "Enable projection…") { request(.projection(!enabled)) }.disabled(busy) } }
        ForEach(model.servers) { row in card(row.title) { Text(row.fields["description"] as? String ?? "External tool server");Text("\(NativeIntegrationWire.bool(row.fields["connected"]) == true ? "Connected" : "Not connected") · Install state: \(row.fields["install_state"] as? String ?? "Unavailable")").font(.caption);Button("Configure and review live connection…") { beginMCP(row.id) }.disabled(busy);Text("Required environment values are hidden. Launch may download packages and grant the agent tools.").font(.caption).foregroundStyle(.secondary) } }
        if !model.toolRows.isEmpty { card("Reported external tools") { ForEach(model.toolRows) { tool in DisclosureGroup(tool.title) { Text(tool.fields["description"] as? String ?? "");ForEach(tool.fields["parameters"] as? [String] ?? [],id:\.self) { Text($0).font(.caption) };Text("Required: " + (tool.fields["required"] as? [String] ?? []).joined(separator:", ")).font(.caption) } };Text("Read-only tool metadata. Parameter defaults/examples and opaque schema fields are hidden; no tool is executed here.").font(.caption).foregroundStyle(.secondary) } }
    }
    @ViewBuilder private var vault:some View {
        failure("vault")
        Text("Only default-namespace key names and fingerprints are listed. OAuth/publisher namespaces and secret values are omitted by the backend.").font(.caption).foregroundStyle(.secondary)
        ForEach(model.keys) { row in card(row.title) { Text("Fingerprint: " + (row.fields["fingerprint"] as? String ?? "Unavailable")).font(.system(.caption,design:.monospaced));Button("Delete credential…",role:.destructive) { request(.vaultDelete(row.id)) }.disabled(busy) } }
        card("Store a new credential") { TextField("Key name",text:$keyName);SecureField("New secret value",text:$secret);Button("Review store or overwrite…") { request(.vaultStore(name:keyName,value:secret)) }.disabled(busy || keyName.isEmpty || secret.isEmpty);Text("Existing values are never retrieved. This does not reset the macOS Keychain or automatically activate every service.").font(.caption).foregroundStyle(.secondary) }
    }
    private func parsedIDs(_ text:String) -> [String] { text.split(separator:",").map { $0.trimmingCharacters(in:.whitespacesAndNewlines) }.filter { !$0.isEmpty } }
    private func bindChannelPolicy() { senderIDs = model.channelList(type:channel,key:"allowed_senders").joined(separator:", ");chatIDs = model.channelList(type:channel,key:"allowed_chats").joined(separator:", ") }
    private func beginMCP(_ id:String) {
        let draft = model.mcpDraft(id)
        serverID = id;transport = draft["transport"] as? String ?? "stdio";executable = draft["command"] as? String ?? "";remoteURL = "";argumentText = "";replaceArguments = false;declaredScope = "";environmentDrafts = [NativeIntegrationSecretDraft()];headerDrafts = [NativeIntegrationSecretDraft()];editingMCP = true
    }
    private func secretFields(_ rows:[NativeIntegrationSecretDraft]) throws -> [String:String] {
        let entered = rows.filter { !$0.name.isEmpty || !$0.value.isEmpty }
        guard Set(entered.map(\.name)).count == entered.count,!entered.contains(where:{$0.name.isEmpty}) else { throw NativeIntegrationError("Each new credential entry needs a unique name.") }
        return Dictionary(uniqueKeysWithValues:entered.map { ($0.name,$0.value) })
    }
    private func prepareMCP() {
        do {
            var changes:[String:Any] = ["transport":transport,"env":try secretFields(environmentDrafts),"headers":try secretFields(headerDrafts)]
            if !executable.isEmpty { changes["command"] = executable }
            if !remoteURL.isEmpty { changes["url"] = remoteURL }
            if replaceArguments { changes["args"] = argumentText.split(separator:"\n",omittingEmptySubsequences:false).map(String.init).filter { !$0.isEmpty } }
            let prepared = try model.review(.mcpConfigured(id:serverID,changes:changes,scope:declaredScope))
            editingMCP = false;environmentDrafts = [];headerDrafts = []
            DispatchQueue.main.async { review = prepared }
        } catch { self.error = error.localizedDescription }
    }
    private var mcpForm:some View {
        VStack(alignment:.leading,spacing:14) {
            Text("Configure a live MCP connection").font(.title2.bold())
            ScrollView {
                VStack(alignment:.leading,spacing:12) {
                    TextField("Server ID",text:$serverID)
                    Picker("Transport",selection:$transport) { Text("Local executable (stdio)").tag("stdio");Text("Remote HTTP endpoint").tag("http") }.pickerStyle(.segmented)
                    if transport == "stdio" { TextField("Executable path or name",text:$executable);Toggle("Replace exact argument list",isOn:$replaceArguments);if replaceArguments { Text("One argument per line. No shell expansion is performed.").font(.caption);NativePlainTextEditor(text:$argumentText, label:"Exact MCP arguments", monospaced:true).frame(height:90) } }
                    else { TextField("New HTTP(S) endpoint URL; empty preserves catalogue URL",text:$remoteURL) }
                    TextField("Declared file/account scope (required)",text:$declaredScope)
                    Text("Catalogue presets may differ from a previously configured live server. Existing same-ID connection is replaced. This form does not persist a server file or grant sandbox exceptions.").font(.caption).foregroundStyle(.secondary)
                    Text("New environment values").font(.headline)
                    ForEach($environmentDrafts) { $entry in HStack { TextField("Environment name",text:$entry.name);SecureField("New value",text:$entry.value) } }
                    Button("Add environment entry") { environmentDrafts.append(NativeIntegrationSecretDraft()) }.disabled(environmentDrafts.count >= 50)
                    Text("New HTTP header values").font(.headline)
                    ForEach($headerDrafts) { $entry in HStack { TextField("Header name",text:$entry.name);SecureField("New value",text:$entry.value) } }
                    Button("Add header entry") { headerDrafts.append(NativeIntegrationSecretDraft()) }.disabled(headerDrafts.count >= 50)
                    Text("Existing values and argument strings are not displayed. New credential entries replace matching names; empty supplied values clear them. Unedited canonical fields and credential siblings are preserved from the catalogue snapshot.").font(.caption).foregroundStyle(.secondary)
                }
            }.frame(maxHeight:430)
            HStack { Spacer();Button("Cancel") { environmentDrafts = [];headerDrafts = [];editingMCP = false };Button("Review live connection…") { prepareMCP() }.disabled(busy || declaredScope.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty) }
        }.padding(24).frame(width:620)
    }
    private func prepareAccount(_ row:NativeIntegrationRow) {
        do {
            let action:NativeIntegrationAction
            if row.fields["auth_type"] as? String == "oauth2" {
                action = .oauthClient(provider:row.id,clientID:identifier,secret:secret)
            } else {
                action = .token(provider:row.id,token:secret,address:address,url:endpoint)
            }
            let prepared = try model.review(action)
            selected = nil
            DispatchQueue.main.async { review = prepared }
        } catch { self.error = error.localizedDescription }
    }
    private func accountForm(_ row:NativeIntegrationRow) -> some View {
        VStack(alignment:.leading,spacing:16) {
            Text("Configure \(row.title)").font(.title2.bold())
            if row.fields["auth_type"] as? String == "oauth2" {
                TextField("OAuth app Client ID",text:$identifier)
                SecureField("New Client Secret (if required)",text:$secret)
            } else {
                if row.id == "gmail" { TextField("Gmail address",text:$address) }
                if row.id == "home_assistant" { TextField("Home Assistant URL (optional)",text:$endpoint) }
                SecureField(row.id == "gmail" ? "New App Password" : "New account token",text:$secret)
            }
            Text("Existing secret values are never loaded. Review replacement and any immediate service probes before applying.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Cancel") { secret = "";selected = nil }
                Button("Review changes…") { prepareAccount(row) }
            }
        }.padding(24).frame(width:540)
    }
}
