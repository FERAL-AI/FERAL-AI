import Foundation

/// Pure receiving-side reducer. The host must reconstruct it after any
/// service, socket, session or reviewed manifest change and invalidate held
/// action reviews whenever revision changes. It never dispatches UI actions.
struct NativeSurfaceUpdateReducer {
    let origin:URL
    let sessionID:String
    let connectionID:UUID
    let appID:String
    let manifest:[String:Any]
    private(set) var surfaceID:String
    private(set) var screenID:String
    private(set) var rawRoot:[String:Any]
    private(set) var node:NativeSurfaceNode
    private(set) var revision=UUID()

    init(origin:URL,sessionID:String,connectionID:UUID,manifest:[String:Any],surfaceID:String,root:[String:Any])throws {
        guard origin.scheme=="http",["127.0.0.1","::1","[::1]"].contains(origin.host ?? ""),origin.user==nil,origin.password==nil,
              !sessionID.isEmpty,sessionID.utf8.count<=256,let app=NativeSurfaceWire.identifier(manifest["app_id"]),JSONSerialization.isValidJSONObject(manifest) else{
            throw NativeSurfaceFailure("Surface updates require a verified local origin, owning session and manifest.")
        }
        _=try NativeSurfaceWire.manifest(["app_id":app,"version":manifest["version"] ?? NSNull(),"manifest":manifest],app:app)
        guard (manifest["surfaces"] as? [[String:Any]])?.contains(where:{$0["surface_id"] as? String==surfaceID})==true else{throw NativeSurfaceFailure("The mounted surface is not declared by this app.")}
        self.origin=origin;self.sessionID=sessionID;self.connectionID=connectionID;self.appID=app;self.manifest=manifest
        self.surfaceID=surfaceID;self.screenID=NativeSurfaceWire.screen(app:app,surface:surfaceID,session:sessionID)
        self.rawRoot=root;self.node=try NativeSurfaceWire.tree(root)
    }

    /// Returns false for unrelated frames and authoritative confirmations.
    /// A malformed related update throws with all mounted state unchanged.
    @discardableResult mutating func consume(_ frame:[String:Any],origin:URL,sessionID:String,connectionID:UUID,allowedNavigationTarget:String?=nil)throws->Bool {
        guard origin==self.origin,sessionID==self.sessionID,connectionID==self.connectionID,
              frame["session_id"] as? String==self.sessionID,let payload=frame["payload"] as? [String:Any],
              (payload["session_id"] as? String).map({$0==self.sessionID}) ?? true else{return false}
        let type=frame["type"] as? String ?? ""
        guard type=="sdui" || type=="sdui_patch" else{return false}
        if let confirmation=payload["confirmation"],!(confirmation is NSNull){return false}
        guard let incomingScreen=payload["screen_id"] as? String else{return false}
        let declared=(manifest["surfaces"] as? [[String:Any]] ?? []).compactMap{$0["surface_id"] as? String}
        guard let target=declared.first(where:{NativeSurfaceWire.screen(app:appID,surface:$0,session:self.sessionID)==incomingScreen}),
              (payload["app_id"] as? String).map({$0==appID}) ?? true,
              (payload["surface_id"] as? String).map({$0==target}) ?? true else{return false}
        if incomingScreen != screenID {
            guard type=="sdui",target==allowedNavigationTarget else{return false}
        }
        guard JSONSerialization.isValidJSONObject(frame),let data=try? JSONSerialization.data(withJSONObject:frame),data.count<=65536 else{throw NativeSurfaceFailure("Surface update is invalid JSON or exceeds 64 KiB; the mounted surface was retained.")}
        var candidate:[String:Any]
        if type=="sdui" {
            guard let root=payload["root"] as? [String:Any] else{throw NativeSurfaceFailure("Surface update has no native component tree.")}
            candidate=root
        }else{
            guard let patches=payload["patches"] as? [[String:Any]],!patches.isEmpty,patches.count<=64 else{throw NativeSurfaceFailure("Surface patch count is unsupported; the mounted surface was retained.")}
            var value:Any=rawRoot
            for patch in patches {
                guard let op=patch["op"] as? String,["replace","add","remove"].contains(op),let path=patch["path"] as? String else{throw NativeSurfaceFailure("Surface patch operation is unsupported.")}
                let segments=try Self.path(path)
                guard op=="remove" || patch["value"] != nil else{throw NativeSurfaceFailure("Surface patch value is missing.")}
                value=try Self.apply(value,path:ArraySlice(segments),op:op,value:patch["value"])
            }
            guard let root=value as? [String:Any] else{throw NativeSurfaceFailure("Surface patches must retain an object root.")};candidate=root
        }
        let rendered=try NativeSurfaceWire.tree(candidate)
        // Commit only after every patch and the whole native tree pass. This
        // rotates even for duplicate/no-op frames: no server sequence exists.
        rawRoot=candidate;node=rendered;surfaceID=target;screenID=incomingScreen;revision=UUID()
        return true
    }
    private static func path(_ raw:String)throws->[String] {
        guard !raw.isEmpty,raw.utf8.count<=1024 else{throw NativeSurfaceFailure("Surface patch path is missing or too long.")}
        let path=raw.hasPrefix("/") ? String(raw.dropFirst()) : raw
        let segments=path.split(separator:"/",omittingEmptySubsequences:false).map(String.init)
        guard !segments.isEmpty,segments.count<=16,segments.allSatisfy({!$0.isEmpty && $0.range(of:"^[A-Za-z0-9_-]+$",options:.regularExpression) != nil && !["__proto__","prototype","constructor"].contains($0)}) else{throw NativeSurfaceFailure("Surface patches support slash-separated literal field paths only.")}
        return segments
    }
    private static func apply(_ current:Any,path:ArraySlice<String>,op:String,value:Any?)throws->Any {
        guard let key=path.first else{throw NativeSurfaceFailure("Whole-root patches are unsupported.")}
        let remaining=path.dropFirst()
        if var object=current as? [String:Any] {
            if remaining.isEmpty {
                switch op {
                case "replace":guard object[key] != nil else{throw NativeSurfaceFailure("Surface patch replacement target is missing.")};object[key]=value!
                case "add":object[key]=value!
                default:guard object.removeValue(forKey:key) != nil else{throw NativeSurfaceFailure("Surface patch removal target is missing.")}
                }
            }else{guard let child=object[key] else{throw NativeSurfaceFailure("Surface patch parent is missing.")};object[key]=try apply(child,path:remaining,op:op,value:value)}
            return object
        }
        if var array=current as? [Any] {
            let index:Int
            if key=="-",remaining.isEmpty,op=="add"{index=array.count}
            else{guard key.range(of:"^(0|[1-9][0-9]*)$",options:.regularExpression) != nil,let parsed=Int(key) else{throw NativeSurfaceFailure("Surface patch array index is unsupported.")};index=parsed}
            if remaining.isEmpty {
                switch op {
                case "add":guard index<=array.count else{throw NativeSurfaceFailure("Surface patch array insertion is out of bounds.")};array.insert(value!,at:index)
                case "replace":guard index<array.count else{throw NativeSurfaceFailure("Surface patch array replacement is out of bounds.")};array[index]=value!
                default:guard index<array.count else{throw NativeSurfaceFailure("Surface patch array removal is out of bounds.")};array.remove(at:index)
                }
            }else{guard index<array.count else{throw NativeSurfaceFailure("Surface patch array parent is out of bounds.")};array[index]=try apply(array[index],path:remaining,op:op,value:value)}
            return array
        }
        throw NativeSurfaceFailure("Surface patch parent is not an object or array.")
    }
}
