import Foundation

private final class SetupFixture:URLProtocol {
    static var requests:[URLRequest]=[]
    static var config:[String:Any]=["provider":"ollama","model":"existing-local-model","base_url":"http://127.0.0.1:11434","fallback_providers":["saved-fallback"],"configured":true]
    static var completed=false,vaultReady=false,cloudConfigured=false,rejectWrite=false,partialPersistence=false,denyCompletionReadback=false
    static var delayActivation=false,includeTemplate=false,foreignResponse=false
    static var modelRows:[Any]=["cached-model"]
    static var modelSource="cache"
    static var probeReply:[String:Any]?
    static var probeEffect:(()->Void)?
    static var customProviders:[[String:Any]]?
    static var activationError:Any?
    static let template="https://[{WorkspaceId}].ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
    // Passive 18-provider catalogue observed from the disposable backend.
    static let fullCatalogue=(try! JSONSerialization.jsonObject(with:Data(#"[{"id":"anthropic","display_name":"Anthropic","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.anthropic.com/v1","default_model":"claude-opus-5","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"ANTHROPIC_API_KEY","aliases":["claude","anthropic api"],"notes":"No public /v1/models endpoint — models curated from the bundled catalog."},{"id":"bedrock","display_name":"Amazon Bedrock","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"","default_model":"anthropic.claude-3-7-sonnet-20250219-v1:0","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"AWS_ACCESS_KEY_ID","aliases":["aws bedrock","amazon"],"notes":"Auth via AWS IAM (AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY). Reached through the Bedrock Converse API; the FERAL LLMProvider runtime currently routes Bedrock through the catalog adapter rather than the httpx OpenAI-compat path, so it ships as catalog-ready (chat available via the catalog adapter / route_call) but does not appear in SUPPORTED_RUNTIME_PROVIDERS — see findings/13-llm-core.md."},{"id":"codex","display_name":"Codex (ChatGPT sign-in)","supports_local":false,"requires_api_key":false,"configured":true,"reachable":null,"default_base_url":"","default_model":"","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"","aliases":["codex","codex oauth","chatgpt sign-in"],"notes":"Requires the Codex CLI and an existing `codex login` session."},{"id":"deepseek","display_name":"DeepSeek","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.deepseek.com/v1","default_model":"deepseek-v4-pro","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"DEEPSEEK_API_KEY","aliases":[],"notes":""},{"id":"fireworks","display_name":"Fireworks AI","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.fireworks.ai/inference/v1","default_model":"accounts/fireworks/models/llama-v3p1-70b-instruct","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"FIREWORKS_API_KEY","aliases":["fireworks ai"],"notes":""},{"id":"gemini","display_name":"Google Gemini","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://generativelanguage.googleapis.com/v1beta","default_model":"gemini-3.1-pro-preview","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"GOOGLE_API_KEY","aliases":["google","google gemini","gemini api"],"notes":""},{"id":"groq","display_name":"Groq","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.groq.com/openai/v1","default_model":"llama-3.3-70b-versatile","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"GROQ_API_KEY","aliases":["groq cloud"],"notes":""},{"id":"lmstudio","display_name":"LM Studio (local)","supports_local":true,"requires_api_key":false,"configured":true,"reachable":null,"default_base_url":"http://localhost:1234/v1","default_model":"","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"","aliases":["lm studio","lm-studio","local-lmstudio"],"notes":"LM Studio must be running with a model loaded."},{"id":"minimax","display_name":"MiniMax","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.minimax.io/v1","default_model":"MiniMax-M3","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"MINIMAX_API_KEY","aliases":["mini max"],"notes":"Model id is CamelCase — MiniMax-M3."},{"id":"mistral","display_name":"Mistral AI","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.mistral.ai/v1","default_model":"mistral-medium-2604","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"MISTRAL_API_KEY","aliases":["mistral ai","le chat"],"notes":"Model ids are date-coded (mistral-medium-2604); no rolling aliases."},{"id":"moonshot","display_name":"Moonshot (Kimi)","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.moonshot.ai/v1","default_model":"kimi-k3","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"MOONSHOT_API_KEY","aliases":["kimi","moonshot ai"],"notes":"API host is api.moonshot.ai — the docs host moved to platform.kimi.ai but the API host did not. Model ids use dots (kimi-k2.7-code)."},{"id":"ollama","display_name":"Ollama (local)","supports_local":true,"requires_api_key":false,"configured":true,"reachable":null,"default_base_url":"http://localhost:11434/v1","default_model":"","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"","aliases":["local-ollama"],"notes":""},{"id":"openai","display_name":"OpenAI","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.openai.com/v1","default_model":"gpt-5.6-sol","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"OPENAI_API_KEY","aliases":["open ai","openai api","gpt","chatgpt"],"notes":""},{"id":"openrouter","display_name":"OpenRouter","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://openrouter.ai/api/v1","default_model":"anthropic/claude-opus-5","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"OPENROUTER_API_KEY","aliases":["open router","router"],"notes":""},{"id":"qwen","display_name":"Qwen (Alibaba)","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://[{WorkspaceId}].ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1","default_model":"qwen3.7-max","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"DASHSCOPE_API_KEY","aliases":["alibaba","dashscope","tongyi"],"notes":"Base URL is workspace-scoped: {WorkspaceId} must be substituted from DASHSCOPE_WORKSPACE_ID before the client can dial."},{"id":"together","display_name":"Together AI","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.together.xyz/v1","default_model":"meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"TOGETHER_API_KEY","aliases":["together ai"],"notes":""},{"id":"xai","display_name":"xAI (Grok)","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.x.ai/v1","default_model":"grok-4.5","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"XAI_API_KEY","aliases":["grok","x.ai","x ai"],"notes":"OpenAI-compatible chat; model index is hand-curated (not pollable)."},{"id":"zai","display_name":"Z.ai (GLM)","supports_local":false,"requires_api_key":true,"configured":false,"reachable":null,"default_base_url":"https://api.z.ai/api/paas/v4/","default_model":"glm-5.2","last_refresh":0,"error":"","chat_ready":true,"stub_reason":"","credential_env_var":"ZAI_API_KEY","aliases":["z.ai","z ai","glm","zhipu"],"notes":"glm-5.2 is text-only — no vision."}]"#.utf8))) as! [[String:Any]]
    static func providers()->[[String:Any]]{
        if let customProviders{return customProviders}
        var rows:[[String:Any]]=[["id":"ollama","display_name":"Local Ollama","supports_local":true,"requires_api_key":false,"configured":true,"reachable":NSNull(),"chat_ready":true,"runtime_supported":true,"setup_selectable":true,"default_model":"suggestion-only","default_base_url":"http://127.0.0.1:11434"],["id":"openai","display_name":"Cloud","supports_local":false,"requires_api_key":true,"configured":cloudConfigured,"reachable":NSNull(),"chat_ready":true,"runtime_supported":true,"setup_selectable":true,"default_model":"suggested-cloud-model","default_base_url":"https://api.openai.com/v1"]]
        if includeTemplate{rows=fullCatalogue.map{row in var copy=row;if row["id"] as? String=="qwen"{copy["configured"]=true};let supported = !["bedrock","fireworks","together"].contains(row["id"] as? String ?? "");copy["runtime_supported"]=supported;copy["setup_selectable"]=supported;return copy}}
        return rows
    }
    // Exact fields serialized by providers.catalog.ProviderStatus.to_dict().
    static func providerStatus(reachable:Bool,error:Any? = "")->[String:Any]{
        var status:[String:Any]=["id":"ollama","display_name":"Ollama (local)","supports_local":true,"requires_api_key":false,"configured":true,"reachable":reachable,"default_base_url":"http://127.0.0.1:11434/v1","default_model":"existing-local-model","last_refresh":1.0,"chat_ready":true,"stub_reason":""]
        if let error{status["error"]=error};return status
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
        else if path.hasSuffix("/models"){let id=path.contains("ollama") ? "ollama" : path.contains("qwen") ? "qwen" : "openai";value=["provider_id":id,"models":Self.modelRows,"source":Self.modelSource];code=200}
        else if path=="/api/llm/config",post{Self.config["provider"]=body["provider"];Self.config["model"]=body["model"];Self.config["base_url"]=body["base_url"];Self.config["fallback_providers"]=body["fallback_providers"];var receipt:[String:Any]=["success":true,"provider":body["provider"]!,"model":body["model"]!,"reconfigured":["ok":true,"available":false]];if let error=Self.activationError{receipt["error"]=error};value=receipt;code=200}
        else if path.hasSuffix("/configure"){Self.cloudConfigured=true;value=["success":true,"status":["id":"openai"],"persisted":["ok":!Self.partialPersistence,"warnings":["private-provider-secret-canary"]]];code=200}
        else if path.hasSuffix("/probe"){value=Self.probeReply ?? ["id":path.contains("ollama") ? "ollama" : "openai","reachable":false];Self.probeEffect?();code=200}
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
        check(model.models==["cached-model"] && model.error==nil,"actual backend string model list loads without replacing manual active model")
        check(try NativeSetupWire.models(["models":[["id":"legacy"],["id":"legacy"]],"source":"cache"],provider:"ollama").ids==["legacy"],"homogeneous historical object rows stay compatible")
        for source in ["live","cache","fallback"]{check(try NativeSetupWire.models(["models":[],"source":source],provider:"ollama").ids.isEmpty,"empty \(source) model catalogue remains normal")}
        let invalidModelLists:[[String:Any]]=[
            ["models":["good",["id":"legacy"]],"source":"cache"],
            ["models":[["id":"good"],[:]],"source":"cache"],
            ["models":[["id":1]],"source":"cache"],
            ["models":[""],"source":"cache"],
            ["models":["bad model"],"source":"cache"],
            ["models":["bad\nmodel"],"source":"cache"],
            ["models":[String(repeating:"x",count:257)],"source":"cache"],
            ["models":[1],"source":"cache"],
            ["models":["good"],"source":"unknown"],
            ["models":["good"],"source":"cache","provider_id":"openai"],
            ["models":["good"],"source":"cache","warning":true],
            ["models":Array(repeating:"good",count:2001),"source":"cache"]]
        for value in invalidModelLists{reject({_ = try NativeSetupWire.models(value,provider:"ollama")},"malformed model payload fails as a whole")}
        SetupFixture.modelRows=["good",["id":"legacy"]]
        let malformedModel=NativeOnboardingSetupModel(session:URLSession(configuration:configuration));malformedModel.configure(baseURL:base);await malformedModel.refresh()
        check(malformedModel.error != nil && malformedModel.models.isEmpty && SetupFixture.posts.isEmpty,"actual mixed-response refresh refuses before any setup write")
        SetupFixture.modelRows=["cached-model"]
        check(model.providers.count==2 && !model.vaultReady && model.runtimeAvailable==false,"cached readiness stays separate from vault and runtime")
        check(SetupFixture.posts.isEmpty && SetupFixture.requests.filter{$0.url?.path.hasSuffix("/models")==true}.allSatisfy{isPassiveChatSuggestions($0)},"ambient setup only passive recommended chat suggestions and no provider calls")
        for (source,label) in [("cache","Cached; no live request"),("fallback","Bundled fallback; no live request"),("live","Previously discovered; no live request")] {
            SetupFixture.modelSource=source;await model.refresh()
            check(model.modelsSource==label && model.model=="existing-local-model","passive refresh labels \(source) provenance without claiming live discovery or changing active model")
        }
        SetupFixture.modelSource="cache"
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
        check(model.lastProbeReachable==false && model.providers.first(where:{$0.id=="ollama"})?.reachable==nil && model.probeStatusText.contains("Last explicit probe: unreachable"),"explicit negative probe remains visible despite actual passive null readback")
        let passivePosts=SetupFixture.posts.count;await model.refresh()
        check(model.lastProbeReachable==false && SetupFixture.posts.count==passivePosts,"unchanged passive refresh keeps historical result without another provider call")
        model.endpoint += "/changed"
        check(model.lastProbeReachable==nil && model.probeStatusText=="No probe result available in this view.","endpoint draft edit invalidates historical probe without overwriting catalogue")
        model.endpoint=SetupFixture.config["base_url"] as! String
        for statusError:Any? in ["", NSNull(), nil] {
            SetupFixture.probeReply=SetupFixture.providerStatus(reachable:true,error:statusError)
            let requestCount=SetupFixture.posts.count
            check(await model.perform(try model.review(.probe)) && model.error==nil && model.notice?.contains("saved provider reachable")==true && model.notice?.contains("still unverified")==true,"actual ProviderStatus positive accepts empty/null/omitted error without claiming inference")
            check(model.lastProbeReachable==true && model.probeStatusText.contains("Model inference is not verified"),"positive historical probe never certifies inference")
            check(SetupFixture.posts.count==requestCount+1 && SetupFixture.posts.last?.url?.path=="/api/llm/providers/ollama/probe" && SetupFixture.body(SetupFixture.posts.last!).isEmpty,"probe only submits explicit saved-provider request with empty body")
        }
        SetupFixture.probeReply=SetupFixture.providerStatus(reachable:false,error:"private-provider-secret-canary")
        check(await model.perform(try model.review(.probe)) && model.notice?.contains("unreachable")==true && model.error==nil && model.notice?.contains("private-provider-secret-canary")==false,"actual negative ProviderStatus is an acknowledged unreachable outcome with private details withheld")
        let exactConfig=SetupFixture.config
        SetupFixture.probeEffect={SetupFixture.config["base_url"]="http://127.0.0.1:1/v1"}
        check(!(await model.perform(try model.review(.probe))) && model.lastProbeReachable==nil && model.notice==nil,"configuration replacement during an acknowledged probe never publishes stale historical result")
        SetupFixture.probeEffect=nil;SetupFixture.config=exactConfig;await model.refresh()
        let originalRows=SetupFixture.providers()
        SetupFixture.probeEffect={SetupFixture.customProviders=originalRows.map{row in var value=row;if value["id"] as? String=="ollama"{value["runtime_supported"]=false};return value}}
        check(!(await model.perform(try model.review(.probe))) && model.lastProbeReachable==nil,"runtime capability replacement during probe invalidates scope")
        SetupFixture.probeEffect=nil;SetupFixture.customProviders=nil;await model.refresh()
        SetupFixture.probeEffect={SetupFixture.customProviders=originalRows.map{row in var value=row;if value["id"] as? String=="ollama"{value["last_refresh"]=123.0;value["default_model"]="new-discovered-suggestion"};return value}}
        check(await model.perform(try model.review(.probe)) && model.lastProbeReachable==false,"probe-produced discovery timestamp/suggestion updates do not invalidate their own historical receipt")
        SetupFixture.probeEffect=nil;SetupFixture.customProviders=nil;await model.refresh()
        check(model.lastProbeReachable==false,"passive suggestion changes preserve historical evidence")
        model.model += "-draft";check(model.lastProbeReachable==nil,"model draft edit invalidates historical status");model.model=exactConfig["model"] as! String
        var invalidProbes:[[String:Any]]=[]
        for badError:Any in [1,["private-provider-secret-canary"],["detail":"private-provider-secret-canary"],String(repeating:"x",count:4097)] {
            invalidProbes.append(SetupFixture.providerStatus(reachable:false,error:badError))
        }
        invalidProbes.append(SetupFixture.providerStatus(reachable:true,error:"private-provider-secret-canary"))
        for (field,badValue): (String,Any) in [("reachable",1),("reachable",NSNull()),("id","openai"),("id",1),("provider_id","openai")] {
            var reply=SetupFixture.providerStatus(reachable:true);reply[field]=badValue;invalidProbes.append(reply)
        }
        var missingIdentity=SetupFixture.providerStatus(reachable:true);missingIdentity.removeValue(forKey:"id");invalidProbes.append(missingIdentity)
        for reply in invalidProbes {
            SetupFixture.probeReply=reply
            check(!(await model.perform(try model.review(.probe))) && model.lastProbeReachable==nil && model.error != nil && model.notice==nil && model.error?.contains("private-provider-secret-canary")==false,"malformed or contradictory probe cannot establish reachability or expose details")
        }
        SetupFixture.probeReply=nil
        for unrelatedError:Any in ["",NSNull(),"private-provider-secret-canary"] {
            SetupFixture.activationError=unrelatedError
            check(!(await model.perform(try model.review(.activate))) && model.error != nil && model.error?.contains("private-provider-secret-canary")==false,"probe-specific status compatibility never loosens activation error refusal")
        }
        SetupFixture.activationError=nil
        await model.select("openai")
        check(SetupFixture.requests.last.map{isPassiveChatSuggestions($0) && $0.url?.path=="/api/llm/providers/openai/models"}==true,"provider selection reads passive chat suggestions with canonical filters")
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
        check(model.providers.filter{$0.automaticChoice}.count==15 && model.providers.first{$0.id=="moonshot"}?.automaticChoice==true,"actual alias-aware capability flags admit supported providers and separate three catalogue-only entries")
        let templated=model.providers.first{$0.id=="qwen"}!
        check(templated.defaultEndpoint.isEmpty && templated.endpointTemplate==SetupFixture.template && templated.raw["default_base_url"] as? String==SetupFixture.template,"workspace template retained informationally and never used as runtime default")
        await model.select("qwen")
        check(model.selected=="qwen" && model.endpoint.isEmpty,"template provider remains selectable with no silently usable endpoint")
        reject({_ = try model.review(.activate)},"configured credentials cannot imply resolved workspace endpoint")
        model.endpoint=SetupFixture.template
        reject({_ = try model.review(.activate)},"unresolved template cannot be submitted as explicit user endpoint")
        model.endpoint="https://workspace-example.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
        check(try model.review(.activate).endpoint==model.endpoint,"valid explicitly resolved endpoint can be reviewed")
        let gateway:[String:Any]=["id":"catalog-only","display_name":"Custom gateway","supports_local":false,"requires_api_key":false,"configured":true,"reachable":NSNull(),"chat_ready":true,"default_model":"catalog-model","default_base_url":"https://catalog.example/v1","runtime_supported":false,"setup_selectable":false]
        SetupFixture.customProviders=[gateway]
        SetupFixture.config=["provider":"catalog-only","model":"saved-model","base_url":"https://user.example/v1","fallback_providers":[],"configured":true]
        model.configure(baseURL:nil);model.configure(baseURL:base);await model.refresh()
        check(model.selected=="catalog-only" && model.endpoint=="https://user.example/v1" && model.providers.first?.automaticChoice==false,"saved custom gateway preserves explicit endpoint without promoting adapter support")
        check(try model.review(.activate).endpoint=="https://user.example/v1","saved manual gateway remains explicitly reviewable")
        model.endpoint="";reject({_ = try model.review(.activate)},"catalogue chat readiness cannot authorize unsupported default endpoint")
        check(model.providers.first?.defaultEndpoint.isEmpty==true,"unsupported catalogue default URL cannot masquerade as usable setup default")
        for flag in ["runtime_supported","setup_selectable"]{for malformed:Any in [NSNull(),1,"true"]{var row=gateway;row[flag]=malformed;reject({_ = try NativeSetupWire.providers(["providers":[row]])},"selection capability flag must be an exact boolean")}}
        var legacy=gateway;legacy.removeValue(forKey:"runtime_supported");legacy.removeValue(forKey:"setup_selectable")
        let unknown=try NativeSetupWire.providers(["providers":[legacy]])[0]
        check(!unknown.automaticChoice && unknown.defaultEndpoint.isEmpty,"legacy missing capabilities stay unknown and manual only")
        SetupFixture.customProviders=nil
        let beforeForeign=SetupFixture.posts.count
        SetupFixture.foreignResponse=true;model.configure(baseURL:nil);model.configure(baseURL:base);await model.refresh()
        check(model.providers.isEmpty && model.error != nil && SetupFixture.posts.count==beforeForeign,"foreign response URL cannot establish catalogue or write authority")
        SetupFixture.foreignResponse=false
        reject({_ = try NativeSetupWire.endpoint("https://user:secret@example.com/v1")},"endpoint embedded credentials refused")
        reject({_ = try NativeSetupWire.endpoint("https://example.com/v1?api_key=secret")},"endpoint query secret refused")
        print("Native onboarding setup: \(count) assertions passed.")
    }
    static func isPassiveChatSuggestions(_ request:URLRequest)->Bool {
        guard request.httpMethod=="GET",let url=request.url,url.path.hasSuffix("/models"),let query=URLComponents(url:url,resolvingAgainstBaseURL:false)?.queryItems else{return false}
        return query.count==3 && query.first{$0.name=="live"}?.value=="false" && query.first{$0.name=="recommended"}?.value=="true" && query.first{$0.name=="model_class"}?.value=="chat"
    }
}
