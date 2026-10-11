import Foundation

@main struct NativeSurfaceUpdateFeatureTests {
    static var count=0
    static func check(_ condition:Bool,_ name:String){guard condition else{fatalError("FAIL: "+name)};count+=1}
    static func rejects(_ body:()throws->Void,_ name:String){do{try body();fatalError("Accepted: "+name)}catch{count+=1}}
    static let origin=URL(string:"http://127.0.0.1:9464")!,sid="app /:session",socket=UUID()
    static let manifest:[String:Any]=["app_id":"fixture-app","version":"1","entry_surface_id":"home","surfaces":[["surface_id":"home","kind":"authored","action_contract":[]],["surface_id":"details","kind":"generated","action_contract":[]]]]
    static let root:[String:Any]=["type":"VStack","children":[["type":"Text","value":"before"],["type":"Button","action_id":"opaque","label":"Disabled by host unless declared"]]]
    static func frame(_ patches:[[String:Any]],surface:String="home",owner:String=sid)->[String:Any]{["type":"sdui_patch","session_id":owner,"payload":["screen_id":NativeSurfaceWire.screen(app:"fixture-app",surface:surface,session:owner),"patches":patches]]}
    static func replacement(_ tree:[String:Any],surface:String="details")->[String:Any]{["type":"sdui","session_id":sid,"payload":["screen_id":NativeSurfaceWire.screen(app:"fixture-app",surface:surface,session:sid),"confirmation":NSNull(),"root":tree]]}
    static func main()throws {
        var reducer=try NativeSurfaceUpdateReducer(origin:origin,sessionID:sid,connectionID:socket,manifest:manifest,surfaceID:"home",root:root)
        let original=reducer.revision
        check(try reducer.consume(frame([["op":"replace","path":"/children/0/value","value":"after"]]),origin:origin,sessionID:sid,connectionID:socket),"backend slash-path replacement accepted")
        check((reducer.node.children[0].raw["value"] as? String)=="after" && reducer.revision != original,"validated output and held-review revision rotate atomically")
        check(try reducer.consume(frame([["op":"add","path":"children/-","value":["type":"Text","value":"appended"]]]),origin:origin,sessionID:sid,connectionID:socket),"actual web optional leading slash and array append")
        check(reducer.node.children.count==3,"appended component present")
        _=try reducer.consume(frame([["op":"add","path":"children/1","value":["type":"Text","value":"inserted"]],["op":"remove","path":"children/2"]]),origin:origin,sessionID:sid,connectionID:socket)
        check(reducer.node.children.count==3 && reducer.node.children[1].raw["value"] as? String=="inserted","ordered insert/remove uses current candidate indices")
        let stable=reducer.revision,stableTree=NativeSurfaceWire.json(reducer.rawRoot)
        let invalidPaths=["children.0.value","//children/0/value","children//0/value","children/01/value","children/999/value","children/~1/value","children/__proto__/value",""]
        for path in invalidPaths {rejects({_ = try reducer.consume(frame([["op":"replace","path":path,"value":"bad"]]),origin:origin,sessionID:sid,connectionID:socket)},"invalid path rejected: "+path)}
        rejects({_ = try reducer.consume(frame([["op":"replace","path":"children/0/value","value":"partial"],["op":"replace","path":"missing/value","value":"invalid"]]),origin:origin,sessionID:sid,connectionID:socket)},"one invalid patch rejects entire batch")
        check(reducer.revision==stable && NativeSurfaceWire.json(reducer.rawRoot)==stableTree,"invalid updates retain exact tree and revision")
        check(!(try reducer.consume(frame([],owner:"foreign"),origin:origin,sessionID:sid,connectionID:socket)),"outer owner mandatory")
        check(!(try reducer.consume(frame([]),origin:origin,sessionID:sid,connectionID:UUID())),"same session stale socket rejected")
        check(!(try reducer.consume(frame([]),origin:URL(string:"http://127.0.0.1:9465")!,sessionID:sid,connectionID:socket)),"exact service origin mandatory")
        check(!(try reducer.consume(frame([],surface:"details"),origin:origin,sessionID:sid,connectionID:socket)),"patch cannot navigate or affect unmounted screen")
        check(!(try reducer.consume(replacement(root,surface:"undeclared"),origin:origin,sessionID:sid,connectionID:socket)),"full tree restricted to declared app surface")
        var wrongApp=replacement(root);var payload=wrongApp["payload"] as! [String:Any];payload["app_id"]="another-app";wrongApp["payload"]=payload
        check(!(try reducer.consume(wrongApp,origin:origin,sessionID:sid,connectionID:socket)),"optional app metadata cannot contradict canonical screen")
        var confirmation=replacement(root,surface:"home");payload=confirmation["payload"] as! [String:Any];payload["confirmation"]=["request_id":"opaque"];confirmation["payload"]=payload
        check(!(try reducer.consume(confirmation,origin:origin,sessionID:sid,connectionID:socket)) && reducer.revision==stable,"authoritative confirmations excluded from surface reducer")
        let invalidValues:[Any]=[Double.infinity,Double.nan,Date()]
        for bad in invalidValues {
            rejects({_ = try reducer.consume(frame([["op":"replace","path":"children/0/value","value":bad]]),origin:origin,sessionID:sid,connectionID:socket)},"non-JSON patch value refused before writer")
        }
        rejects({_ = try reducer.consume(frame(Array(repeating:["op":"replace","path":"children/0/value","value":"x"],count:65)),origin:origin,sessionID:sid,connectionID:socket)},"patch count bounded")
        rejects({_ = try reducer.consume(frame([["op":"replace","path":"children/0/value","value":String(repeating:"x",count:66000)]]),origin:origin,sessionID:sid,connectionID:socket)},"wire budget bounded")
        var deep:[String:Any]=["type":"Text","value":"leaf"];for _ in 0..<18{deep=["type":"VStack","children":[deep]]}
        rejects({_ = try reducer.consume(replacement(deep),origin:origin,sessionID:sid,connectionID:socket,allowedNavigationTarget:"details")},"replacement depth bounded")
        check(!(try reducer.consume(replacement(root),origin:origin,sessionID:sid,connectionID:socket)),"unsolicited declared navigation requires exact host authorization")
        let unknown:[String:Any]=["type":"WebView","children":[["type":"Button","action_id":"execute","label":"Unsafe"]],"url":"javascript:alert(1)"]
        check(try reducer.consume(replacement(unknown),origin:origin,sessionID:sid,connectionID:socket,allowedNavigationTarget:"details"),"unknown component retains inspection representation")
        check(!reducer.node.supported && reducer.node.actionIDs.isEmpty && reducer.surfaceID=="details" && reducer.screenID==NativeSurfaceWire.screen(app:"fixture-app",surface:"details",session:sid),"unknown component descendants inactive and canonical declared navigation commits")
        rejects({_ = try NativeSurfaceUpdateReducer(origin:URL(string:"https://attacker.example")!,sessionID:sid,connectionID:socket,manifest:manifest,surfaceID:"home",root:root)},"remote origin cannot establish reducer")
        print("PASS NativeSurfaceUpdateFeatureTests \(count) assertions")
    }
}
