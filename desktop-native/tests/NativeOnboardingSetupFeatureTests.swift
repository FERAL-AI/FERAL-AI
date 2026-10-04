import Foundation

private final class SetupFixture:URLProtocol {
    static var requests:[URLRequest]=[]
    static var config:[String:Any]=["provider":"ollama","model":"existing-local-model","base_url":"http://127.0.0.1:11434","fallback_providers":["saved-fallback"],"configured":true]
    static var completed=false,vaultReady=false,cloudConfigured=false,rejectWrite=false,partialPersistence=false,denyCompletionReadback=false
    static var delayActivation=false,includeTemplate=false,foreignResponse=false
    static let template="https://[{WorkspaceId}].ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
    // Passive 18-provider catalogue observed from the disposable backend.
    static let fullCatalogue=(try! JSONSerialization.jsonObject(with:Data(#"[{"id":"anthropic","display_name":"Anthropic","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.anthropic.com/v1","default_model":"claude-opus-5","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"ANTHROPIC_API_KEY","aliases":["claude","anthropic api"],"notes":"No public /v1/models endpoint — models curated from the bundled catalog."},{"id":"bedrock","display_name":"Amazon Bedrock","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"","default_model":"anthropic.claude-3-7-sonnet-20250219-v1:0","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"AWS_ACCESS_KEY_ID","aliases":["aws bedrock","amazon"],"notes":"Auth via AWS IAM (AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY). Reached through the Bedrock Converse API; the FERAL LLMProvider runtime currently routes Bedrock through the catalog adapter rather than the httpx OpenAI-compat path, so it ships as catalog-ready (chat available via the catalog adapter / route_call) but does not appear in SUPPORTED_RUNTIME_PROVIDERS — see findings/13-llm-core.md."},{"id":"codex","display_name":"Codex (ChatGPT sign-in)","supports_local":false,"requires_api_key":false,"configured":true,"reachable":null,"default_base_url":"","default_model":"","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"","aliases":["codex","codex oauth","chatgpt sign-in"],"notes":"Requires the Codex CLI and an existing `codex login` session."},{"id":"deepseek","display_name":"DeepSeek","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.deepseek.com/v1","default_model":"deepseek-v4-pro","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"DEEPSEEK_API_KEY","aliases":[],"notes":""},{"id":"fireworks","display_name":"Fireworks AI","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.fireworks.ai/inference/v1","default_model":"accounts/fireworks/models/llama-v3p1-70b-instruct","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"FIREWORKS_API_KEY","aliases":["fireworks ai"],"notes":""},{"id":"gemini","display_name":"Google Gemini","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://generativelanguage.googleapis.com/v1beta","default_model":"gemini-3.1-pro-preview","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"GOOGLE_API_KEY","aliases":["google","google gemini","gemini api"],"notes":""},{"id":"groq","display_name":"Groq","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.groq.com/openai/v1","default_model":"llama-3.3-70b-versatile","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"GROQ_API_KEY","aliases":["groq cloud"],"notes":""},{"id":"lmstudio","display_name":"LM Studio (local)","supports_local":true,"requires_api_key":false,"configured":true,"reachable":null,"default_base_url":"http://localhost:1234/v1","default_model":"","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"","aliases":["lm studio","lm-studio","local-lmstudio"],"notes":"LM Studio must be running with a model loaded."},{"id":"minimax","display_name":"MiniMax","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.minimax.io/v1","default_model":"MiniMax-M3","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"MINIMAX_API_KEY","aliases":["mini max"],"notes":"Model id is CamelCase — MiniMax-M3."},{"id":"mistral","display_name":"Mistral AI","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.mistral.ai/v1","default_model":"mistral-medium-2604","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"MISTRAL_API_KEY","aliases":["mistral ai","le chat"],"notes":"Model ids are date-coded (mistral-medium-2604); no rolling aliases."},{"id":"moonshot","display_name":"Moonshot (Kimi)","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.moonshot.ai/v1","default_model":"kimi-k3","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"MOONSHOT_API_KEY","aliases":["kimi","moonshot ai"],"notes":"API host is api.moonshot.ai — the docs host moved to platform.kimi.ai but the API host did not. Model ids use dots (kimi-k2.7-code)."},{"id":"ollama","display_name":"Ollama (local)","supports_local":true,"requires_api_key":false,"configured":true,"reachable":null,"default_base_url":"http://localhost:11434/v1","default_model":"","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"","aliases":["local-ollama"],"notes":""},{"id":"openai","display_name":"OpenAI","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.openai.com/v1","default_model":"gpt-5.6-sol","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"OPENAI_API_KEY","aliases":["open ai","openai api","gpt","chatgpt"],"notes":""},{"id":"openrouter","display_name":"OpenRouter","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://openrouter.ai/api/v1","default_model":"anthropic/claude-opus-5","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"OPENROUTER_API_KEY","aliases":["open router","router"],"notes":""},{"id":"qwen","display_name":"Qwen (Alibaba)","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://[{WorkspaceId}].ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1","default_model":"qwen3.7-max","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"DASHSCOPE_API_KEY","aliases":["alibaba","dashscope","tongyi"],"notes":"Base URL is workspace-scoped: {WorkspaceId} must be substituted from DASHSCOPE_WORKSPACE_ID before the client can dial."},{"id":"together","display_name":"Together AI","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.together.xyz/v1","default_model":"meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"TOGETHER_API_KEY","aliases":["together ai"],"notes":""},{"id":"xai","display_name":"xAI (Grok)","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.x.ai/v1","default_model":"grok-4.5","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"XAI_API_KEY","aliases":["grok","x.ai","x ai"],"notes":"OpenAI-compatible chat; model index is hand-curated (not pollable)."},{"id":"zai","display_name":"Z.ai (GLM)","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.z.ai/api/paas/v4/","default_model":"glm-5.2","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"ZAI_API_KEY","aliases":["z.ai","z ai","glm","zhipu"],"notes":"glm-5.2 is text-only — no vision."}]"#.utf8))) as! [[String:Any]]
    static func providers()->[[String:Any]]{
        var rows:[[String:Any]]=[["id":"ollama","display_name":"Local Ollama","supports_local":true,"requires_api_key":false,"configured":true,"reachable":NSNull(),"chat_ready":true,"default_model":"suggestion-only","default_base_url":"http://127.0.0.1:11434"],["id":"openai","display_name":"Cloud","supports_local":false,"requires_api_key":true,"configured":cloudConfigured,"reachable":NSNull(),"chat_ready":true,"default_model":"suggested-cloud-model","default_base_url":"https://api.openai.com/v1"]]
        if includeTemplate{rows=fullCatalogue.map{row in var copy=row;if row["id"] as? String=="qwen"{copy["configured"]=true};return copy}}
        return rows
    }
    static func body(_ request:URLRequest)->[String:Any]{if let data=request.httpBody{return (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] ?? [:]};guard let stream=request.httpBodyStream else{return [:]};stream.open();defer{stream.close()};var data=Data(),buffer=[UInt8](repeating:0,count:4096);while stream.hasBytesAvailable{let n=stream.read(&buffer,maxLength:buffer.count);if n<=0{break};data.append(buffer,count:n)};return (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] ?? [:]}
    override class func canInit(with request:URLRequest)->Bool{true}
    override class func canonicalRequest(for request:URLRequest)->URLRequest{request}
    override func startLoading(){
        var captured=request;if request.httpMethod=="POST"{captured.httpBody=try! JSONSerialization.data(withJSONObject:Self.body(request))};Self.requests.append(captured)
        let path=request.url!.path,body=Self.body(captured),post=request.httpMethod=="POST",value:[String:Any],code:Int
        if post && Self.rejectWrite{value=["detail":"private-provider-secret-canary"];code=503}
        else if path=="/api/llm/providers"{value=["providers":Self.providers()];code=200}
        else if path=="/api/llm/config",!post{value=Self.config;code=200}
        else if path=="/api/setup/status"{value=["setup_complete":Self.completed && !Self.denyCompletionReadback,"has_identity":true,"settings":[:]];code=200}
        else if path=="/api/llm/status"{value=["available":false,"supported":true];code=200}
        else if path=="/api/security/vault/status"{value=["state":Self.vaultReady ? "ready" : "locked","credentials_available":Self.vaultReady,"in_flight":false];code=200}
        else if path.hasSuffix("/models"){let id=path.contains("ollama") ? "ollama" : path.contains("qwen") ? "qwen" : "openai";value=["provider_id":id,"models":[["id":"cached-model"]],"source":"cache"];code=200}
        else if path=="/api/llm/config",post{Self.config["provider"]=body["provider"];Self.config["model"]=body["model"];Self.config["base_url"]=body["base_url"];Self.config["fallback_providers"]=body["fallback_providers"];value=["success":true,"provider":body["provider"]!,"model":body["model"]!,"reconfigured":["ok":true,"available":false]];code=200}
        else if path.hasSuffix("/configure"){Self.cloudConfigured=true;value=["success":true,"status":["id":"openai"],"persisted":["ok":!Self.partialPersistence,"warnings":["private-provider-secret-canary"]]];code=200}
        else if path.hasSuffix("/probe"){value=["id":path.contains("ollama") ? "ollama" : "openai","reachable":false];code=200}
        else if path=="/api/setup/complete"{Self.completed=true;value=["ok":true,"setup_complete":true];code=200}
        else{value=["detail":"private-provider-secret-canary"];code=404}
        let data=try! JSONSerialization.data(withJSONObject:value),response=HTTPURLResponse(url:Self.foreignResponse ? URL(string:"http://127.0.0.1:9465"+path)! : request.url!,statusCode:code,httpVersion:nil,headerFields:nil)!
        let deliver={self.client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed);self.client?.urlProtocol(self,didLoad:data);self.client?.urlProtocolDidFinishLoading(self)}
        if post && path=="/api/llm/config" && Self.delayActivation{DispatchQueue.global().asyncAfter(deadline:.now()+0.1,execute:deliver)}else{deliver()}
    }
    override func stopLoading(){}
    static var posts:[URLRequest]{requests.filter{$0.httpMethod=="POST"}}
}
@main struct NativeOnboardingSetupFeatureTests {
    static var count=0
    @MainActor static func check(_ condition:Bool,_ name:String){guard condition else{fatalError("FAIL: "+name)};count+=1}
    @MainActor static func reject(_ body:()throws->Void,_ name:String){do{try body();fatalError("Accepted: "+name)}catch{count+=1}}
    @MainActor static func main()async throws {
        let configuration=URLSessionConfiguration.ephemeral;configuration.protocolClasses=[SetupFixture.self]
        let model=NativeOnboardingSetupModel(session:URLSession(configuration:configuration)),base=URL(string:"http://127.0.0.1:9464")!
        model.configure(baseURL:base);await model.refresh()
        check(model.selected=="ollama" && model.model=="existing-local-model" && model.endpoint=="http://127.0.0.1:11434","saved provider/model/endpoint retained ahead of static suggestions")
        check(model.providers.count==2 && !model.vaultReady && model.runtimeAvailable==false,"cached readiness stays separate from vault and runtime")
        check(SetupFixture.posts.isEmpty && SetupFixture.requests.filter{$0.url?.path.hasSuffix("/models")==true}.allSatisfy{URLComponents(url:$0.url!,resolvingAgainstBaseURL:false)?.queryItems?.first{$0.name=="live"}?.value=="false"},"ambient setup only passive catalogue/config/cached models and no provider calls")
        model.model="explicit-new-model";let activation=try model.review(.activate)
        check(activation.explanation.contains("fallback") && activation.explanation.contains("Coding and voice") && activation.explanation.contains("contact"),"activation scope states exact endpoint/model and preserved fallback boundaries")
        check(await model.perform(activation),"reviewed activation readback matches model/endpoint")
        let body=SetupFixture.body(SetupFixture.posts.last!)
        check(body["fallback_providers"] as? [String]==["saved-fallback"] && body["model"] as? String=="explicit-new-model" && body["api_key"]==nil,"activation preserves prior fallbacks instead of adding default cloud providers")
        let before=SetupFixture.posts.count;check(!(await model.perform(activation)) && SetupFixture.posts.count==before,"activation review cannot replay")
        let stale=try model.review(.activate);model.endpoint="http://127.0.0.1:11435"
        let staleUsable=model.canUse(stale),staleResult=await model.perform(stale)
        check(!staleUsable && !staleResult && SetupFixture.posts.count==before,"draft endpoint edits invalidate previous exact review")
        model.endpoint="http://127.0.0.1:11434";let drift=try model.review(.activate);SetupFixture.config["fallback_providers"]=["changed-existing-fallback"]
        check(!(await model.perform(drift)) && SetupFixture.posts.count==before,"fresh server config drift blocks write")
        await model.refresh();let probe=try model.review(.probe)
        check(probe.explanation.contains("BACKEND-SAVED") && probe.explanation.contains("not sent"),"probe does not pretend draft endpoint/model are sent")
        check(await model.perform(probe) && model.notice?.contains("unreachable")==true,"exact provider probe receipt reports failed reachability without fake model success")
        await model.select("openai")
        reject({_ = try model.review(.saveCredential,secret:"fixture-secret")},"cloud key review blocked before existing vault authenticated")
        reject({_ = try model.review(.activate)},"cloud activation without configured key refused")
        SetupFixture.vaultReady=true;await model.refresh();let secret="fixture-private-key-DO-NOT-ECHO",key=try model.review(.saveCredential,secret:secret)
        check(!key.explanation.contains(secret),"cloud key is never included in review text")
        SetupFixture.vaultReady=false;let preKey=SetupFixture.posts.count
        check(!(await model.perform(key)) && SetupFixture.posts.count==preKey,"fresh vault preflight blocks credential write after lock")
        SetupFixture.vaultReady=true;await model.refresh();let saving=try model.review(.saveCredential,secret:secret)
        check(await model.perform(saving),"existing encrypted-vault credential route reports persistence")
        check(SetupFixture.posts.last?.url?.path=="/api/llm/providers/openai/configure" && SetupFixture.body(SetupFixture.posts.last!)["api_key"] as? String==secret && model.notice?.contains(secret)==false,"key submitted only to exact configure route and never echoed")
        let partial=try model.review(.saveCredential,secret:secret);SetupFixture.partialPersistence=true
        check(!(await model.perform(partial)) && model.error?.contains("may already have taken effect")==true && model.error?.contains("private-provider-secret-canary")==false,"partial key persistence remains explicit and private")
        SetupFixture.partialPersistence=false;await model.refresh();check(await model.perform(try model.review(.activate)),"cloud provider can activate only after configured credential")
        let completion=try model.review(.complete)
        check(completion.explanation.contains("empty body") && completion.explanation.contains("deferred") && completion.explanation.contains("Avatar"),"finish separates prior avatar from optional voice/access/device consent")
        check(await model.perform(completion) && model.setupComplete,"setup endpoint and passive readback confirm completion")
        check(SetupFixture.body(SetupFixture.posts.last!).isEmpty && SetupFixture.posts.last?.url?.path=="/api/setup/complete","completion never sends broad settings credentials identity or grants")
        SetupFixture.denyCompletionReadback=true;await model.refresh();let incomplete=try model.review(.complete)
        check(!(await model.perform(incomplete)) && !model.setupComplete,"acknowledgement cannot substitute for completion readback")
        SetupFixture.denyCompletionReadback=false;await model.refresh();SetupFixture.rejectWrite=true
        check(!(await model.perform(try model.review(.activate))) && model.error?.contains("private-provider-secret-canary")==false,"upstream failures are generic and contain no secrets")
        SetupFixture.rejectWrite=false;SetupFixture.delayActivation=true
        let late=try model.review(.activate),task=Task{await model.perform(late)}
        for _ in 0..<1000{if model.busy{break};try await Task.sleep(nanoseconds:1_000_000)}
        model.configure(baseURL:nil);_=await task.value
        check(model.providers.isEmpty && !model.setupComplete && !model.busy,"connection change revokes late setup receipt and clears private state")
        SetupFixture.delayActivation=false;SetupFixture.includeTemplate=true
        model.configure(baseURL:base);await model.refresh()
        check(model.providers.count==18 && model.providers.contains(where:{$0.id=="ollama"}) && model.error==nil,"full actual catalogue with unresolved Qwen template cannot disable valid local catalogue entries")
        let templated=model.providers.first{$0.id=="qwen"}!
        check(templated.defaultEndpoint.isEmpty && templated.endpointTemplate==SetupFixture.template && templated.raw["default_base_url"] as? String==SetupFixture.template,"workspace template retained informationally and never used as runtime default")
        await model.select("qwen")
        check(model.selected=="qwen" && model.endpoint.isEmpty,"template provider remains selectable with no silently usable endpoint")
        reject({_ = try model.review(.activate)},"configured credentials cannot imply resolved workspace endpoint")
        model.endpoint=SetupFixture.template
        reject({_ = try model.review(.activate)},"unresolved template cannot be submitted as explicit user endpoint")
        model.endpoint="https://workspace-example.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
        check(try model.review(.activate).endpoint==model.endpoint,"valid explicitly resolved endpoint can be reviewed")
        let beforeForeign=SetupFixture.posts.count
        SetupFixture.foreignResponse=true;model.configure(baseURL:nil);model.configure(baseURL:base);await model.refresh()
        check(model.providers.isEmpty && model.error != nil && SetupFixture.posts.count==beforeForeign,"foreign response URL cannot establish catalogue or write authority")
        SetupFixture.foreignResponse=false
        reject({_ = try NativeSetupWire.endpoint("https://user:secret@example.com/v1")},"endpoint embedded credentials refused")
        reject({_ = try NativeSetupWire.endpoint("https://example.com/v1?api_key=secret")},"endpoint query secret refused")
        print("Native onboarding setup: \(count) assertions passed.")
    }
}
