import Foundation

@main struct NativeAppConfirmationFeatureTests {
    static var count=0
    static let requestID="8fbc0fca-e54c-4a03-b954-4cfab7b53554"
    static let secondID="28d3a1bc-dede-443a-8337-4cfab7b53554"
    static let sid="native-apps-fixture"
    @MainActor static func check(_ value:Bool,_ message:String){guard value else{fatalError("FAIL: "+message)};count+=1}
    @MainActor static func refuses(_ action:()throws->Void,_ message:String){do{try action();fatalError("Accepted: "+message)}catch{count+=1}}
    static func request(_ id:String=requestID,owner:String=sid,metadataChanges:[String:Any]=[:],rootOverride:[String:Any]?=nil)->[String:Any]{
        let screen=NativeAppConfirmationWire.screen(app:"fixture-app",surface:"home",session:owner)
        var metadata:[String:Any]=["contract_version":1,"request_id":id,"session_id":owner,"app_id":"fixture-app","surface_id":"home","action_id":"purchase_review","screen_id":screen,"scope":"app_action","created_at":1000.0,"expires_at":1300.0,"handler":"app_event","target":"","event":"tap","value":["item":"Explicit fixture item","amount":12],"requires_confirmation":true]
        for(key,value)in metadataChanges{metadata[key]=value}
        let root:[String:Any]=rootOverride ?? ["type":"VStack","children":[["type":"Text","value":"Confirm action"],["type":"Text","value":"fixture-app requested purchase_review. Confirm to continue."],["type":"HStack","children":[["type":"Button","action_id":"confirm_"+id,"label":"Confirm"],["type":"Button","action_id":"reject_"+id,"label":"Cancel"]]]]]
        return ["type":"sdui","session_id":owner,"payload":["screen_id":screen,"root":root,"confirmation":metadata]]
    }
    static func receipt(_ status:String="accepted",id:String=requestID,changes:[String:Any]=[:],owner:String=sid)->[String:Any]{
        var payload:[String:Any]=["request_id":id,"status":status,"app_id":"fixture-app","surface_id":"home","action_id":"purchase_review","screen_id":NativeAppConfirmationWire.screen(app:"fixture-app",surface:"home",session:owner),"scope":"app_action","dispatch_accepted":status=="accepted","tool_outcome_verified":false]
        for(key,value)in changes{payload[key]=value};return ["type":"confirmation_decision","session_id":owner,"payload":payload]
    }
    @MainActor static func main()async throws{
        var clock=1000.0
        let socket=UUID(),model=NativeAppConfirmationModel(now:{clock})
        check(!model.consume(request(),connectionID:socket) && model.requests.isEmpty,"disconnected parser cannot acquire confirmation authority")
        model.configure(sessionID:sid,connectionID:socket)
        check(!model.consume(request(owner:"foreign"),connectionID:socket) && !model.consume(request(),connectionID:UUID()),"foreign session and stale socket ignored")
        check(model.consume(request(),connectionID:socket) && model.requests.count==1,"typed payload-level server confirmation parsed")
        let review=try model.review(requestID,confirm:true)
        check(review.explanation.contains("Explicit fixture item") && review.explanation.contains("amount") && review.explanation.contains("confirm_"+requestID) && review.explanation.contains("not proof"),"exact rendered prompt opaque ID and original value reviewed without completion claim")
        var sent:NativeAppConfirmationResponse?
        await model.respond(review){response in check(model.isLiveResponse(response),"response matches live model before owning socket sends");sent=response}
        let payload=sent!.wireFrame["payload"] as! [String:Any]
        check(sent?.sessionID==sid && sent?.connectionID==socket && sent?.requestID==requestID && payload["screen_id"] as? String==model.requests[0].screenID && payload["app_id"] as? String=="fixture-app","exact session socket request and screen response identity")
        check(payload["action_id"] as? String=="confirm_"+requestID && payload["value"]==nil && payload["event"] as? String=="tap","opaque confirm response never resubmits or rewrites original value")
        check(model.requests[0].state=="response_sent_unconfirmed","successful transport send remains unconfirmed")
        var duplicate=0;await model.respond(review){_ in duplicate+=1}
        check(duplicate==0 && !model.canRespond(review),"sent review cannot replay")
        check(model.consume(receipt(),connectionID:socket) && model.requests[0].state=="accepted_dispatch","matching backend acknowledgement means accepted dispatch only")
        check(model.consume(receipt(),connectionID:socket) && model.requests[0].state=="accepted_dispatch","duplicate matching receipt is idempotent and does not reopen authority")
        check(model.consume(request(),connectionID:socket) && model.requests.count==1 && model.requests[0].state=="accepted_dispatch","replayed request does not recreate answered authority")
        let rejection=NativeAppConfirmationModel(now:{1000});rejection.configure(sessionID:sid,connectionID:socket);_=rejection.consume(request(),connectionID:socket)
        let rejected=try rejection.review(requestID,confirm:false)
        await rejection.respond(rejected){response in check((response.wireFrame["payload"] as! [String:Any])["action_id"] as? String=="reject_"+requestID,"rejection uses real opaque owning backend ID");_=rejection.consume(receipt("rejected"),connectionID:socket)}
        check(rejection.requests[0].state=="rejected_confirmed","early authoritative rejection preserved across await")
        let early=NativeAppConfirmationModel(now:{1000});early.configure(sessionID:sid,connectionID:socket);_=early.consume(request(),connectionID:socket)
        await early.respond(try early.review(requestID,confirm:true)){_ in _=early.consume(receipt(),connectionID:socket)}
        check(early.requests[0].state=="accepted_dispatch","early accepted receipt not replaced with sent-unconfirmed")
        let unexpected=NativeAppConfirmationModel(now:{1000});unexpected.configure(sessionID:sid,connectionID:socket);_=unexpected.consume(request(),connectionID:socket)
        _=unexpected.consume(receipt(),connectionID:socket)
        check(unexpected.requests[0].state=="unexpected_acceptance","unprompted acceptance not represented as this client approval")
        let stale=NativeAppConfirmationModel(now:{clock});stale.configure(sessionID:sid,connectionID:socket);_=stale.consume(request(),connectionID:socket);let held=try stale.review(requestID,confirm:true)
        clock=1301;var writes=0;await stale.respond(held){_ in writes+=1}
        check(writes==0 && !stale.canRespond(held),"expiry between review and send blocks response")
        clock=1000
        let changed=NativeAppConfirmationModel(now:{clock});changed.configure(sessionID:sid,connectionID:socket);_=changed.consume(request(),connectionID:socket);let first=try changed.review(requestID,confirm:true)
        _=changed.consume(request(metadataChanges:["value":["amount":999]]),connectionID:socket)
        await changed.respond(first){_ in writes+=1}
        check(writes==0 && changed.requests[0].state=="request_changed","same ID changed data invalidates held review")
        let disconnected=NativeAppConfirmationModel(now:{clock});disconnected.configure(sessionID:sid,connectionID:socket);_=disconnected.consume(request(),connectionID:socket);let previous=try disconnected.review(requestID,confirm:true)
        disconnected.configure(sessionID:nil,connectionID:nil);await disconnected.respond(previous){_ in writes+=1}
        check(writes==0 && disconnected.requests.isEmpty,"disconnect revokes all approval authority")
        let replaced=NativeAppConfirmationModel(now:{clock});replaced.configure(sessionID:sid,connectionID:socket);_=replaced.consume(request(),connectionID:socket);let oldSocket=try replaced.review(requestID,confirm:false)
        replaced.configure(sessionID:sid,connectionID:UUID());await replaced.respond(oldSocket){_ in writes+=1}
        check(writes==0 && !replaced.consume(receipt(),connectionID:socket),"same session reconnect still invalidates old socket reviews and receipts")
        let uncertain=NativeAppConfirmationModel(now:{clock});uncertain.configure(sessionID:sid,connectionID:socket);_=uncertain.consume(request(),connectionID:socket);let failed=try uncertain.review(requestID,confirm:true)
        await uncertain.respond(failed){_ in throw NativeAppConfirmationFailure("private-server-canary")}
        check(uncertain.requests[0].state=="delivery_uncertain" && uncertain.error?.contains("private-server-canary")==false && !uncertain.canRespond(failed),"uncertain transport cannot echo secrets or allow retries")
        let mismatch=NativeAppConfirmationModel(now:{clock});mismatch.configure(sessionID:sid,connectionID:socket);_=mismatch.consume(request(),connectionID:socket)
        await mismatch.respond(try mismatch.review(requestID,confirm:true)){_ in _=mismatch.consume(receipt(changes:["action_id":"another_action"]),connectionID:socket)}
        check(mismatch.requests[0].state=="receipt_mismatch" && mismatch.error?.contains("already have occurred")==true,"wrong action receipt retains possible dispatch uncertainty")
        let falseProof=NativeAppConfirmationModel(now:{clock});falseProof.configure(sessionID:sid,connectionID:socket);_=falseProof.consume(request(),connectionID:socket)
        await falseProof.respond(try falseProof.review(requestID,confirm:true)){_ in _=falseProof.consume(receipt(changes:["tool_outcome_verified":true]),connectionID:socket)}
        check(falseProof.requests[0].state=="receipt_mismatch","confirmation cannot fabricate verified tool outcome")
        let service=NativeAppConfirmationModel(now:{clock});service.configure(sessionID:sid,connectionID:socket);_=service.consume(request(),connectionID:socket)
        await service.respond(try service.review(requestID,confirm:true)){_ in _=service.consume(receipt("error",changes:["error":"private-server-canary"]),connectionID:socket)}
        check(service.requests[0].state=="backend_error" && service.error?.contains("private-server-canary")==false,"negative backend receipt is explicit and private details withheld")
        let invalid=NativeAppConfirmationModel(now:{clock});invalid.configure(sessionID:sid,connectionID:socket)
        let invalidMetadata:[[String:Any]]=[["contract_version":2],["session_id":"foreign"],["scope":"folder_grant"],["expires_at":NSNull()],["created_at":0],["expires_at":Double.infinity],["request_id":"short-id"],["handler":"patch"],["target":"https://attacker.example"],["action_id":"perm_grant_fake"]]
        for changes in invalidMetadata{
            _=invalid.consume(request(metadataChanges:changes),connectionID:socket)
        }
        check(invalid.requests.isEmpty && !invalid.notices.isEmpty,"unsupported version owner scope expiry IDs handlers and URLs never create authority")
        let nonJSONChanges:[[String:Any]]=[["created_at":Double.nan],["value":["nested":["amount":Double.infinity]]],["value":["date":Date()]]]
        for changes in nonJSONChanges {
            let malformed=NativeAppConfirmationModel(now:{1000});malformed.configure(sessionID:sid,connectionID:socket)
            _=malformed.consume(request(metadataChanges:changes),connectionID:socket)
            check(malformed.requests.isEmpty && !malformed.notices.isEmpty,"nonfinite timestamp or non-JSON submitted value is rejected before serialization")
        }
        check(NativeAppConfirmationWire.json(["bad":Double.nan])=="Unreadable confirmation" && NativeAppConfirmationWire.json(["bad":Date()])=="Unreadable confirmation","non-JSON review text never invokes unsafe writer")
        refuses({_ = try NativeAppConfirmationWire.prompt(["type":"Text","value":"Safe","extra":Double.infinity],request:requestID)},"nonfinite prompt metadata rejected before writer")
        let invalidReceipt=NativeAppConfirmationModel(now:{1000});invalidReceipt.configure(sessionID:sid,connectionID:socket);_=invalidReceipt.consume(request(),connectionID:socket)
        await invalidReceipt.respond(try invalidReceipt.review(requestID,confirm:true)){_ in _=invalidReceipt.consume(receipt(changes:["extra":["amount":Double.infinity]]),connectionID:socket)}
        check(invalidReceipt.requests[0].state=="receipt_mismatch","nonfinite receipt safely preserves uncertain dispatch without claiming acceptance")
        var ordinary=request();var ordinaryPayload=ordinary["payload"] as! [String:Any];let metadata=ordinaryPayload.removeValue(forKey:"confirmation")!;var ordinaryRoot=ordinaryPayload["root"] as! [String:Any];ordinaryRoot["confirmation"]=metadata;ordinaryPayload["root"]=ordinaryRoot;ordinary["payload"]=ordinaryPayload
        check(!invalid.consume(ordinary,connectionID:socket) && invalid.requests.isEmpty,"app-authored root metadata cannot masquerade as server confirmation")
        var ordinaryNull=ordinary;var nullPayload=ordinaryNull["payload"] as! [String:Any];nullPayload["confirmation"]=NSNull();ordinaryNull["payload"]=nullPayload
        let noticesBeforeNull=invalid.notices.count
        check(!invalid.consume(ordinaryNull,connectionID:socket) && invalid.requests.isEmpty && invalid.notices.count==noticesBeforeNull,"ordinary Pydantic confirmation null creates neither authority nor false inspection warning")
        var malformed=ordinaryNull;nullPayload["confirmation"]="malformed";malformed["payload"]=nullPayload
        check(invalid.consume(malformed,connectionID:socket) && invalid.requests.isEmpty && invalid.notices.count>noticesBeforeNull,"present non-null malformed confirmation remains inspection only")
        let spoof:[String:Any]=["type":"VStack","children":[["type":"Text","value":"Approve"],["type":"Button","action_id":"confirm_"+requestID,"label":"Confirm"],["type":"Button","action_id":"reject_other-id","label":"Cancel"]]]
        _=invalid.consume(request(rootOverride:spoof),connectionID:socket)
        check(invalid.requests.isEmpty,"mismatched confirm reject pair remains inspection only")
        let web:[String:Any]=["type":"WebView","url":"javascript:alert(1)"]
        _=invalid.consume(request(rootOverride:web),connectionID:socket)
        check(invalid.requests.isEmpty,"web components cannot be authoritative confirmation UI")
        var deep:[String:Any]=["type":"Text","value":"leaf"]
        for _ in 0..<10{deep=["type":"VStack","children":[deep]]}
        refuses({_ = try NativeAppConfirmationWire.prompt(deep,request:requestID)},"confirmation depth budget")
        refuses({_ = try NativeAppConfirmationWire.prompt(["type":"VStack","children":Array(repeating:["type":"Text","value":"node"],count:65)],request:requestID)},"confirmation node budget")
        refuses({_ = try NativeAppConfirmationWire.prompt(["type":"Text","value":String(repeating:"中",count:12000)],request:requestID)},"UTF8 prompt budget")
        let pair=NativeAppConfirmationModel(now:{clock});pair.configure(sessionID:sid,connectionID:socket);_=pair.consume(request(),connectionID:socket);_=pair.consume(request(secondID),connectionID:socket)
        let approved=try pair.review(requestID,confirm:true);await pair.respond(approved){_ in _=pair.consume(receipt(id:secondID),connectionID:socket)}
        check(pair.requests[0].state=="response_sent_unconfirmed" && pair.requests[1].state=="unexpected_acceptance","receipt for another request cannot confirm reviewed action")
        print("Native app confirmations: \(count) fixture assertions passed; no real confirmation or tool execution.")
    }
}
