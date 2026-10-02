import Foundation
import SwiftUI
import CoreFoundation

struct NativeAppConfirmationFailure:LocalizedError {
    let message:String
    init(_ message:String){self.message=message}
    var errorDescription:String?{message}
}
enum NativeAppConfirmationWire {
    static func bool(_ value:Any?)->Bool?{guard let n=value as? NSNumber,CFGetTypeID(n)==CFBooleanGetTypeID() else{return nil};return n.boolValue}
    static func number(_ value:Any?)->Double?{guard let n=value as? NSNumber,CFGetTypeID(n) != CFBooleanGetTypeID(),n.doubleValue.isFinite else{return nil};return n.doubleValue}
    static func json(_ value:Any)->String{guard JSONSerialization.isValidJSONObject(value),let data=try? JSONSerialization.data(withJSONObject:value,options:[.sortedKeys,.prettyPrinted]) else{return "Unreadable confirmation"};return String(decoding:data,as:UTF8.self)}
    static func segment(_ value:String)->String{value.addingPercentEncoding(withAllowedCharacters:CharacterSet(charactersIn:"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? ""}
    static func screen(app:String,surface:String,session:String)->String{[app,surface,session].map(segment).joined(separator:":")}
    static func text(_ raw:Any?,limit:Int)->String?{guard let text=raw as? String,!text.isEmpty,text.utf8.count<=limit,!text.unicodeScalars.contains(where:{$0.value<32 || $0.value==127}) else{return nil};return text}
    static func metadata(_ payload:[String:Any],session:String,now:Double)throws->[String:Any]{
        guard let metadata=payload["confirmation"] as? [String:Any],number(metadata["contract_version"])==1,
              metadata["scope"] as? String=="app_action",metadata["session_id"] as? String==session,bool(metadata["requires_confirmation"])==true,
              let request=text(metadata["request_id"],limit:36),request.range(of:"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$",options:.regularExpression) != nil,
              let app=text(metadata["app_id"],limit:64),app.range(of:"^[a-z0-9][a-z0-9-]{2,63}$",options:.regularExpression) != nil,
              let surface=text(metadata["surface_id"],limit:256),let action=text(metadata["action_id"],limit:120),
              action.range(of:"^[A-Za-z][A-Za-z0-9_.:-]{0,119}$",options:.regularExpression) != nil,
              !["confirm_","reject_","perm_grant_","perm_deny_"].contains(where:{action.hasPrefix($0)}),
              let screenID=metadata["screen_id"] as? String,screenID==payload["screen_id"] as? String,
              screenID==screen(app:app,surface:surface,session:session),let expiry=number(metadata["expires_at"]),expiry>now,expiry<=now+3600,
              let created=number(metadata["created_at"]),created<=now+5,expiry>created,expiry-created<=300,
              let handler=text(metadata["handler"],limit:32),["navigate","close","skill_call","app_event"].contains(handler),metadata["target"] is String,
              text(metadata["event"],limit:32) != nil,metadata["value"] != nil else{throw NativeAppConfirmationFailure("This structured confirmation lacks a supported authoritative app contract; inspection only.")}
        let target=metadata["target"] as! String
        if ["close","app_event"].contains(handler),!target.isEmpty{throw NativeAppConfirmationFailure("Opaque app confirmation target; inspection only.")}
        if handler=="skill_call",target.range(of:"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",options:.regularExpression)==nil{throw NativeAppConfirmationFailure("Unsupported confirmed skill target; inspection only.")}
        if handler=="navigate",text(target,limit:256)==nil{throw NativeAppConfirmationFailure("Missing confirmed navigation target; inspection only.")}
        return metadata
    }
    static func prompt(_ root:[String:Any],request:String)throws->String{
        guard JSONSerialization.isValidJSONObject(root),let data=try? JSONSerialization.data(withJSONObject:root),data.count<=32768 else{throw NativeAppConfirmationFailure("Confirmation prompt exceeds the native inspection budget.")}
        var nodes=0,lines:[String]=[],buttons:[String]=[]
        func visit(_ node:[String:Any],depth:Int)throws{
            nodes+=1;guard nodes<=64,depth<=8 else{throw NativeAppConfirmationFailure("Confirmation prompt exceeds the node/depth budget.")}
            let type=node["type"] as? String ?? ""
            guard ["VStack","HStack","Text","Button","Divider","Spacer"].contains(type) else{throw NativeAppConfirmationFailure("Unsupported confirmation component; inspection only.")}
            if type=="Text"{
                guard let text=node["value"] as? String,text.utf8.count<=8192 else{throw NativeAppConfirmationFailure("Unreadable confirmation text.")};lines.append(text)
            }else if type=="Button"{
                guard let id=node["action_id"] as? String,["confirm_"+request,"reject_"+request].contains(id),node["value"]==nil,
                      let label=node["label"] as? String,label.utf8.count<=256,NativeAppConfirmationWire.bool(node["disabled"]) != true else{throw NativeAppConfirmationFailure("Confirmation buttons do not match the authoritative request.")}
                buttons.append(id);lines.append("Button: "+label+" ["+id+"]")
            }
            if let children=node["children"]{
                guard ["VStack","HStack"].contains(type),let children=children as? [[String:Any]] else{throw NativeAppConfirmationFailure("Unreadable confirmation children.")}
                for child in children{try visit(child,depth:depth+1)}
            }
        }
        try visit(root,depth:0)
        guard buttons.count==2,Set(buttons)==Set(["confirm_"+request,"reject_"+request]),lines.contains(where:{!$0.hasPrefix("Button: ")}),lines.joined(separator:"\n").utf8.count<=16384 else{throw NativeAppConfirmationFailure("The exact confirm/reject prompt is incomplete or duplicated.")}
        return lines.joined(separator:"\n")
    }
}
struct NativeAppConfirmationRequest:Identifiable {
    let id:String,sessionID:String,connectionID:UUID,generation:UUID
    let metadata:[String:Any]
    let prompt:String
    let signature:String
    var state="pending"
    var decision:Bool?
    var expiry:Double{NativeAppConfirmationWire.number(metadata["expires_at"]) ?? 0}
    var appID:String{metadata["app_id"] as? String ?? ""}
    var surfaceID:String{metadata["surface_id"] as? String ?? ""}
    var screenID:String{metadata["screen_id"] as? String ?? ""}
    var actionID:String{metadata["action_id"] as? String ?? ""}
}
struct NativeAppConfirmationReview:Identifiable {
    let id=UUID()
    let request:NativeAppConfirmationRequest
    let confirm:Bool
    var title:String{confirm ? "Confirm this exact app action?" : "Reject this app action?"}
    var explanation:String{
        "App \(request.appID), surface \(request.surfaceID), session \(request.sessionID).\n\nExact server-rendered prompt:\n\(request.prompt)\n\nExact original action contract and submitted value:\n\(NativeAppConfirmationWire.json(request.metadata))\n\nThis sends only the owning backend’s opaque \(confirm ? "confirm" : "reject") response. Confirmation can dispatch the declared action, including provider calls or tools under existing policy. It does not grant new folder access. An accepted dispatch receipt is not proof that a tool, purchase, navigation or model task completed. Memory, identity and persistent permissions remain shared across this brain."
    }
}
struct NativeAppConfirmationResponse {
    let sessionID:String,requestID:String,screenID:String,appID:String
    let connectionID:UUID
    let confirm:Bool
    var wireFrame:[String:Any]{["type":"ui_event","hop":"client","session_id":sessionID,"payload":["screen_id":screenID,"app_id":appID,"action_id":(confirm ? "confirm_" : "reject_")+requestID,"event":"tap"]]}
}
@MainActor final class NativeAppConfirmationModel:ObservableObject {
    @Published private(set) var requests:[NativeAppConfirmationRequest]=[]
    @Published private(set) var notices:[String]=[]
    @Published private(set) var error:String?
    private var sessionID:String?
    private var connectionID:UUID?
    private var generation=UUID()
    private var consumed:Set<UUID>=[]
    private var receipts:[String:String]=[:]
    private let now:()->Double
    init(now:@escaping()->Double={Date().timeIntervalSince1970}){self.now=now}
    func configure(sessionID:String?,connectionID:UUID?){
        guard self.sessionID != sessionID || self.connectionID != connectionID else{return}
        self.sessionID=sessionID;self.connectionID=connectionID;generation=UUID();requests=[];notices=[];error=nil;consumed=[];receipts=[:]
    }
    @discardableResult func consume(_ frame:[String:Any],connectionID:UUID)->Bool{
        guard self.connectionID==connectionID,let sid=sessionID,!sid.isEmpty,frame["session_id"] as? String==sid,
              let payload=frame["payload"] as? [String:Any],(payload["session_id"] as? String).map({$0==sid}) ?? true else{return false}
        let type=frame["type"] as? String ?? ""
        if type=="sdui"{
            // Optional Pydantic metadata is emitted as JSON null on ordinary
            // app surfaces. Only a present non-null envelope is a candidate.
            guard let confirmation=payload["confirmation"],!(confirmation is NSNull) else{return false}
            do{
                let metadata=try NativeAppConfirmationWire.metadata(payload,session:sid,now:now())
                guard JSONSerialization.isValidJSONObject(payload),let bytes=try? JSONSerialization.data(withJSONObject:payload),bytes.count<=65536 else{throw NativeAppConfirmationFailure("Authoritative confirmation payload is invalid JSON or exceeds 64 KiB; inspection only.")}
                guard let root=payload["root"] as? [String:Any] else{throw NativeAppConfirmationFailure("Missing exact confirmation prompt.")}
                let id=metadata["request_id"] as! String,prompt=try NativeAppConfirmationWire.prompt(root,request:id),signature=NativeAppConfirmationWire.json(payload)
                if let index=requests.firstIndex(where:{$0.id==id}){
                    guard requests[index].state=="pending",requests[index].signature==signature else{
                        if requests[index].state=="pending"{requests[index].state="request_changed";error="This request ID was reused with changed prompt or scope. Ask for a fresh backend confirmation."};return true
                    }
                    return true
                }
                guard requests.count<32 else{throw NativeAppConfirmationFailure("Too many app confirmations are pending; inspection only.")}
                requests.append(NativeAppConfirmationRequest(id:id,sessionID:sid,connectionID:connectionID,generation:generation,metadata:metadata,prompt:prompt,signature:signature));return true
            }catch{notices.append((error as? NativeAppConfirmationFailure)?.message ?? "Unreadable app confirmation; inspection only.");if notices.count>20{notices.removeFirst()};return true}
        }
        guard type=="confirmation_decision",let id=payload["request_id"] as? String,let index=requests.firstIndex(where:{$0.id==id && $0.generation==generation}),let status=payload["status"] as? String else{return false}
        let request=requests[index]
        guard JSONSerialization.isValidJSONObject(payload),let bytes=try? JSONSerialization.data(withJSONObject:payload),bytes.count<=65536 else{requests[index].state="receipt_mismatch";error="The app-confirmation receipt was unreadable. Dispatch may already have occurred; inspect the app session.";return true}
        let receiptSignature=NativeAppConfirmationWire.json(payload)
        if let previous=receipts[id]{
            if previous==receiptSignature{return true}
            requests[index].state="receipt_mismatch";error="Conflicting app-confirmation receipts were received. Dispatch outcome is uncertain; inspect the app session without resending.";return true
        }
        guard payload["scope"] as? String=="app_action",payload["app_id"] as? String==request.appID,payload["surface_id"] as? String==request.surfaceID,
              payload["action_id"] as? String==request.actionID,payload["screen_id"] as? String==request.screenID,
              NativeAppConfirmationWire.bool(payload["tool_outcome_verified"])==false,
              NativeAppConfirmationWire.bool(payload["dispatch_accepted"])==(status=="accepted") else{
            requests[index].state="receipt_mismatch";error="The app confirmation receipt did not match the reviewed request. Dispatch may already have occurred; do not resend this confirmation.";return true
        }
        receipts[id]=receiptSignature
        if status=="accepted"{
            guard request.decision==true,["responding","response_sent_unconfirmed"].contains(request.state) else{requests[index].state="unexpected_acceptance";error="The backend reported acceptance without this client’s reviewed confirmation. Inspect outcomes before proceeding.";return true}
            requests[index].state="accepted_dispatch"
        }else if status=="rejected"{requests[index].state="rejected_confirmed"}
        else if status=="expired"{requests[index].state="expired"}
        else if status=="error"{requests[index].state="backend_error";error="The backend could not confirm app dispatch. Details are withheld; inspect the app session before retrying."}
        else{requests[index].state="unknown_receipt";error="Unsupported app-confirmation receipt; outcome is unverified."}
        return true
    }
    func review(_ id:String,confirm:Bool)throws->NativeAppConfirmationReview{
        guard let request=requests.first(where:{$0.id==id}),request.generation==generation,request.sessionID==sessionID,request.connectionID==connectionID,
              request.state=="pending",request.expiry>now() else{throw NativeAppConfirmationFailure("This app confirmation is stale, expired or already answered.")}
        return NativeAppConfirmationReview(request:request,confirm:confirm)
    }
    func canRespond(_ review:NativeAppConfirmationReview)->Bool{
        guard let current=requests.first(where:{$0.id==review.request.id}) else{return false}
        return !consumed.contains(review.id) && current.generation==generation && current.sessionID==sessionID && current.connectionID==connectionID && current.signature==review.request.signature && current.state=="pending" && current.expiry>now()
    }
    func isLiveResponse(_ response:NativeAppConfirmationResponse)->Bool{
        requests.contains{$0.id==response.requestID && $0.sessionID==sessionID && $0.sessionID==response.sessionID && $0.connectionID==connectionID && $0.connectionID==response.connectionID && $0.generation==generation && $0.screenID==response.screenID && $0.appID==response.appID && $0.state=="responding" && $0.decision==response.confirm && $0.expiry>now()}
    }
    func respond(_ review:NativeAppConfirmationReview,send:@escaping(NativeAppConfirmationResponse)async throws->Void)async{
        guard canRespond(review),let index=requests.firstIndex(where:{$0.id==review.request.id}) else{error="This app review expired or changed. Request a fresh backend confirmation.";return}
        consumed.insert(review.id);requests[index].state="responding";requests[index].decision=review.confirm;let started=generation
        do{
            try await send(NativeAppConfirmationResponse(sessionID:review.request.sessionID,requestID:review.request.id,screenID:review.request.screenID,appID:review.request.appID,connectionID:review.request.connectionID,confirm:review.confirm))
            guard started==generation,let index=requests.firstIndex(where:{$0.id==review.request.id}) else{return}
            if requests[index].state=="responding"{requests[index].state="response_sent_unconfirmed"}
        }catch{
            guard started==generation,let index=requests.firstIndex(where:{$0.id==review.request.id}) else{return}
            if requests[index].state=="responding"{requests[index].state="delivery_uncertain";self.error="App confirmation delivery is unconfirmed. Do not resend the old response; inspect the app session first."}
        }
    }
    func report(_ message:String){error=message}
}
struct NativeAppConfirmationFeatureView:View {
    @ObservedObject var model:NativeAppConfirmationModel
    let onResponse:((NativeAppConfirmationResponse)async throws->Void)?
    @State private var review:NativeAppConfirmationReview?
    init(model:NativeAppConfirmationModel,onResponse:((NativeAppConfirmationResponse)async throws->Void)?=nil){self.model=model;self.onResponse=onResponse}
    var body:some View{
        VStack(alignment:.leading,spacing:12){
            if let error=model.error{Text(error).foregroundStyle(.red).textSelection(.enabled)}
            ForEach(Array(model.notices.enumerated()),id:\.offset){_,notice in Text(notice).font(.caption).foregroundStyle(.secondary)}
            ForEach(model.requests){request in
                VStack(alignment:.leading,spacing:8){
                    Text("App confirmation: "+request.appID).font(.headline)
                    Text(verbatim:request.prompt).textSelection(.enabled)
                    Text("Action: \(request.actionID) · \(request.state.replacingOccurrences(of:"_",with:" "))").font(.caption)
                    Text("Accepted dispatch is not verified tool or purchase completion. This does not grant folder access.").font(.caption).foregroundStyle(.secondary)
                    if request.state=="pending",onResponse != nil{
                        HStack{Button("Review confirmation…"){prepare(request.id,confirm:true)};Button("Review rejection…"){prepare(request.id,confirm:false)}}
                    }
                    DisclosureGroup("Exact app action and submitted value"){Text(NativeAppConfirmationWire.json(request.metadata)).font(.system(.caption,design:.monospaced)).textSelection(.enabled)}
                }.padding(14).frame(maxWidth:.infinity,alignment:.leading).background(Color.orange.opacity(0.07),in:RoundedRectangle(cornerRadius:12))
            }
        }
        .sheet(item:$review){item in VStack(alignment:.leading,spacing:16){
            Text(item.title).font(.title2.bold());ScrollView{Text(verbatim:item.explanation).textSelection(.enabled).frame(maxWidth:.infinity,alignment:.leading)}
            HStack{Button("Cancel"){review=nil};Spacer();Button(item.confirm ? "Send reviewed confirmation" : "Send rejection"){review=nil;Task{if let onResponse=onResponse{await model.respond(item,send:onResponse)}}}.disabled(!model.canRespond(item))}
        }.padding(24).frame(width:680,height:540)}
    }
    private func prepare(_ id:String,confirm:Bool){do{review=try model.review(id,confirm:confirm)}catch{model.report((error as? NativeAppConfirmationFailure)?.message ?? "App confirmation could not be reviewed.")}}
}
